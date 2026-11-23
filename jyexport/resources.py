"""Read-only resource admission checks for higher render concurrency."""
from __future__ import annotations
import ctypes
import os
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

MIB=1024*1024


def memory_status():
    if os.name!='nt':return {}
    class Memory(ctypes.Structure):
        _fields_=[('length',ctypes.c_uint32),('load',ctypes.c_uint32),
                  *[(key,ctypes.c_uint64) for key in ('total_physical','available_physical','total_commit',
                      'available_commit','total_virtual','available_virtual','extended')]]
    value=Memory();value.length=ctypes.sizeof(value)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):return {}
    return {'physical_available_mib':value.available_physical//MIB,
            'physical_total_mib':value.total_physical//MIB,'commit_available_mib':value.available_commit//MIB}


def gpu_status():
    tool=shutil.which('nvidia-smi')
    if not tool:
        candidate=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/nvidia-smi.exe'
        if candidate.is_file():tool=str(candidate)
    if not tool:return []
    try:
        result=subprocess.run([tool,'--query-gpu=index,name,memory.total,memory.free,utilization.gpu,utilization.encoder,encoder.stats.sessionCount',
            '--format=csv,noheader,nounits'],stdin=subprocess.DEVNULL,capture_output=True,timeout=2,
            encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            result=subprocess.run([tool,'--query-gpu=index,name,memory.total,memory.free','--format=csv,noheader,nounits'],
                stdin=subprocess.DEVNULL,capture_output=True,timeout=2,encoding='utf-8',errors='replace',
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:return []
        rows=[]
        for line in result.stdout.splitlines():
            parts=[p.strip() for p in line.split(',')]
            if len(parts) not in (4,7):continue
            def number(index):
                try:return int(parts[index])
                except (ValueError,IndexError):return None
            rows.append({'index':number(0),'name':parts[1],'total_mib':number(2),'free_mib':number(3),
                         'gpu_percent':number(4),'encoder_percent':number(5),'encoder_sessions':number(6)})
        return rows
    except (OSError,subprocess.TimeoutExpired):return []


def resource_status():
    return {**memory_status(),'gpus':gpu_status()}


class ResourcePressure(RuntimeError):pass


class RenderAdmission:
    """Reserve startup headroom before the OS/GPU counters catch up with a launch.

    Unknown GPU counters never claim free VRAM. Physical/commit counters and
    normal engine error fallback still apply on non-NVIDIA computers.
    """
    def __init__(self,probe=resource_status,log=lambda value:None,timeout=45):
        self.probe=probe;self.log=log;self.timeout=timeout;self.latest={};self.sampled_at=0.
        self.starts={};self.wait_started=None;self.wait_reason='';self.wait_events=0
        self.minimum_physical=None;self.minimum_vram=None;self.probe_errors=0
        self.lock=threading.RLock()

    def admit(self,kind,active,token=None):
        if kind not in ('render','validation'):return ''
        with self.lock:return self._admit(kind,active,token)

    def _admit(self,kind,active,token):
        tick=time.monotonic()
        if tick-self.sampled_at>=.5:
            try:self.latest=self.probe()
            except (OSError,ValueError,RuntimeError):self.latest={};self.probe_errors+=1
            self.sampled_at=tick
        data=self.latest
        pending=[role for role,start in self.starts.values() if tick-start<5]
        # A fresh exporter needs room while its buffers are still being allocated.
        ram_needed=1536+sum(768 if role=='render' else 256 for role in pending)+(768 if kind=='render' else 256)
        vram_needed=512+512*(pending.count('render')+1)
        available=data.get('physical_available_mib');commit=data.get('commit_available_mib')
        if available is not None:self.minimum_physical=available if self.minimum_physical is None else min(available,self.minimum_physical)
        reasons=[]
        if available is None and active>=(2 if kind=='render' else 1):reasons.append('内存探测暂不可用，暂按最多2个渲染进程 / 1个核验排队')
        if available is not None and available<ram_needed:reasons.append(f'可用内存{available}MB，启动余量需{ram_needed}MB')
        if commit is not None and commit<ram_needed:reasons.append(f'可用提交空间{commit}MB，启动余量需{ram_needed}MB')
        # The SDK does not expose an adapter selector. Every reported NVIDIA
        # adapter must have headroom; do not assume it chose the emptiest card.
        for gpu in data.get('gpus',[]) if kind=='render' else []:
            free=gpu.get('free_mib')
            if free is None:continue
            self.minimum_vram=free if self.minimum_vram is None else min(free,self.minimum_vram)
            if free<vram_needed:reasons.append(f'{gpu["name"]}可用显存{free}MB，启动余量需{vram_needed}MB')
        reason='；'.join(reasons)
        if reason:
            if self.wait_started is None:self.wait_started=tick;self.wait_events+=1
            if not self.wait_reason:self.log('资源余量不足，等待后再启动新处理：'+reason)
            self.wait_reason=reason
            if not self.starts and not active and tick-self.wait_started>=self.timeout:
                raise ResourcePressure('资源余量持续不足，未启动新的渲染/核验；请释放内存/显存后继续本批次：'+reason)
        else:
            self.wait_started=None;self.wait_reason=''
            if token is not None:self.starts[token]=(kind,tick)
        return reason

    def started(self,token,kind):
        if kind in ('render','validation'):
            with self.lock:self.starts[token]=(kind,time.monotonic())

    def finished(self,token):
        with self.lock:self.starts.pop(token,None)

    def snapshot(self):
        return {'enabled':True,'startup_reserve_ram_mib':768,'validation_startup_reserve_ram_mib':256,
                'startup_reserve_seconds':5,'startup_reserve_vram_mib':512,
                'physical_floor_mib':1536,'vram_floor_mib':512,'wait_events':self.wait_events,
                'waiting_reason':self.wait_reason,'minimum_observed_physical_mib':self.minimum_physical,
                'minimum_observed_vram_mib':self.minimum_vram,'last_sample':self.latest,'probe_errors':self.probe_errors}


@contextmanager
def machine_render_lock():
    """One coordinated batch per Windows login session, including other windows."""
    if os.name!='nt':
        import tempfile
        from .inbox import _state_lock
        with _state_lock(Path(tempfile.gettempdir())/'JianyingExportAssistant-render-budget'):yield
        return
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR];kernel.CreateMutexW.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD];kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.ReleaseMutex.argtypes=[wintypes.HANDLE];kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateMutexW(None,False,'Local\\JianyingExportAssistant.RenderBudget.v2')
    if not handle:raise OSError(ctypes.get_last_error(),'无法建立跨窗口渲染锁')
    acquired=False
    try:
        status=kernel.WaitForSingleObject(handle,0)
        if status==0x102:raise ValueError('其他助手窗口正在导出；请在原窗口继续，避免多窗口叠加超出并发上限。')
        if status not in (0,0x80):raise OSError(ctypes.get_last_error(),'无法检查跨窗口渲染锁')
        acquired=True;yield
    finally:
        if acquired:kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)
