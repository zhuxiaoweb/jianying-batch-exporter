"""Frozen per-batch review destinations, separate from private render records.

Legacy tasks have no archive_layout field and retain their original boundaries.
Every grouped task can be validated alone, including in an isolated worker.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .draft import VARIANTS

MODE = 'series_v1'
KEYS = (*VARIANTS, 'SRT')


def _value(draft, key):
    value = draft.get(key) if isinstance(draft, dict) else getattr(draft, key)
    if not isinstance(value, str):
        raise ValueError('剧名或时间线名称无效，无法生成归档路径')
    return value


def _component(value):
    from .storage import safe_name
    # Truncation can expose a trailing dot/space that Windows would erase.
    return safe_name(value).rstrip(' .') or '未命名'


def _project_component(name, disambiguated):
    result = _component(name)
    if disambiguated:
        result += '__' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]
    return result


def _basename(name, job_id, disambiguated):
    return _component(name) + ('__' + job_id if disambiguated else '')


def _valid_id(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', job_id):
        raise ValueError('任务编号无效，无法生成私有归档路径')
    return job_id


def resolved_path(path):
    # Concurrent parent creation can leave a legal Win32 extended prefix in
    # realpath's result. Normalize drive/UNC forms before exact comparisons.
    value=str(Path(path).resolve())
    if value.startswith('\\\\?\\UNC\\'):value='\\\\'+value[8:]
    elif re.match(r'^\\\\\?\\[A-Za-z]:\\',value):value=value[4:]
    return Path(value)


def _canonical(path):
    return str(resolved_path(path)).casefold()


def _project_flags(names):
    flags = {name: False for name in set(names)}
    # A literal title can itself equal another title's suffixed safe name.
    # Promote all members of every collision until the allocation is stable.
    for _ in range(len(flags) + 1):
        groups = {}
        for name, flag in flags.items():
            groups.setdefault(_project_component(name, flag).casefold(), []).append(name)
        collisions = [group for group in groups.values() if len(group) > 1]
        if not collisions:
            return flags
        changed = False
        for group in collisions:
            for name in group:
                if not flags[name]:
                    flags[name] = True
                    changed = True
        if not changed:
            raise ValueError('剧名清理后的归档目录仍重名，请调整剧名后重新选集')
    raise ValueError('无法唯一确定剧名归档目录')


def _targets(project, basename):
    return {**{key: str(project / (key + '_' + spec['label']) / (basename + '.mp4'))
               for key, spec in VARIANTS.items()},
            'SRT': str(project / '字幕srt' / (basename + '.srt'))}


def is_grouped(job):
    layout = job.get('archive_layout')
    return isinstance(layout, dict) and layout.get('mode') == MODE


def prepare_layout(folder, draft, jobid, drafts, *, planned_ids=None):
    folder = resolved_path(folder)
    jobid = _valid_id(jobid)
    name = _value(draft, 'name')
    episode = _value(draft, 'timeline_name')
    chosen = list(drafts)
    flags = _project_flags([name, *(_value(item, 'name') for item in chosen)])
    similar = [item for item in chosen if _value(item, 'name') == name
               and _component(_value(item, 'timeline_name')).casefold() == _component(episode).casefold()]
    timeline_disambiguated = len(similar) > 1
    if planned_ids is not None:
        entries=[(item,identifier) for item,identifier in planned_ids if _value(item,'name')==name]
        episode_flags={identifier:sum(_component(_value(other,'timeline_name')).casefold()==_component(_value(item,'timeline_name')).casefold()
            for other,_ in entries)>1 for item,identifier in entries}
        for _ in range(len(entries)+1):
            allocated={}
            for item,identifier in entries:
                allocated.setdefault(_basename(_value(item,'timeline_name'),identifier,episode_flags[identifier]).casefold(),[]).append(identifier)
            collisions=[group for group in allocated.values() if len(group)>1]
            if not collisions:break
            changed=False
            for group in collisions:
                for identifier in group:
                    if not episode_flags[identifier]:episode_flags[identifier]=True;changed=True
            if not changed:raise ValueError('时间线清理后的归档文件仍重名，请调整名称后重新选集')
        timeline_disambiguated=episode_flags[jobid]
    project = folder / '交付' / _project_component(name, flags[name])
    basename = _basename(episode, jobid, timeline_disambiguated)
    return {'mode': MODE, 'project_directory': str(project), 'basename': basename,
            'project_disambiguated': flags[name], 'timeline_disambiguated': timeline_disambiguated,
            'targets': _targets(project, basename)}


def validate_layout(folder, job):
    folder = resolved_path(folder)
    layout = job.get('archive_layout')
    if not isinstance(layout, dict) or layout.get('mode') != MODE:
        raise ValueError('归档布局版本尚未验证，停止处理本批次')
    required = {'mode', 'project_directory', 'basename', 'project_disambiguated',
                'timeline_disambiguated', 'targets'}
    if set(layout) != required:
        raise ValueError('归档布局字段不完整或含未知配置')
    if type(layout['project_disambiguated']) is not bool or type(layout['timeline_disambiguated']) is not bool:
        raise ValueError('归档重名标记无效')
    jobid = _valid_id(job['id'])
    project_name = _project_component(_value(job['draft'], 'name'), layout['project_disambiguated'])
    basename = _basename(_value(job['draft'], 'timeline_name'), jobid, layout['timeline_disambiguated'])
    private_root = folder / '任务记录'
    delivery_root = folder / '交付'
    def no_link(path):
        if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
            raise ValueError('归档目录或目标文件不能是符号链接或目录联接：'+str(path))
    for root in (private_root,delivery_root):no_link(root)
    private_root=resolved_path(private_root);delivery_root=resolved_path(delivery_root)
    if (private_root.parent != folder or delivery_root.parent != folder
            or private_root == delivery_root):
        raise ValueError('归档根目录含越界链接或无效路径')
    private = private_root / jobid
    project = delivery_root / project_name
    no_link(private);no_link(project)
    private=resolved_path(private);project=resolved_path(project)
    if private.parent != private_root or project.parent != delivery_root:
        raise ValueError('归档目录超出本批次范围')
    if (not Path(job['directory']).is_absolute() or _canonical(job['directory']) != _canonical(private)
            or not isinstance(layout['project_directory'], str) or not Path(layout['project_directory']).is_absolute()
            or _canonical(layout['project_directory']) != _canonical(project)
            or layout['basename'] != basename):
        raise ValueError('归档目录或文件名与本集冻结计划不符')
    targets = layout['targets']
    if not isinstance(targets, dict) or set(targets) != set(KEYS):
        raise ValueError('归档目标必须包含四版视频和SRT位置')
    expected = _targets(project, basename)
    if any(len(path.encode('utf-16-le'))//2>245 for path in expected.values()):
        raise ValueError('输出位置或剧名、时间线名称过长，请选择较短输出目录或缩短名称后重新选集')
    for key, target in targets.items():
        if (not isinstance(target, str) or not Path(target).is_absolute()
                or _canonical(target) != _canonical(expected[key])):
            raise ValueError('归档目标路径与本集冻结计划不符：' + key)
        directory = project / (key + '_' + VARIANTS[key]['label'] if key != 'SRT' else '字幕srt')
        no_link(directory);no_link(Path(target))
        actual = resolved_path(target)
        if actual.parent != directory or resolved_path(directory).parent != project:
            raise ValueError('归档版本目录含越界链接：' + key)
    return layout


def output_path_allowed(job, key, path):
    if key not in KEYS:
        return False
    if 'archive_layout' in job and not is_grouped(job):
        return False
    try:
        resolved = resolved_path(path)
        private = resolved_path(job['directory'])
        if is_grouped(job):validate_layout(private.parent.parent,job)
        if resolved != private and resolved.is_relative_to(private):
            return True
        return bool(is_grouped(job) and key in job['archive_layout'].get('targets', {})
                    and _canonical(resolved) == _canonical(job['archive_layout']['targets'][key]))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def project_directory(job):
    return Path(job['archive_layout']['project_directory']).resolve() if is_grouped(job) else Path(job['directory']).resolve()
