"""File-only companion to AI-operated native Jianying exports.

This module never injects input, decrypts projects, or writes editor projects.
The UI agent uses a supported Computer Use tool to duplicate/edit/render.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import uuid
from pathlib import Path
from .codec import Codec
from .discovery import load_json, resource_dirs, resolve_app_dir, roots_and_index, scan, user_data
from .draft import VARIANTS
from .media import archive_srt, archive_video
from .storage import atomic_json, create_session, event, now, safe_name, save_session, sha, validate_session

ORDER = ('V4', 'V3', 'V1', 'V2', 'SRT')
FAST_ORDER = ('V3', 'V2', 'SRT', 'V1', 'V4')


def native_plan_hash(plan):
    """Fingerprint editing semantics, excluding timestamps and prose evidence."""
    def semantic(value):
        if isinstance(value, dict):
            return {key: semantic(item) for key, item in value.items()
                    if key not in ('recorded_at', 'observations', 'evidence', 'pending_decisions', 'user_classification')}
        if isinstance(value, list):
            items = [semantic(item) for item in value]
            if items and all(isinstance(item, dict) for item in items):
                items.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
            return items
        return value
    keys = ('copy_name', 'timeline_name', 'duration_us', 'ratio', 'bgm_present',
            'bgm_targets', 'subtitles_present', 'subtitle_targets')
    content = semantic({key: plan.get(key) for key in keys})
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def _archive_problem(item, code):
    if not item.get('file'):
        return None
    path = Path(item['file'])
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return '已登记文件缺失或为空'
        if sha(path) != item.get('sha256'):
            return '已登记文件哈希已变化'
    except OSError as exc:
        return '已登记文件暂时无法读取：' + str(exc)
    if code != 'SRT':
        result = item.get('probe') or {}
        if not (result.get('full_decode') and result.get('width') == 1080 and
                result.get('height') == 1920 and result.get('fps') == '60' and
                result.get('audio_streams', 0) >= 1):
            return '已登记视频缺少合格的规格/完整解码记录'
    return None


def _master_plan_problem(job, code):
    item = job['variants'].get(code) or {}
    if not item.get('file'):
        return None
    if item.get('plan_review_required'):
        return '分类计划已变更，原母版需要复核'
    recorded = item.get('native_plan_hash')
    if not recorded:
        return '旧版母版证据没有分类计划指纹，需明确复核或在新批次重新原生登记'
    if recorded != native_plan_hash(job['ui']['plan']):
        return '母版的分类计划指纹与当前计划不一致'
    return None


def _unregistered_exports(job):
    found = {}
    directory = Path(job['directory'])
    for code, item in job['variants'].items():
        if not item.get('file'):
            path = directory / (code + '_' + item['label'] + '.mp4')
            if path.exists():
                found[code] = str(path)
    if job['want_srt'] and not (job.get('srt') or {}).get('file'):
        path = directory / '字幕.srt'
        if path.exists():
            found['SRT'] = str(path)
    return found

def read_batch(path):
    path = Path(path).resolve()
    manifest = path if path.is_file() else path / '任务清单.json'
    data = load_json(manifest)
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise ValueError('无法识别任务清单')
    validate_session(manifest.parent, data)
    return manifest.parent, data

def get_job(session, job_id):
    matches = [job for job in session['jobs'] if job['id'] == job_id]
    if len(matches) != 1:
        raise ValueError('任务ID不存在或不唯一')
    return matches[0]

def verify_backup(session, job):
    from .preflight import check_all
    record = next((item for item in session['backups'] if item['backup'] == job['backup']), None)
    if not record:
        raise ValueError('没有完整备份记录，禁止继续编辑')
    def verify(item):
        path = Path(record['backup']) / item['path']
        if not path.is_file() or sha(path) != item['sha256']:
            raise ValueError(f'备份校验失败：{path}')
    check_all(verify, record['files'])
    return record

def current_index():
    data = load_json(user_data() / 'Projects/com.lveditor.draft/root_meta_info.json', {})
    return [item for item in data.get('all_draft_store', []) if not item.get('tm_draft_removed') and not item.get('draft_is_invisible')]

def backup_index(folder, session):
    directory = Path(folder) / '批次环境备份'
    directory.mkdir(exist_ok=True)
    candidates = [user_data() / 'Projects/com.lveditor.draft/root_meta_info.json', user_data() / 'Projects/com.lveditor.draft/root_meta_info.json.bak']
    candidates.extend((user_data() / 'Config/LocalDraftFolder').glob('*.json'))
    results = []
    for source in candidates:
        if not source.is_file():
            continue
        before = sha(source)
        destination = directory / (uuid.uuid4().hex[:8] + '_' + source.name)
        shutil.copy2(source, destination)
        if sha(destination) != before or sha(source) != before:
            raise RuntimeError('备份本地草稿列表时内容变化，请保存并退出剪映后重试')
        results.append({'source': str(source), 'backup': str(destination), 'sha256': before})
    session['ui_index_backups'] = results

def select_drafts(drafts, project, timelines=None, all_timelines=False):
    candidates = [item for item in drafts if item.name == project or str(Path(item.folder).resolve()) == str(Path(project).resolve())]
    folders = {item.folder for item in candidates}
    if len(folders) != 1:
        raise ValueError('草稿名称未找到或对应多个目录。请指定完整草稿目录，不能模糊匹配执行。')
    if all_timelines:
        return candidates
    if not timelines:
        if len(candidates) != 1:
            raise ValueError('该草稿有多集，请通过 --timeline 精确指定集号，或明确使用 --all-timelines')
        return candidates
    selected = []
    for value in timelines:
        matches = [item for item in candidates if item.timeline_name == value or item.timeline_id == value]
        if len(matches) != 1:
            raise ValueError(f'集号/时间线ID不存在或不唯一：{value}')
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected

def prepare_native(drafts, output, variants, want_srt, config, log=lambda value: None):
    folder, session = create_session(drafts, output, variants, want_srt, log=log)
    enable_native(folder, session, config)
    return folder, session

def enable_native(folder, session, config):
    validate_session(folder, session)
    if session.get('native_workflow'):
        return
    source_ids = {str(Path(item.get('draft_fold_path', '')).resolve()).casefold():item.get('draft_id') for item in current_index() if item.get('draft_fold_path')}
    aliases = {}
    for job in session['jobs']:
        source = job['draft']['folder']
        if source not in aliases:
            aliases[source] = 'AI交付_' + uuid.uuid4().hex[:8] + '_' + safe_name(job['draft']['name'])[:24]
        job['ui'] = {'copy_name': aliases[source], 'copy': None, 'source_project_id': source_ids.get(str(Path(source).resolve()).casefold()), 'plan': None, 'state': '待原生复制', 'last_variant': None}
    strategy = 'two_native_masters' if all(set(job['variants']) == set(VARIANTS) for job in session['jobs']) else 'four_native'
    session['native_workflow'] = {'version':2, 'created':now(), 'config':config, 'strategy':strategy,
        'render_order':list(FAST_ORDER if strategy == 'two_native_masters' else ORDER),
        'initial_project_ids':[item.get('draft_id') for item in current_index() if item.get('draft_id')]}
    backup_index(folder, session)
    event(session, '原生导出工作流已准备', projects=len(aliases), jobs=len(session['jobs']))
    save_session(folder, session)
    guide = ['# AI 原生导出工作单', '', '状态：备份已完成，尚未通过剪映界面复制、分类或导出。', '',
        '使用已安装的 $jianying-delivery skill，在剪映原生界面操作。不能把本工作单当成已完成成片。',
        '四版任务优先原生导出V3（有BGM无字幕）、V2（无BGM有字幕）与SRT，母版之间只切换BGM和字幕，不改任何剪辑时序或其他画面；complete-fast将复用媒体流补齐V1/V4。若包时序不一致，退回四次原生导出。',
        '在点击复制、打开编辑或切换任何音轨之前，先确认本批次源草稿和本地列表备份存在。',
        '若截图超时且控件树只有HomeWindow，停止界面动作；不使用猜测坐标或盲按快捷键。', '']
    for job in session['jobs']:
        guide += [f'## {job["draft"]["name"]} / {job["draft"]["timeline_name"]}', f'- 任务ID：`{job["id"]}`',
            f'- 原草稿：`{job["draft"]["folder"]}`', f'- 剪映内新副本必须命名：`{job["ui"]["copy_name"]}`',
            f'- 交付目录：`{job["directory"]}`', '- 不编辑源草稿，不直接更改加密JSON。', '']
    (folder / 'AI原生导出工作单.md').write_text('\n'.join(guide), 'utf-8-sig')

def bind_native_copy(folder, session, job, name):
    verify_backup(session, job)
    if not job.get('ui') or name != job['ui']['copy_name']:
        raise ValueError('副本名称必须与本批次生成的唯一名称一致')
    matches = [item for item in current_index() if item.get('draft_name') == name]
    if len(matches) != 1:
        raise ValueError('本地列表中没有唯一对应的新副本。请在剪映主页用原生复制功能建立并命名，然后重试。')
    item = matches[0]
    draft_id = item.get('draft_id')
    if not draft_id or draft_id in session['native_workflow']['initial_project_ids']:
        raise ValueError('该条目不是批次开始后创建的新草稿，禁止绑定原草稿')
    destination = Path(item['draft_fold_path']).resolve()
    for source_job in session['jobs']:
        source = Path(source_job['draft']['folder']).resolve()
        if destination == source or source in destination.parents or destination in source.parents:
            raise ValueError('副本路径指向原草稿或其内部，已停止')
    if Path(folder).resolve() in destination.parents or not (destination/'draft_content.json').is_file():
        raise ValueError('副本不是剪映正常创建的本地项目')
    project = load_json(destination/'Timelines/project.json', {})
    timelines = [item for item in project.get('timelines', []) if not item.get('is_marked_delete')]
    related = [candidate for candidate in session['jobs'] if candidate['draft']['folder'] == job['draft']['folder']]
    bindings = []
    for candidate in related:
        if candidate['draft']['timeline_id']:
            matched = [timeline for timeline in timelines if timeline.get('name') == candidate['draft']['timeline_name']]
            if len(matched) != 1:
                raise ValueError(f'副本中的集号缺失或重复：{candidate["draft"]["timeline_name"]}')
            timeline_id = matched[0]['id']
            if not (destination/'Timelines'/timeline_id/'draft_content.json').is_file():
                raise ValueError('副本中缺少目标时间线文件')
        else:
            timeline_id = ''
        bindings.append((candidate, timeline_id))
    for candidate, timeline_id in bindings:
        candidate['ui']['copy'] = {'id':draft_id, 'folder':str(destination), 'name':name, 'timeline_id':timeline_id, 'bound_at':now()}
        candidate['ui']['state'] = '副本已绑定·待片段分类'
    event(session, '绑定剪映原生副本', copy_id=draft_id, name=name, path=str(destination), jobs=[candidate['id'] for candidate,_ in bindings])
    save_session(folder, session)
    return {'name':name, 'id':draft_id, 'folder':str(destination), 'jobs_bound':len(bindings)}

def record_plan(folder, session, job, plan):
    if not job.get('ui',{}).get('copy'):
        raise ValueError('尚未验证原生新副本，不允许记录编辑计划')
    if plan.get('timeline_name') != job['draft']['timeline_name'] or plan.get('copy_name') != job['ui']['copy_name']:
        raise ValueError('片段计划的副本或集号与任务不一致')
    if not isinstance(plan.get('duration_us'), int) or plan['duration_us'] <= 0:
        raise ValueError('必须从当前集的原生界面确认总时长，并记录为微秒')
    if plan.get('ratio') != '9:16' or not isinstance(plan.get('observations'), list) or not plan['observations']:
        raise ValueError('缺少9:16画布确认或原生界面观察记录')
    for flag, key in (('bgm_present','bgm_targets'), ('subtitles_present','subtitle_targets')):
        if not isinstance(plan.get(flag), bool) or not isinstance(plan.get(key), list):
            raise ValueError(f'计划必须包含 {flag} 布尔值与 {key} 列表')
        if plan[flag] != bool(plan[key]):
            raise ValueError('是否存在配乐/字幕的标记与实际片段列表不一致')
        for target in plan[key]:
            if target.get('scope') not in ('track','segments') or not target.get('label') or not target.get('evidence'):
                raise ValueError('每个目标必须包含原生界面定位、范围及分类依据')
            if target.get('scope') == 'track' and target.get('contains_other_content') is not False:
                raise ValueError('包含其他音频/文字的混排轨道不能整体静音或隐藏')
            if target.get('scope') == 'segments' and not target.get('segments'):
                raise ValueError('片段操作必须列出具体片段，不能按轨道猜测')
    old_plan = job['ui'].get('plan')
    if old_plan and native_plan_hash(old_plan) != native_plan_hash(plan):
        for item in [*job['variants'].values(), job.get('srt') or {}]:
            if item.get('file'):
                item['plan_review_required'] = True
                item['reviewed'] = False
                item['status'] = '分类计划已变更·待复核'
    job['ui']['plan'] = {**plan, 'recorded_at':now()}
    for code in ('V1','V2'):
        if code in job['variants'] and not job['variants'][code].get('file'):
            job['variants'][code]['status'] = '待短音乐分类' if plan.get('pending_decisions') else '待导出'
    job['ui']['state'] = '计划已记录·待原生导出'
    job['draft']['duration'] = plan['duration_us']
    event(session,'保存原生界面分类计划',job=job['id'])
    save_session(folder,session)

def record_native_export(folder, session, job, variant, file, app_dir, evidence):
    ui = job.get('ui',{})
    if not ui.get('copy') or not ui.get('plan'):
        raise ValueError('尚未验证新副本和分类计划，拒绝登记自动导出成功')
    if variant in ('V1','V2') and ui['plan'].get('pending_decisions'):
        raise ValueError('短音乐分类仍待用户确认，不能登记无背景音乐版本')
    if evidence.get('copy_name') != ui['copy_name'] or evidence.get('timeline_name') != job['draft']['timeline_name']:
        raise ValueError('导出观察记录中的副本/集号不符')
    if not evidence.get('export_finished') or not evidence.get('observations'):
        raise ValueError('尚未观察到剪映原生导出完成')
    if variant == 'SRT':
        archive_srt(folder,session,job,file)
    else:
        if variant not in job['variants']:
            raise ValueError('该版本不在导出计划中')
        setting = VARIANTS[variant]
        if evidence.get('bgm_enabled') is not setting['bgm'] or evidence.get('subtitles_enabled') is not setting['subtitles']:
            raise ValueError('原生轨道状态与版本不符')
        expected = session['specification']
        if evidence.get('settings') != expected:
            raise ValueError('必须在导出界面确认1080×1920、MP4、60fps、9:16')
        archive_video(folder,session,job,variant,file,app_dir,False)
    item = job['srt'] if variant == 'SRT' else job['variants'][variant]
    item['native_ui_evidence'] = evidence
    item['native_plan_hash'] = native_plan_hash(ui['plan'])
    item.pop('plan_review_required', None)
    ui['last_variant'] = variant
    event(session,'登记原生导出结果',job=job['id'],variant=variant)
    save_session(folder,session)
    return {'file':item['file'],'status':item['status'],'sha256':item['sha256']}


def complete_fast(folder, session, job, app_dir, log=lambda value: None):
    if job.get('archive_layout'):raise ValueError('按剧汇总批次请在主界面继续制作交付包；原生手动流程入口仅适用原结构。')
    from .fast import compose_pair
    validate_session(folder, session)
    verify_backup(session, job)
    ui = job.get('ui') or {}
    if not ui.get('copy') or not ui.get('plan') or ui['plan'].get('pending_decisions'):
        raise ValueError('副本身份/音频分类尚未确认，不能生成四版')
    for code, item in job['variants'].items():
        problem = _archive_problem(item, code)
        if problem:
            raise ValueError(f'{code}：{problem}；停止自动补齐，不覆盖已登记成片，请先修复归档记录')
        if item.get('file') and item.get('plan_review_required'):
            raise ValueError(f'{code}分类计划已变更，必须先复核已归档版本')
    unregistered = _unregistered_exports(job)
    if any(code in unregistered for code in ('V1','V4')):
        raise ValueError('存在未登记的V1/V4成片，请先核验来源并修复记录；不覆盖或重复补齐')
    missing = [key for key in ('V1', 'V4') if key in job['variants'] and not job['variants'][key].get('file')]
    # A verified already-complete legacy batch is a no-op, not a silent plan
    # migration. Legacy masters still cannot create any new derived output.
    if not missing and not ui.get('fast_failure'):
        return {'generated': [], 'reason': '所需V1/V4已经归档，不覆盖'}
    for key in ('V2', 'V3'):
        item = job['variants'].get(key) or {}
        evidence = item.get('native_ui_evidence') or {}
        if not item.get('file') or sha(item['file']) != item.get('sha256'):
            raise ValueError(f'{key}母版尚未登记或哈希变化')
        expected = VARIANTS[key]
        if (evidence.get('copy_name') != ui['copy_name'] or
            evidence.get('timeline_name') != job['draft']['timeline_name'] or
            evidence.get('settings') != session['specification'] or
            evidence.get('bgm_enabled') is not expected['bgm'] or
            evidence.get('subtitles_enabled') is not expected['subtitles'] or
            not evidence.get('export_finished') or not item.get('probe', {}).get('full_decode')):
            raise ValueError(f'{key}缺少正确副本/集号/状态的原生导出依据；独立母版请用快速交付页确认')
        problem = _master_plan_problem(job, key)
        if problem:
            raise ValueError(f'{key}：{problem}；不能自动升级为当前分类已确认')
    if not missing:
        if ui.pop('fast_failure', None):
            ui['state'] = '已复查已归档版本·待声画审核'
            event(session, '复查两母版失败后的已归档成片', job=job['id'])
            save_session(folder, session)
        return {'generated': [], 'reason': '所需V1/V4已经归档，不覆盖'}
    def saved(key, item, sources):
        job['variants'][key].update(item)
        job['variants'][key]['derivation'] = {'method':'native_stream_copy',
            'sources':{code:{'file':source['file'],'sha256':source['sha256']} for code, source in sources.items()},
            'video_source':item['video_source'], 'audio_source':item['audio_source']}
        job['variants'][key]['native_plan_hash'] = native_plan_hash(ui['plan'])
        event(session, '两母版复用生成成片', job=job['id'], variant=key)
        save_session(folder, session)
    try:
        result = compose_pair(job['variants']['V2']['file'], job['variants']['V3']['file'],
            job['directory'], app_dir, job['draft'].get('duration',0), missing, log, saved)
    except Exception as exc:
        ui['fast_failure'] = {'at':now(), 'error':str(exc)}
        ui['state'] = '两母版复用失败·已完成版本保留'
        event(session, '两母版复用失败', job=job['id'], error=str(exc))
        save_session(folder, session)
        raise
    ui.pop('fast_failure', None)
    job['ui']['state'] = '两母版已补齐四版·待声画审核'
    save_session(folder, session)
    atomic_json(Path(job['directory'])/'两母版复用记录.json', result)
    return {'generated':list(result['variants']), 'seconds':result['total_seconds'], 'directory':job['directory']}

def next_steps(folder, session):
    result, missing = [], 0
    for job in session['jobs']:
        ui = job.get('ui',{})
        base = {'job_id':job['id'],'project':job['draft']['name'],'timeline':job['draft']['timeline_name'],'copy_name':ui.get('copy_name')}
        if not ui.get('copy'):
            action = {'action':'create_native_copy','reason':'先在剪映主页复制并按唯一名称命名；再bind-copy验证新ID'}
        elif not ui.get('plan'):
            action = {'action':'inspect_native_timeline','reason':'确认正确集号、总时长、全部BGM与字幕目标，保留人声/环境音/音效及标题'}
        else:
            pending=[]
            items = {**job['variants']}
            if job['want_srt']:
                items['SRT'] = job.get('srt') or {}
            archive_problems = {code: problem for code, item in items.items()
                                if (problem := _archive_problem(item, code))}
            plan_problems = {code:'分类计划已变更，已登记结果需要复核'
                             for code, item in items.items()
                             if item.get('file') and item.get('plan_review_required')}
            unregistered = _unregistered_exports(job)
            order = FAST_ORDER if session.get('native_workflow', {}).get('strategy') == 'two_native_masters' else ORDER
            for key in order:
                if key == 'SRT':
                    if not job['want_srt']:
                        continue
                    item=job.get('srt') or {}
                elif key not in job['variants']:
                    continue
                else:
                    item=job['variants'][key]
                valid = bool(item.get('file') and key not in archive_problems)
                if not valid:
                    pending.append(key)
            if archive_problems:
                action={'action':'repair_required','problems':archive_problems,
                        'reason':'已登记文件缺失、变化或证据不完整；停止自动补齐，不覆盖',
                        'directory':job['directory']}
            elif plan_problems:
                action={'action':'review_plan_required','problems':plan_problems,
                        'reason':'分类计划已经变更，不能沿用旧母版或旧审核结论',
                        'directory':job['directory']}
            elif unregistered:
                action={'action':'register_existing_export','files':unregistered,
                        'reason':'发现未登记成片，可能上次被中断；先核验登记，不覆盖或重复补齐',
                        'directory':job['directory']}
            elif pending:
                key=pending[0]
                setting=VARIANTS.get(key,{})
                action={'action':'native_export','variant':key,'bgm':setting.get('bgm'),'subtitles':setting.get('subtitles'),'pending':pending,'directory':job['directory']}
                if key in ('V1','V2') and ui['plan'].get('pending_decisions'):
                    action={'action':'await_content_classification','pending':pending,'questions':ui['plan']['pending_decisions'],'directory':job['directory']}
                elif key in ('V1','V4') and session.get('native_workflow', {}).get('strategy') == 'two_native_masters' and not any(code in pending for code in ('V2','V3')):
                    problems = {code: problem for code in ('V2','V3')
                                if (problem := _master_plan_problem(job, code))}
                    if problems:
                        action={'action':'review_plan_required','problems':problems,
                                'reason':'母版缺少当前分类计划确认，不能自动复用',
                                'directory':job['directory']}
                    else:
                        action={'action':'compose_missing_variants','pending':pending,'directory':job['directory'],
                                'command':'complete-fast','reason':'V2/V3已原生导出，复用媒体流补齐V1/V4，无需再次渲染'}
            elif ui.get('fast_failure'):
                action={'action':'verify_failed_fast_run','reason':ui['fast_failure']['error'],
                        'command':'complete-fast','directory':job['directory']}
            else:
                action={'action':'exports_present_pending_content_review','directory':job['directory']}
        if action['action'] != 'exports_present_pending_content_review':
            missing+=1
        result.append({**base,**action})
    return {'batch':str(folder),'all_exports_present':missing==0 and bool(result),'incomplete_jobs':missing,'next':result}

def cli(argv=None, default_config=None):
    parser=argparse.ArgumentParser(description='剪映AI原生导出：只处理本地文件，界面由Computer Use操作')
    parser.add_argument('--config',default=str(default_config) if default_config else None)
    parser.add_argument('--app-dir')
    parser.add_argument('--draft-root',action='append')
    parser.add_argument('--cache-dir')
    parser.add_argument('--presets-dir')
    parser.add_argument('--output',help='另存本次JSON结果，不改变命令的实际成片输出目录')
    commands=parser.add_subparsers(dest='command',required=True)
    doctor=commands.add_parser('doctor');doctor.add_argument('--probe-codec',action='store_true')
    listing=commands.add_parser('list');listing.add_argument('--search',default='')
    prepare=commands.add_parser('prepare');prepare.add_argument('--project',required=True);prepare.add_argument('--timeline',action='append');prepare.add_argument('--all-timelines',action='store_true');prepare.add_argument('--out-root',required=True);prepare.add_argument('--variants',nargs='+',choices=list(VARIANTS),default=list(VARIANTS));prepare.add_argument('--no-srt',action='store_true')
    for name in ('next','verify-backup','bind-copy','record-plan','record-export','complete-fast','record-blocker'):
        sub=commands.add_parser(name);sub.add_argument('--batch',required=True)
        if name not in ('next','record-blocker'):sub.add_argument('--job-id',required=True)
        if name=='bind-copy':sub.add_argument('--name',required=True)
        if name=='record-plan':sub.add_argument('--plan',required=True)
        if name=='record-export':
            sub.add_argument('--variant',choices=[*VARIANTS,'SRT'],required=True);sub.add_argument('--file',required=True);sub.add_argument('--evidence',required=True)
        if name=='record-blocker':sub.add_argument('--reason',required=True)
    args=parser.parse_args(argv)
    if args.output and Path(args.output).exists():
        raise FileExistsError('JSON输出已存在，拒绝覆盖；尚未开始执行命令')
    config=load_json(args.config,{}) if args.config else {}
    if not isinstance(config,dict):raise ValueError('配置文件不是JSON对象')
    app_dir=resolve_app_dir(args.app_dir or config.get('app_dir'))
    roots,index=roots_and_index()
    roots=args.draft_root or config.get('draft_roots') or roots
    resources=resource_dirs()
    settings={'app_dir':app_dir,'draft_roots':roots,'cache':args.cache_dir or config.get('cache') or resources['cache'],'presets':args.presets_dir or config.get('presets') or resources['presets']}
    if args.command in ('doctor','list','prepare'):
        drafts,warnings=scan(roots,index)
    if args.command=='doctor':
        sample_result={'status':'未探测；加密草稿工作流不依赖外部解密'}
        sample=next((draft for draft in drafts if draft.encrypted),None)
        if args.probe_codec and sample and app_dir:
            codec=Codec(app_dir);before=sha(sample.content)
            try:codec.read(sample.content);sample_result={'status':'可读取'}
            except Exception as exc:sample_result={'status':'不可读取','reason':str(exc)}
            finally:codec.close()
            sample_result['source_unchanged']=before==sha(sample.content)
        preset_root=Path(settings['presets'])/'Combination/Presets'
        preset_names=sorted(p.name for p in preset_root.iterdir() if p.is_dir())[:128] if preset_root.is_dir() else []
        result={'at':now(),'os':platform.platform(),'config':settings,'resources':{key:{'path':settings[key],'exists':Path(settings[key]).is_dir()} for key in ('cache','presets')},
            'editor_executable':str(Path(app_dir)/'JianyingPro.exe') if app_dir else None,'media_probe_ready':bool(app_dir and (Path(app_dir)/'ffprobe.exe').is_file() and (Path(app_dir)/'ffmpeg.exe').is_file()),
            'projects':len({draft.folder for draft in drafts}),'timelines':len(drafts),'encrypted_timelines':sum(draft.encrypted for draft in drafts),'codec':sample_result,'presets':preset_names,'warnings':warnings,
            'native_ui':'需要Computer Use单独验证：必须能看到真实界面，HomeWindow根节点不足以操作。'}
    elif args.command=='list':
        term=args.search.casefold()
        result={'drafts':[draft.dict() for draft in drafts if term in (draft.name+' '+draft.timeline_name).casefold()],'warnings':warnings}
    elif args.command=='prepare':
        selected=select_drafts(drafts,args.project,args.timeline,args.all_timelines)
        folder,session=prepare_native(selected,args.out_root,args.variants,not args.no_srt,settings,log=lambda text:None)
        result=next_steps(folder,session)
    else:
        folder,session=read_batch(args.batch)
        if not session.get('native_workflow'):
            raise ValueError('这不是AI原生导出批次，请在EXE中生成AI工作单，或用prepare命令准备')
        if args.command=='next':result=next_steps(folder,session)
        elif args.command=='record-blocker':
            event(session,'原生界面操作受阻',reason=args.reason);session['native_workflow']['blocker']={'at':now(),'reason':args.reason};save_session(folder,session)
            result={'recorded':True,'render_completed':False,'reason':args.reason}
        else:
            job=get_job(session,args.job_id)
            if args.command=='verify-backup':
                record=verify_backup(session,job);result={'verified':True,'files':len(record['files']),'backup':record['backup']}
            elif args.command=='bind-copy':result=bind_native_copy(folder,session,job,args.name)
            elif args.command=='record-plan':
                plan=load_json(args.plan)
                if not isinstance(plan,dict):raise ValueError('分类计划不是JSON对象')
                record_plan(folder,session,job,plan);result=next_steps(folder,session)
            elif args.command=='record-export':
                evidence=load_json(args.evidence)
                if not isinstance(evidence,dict):raise ValueError('导出观察记录不是JSON对象')
                result=record_native_export(folder,session,job,args.variant,args.file,app_dir,evidence)
            elif args.command=='complete-fast':
                result=complete_fast(folder,session,job,app_dir)
    if args.output:
        destination=Path(args.output).resolve()
        if destination.exists():raise FileExistsError('JSON输出已存在，拒绝覆盖')
        destination.parent.mkdir(parents=True,exist_ok=True);atomic_json(destination,result)
    if sys.stdout is not None:
        try:sys.stdout.reconfigure(encoding='utf-8')
        except (AttributeError,ValueError):pass
        print(json.dumps(result,ensure_ascii=False,indent=2))
    return result
