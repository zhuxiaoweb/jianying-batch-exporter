"""Place large runtime files away from a full system disk; preserve old caches."""
from __future__ import annotations
import json
import os
import shutil
import string
import subprocess
import time
import uuid
from pathlib import Path

INSTALLER_BYTES=923476976
RUNTIME_BYTES=3101133186
RESERVE_BYTES=512*1024*1024
CONFIG_RESERVE_BYTES=64*1024*1024
REQUIRED_BYTES=INSTALLER_BYTES+RUNTIME_BYTES+RESERVE_BYTES


def free_bytes(path):
    parent=Path(path).resolve()
    while not parent.exists() and parent.parent!=parent:parent=parent.parent
    return shutil.disk_usage(parent).free


def assert_not_draft(path):
    target=Path(path).resolve()
    for parent in (target,*target.parents):
        if (parent/'draft_content.json').is_file():
            raise ValueError('引擎及配置不能放在已有剪映草稿内部')
    return target


def storage_candidates(base,state):
    base,state=Path(base).resolve(),Path(state).resolve()
    system=Path(os.environ.get('SystemRoot','C:/Windows')).anchor.casefold()
    values=[]
    if base.anchor.casefold()!=system:values.append(base/'引擎数据')
    drives=os.listdrives() if hasattr(os,'listdrives') else [letter+':/' for letter in string.ascii_uppercase if Path(letter+':/').exists()]
    values.extend(Path(drive)/'JianyingExportAssistantData/HeadlessRuntime' for drive in sorted(drives)
                  if Path(drive).anchor.casefold()!=system)
    values.extend([state/'HeadlessRuntime',base/'引擎数据'])
    return list(dict.fromkeys(values))


def recommend_storage(base,state,preferred=None):
    if preferred:return assert_not_draft(preferred)
    candidates=storage_candidates(base,state)
    for path in candidates:
        try:
            assert_not_draft(path)
            needed=RUNTIME_BYTES+RESERVE_BYTES+(0 if cached_installer(path,state) else INSTALLER_BYTES)
            if free_bytes(path)>=needed:return path
        except (OSError,ValueError):continue
    return state/'HeadlessRuntime'


def _writable_directory(path):
    path=assert_not_draft(path)
    if free_bytes(path)<CONFIG_RESERVE_BYTES:raise OSError('配置盘剩余空间不足')
    path.mkdir(parents=True,exist_ok=True)
    probe=path/('.write-check-'+uuid.uuid4().hex)
    try:
        with probe.open('xb') as stream:stream.write(b'write check')
    finally:
        if probe.exists():probe.unlink()
    return path


def configuration_directory(base):
    base=Path(base).resolve()
    legacy=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'JianyingExportAssistant'
    alternatives=[base/'user-config',*[path.parent/'Config' for path in storage_candidates(base,legacy) if path.anchor.casefold()!=legacy.anchor.casefold()]]
    # Once a fallback has been established, reopening follows the same files.
    ordered=[path for path in alternatives if (path/'settings.json').is_file() or (path/'engine_runtime.local.json').is_file()]
    ordered.extend([legacy,*alternatives])
    errors=[]
    for path in dict.fromkeys(ordered):
        try:chosen=_writable_directory(path)
        except (OSError,ValueError) as exc:errors.append(str(exc));continue
        if chosen!=legacy:
            for name in ('settings.json','engine_runtime.local.json'):
                source,target=legacy/name,chosen/name
                if source.is_file() and not target.exists():
                    # Only the tool's two small JSON files are copied; originals stay.
                    try:json.loads(source.read_text('utf-8-sig'))
                    except (OSError,ValueError):continue
                    shutil.copy2(source,target)
        return chosen
    raise OSError('没有可写的工具配置位置；请把完整工具解压到有剩余空间的数据盘。'+'；'.join(errors[-2:]))


def cached_installer(storage,state):
    legacy=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'JianyingExportAssistant'
    for root in (Path(storage),Path(state)/'HeadlessRuntime',legacy/'HeadlessRuntime'):
        candidate=root/'downloads/Jianying_11_6_0_14552.exe'
        if candidate.is_file() and candidate.stat().st_size==INSTALLER_BYTES:return candidate
    return None


