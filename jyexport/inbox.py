"""Offline, restartable handoff inbox for native V2/V3/SRT exports.

Each direct child of ``inbox`` may contain a UTF-8 ``task.json`` with schema=1,
id, identity, same_timeline_confirmed=true and relative V2/V3/SRT paths.  This
module never opens the editor, deletes inputs or starts a background watcher
on import.  ``scan_once`` is deterministic; ``run`` needs an explicit stop
event and finishes the current media operation before stopping.
"""
from __future__ import annotations

import hashlib
import csv
import json
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path, PureWindowsPath

from .fast import compose_bundle
from .media import read_srt
from .storage import atomic_json, now, sha


class InboxBusy(RuntimeError):
    """Another live process owns this state directory's OS file lock."""


def _inside(path, root):
    return path != root and root in path.parents


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def _not_draft(path):
    if any((directory / 'draft_content.json').is_file()
           for directory in (path, *path.parents)):
        raise ValueError('收件箱、回执或交付目录不能位于剪映草稿内部')


def _no_links(path, root):
    """Check lexical components too; resolve() alone would hide a junction."""
    if not (path == root or _inside(path, root)):
        raise ValueError('任务路径越出文件夹')
    current = path
    while True:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('任务路径包含符号链接或目录联接')
        if current == root:
            break
        current = current.parent


def _relative_file(value, root):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('V2/V3/SRT需要非空的相对文件路径')
    windows = PureWindowsPath(value)
    path = Path(value.replace('\\', '/'))
    if windows.drive or windows.root or path.is_absolute() or '..' in path.parts:
        raise ValueError('任务媒体路径必须位于当前任务文件夹，不能使用绝对路径或..')
    lexical = root / path
    _no_links(lexical, root)
    resolved = lexical.resolve()
    if not _inside(resolved, root):
        raise ValueError('任务媒体路径越出文件夹')
    return resolved


def _read_json(path, with_hash=False):
    if path.stat().st_size > 4*1024*1024:
        raise ValueError('任务或回执JSON超过4MiB限制')
    raw = path.read_bytes()
    value = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('JSON内容必须是对象')
    return (value,hashlib.sha256(raw).hexdigest()) if with_hash else value


def _read_task(folder):
    """Read a submitted task without writing anywhere in its input folder."""
    folder = Path(folder).absolute()
    _no_links(folder, folder)
    folder = folder.resolve()
    manifest = folder / 'task.json'
    _no_links(manifest, folder)
    task, manifest_hash = _read_json(manifest,with_hash=True)
    if type(task.get('schema')) is not int or task['schema'] != 1:
        raise ValueError('不支持的task.json schema')
    identifier = task.get('id')
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', identifier):
        raise ValueError('任务id需为1至128位字母、数字、点、下划线或短横线')
    if not isinstance(task.get('identity'), str) or not task['identity'].strip():
        raise ValueError('任务需要剧名与集号identity')
    if task.get('same_timeline_confirmed') is not True:
        raise ValueError('尚未确认两母版来自同集且仅切换配乐和字幕')
    paths = {key: _relative_file(task.get(key), folder) for key in ('V2', 'V3', 'SRT')}
    if len(set(paths.values())) != 3 or manifest.resolve() in paths.values():
        raise ValueError('V2、V3、SRT必须是三份独立文件')
    if all(path.is_file() for path in paths.values()) and any(
            os.path.samefile(paths[a],paths[b]) for a,b in (('V2','V3'),('V2','SRT'),('V3','SRT'))):
        raise ValueError('V2、V3、SRT不能通过硬链接指向同一文件')
    if any(paths[key].suffix.lower() != '.mp4' for key in ('V2', 'V3')) or paths['SRT'].suffix.lower() != '.srt':
        raise ValueError('母版应为MP4、字幕应为SRT')
    return task, {'task.json': manifest, **paths}, manifest_hash


def read_task(folder):
    task, paths, _ = _read_task(folder)
    return task, paths


