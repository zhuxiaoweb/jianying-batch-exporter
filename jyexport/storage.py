from __future__ import annotations
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .draft import VARIANTS, transform, srt
from .discovery import load_json, user_data

class Cancelled(RuntimeError):
    pass


class SourceChanged(RuntimeError):
    pass

def now():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).isoformat(timespec='seconds')

def safe_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:65] or '未命名'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I):
        name = '_' + name
    return name

def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(2 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

def _sharing_retry(error,attempt):
    if os.name=='nt' and getattr(error,'winerror',None) in (5,32,33) and attempt<7:
        time.sleep(min(.02*(2**attempt),.2));return True
    return False


def read_bytes_retry(path):
    for attempt in range(8):
        try:return Path(path).read_bytes()
        except PermissionError as exc:
            if not _sharing_retry(exc,attempt):raise


def atomic_json(path, data,expected_sha256=None):
    path = Path(path)
    temporary = path.with_name('.j-' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(8):
        try:
            if expected_sha256 is not None and sha(path)!=expected_sha256:
                raise ValueError('目标文件被其他程序更新，停止写入并保留已有结果：'+str(path))
            os.replace(temporary,path);return
        except PermissionError as exc:
            if not _sharing_retry(exc,attempt):raise

def files_under(root):
    root = Path(root).resolve()
    result = []
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
                raise ValueError(f'草稿包含链接/联接点，已停止，避免备份越界：{path}')
        result.extend(Path(current) / name for name in files)
    return sorted(result)

def snapshot(source, destination, cancel=None, log=lambda message: None):
    try:return _snapshot(source,destination,cancel,log)
    except OSError as exc:
        changed_path=Path(exc.filename).resolve() if exc.filename else None
        if (isinstance(exc,FileNotFoundError) or getattr(exc,'winerror',None) in (32,33)) and changed_path is not None and changed_path.is_relative_to(Path(source).resolve()):
            raise SourceChanged('草稿仍在保存或已移动，请完成保存后稍等，再刷新列表重试。') from exc
        raise


def _snapshot(source, destination, cancel=None, log=lambda message: None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or source in destination.parents:
        raise ValueError('备份目录不能放在源草稿内部')
    if destination.exists():
        raise FileExistsError('备份目标已存在，拒绝覆盖')
    files = files_under(source)
    total = sum(path.stat().st_size for path in files)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < total + 64 * 1024 * 1024:
        raise OSError('备份磁盘空间不足')
    destination.mkdir()
    records = []
    for file in files:
        if cancel and cancel.is_set():
            raise Cancelled('已取消；未完成的备份保留在任务目录，不会用于生成副本')
        relative = file.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        stat = file.stat()
        shutil.copy2(file, target)
        copied_hash = sha(target)
        if sha(file) != copied_hash or file.stat().st_mtime_ns != stat.st_mtime_ns:
            raise SourceChanged('备份时草稿仍在保存，请保存后稍等，再点击开始。')
        records.append({'path': relative.as_posix(), 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sha256': copied_hash})
    if [p.relative_to(source).as_posix() for p in files_under(source)] != [r['path'] for r in records]:
        raise SourceChanged('草稿目录在备份时发生变化，请完成保存后稍等再试。')
    for record in records:
        path = source / record['path']
        if path.stat().st_mtime_ns != record['mtime_ns'] or sha(path) != record['sha256']:
            raise SourceChanged('源草稿在备份结束前发生变化，备份未通过一致性检查；请完成保存后稍等再试。')
    log(f'备份核验通过：{source.name}（{len(records)} 文件）')
    return {'source': str(source), 'backup': str(destination), 'at': now(), 'bytes': total, 'files': records}


def stable_snapshot(source,destination,cancel=None,log=lambda value:None):
    """Retry a concurrent save into a new directory; retain every partial copy."""
    destination=Path(destination)
    for attempt in range(3):
        target=destination if attempt==0 else destination.with_name(destination.name+'_重试'+str(attempt))
        try:return snapshot(source,target,cancel,log)
        except SourceChanged:
            if attempt==2:raise
            log('草稿正在保存，稍等后自动重新备份；未使用不完整的备份。')
            if cancel is not None:
                if cancel.wait(.35*(attempt+1)):raise Cancelled('已停止等待草稿保存，原稿未改动。')
            else:time.sleep(.35*(attempt+1))

def event(session, action, **fields):
    session.setdefault('events', []).append({'at': now(), 'action': action, **fields})

def validate_session(folder, session):
    folder = Path(folder).resolve()
    from .archive_layout import validate_layout,output_path_allowed
    destinations,projects={},{}
    def inside(path, parent):
        resolved, parent = Path(path).resolve(), Path(parent).resolve()
        if resolved == parent or parent not in resolved.parents:
            raise ValueError(f'任务清单含有越界路径，已停止：{path}')
    for backup in session.get('backups', []):
        inside(backup['backup'], folder / '源草稿备份')
        for item in backup['files']:
            inside(Path(backup['backup']) / item['path'], backup['backup'])
    for job in session.get('jobs', []):
        grouped='archive_layout' in job
        if grouped:
            layout=validate_layout(folder,job)
            project=str(Path(layout['project_directory']).resolve()).casefold()
            original=job['draft']['name']
            if project in projects and projects[project]!=original:
                raise ValueError('不同原始剧名不能共用同一归档目录')
            projects[project]=original
            for target in layout['targets'].values():
                canonical=str(Path(target).resolve()).casefold()
                if canonical in destinations:
                    raise ValueError('本批次存在重复归档目标，停止以免覆盖：'+target)
                destinations[canonical]=job['id']
        else:inside(job['directory'], folder / '交付')
        inside(job['backup_content'], job['backup'])
        inside(job['backup'], folder / '源草稿备份')
        if job['backup'] not in [b['backup'] for b in session.get('backups', [])]:
            raise ValueError('任务引用了未经本批次记录的备份')
        for prepared in job.get('prepared', {}).values():
            inside(prepared['folder'], folder / '可导入草稿副本')
        items=[*job.get('variants', {}).items(),('SRT',job.get('srt') or {})]
        for key,item in items:
            if item.get('file'):
                if grouped:
                    if not output_path_allowed(job,key,item['file']):raise ValueError('归档文件不属于本集精确目标：'+str(item['file']))
                else:inside(item['file'], job['directory'])

def save_session(folder, session,expected_manifest_sha=None):
    validate_session(folder, session)
    if session.get('jobs') and session.get('status') not in ('备份中', '创建失败'):
        items = []
        for job in session['jobs']:
            items.extend(job['variants'].values())
            if job['want_srt']:
                items.append(job.get('srt') or {})
        session['status'] = '已审核' if items and all(x.get('file') and x.get('reviewed') for x in items) else ('待审核' if items and all(x.get('file') for x in items) else '待导出')
    manifest=Path(folder)/'任务清单.json'
    atomic_json(manifest,session,expected_sha256=expected_manifest_sha)
    written_sha=sha(manifest) if expected_manifest_sha is not None else None
    try:export_csv(folder, session)
    except PermissionError:
        session['csv_notice']='审核归档总表被占用，成片继续制作；关闭表格后点“继续上次任务”更新归档表。'
        atomic_json(manifest,session,expected_sha256=written_sha)
    else:
        if session.pop('csv_notice',None) is not None:atomic_json(manifest,session,expected_sha256=written_sha)

def create_session(drafts, output, variants, want_srt=True, cancel=None, log=lambda message: None, *, archive_mode='legacy'):
    drafts=list(drafts)
    if not drafts or not variants and not want_srt:
        raise ValueError('请选择时间线及至少一种输出')
    if archive_mode not in ('legacy','series'):raise ValueError('归档模式只支持legacy或series')
    output = Path(output).resolve()
    for draft in drafts:
        source = Path(draft.folder).resolve()
        if output == source or source in output.parents:
            raise ValueError('输出目录不能在选中的源草稿内部')
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')
    folder = output / f'导出批次_{stamp}_{uuid.uuid4().hex[:6]}'
    folder.mkdir()
    session = {'schema': 1, 'created': now(), 'status': '备份中', 'specification': {'width':1080, 'height':1920, 'fps':60, 'format':'MP4', 'ratio':'9:16'}, 'backups': [], 'jobs': [], 'events': []}
    save_session(folder, session)
    source_map = {}
    try:
        planned=[]
        if archive_mode=='series':
            from .archive_layout import prepare_layout,validate_layout
            identifiers=set()
            for draft in drafts:
                identifier=uuid.uuid4().hex[:10]
                while identifier in identifiers:identifier=uuid.uuid4().hex[:10]
                identifiers.add(identifier);planned.append((draft,identifier))
            layouts=[prepare_layout(folder,draft,identifier,drafts,planned_ids=planned) for draft,identifier in planned]
            for (draft,identifier),layout in zip(planned,layouts):
                validate_layout(folder,{'id':identifier,'draft':draft.dict(),'directory':str(folder/'任务记录'/identifier),'archive_layout':layout})
        for draft in drafts:
            if draft.folder in source_map:
                continue
            backup = folder / '源草稿备份' / (safe_name(Path(draft.folder).name) + '_' + uuid.uuid4().hex[:6])
            source_map[draft.folder] = stable_snapshot(draft.folder, backup, cancel, log)
            session['backups'].append(source_map[draft.folder])
            save_session(folder, session)
        for number,draft in enumerate(drafts):
            if cancel and cancel.is_set():
                raise Cancelled('已取消任务创建')
            job_id = uuid.uuid4().hex[:10]
            if archive_mode=='series':
                job_id=planned[number][1];layout=layouts[number]
                job_folder=folder/'任务记录'/job_id
            else:job_folder = folder / '交付' / f'{safe_name(draft.name)}__{safe_name(draft.timeline_name)}__{job_id[:5]}'
            job_folder.mkdir(parents=True)
            relative = Path(draft.content).relative_to(Path(draft.folder))
            job = {'id': job_id, 'draft': draft.dict(), 'backup': source_map[draft.folder]['backup'], 'backup_content': str(Path(source_map[draft.folder]['backup']) / relative),
                'directory': str(job_folder), 'roles': {}, 'roles_content_sha256': '', 'status': '待剪映原生导出' if draft.encrypted else '待片段分类',
                'want_srt': want_srt, 'srt': None, 'prepared': {}, 'variants': {key: {'label': VARIANTS[key]['label'], 'status':'待导出', 'file':None, 'reviewed':False} for key in variants}}
            if archive_mode=='series':job['archive_layout']=layout
            session['jobs'].append(job)
            guide = ['剪映草稿导出任务', f'草稿：{draft.name}', f'时间线 / 集：{draft.timeline_name}', '', '先在剪映内复制草稿，核对集数，然后仅在副本操作。原草稿已经备份。',
                '每个版本均使用：1080P / MP4 / 60 fps / 9:16 抖音（1080×1920）。', '保留人声、环境音、音效；逐条检查所有音轨，不能只按轨道序号识别背景音乐。',
                '内嵌在源视频画面中的字幕、与其他声音混合在同一素材中的音乐，无法通过隐藏字幕/音乐轨移除。', '导出字幕：剪映原生字幕导出 SRT；若该版本界面不提供独立导出，请提供可读取字幕草稿或已有 SRT。', '']
            for key in variants:
                target=layout['targets'][key] if archive_mode=='series' else job_folder / (key + '_' + VARIANTS[key]['label'] + '.mp4')
                guide.append(f'{key} {VARIANTS[key]["label"]} → {target}')
            guide += ['', '在工具中选中任务和版本，选择原生导出的文件，执行“核验并归档”。', '机器只核验媒体规格和完整解码；声画内容须人工审核。任务清单区分待导出、待审核和已审核。']
            (job_folder / '剪映原生导出步骤.txt').write_text('\n'.join(guide), 'utf-8-sig')
        session['status'] = '待导出'
        event(session, '创建批次', count=len(session['jobs']))
        save_session(folder, session)
        return folder, session
    except Exception as exc:
        session['status'] = '创建失败'
        event(session, '失败', error=str(exc))
        save_session(folder, session)
        raise

def rewrite_paths(value, source, target):
    if isinstance(value, dict):
        return {k: rewrite_paths(v, source, target) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite_paths(v, source, target) for v in value]
    if isinstance(value, str):
        value = re.sub(r'##_draftpath_placeholder_[^#]+_##', lambda match: target.as_posix(), value)
        normalized = value.replace('\\', '/')
        prefix = source.as_posix()
        if normalized.casefold() == prefix.casefold() or normalized.casefold().startswith(prefix.casefold() + '/'):
            return target.as_posix() + normalized[len(prefix):]
    return value

def prepare_job(folder, session, job, codec, log=lambda message: None):
    if 'archive_layout' in job:
        raise ValueError('按剧汇总批次请在主界面继续制作交付包，手动草稿副本入口仅适用原结构')
    validate_session(folder, session)
    backup = Path(job['backup'])
    content = Path(job['backup_content'])
    backup_record = next(x for x in session['backups'] if x['backup'] == str(backup))
    for record in backup_record['files']:
        if sha(backup / record['path']) != record['sha256']:
            raise ValueError('备份已被更改，停止生成副本')
    if job['roles_content_sha256'] != sha(content):
        raise ValueError('片段分类不属于当前备份内容，请重新分类')
    data = codec.read(content)
    from .draft import assert_classified, assert_simple
    assert_classified(data, job['roles'], include_audio=bool(job['variants']))
    assert_simple(data)
    if job['variants']:
        missing = []
        for kind in ('videos', 'audios'):
            for material in data.get('materials', {}).get(kind, []):
                value = material.get('path')
                if not value:
                    continue
                resolved = rewrite_paths(value, Path(job['draft']['folder']), backup)
                if not Path(resolved).is_file():
                    missing.append(resolved)
        if missing:
            raise FileNotFoundError('素材缺失，停止生成避免输出不完整视频：\n' + '\n'.join(missing[:12]))
    expected_bytes = backup_record['bytes'] * len(job['variants'])
    if shutil.disk_usage(folder).free < expected_bytes + 64 * 1024 * 1024:
        raise OSError('生成四版草稿副本的磁盘空间不足')
    if job['want_srt']:
        result = srt(data, job['roles'])
        if result:
            path = Path(job['directory']) / '字幕.srt'
            if path.exists():
                raise FileExistsError('字幕文件已存在，请先核对，避免覆盖')
            path.write_text(result, 'utf-8-sig', newline='\n')
            job['srt'] = {'file': str(path), 'sha256': sha(path), 'status': '待审核', 'source':'草稿时间线'}
        else:
            job['srt'] = {'file':None, 'status':'无已选字幕'}
    for variant in job['variants']:
        if variant in job['prepared']:
            continue
        name = safe_name(f'导出_{job["draft"]["name"]}_{job["draft"]["timeline_name"]}_{variant}_{job["id"][:5]}')
        dest = Path(folder) / '可导入草稿副本' / name
        if dest.exists():
            raise FileExistsError(f'副本目录已存在，拒绝覆盖：{dest}')
        def ignore(path, names):
            return [n for n in names if n in ('Timelines', '.locked', 'draft_content.json', 'draft_content.json.bak', 'draft_meta_info.json', 'draft_meta_info.json.bak', 'timeline_layout.json', 'draft_content.json.cache', 'crypto_key_store.dat', 'crypto_key_store.dat.bak', 'agent_adoption_ledger.db')]
        shutil.copytree(backup, dest, ignore=ignore)
        new_data = rewrite_paths(transform(data, job['roles'], variant), Path(job['draft']['folder']), dest)
        uid = str(uuid.uuid4()).upper()
        new_data['id'] = uid
        new_data['path'] = str(dest)
        for filename in ('draft_content.json', 'draft_content.json.bak'):
            (dest / filename).write_text(json.dumps(new_data, ensure_ascii=False, separators=(',', ':')), 'utf-8')
        timestamp = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
        meta = {'draft_id':uid, 'draft_name':name, 'draft_fold_path':dest.as_posix(), 'draft_root_path':dest.parent.as_posix(),
            'draft_json_file':(dest/'draft_content.json').as_posix(), 'draft_cover':(dest/'draft_cover.jpg').as_posix(),
            'draft_new_version':new_data.get('new_version',''), 'tm_duration':new_data.get('duration',0), 'tm_draft_create':timestamp,
            'tm_draft_modified':timestamp, 'tm_draft_removed':0, 'draft_is_invisible':False, 'draft_is_cloud_temp_draft':False, 'cloud_draft_sync':False}
        atomic_json(dest / 'draft_meta_info.json', meta)
        job['prepared'][variant] = {'folder': str(dest), 'id': uid, 'name': name, 'status':'待在剪映检查并渲染'}
        log(f'生成 {name}；仍需在剪映检查并原生渲染')
        event(session, '生成草稿副本', job=job['id'], variant=variant, path=str(dest))
        save_session(folder, session)
    job['status'] = '草稿副本已生成，待原生渲染'
    save_session(folder, session)

def register_prepared(folder, session):
    validate_session(folder, session)
    if os.name != 'nt':
        raise RuntimeError('仅支持 Windows')
    checked = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if checked.returncode:
        raise RuntimeError('无法检查剪映进程状态，未修改草稿列表')
    running = checked.stdout.decode(errors='replace')
    if 'jianyingpro.exe' in running.casefold():
        raise RuntimeError('请保存并完全退出剪映后再注册副本，避免剪映覆盖草稿列表。')
    index_path = user_data() / 'Projects/com.lveditor.draft/root_meta_info.json'
    index = load_json(index_path)
    if not isinstance(index, dict) or not isinstance(index.get('all_draft_store'), list):
        raise ValueError('无法识别本机草稿列表结构；未修改列表')
    additions = []
    ids = {x.get('draft_id') for x in index['all_draft_store']}
    for job in session['jobs']:
        for prepared in job['prepared'].values():
            meta = load_json(Path(prepared['folder']) / 'draft_meta_info.json')
            if not meta or meta.get('draft_id') != prepared['id']:
                raise ValueError('副本元数据发生变化，停止注册')
            if meta and meta['draft_id'] not in ids:
                additions.append(meta)
                ids.add(meta['draft_id'])
    if not additions:
        raise ValueError('没有需要注册的新副本')
    before = sha(index_path)
    saved = Path(folder) / ('草稿列表备份_' + uuid.uuid4().hex[:8] + '.json')
    shutil.copy2(index_path, saved)
    if sha(saved) != before or sha(index_path) != before:
        raise RuntimeError('备份草稿列表时内容发生变化，已停止')
    index['all_draft_store'][0:0] = additions
    if isinstance(index.get('draft_ids'), list):
        index['draft_ids'].extend(x['draft_id'] for x in additions)
    if sha(index_path) != before:
        raise RuntimeError('写入前草稿列表已变化，停止注册；备份已保留')
    atomic_json(index_path, index)
    event(session, '注册副本', count=len(additions), index_backup=str(saved), index_before_sha256=before)
    save_session(folder, session)
    return len(additions)

def export_csv(folder, session):
    path = Path(folder) / '审核归档总表.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        raw_writer = csv.writer(stream)
        class SafeWriter:
            def writerow(self, values):
                raw_writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in values])
        writer = SafeWriter()
        writer.writerow(['草稿', '集/时间线', '版本', '内容', '状态', '人工声画审核', '文件', 'SHA256'])
        for job in session['jobs']:
            for key, item in job['variants'].items():
                writer.writerow([job['draft']['name'],job['draft']['timeline_name'],key,item['label'],item['status'],'通过' if item['reviewed'] else '待审核',item.get('file') or '',item.get('sha256','')])
            if job['want_srt'] or (job.get('srt') or {}).get('omitted'):
                item = job.get('srt') or {}
                writer.writerow([job['draft']['name'],job['draft']['timeline_name'],'SRT','字幕',item.get('status','待导出'),'通过' if item.get('reviewed') else '待审核',item.get('file') or '',item.get('sha256','')])