def check_storage(storage,installer=None,existing_runtime=None):
    storage=assert_not_draft(storage)
    required=CONFIG_RESERVE_BYTES if existing_runtime else RUNTIME_BYTES+RESERVE_BYTES+(0 if installer else INSTALLER_BYTES)
    available=free_bytes(storage)
    if available<required:
        raise ValueError(f'引擎存放盘空间不足：{storage}，剩余{available/1024**3:.2f} GiB，至少需要{required/1024**3:.2f} GiB。请在“引擎存放位置”选择其他有空间的数据盘。')
    return {'required_bytes':required,'available_bytes':available,'installer_reused':bool(installer)}


def launch_setup(app,installer=None):
    from .discovery import load_json
    from .engine import EngineAdapter
    script=app.engine_component/'Setup-HeadlessRuntime.ps1'
    if not script.is_file():raise ValueError('缺少引擎准备脚本，请保持完整解压目录')
    storage=Path(app.engine_storage.get()).resolve()
    config=app.state_dir/'engine_runtime.local.json'
    powershell=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    cached=Path(installer) if installer else cached_installer(storage,app.state_dir)
    existing=None
    if app.engine_runtime.get().strip():
        try:app.engine_adapter().verify();existing=app.engine_runtime.get()
        except (OSError,ValueError):pass
    cache=app.cache.get()
    app.engine_state.set('正在检查存放盘空间；优先复用已下载安装包，不覆盖当前剪映。')
    def work():
        check_storage(storage,cached,existing)
        diagnostics=storage/'diagnostics';diagnostics.mkdir(parents=True,exist_ok=True)
        token=time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
        report=diagnostics/('setup_'+token+'.json');logfile=report.with_suffix('.log')
        command=[str(powershell),'-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
                 '-RuntimeRoot',str(storage),'-ConfigurationPath',str(config),'-ReportPath',str(report)]
        if cached:command+=['-InstallerPath',str(cached)]
        if existing:command+=['-ExistingRuntimeDirectory',str(existing)]
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        started=time.monotonic();last=0
        while True:
            try:output,_=process.communicate(timeout=.5);break
            except subprocess.TimeoutExpired:
                if app.cancel.is_set():
                    subprocess.run([str(powershell.parents[2]/'taskkill.exe'),'/PID',str(process.pid),'/T','/F'],
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    if process.poll() is None:process.kill()
                    output,_=process.communicate(timeout=15);logfile.write_text(output,'utf-8')
                    raise ValueError('已停止准备；下载及诊断保留在 '+str(storage))
                elapsed=time.monotonic()-started
                if elapsed-last>=3:
                    phase=load_json(report,{}).get('phase','preflight')
                    label={'preflight':'检查空间','download':'下载','extract':'解压','verify':'校验','configure':'保存配置'}.get(phase,phase)
                    app.messages.put(('engine_progress',f'准备引擎：{label}，已用{int(elapsed)}秒。存放位置：{storage}'));last=elapsed
        logfile.write_text(output,'utf-8');app.log(output[-2200:])
        if process.returncode:
            detail=load_json(report,{})
            if detail.get('error_code')=='INSUFFICIENT_SPACE':
                raise ValueError(f'引擎盘空间不足：剩余{detail.get("free_bytes",0)/1024**3:.2f} GiB，需要{detail.get("required_bytes",0)/1024**3:.2f} GiB。请选择其他数据盘。诊断：{report}')
            raise ValueError('独立引擎准备未完成。诊断：'+str(report)+'\n'+output[-1200:])
        prepared=load_json(config,{})
        EngineAdapter(app.engine_component/'bin',prepared.get('runtime_directory',''),cache).verify()
        return prepared
    def done(prepared):
        app.engine_runtime.set(prepared['runtime_directory']);app.save_preferences()
        app.engine_state.set('引擎已准备并校验通过。存放于：'+prepared['runtime_directory'])
    app.dispatch(work,done)