def _stats(paths):
    result = {}
    for key, path in paths.items():
        if not path.is_file():
            raise FileNotFoundError(f'等待文件：{key}')
        stat = path.stat()
        if stat.st_size <= 0:
            raise ValueError(f'等待非空文件：{key}')
        result[key] = {'size':stat.st_size, 'mtime_ns':stat.st_mtime_ns}
    return result


def _fingerprint(paths, expected_stats):
    fingerprints = {key: sha(path) for key, path in paths.items()}
    if _stats(paths) != expected_stats:
        raise RuntimeError('核对哈希时来源仍在变化，等待文件稳定')
    return fingerprints


def _metadata(paths):
    """Cheap observations only: deliberately no payload hashing or ctime claim."""
    result={}
    for key,path in paths.items():
        if not path.is_file():raise FileNotFoundError(str(path))
        stat=path.stat()
        result[key]={'path':str(path.resolve()),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,
                     'device':stat.st_dev,'file_id':stat.st_ino}
    return result


def _tree_metadata(root):
    """Observe names, paths and metadata for a small per-task output tree."""
    root=Path(root)
    if not root.is_dir():raise FileNotFoundError(str(root))
    result={}
    for directory,dirs,files in os.walk(root,followlinks=False):
        for path in [Path(directory),*(Path(directory)/name for name in dirs+files)]:
            _no_links(path,root)
            if not(path.is_file() or path.is_dir()):raise ValueError('输出目录包含不支持的特殊文件')
            stat=path.stat();relative=path.relative_to(root).as_posix()
            result[relative]={'path':str(path.resolve()),'directory':path.is_dir(),
                'size':stat.st_size,'mtime_ns':stat.st_mtime_ns,'device':stat.st_dev,'file_id':stat.st_ino}
    return result


