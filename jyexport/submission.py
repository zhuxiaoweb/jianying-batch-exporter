"""User-confirmed native-export folder submission and foreground inbox CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

from .discovery import resolve_app_dir
from .inbox import InboxBusy, InboxWorker, _inside, _no_links, _not_draft, _overlap, _state_lock, read_task
from .media import read_srt
from .storage import atomic_json, now, safe_name, sha


DONE = {'completed','already_completed','recovered','completed_cached'}
PENDING = {'settling','waiting_files','waiting','processing'}


def prepare_roots(inbox, output_root=None, *, create=False):
    inbox=Path(inbox).resolve()
    output=Path(output_root).resolve() if output_root is not None else None
    _not_draft(inbox)
    if output is not None:
        _not_draft(output)
        if output.exists() and not output.is_dir():raise ValueError('交付输出路径不是文件夹')
        if _overlap(inbox,output):raise ValueError('收件箱和交付输出目录不能互相包含或重叠')
    if create:inbox.mkdir(parents=True,exist_ok=True)
    if not inbox.is_dir():raise FileNotFoundError('请先创建或选择收件箱目录')
    return inbox,output


def discover_exports(folder):
    """Find only documented V2/V3/native-SRT naming, never guess by duration."""
    lexical=Path(folder).absolute()
    _no_links(lexical,lexical)
    folder=lexical.resolve()
    if not folder.is_dir():raise FileNotFoundError('单集导出目录不存在')
    _not_draft(folder)
    candidates={key:[] for key in ('V2','V3','SRT')}
    role=lambda stem:next((key for key in ('V2','V3') if re.fullmatch(key+r'(?:_.*)?',stem,re.I)),None)
    for path in folder.iterdir():
        if path.is_file():
            if path.suffix.lower()=='.mp4' and role(path.stem):candidates[role(path.stem)].append(path)
            if path.suffix.lower()=='.srt' and (path.stem=='字幕' or role(path.stem)=='V2'):
                candidates['SRT'].append(path)
        elif path.is_dir() and role(path.name):
            _no_links(path,folder)
            for nested in path.iterdir():
                if not nested.is_file() or nested.stem.casefold()!=path.name.casefold():continue
                if nested.suffix.lower()=='.mp4':candidates[role(path.name)].append(nested)
                if nested.suffix.lower()=='.srt' and role(path.name)=='V2':candidates['SRT'].append(nested)
    for key,paths in candidates.items():
        if not paths:raise ValueError(f'缺少唯一的{key}文件；约定V2.mp4、V3.mp4、字幕.srt或同名原生子文件夹')
        if len(paths)!=1:raise ValueError(f'{key}存在{len(paths)}个候选，不能猜选；请整理为一集一份母版')
        _no_links(paths[0],folder)
        if not _inside(paths[0].resolve(),folder):raise ValueError('导出文件越出单集目录')
    paths={key:items[0].resolve() for key,items in candidates.items()}
    if any(os.path.samefile(paths[a],paths[b]) for a,b in (('V2','V3'),('V2','SRT'),('V3','SRT'))):
        raise ValueError('两母版和SRT不能指向同一文件')
    read_srt(paths['SRT'])
    return folder,paths


def _snapshot(paths):
    stats={key:(path.stat().st_size,path.stat().st_mtime_ns) for key,path in paths.items()}
    if any(size<=0 for size,_ in stats.values()):raise ValueError('导出文件为空，请等待剪映完成')
    hashes={key:sha(path) for key,path in paths.items()}
    if any((path.stat().st_size,path.stat().st_mtime_ns)!=stats[key] for key,path in paths.items()):
        raise RuntimeError('导出文件仍在写入，请等待完成后再提交')
    return stats,hashes


def _existing(inbox, identifier, identity, hashes):
    matches=[]
    for folder in inbox.iterdir():
        manifest=folder/'task.json'
        if not folder.is_dir() or not manifest.is_file():continue
        try:
            _no_links(folder,inbox)
            task,paths=read_task(folder)
        except (OSError,ValueError,KeyError,TypeError):continue
        if task['id']!=identifier:continue
        if task['identity']!=identity or task.get('source_sha256')!=hashes:
            raise ValueError('已存在同ID但内容不同的提交，不会覆盖或复用')
        if _snapshot({key:paths[key] for key in ('V2','V3','SRT')})[1]!=hashes:
            raise ValueError('已提交任务来源已变化，请使用新的单集修订目录')
        matches.append((folder,task))
    if len(matches)>1:raise ValueError('收件箱存在重复任务ID，请先处理冲突')
    return matches[0] if matches else None


def _write_new_manifest(path,value):
    # Exclusive creation never replaces a previous submission. The worker also
    # requires stable observations, so an interrupted write cannot be processed.
    with path.open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.flush();os.fsync(stream.fileno())


def _submit_folder(folder, inbox, identity, *, confirmed_pair=False, output_root=None,
                  copy_file=shutil.copy2, log=lambda value:None):
    if not confirmed_pair:raise ValueError('请明确确认本集两母版来自同一时间线，仅切换配乐和字幕')
    if not isinstance(identity,str) or not identity.strip():raise ValueError('请填写剧名与集号')
    identity=identity.strip()
    folder,paths=discover_exports(folder)
    candidate=Path(inbox).resolve()
    if folder==candidate or folder in candidate.parents:
        raise ValueError('不能把包含收件箱的目录作为单集来源')
    inbox,_=prepare_roots(inbox,output_root,create=True)
    stats,hashes=_snapshot(paths)
    content=json.dumps({'identity':identity,'files':hashes},sort_keys=True,separators=(',',':')).encode('utf-8')
    identifier='handoff-'+hashlib.sha256(content).hexdigest()[:40]
    existing=_existing(inbox,identifier,identity,hashes)
    if existing:
        target,task=existing
        return {'id':task['id'],'identity':identity,'folder':str(target),'manifest':str(target/'task.json'),
                'already_submitted':True,'copied':False,'source_sha256':hashes}
    direct=folder.parent==inbox and not (folder/'task.json').exists()
    target=folder if direct else inbox/(safe_name(identity)+'_'+uuid.uuid4().hex[:10])
    if not direct:target.mkdir(exist_ok=False)
    try:
        if direct:
            relative={key:path.relative_to(folder).as_posix() for key,path in paths.items()}
        else:
            relative={'V2':'V2.mp4','V3':'V3.mp4','SRT':'字幕.srt'}
            for key,source in paths.items():
                log('核验复制 '+key+' 到收件箱')
                destination=target/relative[key]
                copy_file(source,destination)
                if sha(destination)!=hashes[key]:raise RuntimeError('提交复制哈希不一致，未发布任务')
        if _snapshot(paths)!=(stats,hashes):raise RuntimeError('提交期间来源变化，未发布任务')
        task={'schema':1,'id':identifier,'identity':identity,'same_timeline_confirmed':True,
              **relative,'source_sha256':hashes,'submitted':now()}
        _write_new_manifest(target/'task.json',task)
        return {'id':identifier,'identity':identity,'folder':str(target),'manifest':str(target/'task.json'),
                'already_submitted':False,'copied':not direct,'source_sha256':hashes}
    except Exception as exc:
        if not direct:
            atomic_json(target/'提交未完成.json',{'status':'未发布task.json','error':str(exc),'at':now()})
        raise


def submit_folder(folder, inbox, identity, *, confirmed_pair=False, output_root=None,
                  copy_file=shutil.copy2, log=lambda value:None):
    """Confirm once and publish a self-contained task, without hand-editing JSON."""
    if not confirmed_pair:raise ValueError('请明确确认本集两母版来自同一时间线，仅切换配乐和字幕')
    source,_=discover_exports(folder)
    candidate=Path(inbox).resolve()
    if source==candidate or source in candidate.parents:
        raise ValueError('不能把包含收件箱的目录作为单集来源')
    inbox,_=prepare_roots(inbox,output_root,create=True)
    lock_directory=inbox/'.submission-state'
    _no_links(lock_directory,inbox)
    deadline=time.monotonic()+30
    while True:
        try:
            with _state_lock(lock_directory):
                return _submit_folder(source,inbox,identity,confirmed_pair=True,output_root=output_root,
                                      copy_file=copy_file,log=log)
        except InboxBusy:
            if time.monotonic()>=deadline:raise InboxBusy('其他提交仍在进行，请稍后重试；没有重复发布本集')
            time.sleep(.1)


def summarize_scan(result, *, stopped=False):
    rows=result.get('results',[])
    done=sum(row.get('state') in DONE for row in rows)
    pending=sum(row.get('state') in PENDING for row in rows)
    failed=sum(row.get('state') not in DONE|PENDING for row in rows)
    cached=sum(row.get('state')=='completed_cached' and row.get('metadata_checked_this_scan') is True
               and row.get('present_this_scan',True) for row in rows)
    verified=sum(row.get('state') in DONE and row.get('hash_verified_this_scan') is True
                 and row.get('metadata_checked_this_scan') is True
                 and row.get('present_this_scan',True) for row in rows)
    busy=bool(result.get('busy'))
    all_present=all(row.get('present_this_scan',True) for row in rows)
    return {'at':now(),'results':rows,'total':len(rows),'completed':done,'pending':pending,
            'failed':failed,'busy':busy,'stopped':bool(stopped),
            'cached':cached,'hash_verified':verified,
            'all_hash_verified_this_scan':bool(rows) and verified==len(rows) and not busy,
            'verification_mode':'cached' if cached else ('strict' if rows and verified==len(rows) else 'partial_or_pending'),
            'all_complete':bool(rows) and done==len(rows) and all_present and not busy and not stopped,
            'exit_code':1 if failed else (2 if pending or busy or stopped else 0)}


def process_once(worker, *, stop_event=None, on_scan=None, wait=None,force_audit=False):
    stop_event=stop_event or threading.Event()
    scan=lambda:worker.scan_once(stop_event,force_audit=True) if force_audit else worker.scan_once(stop_event)
    result=scan()
    if on_scan:on_scan(result)
    if not stop_event.is_set() and any(row.get('state')=='settling' for row in result.get('results',[])):
        (wait or stop_event.wait)(max(worker.stable_seconds,.01))
        if not stop_event.is_set():
            result=scan()
            if on_scan:on_scan(result)
    return summarize_scan(result,stopped=stop_event.is_set())


def cli(argv=None, *, watch=False):
    for stream in (sys.stdout,sys.stderr):
        if stream is not None and hasattr(stream,'reconfigure'):
            try:stream.reconfigure(encoding='utf-8')
            except (AttributeError,ValueError):pass
    parser=argparse.ArgumentParser(description='确认来源后，自动处理本地两母版收件箱')
    parser.add_argument('--inbox',required=True)
    parser.add_argument('--out',required=True)
    parser.add_argument('--app-dir')
    parser.add_argument('--stable-seconds',type=float,default=2)
    parser.add_argument('--interval',type=float,default=2)
    parser.add_argument('--completed-audit-seconds',type=float,default=None,
                        help='已完成任务完整哈希审计间隔；0=每轮严格。once默认0，watch默认3600；窗口内仅核对元数据')
    parser.add_argument('--force-audit',action='store_true',help='忽略缓存窗口，强制完整哈希复核')
    parser.add_argument('--stop-file',required=watch)
    parser.add_argument('--report')
    args=parser.parse_args(argv)
    audit_seconds=args.completed_audit_seconds if args.completed_audit_seconds is not None else (3600.0 if watch else 0.0)
    if not math.isfinite(args.stable_seconds) or args.stable_seconds<0 or not math.isfinite(args.interval) or args.interval<=0:
        parser.error('稳定等待需为有限非负数，扫描间隔需为有限正数')
    if not math.isfinite(audit_seconds) or audit_seconds<0:parser.error('完成任务审计间隔需为有限非负秒数')
    report=None;report_owned=False
    try:
        inbox,output=prepare_roots(args.inbox,args.out,create=True)
        app_dir=resolve_app_dir(args.app_dir)
        if not app_dir or not all((Path(app_dir)/name).is_file() for name in ('ffmpeg.exe','ffprobe.exe')):
            raise ValueError('未发现完整FFmpeg/FFprobe目录，请指定--app-dir')
        report=Path(args.report).resolve() if args.report else output/'收件箱运行状态.json'
        _not_draft(report.parent)
        if report==inbox or inbox in report.parents:raise ValueError('运行报告不能写入收件箱来源目录')
        if report.exists():
            previous=json.loads(report.read_text(encoding='utf-8'))
            if args.report or previous.get('schema')!='jianying-inbox-run/1':
                raise FileExistsError('报告目标已存在，拒绝覆盖非本次运行的自定义文件')
        report.parent.mkdir(parents=True,exist_ok=True)
        report_owned=True
        stop=threading.Event();monitor_done=threading.Event()
        def emit(summary):
            value={'schema':'jianying-inbox-run/1','mode':'watch' if watch else 'once',
                   'inbox':str(inbox),'output_root':str(output),'completed_audit_interval':audit_seconds,
                   'force_audit':args.force_audit,**summary}
            atomic_json(report,value)
            if sys.stdout is not None:print(json.dumps(value,ensure_ascii=False),flush=True)
        worker=InboxWorker(inbox,output,app_dir,stable_seconds=args.stable_seconds,
                           completed_audit_interval=audit_seconds)
        if not watch:
            result=process_once(worker,force_audit=args.force_audit)
        else:
            stop_file=Path(args.stop_file).resolve()
            def monitor():
                while not monitor_done.wait(.25):
                    if stop_file.exists():stop.set();return
            monitor_thread=threading.Thread(target=monitor,daemon=True)
            if stop_file.exists():stop.set()
            monitor_thread.start();last={'results':[]};seen={}
            old_handler=signal.signal(signal.SIGINT,lambda *_:stop.set())
            def observed(scan):
                nonlocal last
                for row in scan.get('results',[]):
                    key=row.get('task') or row.get('id') or repr(row)
                    seen[key]={**row,'present_this_scan':True}
                current_keys={row.get('task') or row.get('id') or repr(row) for row in scan.get('results',[])}
                for key,row in seen.items():
                    if key not in current_keys:
                        row.update(present_this_scan=False,hash_verified_this_scan=False,metadata_checked_this_scan=False)
                last={**scan,'results':list(seen.values())}
                emit(summarize_scan(last,stopped=stop.is_set()))
            try:
                if args.force_audit:worker.run(stop,args.interval,observed,force_audit=True)
                else:worker.run(stop,args.interval,observed)
            finally:
                signal.signal(signal.SIGINT,old_handler);monitor_done.set();monitor_thread.join(timeout=1)
            result=summarize_scan(last,stopped=True)
        emit(result)
        return result['exit_code']
    except InboxBusy as exc:
        result={'results':[],'completed':0,'pending':0,'failed':0,'busy':True,'all_complete':False,'exit_code':2,'error':str(exc)}
    except Exception as exc:
        result={'results':[],'completed':0,'pending':0,'failed':1,'all_complete':False,'exit_code':1,'error':str(exc)}
    if report_owned and report is not None and report.parent.is_dir():
        atomic_json(report,{'schema':'jianying-inbox-run/1',**result})
    if sys.stderr is not None:print(json.dumps(result,ensure_ascii=False),file=sys.stderr)
    return result['exit_code']
