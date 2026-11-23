"""Per-batch resource ceilings and one writer for isolated episode workers."""
from __future__ import annotations
import copy
import json
import threading
import time
import uuid
import hashlib
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from .storage import Cancelled,save_session,validate_session,read_bytes_retry


MAX_CONCURRENCY=8


def concurrency(value,label):
    if type(value) is not int or not 1<=value<=MAX_CONCURRENCY:raise ValueError(label+'只接受1至8')
    return value


def performance_limits(preferences):
    jobs=int(preferences.get('batch_jobs',4));native=int(preferences.get('native_limit',4))
    if preferences.get('performance_settings_version',0)<2 and (jobs,native)==(2,2):jobs=native=4
    return concurrency(jobs,'同时处理集数'),concurrency(native,'同时渲染上限')


class LimitGate:
    def __init__(self,limit,cancel=None,admission=None,default_kind='render'):
        self.limit=limit;self.cancel=cancel;self.active=0;self.peak=0
        self.calls=0;self.wait_seconds=0.;self.condition=threading.Condition()
        self.waiters=deque()
        self.admission=admission
        self.default_kind=default_kind
        self.active_kinds={};self.peak_kinds={}

    @contextmanager
    def slot(self,cancel=None,kind=None):
        kind=kind or self.default_kind
        cancel=cancel or self.cancel;started=time.monotonic()
        with self.condition:
            ticket=object();self.waiters.append(ticket)
            try:
                while True:
                    if cancel is not None and cancel.is_set():raise Cancelled('已停止等待资源，未启动新的处理进程')
                    if self.waiters[0] is ticket and self.active<self.limit:
                        if self.admission is None or not self.admission.admit(kind,self.active,token=ticket):break
                    self.condition.wait(.1)
                if cancel is not None and cancel.is_set():raise Cancelled('已停止，未启动新的处理进程')
            except BaseException:
                if self.admission:self.admission.finished(ticket)
                self.waiters.remove(ticket);self.condition.notify_all();raise
            self.waiters.popleft()
            self.wait_seconds+=time.monotonic()-started;self.active+=1
            self.peak=max(self.peak,self.active);self.calls+=1
            self.active_kinds[kind]=self.active_kinds.get(kind,0)+1
            self.peak_kinds[kind]=max(self.peak_kinds.get(kind,0),self.active_kinds[kind])
            if self.admission:self.admission.started(ticket,kind)
            self.condition.notify_all()
        try:yield
        finally:
            with self.condition:
                self.active-=1
                self.active_kinds[kind]-=1
                if self.admission:self.admission.finished(ticket)
                self.condition.notify_all()

    def reduce(self,observed_limit=None):
        with self.condition:
            if observed_limit is not None and self.limit<observed_limit:return False
            previous=self.limit;self.limit=max(1,self.limit//2);self.condition.notify_all()
            return self.limit<previous

    def snapshot(self):
        with self.condition:
            return {'limit':self.limit,'active':self.active,'peak':self.peak,'calls':self.calls,
                    'wait_seconds':round(self.wait_seconds,3),'queued':len(self.waiters),
                    'active_render':self.active_kinds.get('render',0),'peak_render':self.peak_kinds.get('render',0)}


class ExecutionBudget:
    def __init__(self,native_limit=4,cancel=None,admission=None,validation_limit=1):
        concurrency(native_limit,'同时渲染上限')
        if type(validation_limit) is not int or validation_limit not in (1,2):raise ValueError('完整核验上限只接受1或2')
        self.requested_limit=native_limit;self.admission=admission;self.reductions=[]
        self.native=LimitGate(native_limit,cancel,admission);self.prepare=LimitGate(1,cancel,default_kind='prepare')
        self.validation=LimitGate(validation_limit,cancel,admission,default_kind='validation');self.downgraded=False;self.lock=threading.Lock()

    def reduce(self,observed_limit=None,reason=''):
        with self.lock:
            previous=self.native.snapshot()['limit'];changed=self.native.reduce(observed_limit)
            if changed:
                self.validation.reduce()
                self.downgraded=True;self.reductions.append({'before':previous,'after':self.native.snapshot()['limit'],'reason':reason[:500]})
            return changed

    def snapshot(self):
        return {'native':self.native.snapshot(),'prepare':self.prepare.snapshot(),
                'validation':self.validation.snapshot(),'downgraded':self.downgraded,'requested_native_limit':self.requested_limit,
                'reductions':copy.deepcopy(self.reductions),**({'admission':self.admission.snapshot()} if self.admission else {})}


def plan_contract(job):
    result={key:job.get(key) for key in ('id','draft','backup','backup_content','directory','roles',
        'roles_content_sha256','classification_mode','media_overrides','want_srt','easy_confirmed','delivery_options')}
    result['variant_keys']=sorted(job['variants'])
    if 'archive_layout' in job:result['archive_layout']=copy.deepcopy(job['archive_layout'])
    return result


class BatchContext:
    """The coordinator owns the existing OS batch lock for this whole lifetime.

    Workers never share their dictionaries. Only their own job and newly
    appended events are merged under this writer lock; UI job identities stay.
    """
    def __init__(self,folder,session,on_job=lambda job:None):
        self.folder=Path(folder).resolve();self.session=session;self.on_job=on_job
        self.lock=threading.RLock();self.active=True;self.id=uuid.uuid4().hex
        self.workers={};self.event_offsets={}
        initial=read_bytes_retry(self.folder/'任务清单.json')
        disk=json.loads(initial);validate_session(self.folder,disk)
        if [j['id'] for j in disk['jobs']]!=[j['id'] for j in session['jobs']]:raise ValueError('批次集数发生变化，请重新打开任务')
        for before,current in zip(session['jobs'],disk['jobs']):
            if plan_contract(before)!=plan_contract(current):raise ValueError('已确认的导出计划发生变化，请重新打开任务')
        self._show(disk)
        self.session['active_execution']={'id':self.id,'state':'running'}
        save_session(self.folder,self.session,expected_manifest_sha=hashlib.sha256(initial).hexdigest())
        self.manifest_sha=self._signature(self.session)

    @staticmethod
    def _signature(value):
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8')).hexdigest()

    def worker(self,job_id):
        with self.lock:
            job=copy.deepcopy(next(j for j in self.session['jobs'] if j['id']==job_id))
            local={key:copy.deepcopy(self.session[key]) for key in ('schema','created','status','specification') if key in self.session}
            local.update(jobs=[job],events=[],backups=[copy.deepcopy(b) for b in self.session['backups'] if b['backup']==job['backup']])
            self.workers[id(local)]=(job_id,copy.deepcopy(plan_contract(job)))
            self.event_offsets[id(local)]=len(local.get('events',[]))
            return local,job

    def owns(self,folder,session,job):
        found=self.workers.get(id(session))
        return self.active and Path(folder).resolve()==self.folder and found is not None and found[0]==job['id']

    def _show(self,disk):
        refs={j['id']:j for j in self.session['jobs']};jobs=[]
        for incoming in disk['jobs']:
            current=refs.get(incoming['id'])
            if current is None:current=copy.deepcopy(incoming)
            else:
                stale=set(current)-set(incoming);current.update(copy.deepcopy(incoming))
                for key in stale:current.pop(key,None)
            jobs.append(current)
        stale=set(self.session)-set(disk)
        self.session.update({key:value for key,value in disk.items() if key!='jobs'})
        self.session['jobs']=jobs
        for key in stale:self.session.pop(key,None)

    def persist(self,folder,local):
        with self.lock:
            found=self.workers.get(id(local))
            if not self.active or Path(folder).resolve()!=self.folder or found is None:
                raise ValueError('后台批次写入上下文已结束或不匹配')
            job_id,contract=found;job=next(j for j in local['jobs'] if j['id']==job_id)
            if plan_contract(job)!=contract:raise ValueError('后台工作线程不得改变已确认的导出计划')
            raw=read_bytes_retry(self.folder/'任务清单.json')
            if hashlib.sha256(raw).hexdigest()!=self.manifest_sha:
                raise ValueError('任务清单被其他程序更新，停止合并以保留已有结果')
            # The CAS-bound prior document was validated at its last save.
            # save_session validates the entire merged document before writing.
            disk=json.loads(raw)
            if (disk.get('active_execution') or {}).get('id')!=self.id:
                raise ValueError('任务清单被其他程序更新，停止合并以保留已有结果')
            index=next(i for i,j in enumerate(disk['jobs']) if j['id']==job_id)
            previous=disk['jobs'][index]
            if plan_contract(previous)!=contract:raise ValueError('已确认的导出计划发生变化，停止后台写入')
            for key,value in previous['variants'].items():
                if value.get('file') and value.get('sha256')!=job['variants'][key].get('sha256'):
                    raise ValueError('已有归档结果不能被后台旧状态覆盖')
            old_srt=previous.get('srt') or {};new_srt=job.get('srt') or {}
            if old_srt.get('file') and old_srt.get('sha256')!=new_srt.get('sha256'):
                raise ValueError('已有字幕不能被后台旧状态覆盖')
            disk['jobs'][index]=copy.deepcopy(job)
            offset=self.event_offsets[id(local)];pending=local.get('events',[])[offset:]
            known={item.get('event_id') for item in disk.get('events',[]) if item.get('event_id')}
            for event in pending:
                event.setdefault('event_id',uuid.uuid4().hex)
                if event['event_id'] not in known:disk.setdefault('events',[]).append(copy.deepcopy(event))
            save_session(self.folder,disk,expected_manifest_sha=self.manifest_sha);self.event_offsets[id(local)]=len(local.get('events',[]))
            self.manifest_sha=self._signature(disk)
            self._show(disk);visible=next(j for j in self.session['jobs'] if j['id']==job_id)
        self.on_job(visible)

    def close(self,statistics=None):
        with self.lock:
            raw=read_bytes_retry(self.folder/'任务清单.json');disk=json.loads(raw)
            if hashlib.sha256(raw).hexdigest()==self.manifest_sha and (disk.get('active_execution') or {}).get('id')==self.id:
                disk['active_execution'].update(state='finished',statistics=statistics or {})
                save_session(self.folder,disk,expected_manifest_sha=self.manifest_sha);self._show(disk)
            self.active=False


def resource_failure(exc):
    message=str(exc).casefold()
    return any(text in message for text in ('out of memory','not enough memory','std::bad_alloc',
        'nv_enc_err_encoder_busy','too many concurrent sessions','resource temporarily unavailable',
        'nv_enc_err_out_of_memory','退出码-1073741801','退出码-1073741523','退出码3221225495','退出码3221225773',
        'winerror 1455','显存不足','内存不足'))