def _iso_epoch(value):
    return datetime.fromtimestamp(value,timezone.utc).astimezone(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


@contextmanager
def _state_lock(directory):
    """OS locks release on process exit; no stale-lock time guessing."""
    directory.mkdir(parents=True, exist_ok=True)
    _no_links(directory,directory)
    _no_links(directory/'worker.lock',directory)
    stream = (directory / 'worker.lock').open('a+b')
    if stream.seek(0, os.SEEK_END) == 0:
        stream.write(b'0'); stream.flush()
    stream.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise InboxBusy('另一个收件箱进程正在处理同一回执目录') from exc
    try:
        yield
    finally:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _artifact(path_value, recorded_directory, current_directory):
    """Rebase a report's own files after moving the handoff/output folders."""
    if not isinstance(path_value, str) or not isinstance(recorded_directory, str):
        raise ValueError('交付报告中的文件路径无效')
    if PureWindowsPath(recorded_directory).drive:
        relative = PureWindowsPath(path_value).relative_to(PureWindowsPath(recorded_directory))
        parts = relative.parts
    else:
        relative = Path(path_value).relative_to(Path(recorded_directory))
        parts = relative.parts
    if not parts or '..' in parts:
        raise ValueError('交付产物路径越界')
    lexical = current_directory.joinpath(*parts)
    _no_links(lexical, current_directory)
    actual = lexical.resolve()
    if not _inside(actual, current_directory):
        raise ValueError('交付产物路径越界')
    return actual


def validate_result(result, fingerprint, run_directory, identity, source_paths):
    """Use file hashes and saved decode evidence, not a status word alone."""
    run_directory = Path(run_directory).resolve()
    if (result.get('complete') is not True or result.get('method') != 'two_native_masters_stream_copy'
            or result.get('identity') != identity):
        raise ValueError('处理器回执不属于当前两母版任务')
    if (result.get('reviewed') is not False or result.get('video_reencoded') is not False
            or result.get('audio_reencoded') is not False or result.get('sources_unchanged') is not True
            or result.get('status') != '五项文件完成·待声画审核'):
        raise ValueError('处理器未提交规定的来源完整性/待审核状态')
    recorded_directory = result.get('directory')
    # Initial results live under run_directory; recovered reports supply their
    # actual folder separately, allowing their absolute references to be rebased.
    current = result.get('_actual_directory')
    directory = Path(current or recorded_directory).resolve()
    if not _inside(directory, run_directory):
        raise ValueError('处理器输出越出本任务的独立交付目录')
    _no_links(directory, run_directory)
    for code in ('V2', 'V3'):
        if result.get('sources', {}).get(code, {}).get('sha256') != fingerprint[code]:
            raise ValueError('报告母版哈希与提交来源不一致')
    variants = result.get('variants', {})
    if set(variants) != {'V1', 'V2', 'V3', 'V4'}:
        raise ValueError('回执未包含四版成片')
    artifacts = {}
    for code, item in variants.items():
        if not isinstance(item, dict):raise ValueError('成片回执无效')
        checked = item.get('probe', {})
        if item.get('reviewed') is not False:raise ValueError('机器处理不得自动声称人工审核通过')
        if (checked.get('width'), checked.get('height'), checked.get('fps'), checked.get('container')) != (1080,1920,'60','MP4') or not checked.get('full_decode') or checked.get('audio_streams',0) < 1:
            raise ValueError('成片缺少规定规格/完整解码记录')
        file = _artifact(item.get('file'), recorded_directory, directory)
        if not file.is_file() or file.stat().st_size == 0 or sha(file) != item.get('sha256'):
            raise ValueError('成片不存在或哈希不符')
        if code in ('V2','V3') and item['sha256'] != fingerprint[code]:
            raise ValueError('原生母版复制结果不一致')
        if code in ('V1','V4'):
            expected = ('V3','V2') if code=='V1' else ('V2','V3')
            if (item.get('video_source'),item.get('audio_source')) != expected:
                raise ValueError('派生版来源组合错误')
        artifacts[file.relative_to(directory).as_posix()] = item['sha256']
    srt = result.get('srt') or {}
    file = _artifact(srt.get('file'), recorded_directory, directory)
    if not file.is_file() or sha(file) != srt.get('sha256'):
        raise ValueError('SRT不存在或哈希不符')
    original, cues = read_srt(source_paths['SRT'])
    archived, archived_cues = read_srt(file)
    if original.replace('\r\n','\n') != archived.replace('\r\n','\n') or cues != archived_cues:
        raise ValueError('归档SRT文字/时间码与来源不一致')
    artifacts[file.relative_to(directory).as_posix()] = srt['sha256']
    for name in ('两母版交付报告.json','审核归档总表.csv','交付状态.json'):
        file = directory / name
        _no_links(file, directory)
        if not file.is_file() or file.stat().st_size == 0:
            raise ValueError('交付报告、审核CSV或完成状态尚未齐全')
        artifacts[name] = sha(file)
    status = _read_json(directory/'交付状态.json')
    if status.get('status') != '五项文件完成·待声画审核':
        raise ValueError('处理器未提交五项文件完成状态')
    persisted = _read_json(directory/'两母版交付报告.json')
    submitted = {key:value for key,value in result.items() if key != '_actual_directory'}
    if persisted != submitted:
        raise ValueError('处理器返回值与落盘交付报告不一致')
    with (directory/'审核归档总表.csv').open(encoding='utf-8-sig',newline='') as stream:
        rows = list(csv.DictReader(stream))
    if len(rows)!=5 or {row.get('版本') for row in rows} != {'V1','V2','V3','V4','SRT'}:
        raise ValueError('审核CSV没有完整且唯一的五项交付记录')
    for row in rows:
        item = srt if row['版本']=='SRT' else variants[row['版本']]
        expected_file = _artifact(item['file'],recorded_directory,directory)
        actual_file = _artifact(row.get('文件'),recorded_directory,directory)
        if row.get('项目/集') != identity or row.get('SHA256') != item['sha256'] or actual_file != expected_file:
            raise ValueError('审核CSV的身份、文件或哈希与交付报告不一致')
        if row.get('状态') not in ('规格通过·待声画审核','待审核'):
            raise ValueError('生成审核CSV不能冒充人工审核通过')
    return directory, artifacts


class InboxWorker:
    def __init__(self, inbox, output_root, app_dir, *, state_dir=None,
                 stable_seconds=2.0, processor=None, clock=time.time, log=lambda value: None,on_result=None,
                 completed_audit_interval=0.0):
        self.inbox = Path(inbox).resolve()
        self.output_root = Path(output_root).resolve()
        self.state_dir = (Path(state_dir) if state_dir else self.output_root / '.inbox-state').resolve()
        for path in (self.inbox, self.output_root, self.state_dir):_not_draft(path)
        if _overlap(self.inbox, self.output_root) or _overlap(self.inbox, self.state_dir):
            raise ValueError('收件箱、输出及回执目录不能与输入互相包含或重叠')
        if not self.inbox.is_dir():raise FileNotFoundError('收件箱目录不存在')
        if not math.isfinite(stable_seconds) or stable_seconds < 0:raise ValueError('稳定等待时间需为有限非负数')
        if not math.isfinite(completed_audit_interval) or completed_audit_interval < 0:
            raise ValueError('完成任务审计窗口需为有限非负秒数，0表示每轮严格复核')
        self.app_dir, self.stable_seconds = app_dir, float(stable_seconds)
        self.completed_audit_interval=float(completed_audit_interval)
        self.processor = processor or compose_bundle
        self.clock, self.log = clock, log
        self.on_result=on_result

    def _save(self, path, record):
        record['updated'] = now()
        atomic_json(path, record)

    def _error(self, folder, state, error):
        relative = folder.relative_to(self.inbox).as_posix()
        key = hashlib.sha256(relative.encode('utf-8')).hexdigest()
        errors = self.state_dir/'errors';errors.mkdir(exist_ok=True)
        _no_links(errors,self.state_dir)
        value = {'task':relative, 'state':state, 'error':str(error), 'at':now()}
        atomic_json(errors/(key+'.json'), value)
        return {**value,'diagnostic':str(errors/(key+'.json'))}

    def _owned_output(self,relative,parent):
        lexical=self.output_root/relative
        _no_links(lexical,self.output_root)
        path=lexical.resolve()
        if not _inside(path,parent):raise ValueError('回执中的交付路径越界')
        return path

    def _cache_metadata(self,paths,run,directory):
        return {'roots':{'inbox':str(self.inbox),'output':str(self.output_root),'state':str(self.state_dir)},
                'directory':str(directory),'sources':_metadata(paths),'outputs':_tree_metadata(run)}

    def _audit_fields(self,record,hash_verified):
        audit=record.get('audit') or {}
        epoch=audit.get('verified_epoch')
        due=epoch+self.completed_audit_interval if isinstance(epoch,(int,float)) and self.completed_audit_interval>0 else None
        return {'hash_verified_this_scan':bool(hash_verified),'metadata_checked_this_scan':True,
                'last_verified':audit.get('last_verified'),'audit_due':_iso_epoch(due) if due is not None else None,
                'audit_due_epoch':due,'completed_audit_interval':self.completed_audit_interval}

    def _record_audit(self,record,paths,run,directory,artifacts,source_metadata,output_metadata,verified_epoch):
        metadata=self._cache_metadata(paths,run,directory)
        if metadata['sources']!=source_metadata or metadata['outputs']!=output_metadata:
            raise RuntimeError('严格复核期间来源或结果元数据发生变化，未延长审计窗口')
        record.update(state='completed',result_directory=directory.relative_to(self.output_root).as_posix(),
                      artifacts=artifacts,last_verified=_iso_epoch(verified_epoch))
        record['audit']={'schema':1,'verified_epoch':verified_epoch,'last_verified':record['last_verified'],
                         'fingerprint':dict(record['fingerprint']),'artifacts':dict(artifacts),'metadata':metadata}

    def _cached(self,record,paths,manifest_hash,current,force_audit):
        if force_audit or self.completed_audit_interval==0 or record.get('state')!='completed':return None
        audit=record.get('audit') or {};fingerprint=record.get('fingerprint');artifacts=record.get('artifacts')
        if (not isinstance(audit,dict) or type(audit.get('schema')) is not int or audit.get('schema')!=1 or not isinstance(fingerprint,dict)
                or set(fingerprint)!={'task.json','V2','V3','SRT'} or audit.get('fingerprint')!=fingerprint
                or not isinstance(artifacts,dict) or len(artifacts)!=8 or audit.get('artifacts')!=artifacts
                or not {'两母版交付报告.json','交付状态.json','审核归档总表.csv'}.issubset(artifacts)
                or record.get('last_verified')!=audit.get('last_verified')):return None
        if any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value)
               for value in [*fingerprint.values(),*artifacts.values()]):return None
        if fingerprint['task.json']!=manifest_hash:return None
        epoch=audit.get('verified_epoch')
        if type(epoch) not in (int,float) or not math.isfinite(epoch) or current<epoch or current-epoch>=self.completed_audit_interval:return None
        try:
            run=self._owned_output(record['run_directory'],self.output_root)
            directory=self._owned_output(record['result_directory'],run)
            metadata=self._cache_metadata(paths,run,directory)
            actual_files={item['path'] for item in metadata['outputs'].values() if not item['directory']}
            expected_files=set()
            for name in artifacts:
                relative=Path(name.replace('\\','/'))
                windows=PureWindowsPath(name)
                if windows.drive or windows.root or relative.is_absolute() or '..' in relative.parts:return None
                expected_files.add(str((directory/relative).resolve()))
            if not expected_files.issubset(actual_files):return None
        except (OSError,ValueError,KeyError,TypeError):return None
        if metadata!=audit.get('metadata'):return None
        return {'state':'completed_cached','directory':str(directory),'reviewed':False,
                'verification_note':'本轮仅检查路径/大小/修改时间/文件标识；已登记交付项的隐藏内容变化在到期或强制完整审计时检查',
                **self._audit_fields(record,False)}

    def _task(self, folder,force_audit=False):
        task, paths, manifest_hash = _read_task(folder)
        relative = folder.relative_to(self.inbox).as_posix()
        key = hashlib.sha256(task['id'].encode('utf-8')).hexdigest()
        record_path = self.state_dir/(key+'.json')
        _no_links(record_path,self.state_dir)
        record = _read_json(record_path) if record_path.exists() else {
            'schema':1, 'id':task['id'], 'task':relative, 'identity':task['identity'],
            'state':'waiting', 'attempts':0}
        if record.get('schema') != 1 or record.get('id') != task['id']:
            return self._error(folder,'state_corrupt','回执对象身份无效，未重新执行')
        if record.get('task') != relative:
            return self._error(folder,'duplicate_id','同一任务id出现在不同文件夹，未复用旧回执')
        state=record.get('state')
        if state not in {'waiting','processing','completed','failed','interrupted','source_changed','output_changed'}:
            return self._error(folder,'state_corrupt','回执状态无效，未创建新的处理尝试')
        if state in ('failed','interrupted','source_changed','output_changed'):
            return {'id':task['id'],'task':relative,'state':state,'error':record.get('error',''),
                    'hash_verified_this_scan':False}
        try:
            stats = _stats(paths)
        except (FileNotFoundError,ValueError) as exc:
            if record.get('fingerprint'):
                record['state']='source_changed';record['error']='已提交来源缺失或变成空文件：'+str(exc)
                self._save(record_path,record)
                return {'id':task['id'],'task':relative,'state':'source_changed','error':record['error']}
            return self._error(folder,'waiting_files',exc)
        current = float(self.clock())
        if not math.isfinite(current):raise ValueError('审计时钟不是有限值')
        cached=self._cached(record,paths,manifest_hash,current,force_audit)
        if cached:return {'id':task['id'],'task':relative,**cached}
        observed = record.get('observation') or {}
        if state!='completed' and (observed.get('stats') != stats or current < observed.get('first_seen', current)):
            record['observation'] = {'stats':stats, 'first_seen':current, 'count':1}
            self._save(record_path, record)
            return {'id':task['id'],'task':relative,'state':'settling'}
        observed['count'] = observed.get('count',1)+1
        record['observation'] = observed
        if state!='completed' and current-observed['first_seen'] < self.stable_seconds:
            self._save(record_path, record)
            return {'id':task['id'],'task':relative,'state':'settling'}
        try:
            source_metadata=_metadata(paths)
            fingerprint = _fingerprint(paths, stats)
            if _metadata(paths)!=source_metadata:raise RuntimeError('核对哈希时来源元数据变化')
        except (OSError,RuntimeError) as exc:
            record['observation'] = {}
            self._save(record_path,record)
            return self._error(folder,'settling',exc)
        if fingerprint['task.json'] != manifest_hash:
            record['observation']={}
            self._save(record_path,record)
            return self._error(folder,'settling','读取后task.json发生变化，未执行过期清单')
        submitted = task.get('source_sha256')
        if submitted is not None:
            if (not isinstance(submitted,dict) or set(submitted)!={'V2','V3','SRT'}
                    or any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value) for value in submitted.values())):
                return self._error(folder,'invalid_task','提交来源哈希格式无效')
            if any(fingerprint[key]!=submitted[key] for key in ('V2','V3','SRT')):
                record['state']='source_changed';record['error']='媒体在提交确认后发生变化，请重新确认并提交新任务'
                self._save(record_path,record)
                return {'id':task['id'],'task':relative,'state':'source_changed','error':record['error']}
        if record.get('fingerprint') and record['fingerprint'] != fingerprint:
            record['state']='source_changed';record['error']='同一id的来源内容或task.json变化；请检查并用新任务id重新提交'
            self._save(record_path,record)
            return {'id':task['id'],'task':relative,'state':'source_changed','error':record['error']}
        state = record['state']
        if state in ('completed','processing'):
            run = self.output_root / record['run_directory']
            if not _inside(run.resolve(), self.output_root):raise ValueError('回执的输出路径越界')
            try:output_metadata=_tree_metadata(run)
            except (OSError,ValueError) as exc:
                record['state']='output_changed' if state=='completed' else 'interrupted'
                record['error']='无法安全读取输出目录元数据：'+str(exc)
                self._save(record_path,record)
                return {'id':task['id'],'task':relative,'state':record['state'],'error':record['error']}
            candidates = list(run.rglob('两母版交付报告.json'))
            valid = []
            for candidate in candidates:
                try:
                    _no_links(candidate,run)
                    value = _read_json(candidate)
                    value['_actual_directory'] = str(candidate.parent)
                    directory, artifacts = validate_result(value,fingerprint,run,task['identity'],paths)
                    valid.append((directory,artifacts))
                except (OSError,ValueError,KeyError,TypeError):
                    continue
            if len(valid) != 1:
                record['state']='output_changed' if state=='completed' else 'interrupted'
                record['error']='未找到唯一且五项文件/报告/审核CSV均通过核验的结果；不自动重复生成'
                self._save(record_path,record)
                return {'id':task['id'],'task':relative,'state':record['state'],'error':record['error']}
            directory, artifacts = valid[0]
            if state=='completed' and artifacts != record.get('artifacts'):
                record['state']='output_changed';record['error']='已完成任务的交付记录或文件发生变化，未沿用旧回执'
                self._save(record_path,record)
                return {'id':task['id'],'task':relative,'state':record['state'],'error':record['error']}
            try:self._record_audit(record,paths,run,directory,artifacts,source_metadata,output_metadata,current)
            except RuntimeError as exc:
                record['observation']={};self._save(record_path,record)
                return self._error(folder,'settling',exc)
            self._save(record_path,record)
            return {'id':task['id'],'task':relative,'state':'already_completed' if state=='completed' else 'recovered',
                    'directory':str(directory),'reviewed':False,**self._audit_fields(record,True)}
        run_relative = 'jobs/' + key[:20] + '_' + uuid.uuid4().hex[:12]
        run = self.output_root/run_relative
        _no_links(run,self.output_root)
        run.mkdir(parents=True,exist_ok=False)
        record.update(state='processing', fingerprint=fingerprint, run_directory=run_relative,
                      attempts=record.get('attempts',0)+1, started=now())
        self._save(record_path,record)
        self.log('开始收件任务：'+task['identity'])
        if self.on_result:self.on_result({'id':task['id'],'task':relative,'identity':task['identity'],
            'state':'processing','receipt':str(record_path)})
        try:
            result = self.processor(paths['V2'],paths['V3'],paths['SRT'],run,self.app_dir,
                                    task['identity'],confirmed_pair=True,log=self.log)
            audit_started=float(self.clock());source_metadata=_metadata(paths)
            if _fingerprint(paths, _stats(paths)) != fingerprint:
                raise RuntimeError('处理过程中来源变化，结果未被收件箱登记为完成')
            output_metadata=_tree_metadata(run)
            directory, artifacts = validate_result(result,fingerprint,run,task['identity'],paths)
            self._record_audit(record,paths,run,directory,artifacts,source_metadata,output_metadata,audit_started)
            self._save(record_path,record)
            return {'id':task['id'],'task':relative,'state':'completed','directory':str(directory),'reviewed':False,
                    **self._audit_fields(record,True)}
        except Exception as exc:
            record['state']='failed';record['error']=str(exc)
            self._save(record_path,record)
            self.log('收件任务停止，继续其他任务：'+str(exc))
            return {'id':task['id'],'task':relative,'state':'failed','error':str(exc)}

    def scan_once(self, stop_event=None,*,force_audit=False):
        """Strict by default; an explicit audit window labels metadata-only cache hits."""
        for path in (self.inbox,self.output_root,self.state_dir):
            if path.resolve()!=path:
                raise ValueError('收件箱/输出/回执路径的解析位置发生变化，停止写入')
            _not_draft(path)
        results = []
        with _state_lock(self.state_dir):
            for folder in sorted(self.inbox.iterdir()):
                if stop_event is not None and stop_event.is_set():break
                if not folder.is_dir() or not (folder/'task.json').exists():continue
                try:
                    _no_links(folder,self.inbox)
                    row=self._task(folder,force_audit)
                except Exception as exc:
                    row=self._error(folder,'invalid_task',exc)
                if row.get('id'):
                    key=hashlib.sha256(row['id'].encode('utf-8')).hexdigest()
                    row['receipt']=str(self.state_dir/(key+'.json'))
                results.append(row)
                if self.on_result:self.on_result(dict(row))
        return {'at':now(), 'results':results,
                'completed':sum(item['state'] in ('completed','recovered') for item in results),
                'cached':sum(item['state']=='completed_cached' for item in results),
                'hash_verified':sum(item.get('hash_verified_this_scan') is True for item in results),
                'attention':sum(item['state'] in ('failed','interrupted','invalid_task','duplicate_id','state_corrupt','source_changed','output_changed') for item in results)}

    def run(self, stop_event, poll_seconds=1.0, on_scan=None,*,force_audit=False):
        """Explicit foreground loop; stop between jobs/scans, never kill FFmpeg."""
        if poll_seconds <= 0:raise ValueError('轮询间隔必须大于零')
        if stop_event is None:raise ValueError('运行循环需要可停止的Event')
        while not stop_event.is_set():
            try:
                result=self.scan_once(stop_event,force_audit=True) if force_audit else self.scan_once(stop_event)
            except InboxBusy:
                result={'at':now(),'results':[],'busy':True,'completed':0,'attention':0}
            if on_scan:on_scan(result)
            stop_event.wait(poll_seconds)


def scan_once(inbox, output_root, app_dir, *,force_audit=False,**kwargs):
    """Convenience API; persistent observations survive a fresh instance."""
    return InboxWorker(inbox,output_root,app_dir,**kwargs).scan_once(force_audit=force_audit)
