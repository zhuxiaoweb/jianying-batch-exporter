"""Version-bound background rendering of verified backup timelines.

Only the standalone, fingerprinted 11.6 engine is currently enabled. Neither
the local draft library nor the editor's project index is written here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .draft import VARIANTS, assert_classified, assert_simple, materials, srt, transform
from .fast import compose_bundle, validate_output_root
from .media import probe
from .storage import Cancelled, atomic_json, event, files_under, now, save_session, sha, validate_session
from .workflow import verify_backup
from .audio_diagnostics import assert_audio_safe, has_safe_audio_diagnostics

ENGINE_VERSION = '11.6.0.14552'
ENGINE_SHA256 = '8cfcbb612ccd946df974428977b1902736c1de0ae561e20c26cbd4ea86099cb1'


def meaningful_changes(source, normalized, path=''):
    """Reject lost/non-equivalent supplied values; native-added defaults are OK."""
    changes = []
    if isinstance(source, dict) and isinstance(normalized, dict):
        for key, value in source.items():
            child = path + '/' + key
            if key not in normalized:
                if value not in (None, False, 0, '', [], {}):
                    changes.append(child + ':missing')
            else:
                changes.extend(meaningful_changes(value, normalized[key], child))
    elif isinstance(source, list) and isinstance(normalized, list):
        if len(source) != len(normalized):
            changes.append(path + ':length')
        else:
            for index, (before, after) in enumerate(zip(source, normalized)):
                changes.extend(meaningful_changes(before, after, path + '/' + str(index)))
    elif isinstance(source, bool) or isinstance(normalized, bool):
        if type(source) is not type(normalized) or source != normalized:
            changes.append(path + ':boolean')
    elif isinstance(source, (int, float)) and isinstance(normalized, (int, float)):
        if isinstance(source, int) and isinstance(normalized, int):
            equal = source == normalized
        else:
            equal = math.isfinite(source) and math.isfinite(normalized) and math.isclose(source, normalized, rel_tol=1e-12, abs_tol=1e-12)
        if not equal:
            changes.append(path + ':number')
    elif source != normalized:
        changes.append(path + ':value')
    return changes


def model_changes(source, normalized):
    """Only known inactive channel descriptors are equivalent to native 'none'."""
    before = copy.deepcopy(source)
    aliases = []
    target = {item.get('id'): item for item in normalized.get('materials', {}).get('sound_channel_mappings', [])}
    for item in before.get('materials', {}).get('sound_channel_mappings', []):
        other = target.get(item.get('id'), {})
        source_mapping = item.get('audio_channel_mapping', 0)
        source_config_open = item.get('is_config_open', False)
        source_mapping_default = (type(source_mapping) is int and source_mapping == 0)
        source_config_default = source_config_open is False
        if (item.get('type') in ('', 'sound_channel_mapping') and other.get('type') == 'none'
                and source_mapping_default and source_config_default
                and other.get('audio_channel_mapping') == 0
                and other.get('is_config_open') is False):
            aliases.append({'material_id': item['id'], 'field': 'type', 'before': item['type'], 'after': 'none',
                            'audio_channel_mapping': 0, 'is_config_open': False})
            item['type'] = 'none'
    return meaningful_changes(before, normalized), aliases


def referenced_material_ids(data):
    lookup=materials(data)
    identifiers=set(lookup)
    def collect(value):
        found=set()
        if isinstance(value,dict):
            for child in value.values():found.update(collect(child))
        elif isinstance(value,list):
            for child in value:found.update(collect(child))
        elif isinstance(value,str) and value in identifiers:
            found.add(value)
        return found
    roots={key:value for key,value in data.items() if key!='materials'}
    active=collect(roots)
    pending=list(active)
    while pending:
        for identifier in collect(lookup[pending.pop()]):
            if identifier not in active:
                active.add(identifier);pending.append(identifier)
    return active


def render_projection(data, active):
    """Ignore unused library entries and empty lanes; preserve active ordering."""
    projected=copy.deepcopy(data)
    projected['tracks']=[track for track in projected.get('tracks',[]) if track.get('segments')]
    groups={}
    for key,value in projected.get('materials',{}).items():
        if isinstance(value,list) and all(isinstance(item,dict) and 'id' in item for item in value):
            selected=[item for item in value if item['id'] in active]
            unique={}
            for item in selected:
                previous=unique.get(item['id'])
                if previous is not None and (meaningful_changes(previous,item) or meaningful_changes(item,previous)):
                    raise ValueError('同一素材ID出现不一致参数，不能确定渲染内容：'+str(item['id']))
                unique[item['id']]=item
            if unique:groups[key]=[unique[mid] for mid in sorted(unique,key=str)]
        else:
            groups[key]=value
    projected['materials']=groups
    return projected


def inspect_capabilities(data, referenced=None, resolved_audio=()):
    """Block known unsupported active processing, not inert cached descriptors."""
    lookup = materials(data)
    referenced = set(referenced) if referenced is not None else referenced_material_ids(data)
    direct = {mid for track in data['tracks'] for segment in track.get('segments', [])
              for mid in [segment.get('material_id'), *segment.get('extra_material_refs', [])] if mid}
    referenced.update(direct)
    blocked, inactive = [], []
    for mid in sorted(referenced):
        material = lookup.get(mid)
        if material is None:
            blocked.append(f'引用素材不存在：{mid}')
            continue
        matting = material.get('matting') or {}
        if matting.get('flag', 0):
            blocked.append(f'启用抠像：{mid}')
        elif matting.get('path') or matting.get('custom_matting_id'):
            inactive.append({'material_id': mid, 'feature': 'matting', 'flag': 0})
        if material.get('type') == 'vocal_separation':
            if mid not in resolved_audio and (material.get('choice', 0) or material.get('removed_sounds') or material.get('final_algorithm')):
                blocked.append(f'启用人声处理：{mid}')
        for key in ('enable_speech_enhance', 'enable_ai_denoise', 'enable_voice_enhance'):
            if material.get(key):
                blocked.append(f'未验证的人声处理：{key}/{mid}')
    return {'blocked': blocked, 'inactive_cached_descriptors': inactive,
            'referenced_materials': len(referenced)}


def classify_standard_tracks(data):
    """Apply an explicitly confirmed company naming contract; reject ambiguity."""
    assert_simple(data)
    lookup = materials(data)
    roles, problems = {}, []
    patterns = [
        ('背景音乐', r'^(?:\[BGM\]|BGM(?:\s|\d|$)|长BGM|背景音乐|长配乐)'),
        ('音效', r'^(?:\[SFX\]|SFX(?:\s|\d|$)|\[音效\]|音效|短音乐)'),
        ('人声', r'^(?:\[VOICE\]|VOICE(?:\s|\d|$)|\[人声\]|人声|台词音频)'),
        ('环境音', r'^(?:\[AMBIENCE\]|AMBIENCE(?:\s|\d|$)|\[环境音\]|环境音|环境声)'),
    ]
    for ti, track in enumerate(data.get('tracks', [])):
        kind, name = track.get('type'), track.get('name', '').strip()
        if kind not in ('audio','text') or not track.get('segments'):
            continue
        track_role = next((role for role, pattern in patterns if re.search(pattern, name, re.I)), None)
        for si, segment in enumerate(track['segments']):
            key = f'{track.get("id", ti)}:{segment.get("id", si)}'
            material = lookup.get(segment.get('material_id'), {})
            if kind == 'audio':
                if not track_role:
                    problems.append(f'未按规范命名的音频轨：{name or track.get("id", ti)}')
                elif track_role == '背景音乐' and material.get('type') in ('sound','sound_effect'):
                    problems.append(f'背景音乐轨混入音效：{name}/{material.get("name",segment["id"])}')
                else:
                    roles[key] = track_role
            elif material.get('type') in ('subtitle','caption'):
                roles[key] = '字幕'
            elif re.search(r'^(?:\[TITLE\]|\[标题\]|标题|片名|角标|其他文字)', name, re.I):
                roles[key] = '标题/其他文字'
            else:
                problems.append(f'文字片段需要区分字幕与标题：{name or track.get("id", ti)}')
    if problems:
        raise ValueError('该集不能按公司规范自动分类，请逐片段确认：' + '；'.join(dict.fromkeys(problems))[:800])
    assert_classified(data, roles)
    return roles


def external_resource_snapshot(data, backup_root):
    """Track referenced external assets, including existing font/fallback inputs."""
    backup_root = Path(backup_root).resolve()
    lookup = materials(data)
    used=referenced_material_ids(data)
    paths = set()
    def visit(value, field=''):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == 'matting' and isinstance(child, dict) and not child.get('flag', 0):
                    continue
                visit(child, key)
        elif isinstance(value, list):
            for child in value:visit(child, field)
        elif isinstance(value, str) and value:
            if field == 'content' and value.lstrip().startswith(('{','[')):
                try:visit(json.loads(value))
                except ValueError:pass
            elif field.casefold() in ('path','file_path','filepath') or field.casefold().endswith(('_path','_dir','_directory')):
                candidate = Path(value)
                if candidate.is_absolute():
                    candidate = candidate.resolve()
                    if not candidate.is_relative_to(backup_root):paths.add(candidate)
    for mid in used:
        if mid in lookup:visit(lookup[mid])
    files, missing, directories = {}, [], {}
    for path in sorted(paths):
        if path.is_dir():
            members = files_under(path)
            directories[str(path)] = [member.relative_to(path).as_posix() for member in members]
        elif path.is_file():
            members = [path]
        else:
            missing.append(str(path));continue
        for member in members:
            before = member.stat()
            digest = sha(member)
            after = member.stat()
            if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
                raise ValueError('外部素材仍在改变，请稍后重试')
            files[str(member)] = {'sha256':digest,'size':after.st_size}
    return {'files':files,'missing':missing,'directories':directories}


def verify_external_resources(snapshot):
    for name, evidence in snapshot['files'].items():
        path = Path(name)
        if not path.is_file() or sha(path) != evidence['sha256']:
            raise ValueError('外部素材或字体在处理时改变，停止归档：' + name)
    for name in snapshot['missing']:
        if Path(name).exists():
            raise ValueError('字体或素材的缺失/替代状态发生变化，停止归档：' + name)
    for name, names in snapshot['directories'].items():
        path = Path(name)
        if not path.is_dir() or [member.relative_to(path).as_posix() for member in files_under(path)] != names:
            raise ValueError('外部资源目录内容改变，停止归档：' + name)


def ensure_timeline_identity(job, data):
    observed=data.get('id')
    if not isinstance(observed,str) or not observed:
        raise ValueError('实际草稿缺少时间线ID')
    expected=job['draft'].get('timeline_id')
    if expected:
        if observed!=expected:raise ValueError('实际时间线ID与所选集不一致')
    elif Path(job['backup_content']).resolve()!=(Path(job['backup'])/'draft_content.json').resolve():
        raise ValueError('单时间线任务必须读取本项目已备份的根草稿')
    return observed


def _classify_standard_job(folder, session, job, adapter, confirmed=False):
    if not confirmed:
        raise ValueError('请先确认本批次使用公司轨道命名与分类规范')
    validate_session(folder, session)
    verify_backup(session, job)
    content = Path(job['backup_content'])
    fingerprint = sha(content)
    data = adapter.read(content)
    ensure_timeline_identity(job,data)
    if sha(content) != fingerprint:raise ValueError('备份内容改变')
    roles = getattr(adapter, 'classify_tracks', classify_standard_tracks)(data)
    if any(item.get('file') for item in job['variants'].values()) or (job.get('srt') or {}).get('file'):
        raise ValueError('该集已有归档结果，不能自动更换分类；请在新批次操作')
    job.update(roles=roles, roles_content_sha256=fingerprint,
        classification_mode='scoped_v1' if getattr(adapter,'advanced',False) else 'flat_v1',
        classification_evidence={'mode':'company_track_contract','operator_confirmed':True,
                                 'content_sha256':fingerprint,'at':now()})
    job['status'] = '已按公司规范分类·待后台导出'
    event(session,'按公司轨道规范分类',job=job['id'],segments=len(roles))
    save_session(folder, session)
    return {'job':job['id'],'classified':len(roles)}


def version_tuple(text):
    if not re.fullmatch(r'\d+\.\d+\.\d+', str(text)):
        raise ValueError('草稿结构版本无法识别')
    return tuple(map(int, text.split('.')))


def relocate_document(data, source_root, backup_root, cache_root):
    """Relocate outer resource paths while preserving rich-text JSON verbatim."""
    source_root, backup_root = Path(source_root).resolve(), Path(backup_root).resolve()
    cache_root = Path(cache_root).resolve() if cache_root else None
    changed, missing = {}, set()

    def visit(value, field=''):
        if isinstance(value, dict):
            return {key: visit(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item, field) for item in value]
        if not isinstance(value, str):
            return value
        # Content strings are opaque. In particular, do not rewrite font paths
        # inside serialized caption styles: that changes observed text metrics.
        if value.lstrip().startswith(('{', '[')):
            return value
        field = field.casefold()
        if field not in ('path','file_path','filepath','folder','file') and not field.endswith(('_path','_dir','_directory')):
            return value
        updated = re.sub(r'^##_draftpath_placeholder_[^#]+_##',
                         lambda _: backup_root.as_posix(), value, count=1)
        normalized = updated.replace('\\', '/')
        prefix = source_root.as_posix()
        if normalized.casefold() == prefix.casefold() or normalized.casefold().startswith(prefix.casefold() + '/'):
            updated = backup_root.as_posix() + normalized[len(prefix):]
            normalized = updated.replace('\\', '/')
        marker = '/JianyingPro/User Data/Cache/'
        if re.match(r'^[a-zA-Z]:/', normalized) and marker.casefold() in normalized.casefold():
            offset = normalized.casefold().index(marker.casefold()) + len(marker)
            if cache_root:
                candidate = (cache_root / normalized[offset:]).resolve()
                if candidate.is_relative_to(cache_root) and candidate.exists():
                    updated = candidate.as_posix()
                else:
                    missing.add(value)
            else:
                missing.add(value)
        if updated != value:
            changed[value] = updated
        return updated

    result = visit(data)
    return result, {'rebound_paths': changed, 'unresolved_outer_cache_paths': sorted(missing),
                    'rich_text_content_preserved': True}


def timeline_srt(data, roles):
    """Use the final captions; match the native exporter by truncating milliseconds."""
    text = srt(data, roles)
    # Existing srt() intentionally rounds. Generate with the same caption
    # filtering/order, then replace its timestamps from their microsecond cues.
    from .draft import text_content
    lookup, cues, seen = materials(data), [], set()
    for ti, track in enumerate(data.get('tracks', [])):
        if track.get('type') != 'text':
            continue
        for si, segment in enumerate(track.get('segments', [])):
            key = f'{track.get("id", ti)}:{segment.get("id", si)}'
            if roles.get(key) != '字幕' or segment.get('visible') is False:
                continue
            timerange = segment.get('target_timerange', {})
            start = max(0, int(timerange.get('start', 0)))
            end = int(timerange.get('start', 0)) + int(timerange.get('duration', 0))
            if data.get('duration'):
                end = min(end, int(data['duration']))
            caption = text_content(lookup.get(segment.get('material_id'), {}))
            cue = start, end, caption
            if caption and end > start and cue not in seen:
                seen.add(cue)
                cues.append(cue)
    cues.sort(key=lambda item: (item[0], item[1], item[2]))

    def stamp(us):
        seconds, ms = divmod(max(0, int(us)) // 1000, 1000)
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        return f'{hours:02}:{minute:02}:{sec:02},{ms:03}'

    if not text:
        return ''
    return ''.join(f'{i}\n{stamp(start)} --> {stamp(max(end, start + 1000))}\n{caption}\n\n'
                   for i, (start, end, caption) in enumerate(cues, 1))


class EngineAdapter:
    def __init__(self, tool_directory, runtime_directory, cache_directory=None):
        self.tool_directory = Path(tool_directory).resolve()
        self.runtime_directory = Path(runtime_directory).resolve()
        self.cache_directory = Path(cache_directory).resolve() if cache_directory else None
        self.executable = self.tool_directory / 'jy-draftc.exe'
        self.profile = self.tool_directory / 'profile.json'
        self.last_compatibility = None
        self.execution_budget = None

    def worker_copy(self,budget):
        result=copy.copy(self);result.last_compatibility=None;result.execution_budget=budget
        return result

    def verify(self):
        profile = json.loads(self.profile.read_text('utf-8-sig'))
        if profile.get('schema') != 1 or profile.get('engine_version') != ENGINE_VERSION:
            raise ValueError('后台组件版本清单不匹配，请保持工具目录完整')
        if not re.fullmatch('[0-9a-fA-F]{64}', str(profile.get('tool_sha256', ''))) or sha(self.executable) != profile['tool_sha256'].lower():
            raise ValueError('后台组件文件校验失败')
        dll = self.runtime_directory / 'videoeditor.dll'
        if not dll.is_file() or sha(dll) != ENGINE_SHA256:
            raise ValueError('后台引擎要求精确的11.6.0.14552运行时；未加载不匹配的组件。请准备或绑定独立引擎目录。')
        helper = self.tool_directory / 'model-roundtrip.exe'
        expected = profile.get('model_roundtrip_sha256','')
        if not re.fullmatch('[0-9a-fA-F]{64}',str(expected)) or not helper.is_file() or sha(helper) != expected.lower():
            raise ValueError('模型兼容性检查组件缺失或校验失败')
        return {'engine_version': ENGINE_VERSION, 'runtime': str(self.runtime_directory),
                'dll_sha256': ENGINE_SHA256, 'tool_sha256': profile['tool_sha256'].lower()}

    def _roundtrip_model(self, data):
        profile = json.loads(self.profile.read_text('utf-8-sig'))
        helper = self.tool_directory / 'model-roundtrip.exe'
        expected = profile.get('model_roundtrip_sha256', '')
        if not re.fullmatch('[0-9a-fA-F]{64}', str(expected)) or not helper.is_file() or sha(helper) != expected.lower():
            raise ValueError('缺少已校验的模型兼容性检查组件')
        with tempfile.TemporaryDirectory(prefix='jy_model_check_') as temporary:
            base = Path(temporary)
            source, output = base/'input.json', base/'output.json'
            source.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            with self.execution_budget.native.slot(kind='helper') if self.execution_budget else nullcontext():
                process = subprocess.run([str(helper), str(self.runtime_directory), str(source), str(output)],
                    cwd=base, stdin=subprocess.DEVNULL, capture_output=True, timeout=45,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if process.returncode or not output.is_file():
                raise ValueError('原生模型兼容性检查未通过；未改动草稿或结构版本')
            return json.loads(output.read_bytes())

    def _invoke(self, arguments, directory, timeout, cancel=None):
        with self.execution_budget.native.slot(cancel,kind='render' if '-ex' in arguments else 'helper') if self.execution_budget else nullcontext():
            return self._invoke_unbounded(arguments,directory,timeout,cancel)

    def _invoke_unbounded(self, arguments, directory, timeout, cancel=None):
        self.verify()
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=False)
        command = [str(self.executable), *map(str, arguments), '--jy-dir', str(self.runtime_directory), '-q']
        record = {'started': now(), 'command': command, 'state': 'starting', 'pid': None}
        receipt = directory / 'process.json'
        atomic_json(receipt, record)
        started = time.monotonic()
        with (directory / 'engine.log').open('xb') as log:
            if cancel is not None and cancel.is_set():
                record.update(state='cancelled',elapsed_seconds=0)
                atomic_json(receipt,record);raise Cancelled('已停止，未启动新的后台进程')
            process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            record.update(state='running', pid=process.pid)
            atomic_json(receipt, record)
            try:
                while process.poll() is None:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled('已停止后台渲染；未完成的结果保留在工作目录')
                    if time.monotonic() - started > timeout:
                        raise TimeoutError('后台引擎超时；未完成的视频不会归档')
                    try:
                        process.wait(timeout=.25)
                    except subprocess.TimeoutExpired:
                        pass
                record.update(state='exited', exit_code=process.returncode)
            except BaseException as exc:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=15)
                record.update(state='cancelled' if isinstance(exc, Cancelled) else 'failed',
                              exit_code=process.returncode, error=str(exc))
                raise
            finally:
                record['elapsed_seconds'] = round(time.monotonic() - started, 3)
                atomic_json(receipt, record)
        if process.returncode:
            tail = (directory / 'engine.log').read_text('utf-8', errors='replace')[-1800:]
            raise RuntimeError(f'后台引擎未完成（退出码{process.returncode}）：{tail}')
        return record

    def read(self, path):
        self.verify()
        path = Path(path).resolve(strict=True)
        before = sha(path)
        raw = path.read_bytes()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            with tempfile.TemporaryDirectory(prefix='jy_engine_read_') as temporary:
                base = Path(temporary)
                isolated = base / 'encrypted-input.json'
                isolated.write_bytes(raw)
                output = base / 'decrypted-output.json'
                self._invoke(['-dc', isolated, '-o', output], base / 'process', 60)
                if not output.is_file():
                    raise ValueError('引擎没有返回实际解密结果')
                try:
                    data = json.loads(output.read_bytes())
                except (ValueError, UnicodeError) as exc:
                    raise ValueError('返回内容不是有效JSON，不能视为解密成功') from exc
        if sha(path) != before or hashlib.sha256(raw).hexdigest() != before:
            raise ValueError('读取时草稿发生变化，请重新备份和分类')
        if not isinstance(data, dict) or not isinstance(data.get('tracks'), list) or not isinstance(data.get('materials'), dict):
            raise ValueError('草稿缺少实际时间线或素材结构')
        version = version_tuple(data.get('new_version'))
        if version != (193, 0, 0) and (not (version_tuple('137.0.0') <= version <= version_tuple('191.0.0'))
                                      or version == version_tuple('190.0.0')):
            raise ValueError('此草稿结构尚未通过后台引擎兼容验证（' + str(data.get('new_version'))
                             + '），请使用原生流程；不会直接降低版本号')
        normalized = self._roundtrip_model(data)
        active=referenced_material_ids(data)
        differences,aliases = model_changes(render_projection(data,active), render_projection(normalized,active))
        if differences:
            raise ValueError('引擎无法原样保留这些有效字段，须用原生流程：' + ', '.join(differences[:8]))
        capabilities = inspect_capabilities(normalized)
        if capabilities['blocked']:
            raise ValueError('检测到当前后台路径不能保证保留的处理，须用原生流程：' + '；'.join(capabilities['blocked'][:6]))
        self.last_compatibility = {'declared_version': data['new_version'],
            'normalized_version': normalized.get('new_version'), 'version_rewritten': False,
            'all_active_nondefault_fields_preserved': True, 'capabilities': capabilities,
            'inactive_channel_type_aliases':aliases,
            'unused_materials_excluded_from_check':len(materials(data))-len(active),
            'mode': 'forward_schema_experimental' if version > version_tuple('189.0.0') else 'native_model_checked'}
        return data

    def render(self, content, output, work_directory, hard_encode=True, cancel=None):
        output = Path(output).resolve()
        if output.exists():
            raise FileExistsError('后台输出已存在，拒绝覆盖')
        before = sha(content)
        from .effect_masks import effect_mask_bindings, verify_mask_runtime, render_mask_receipt
        bindings = effect_mask_bindings(json.loads(Path(content).read_bytes()))
        runtime = verify_mask_runtime(self.runtime_directory) if bindings else None
        arguments = ['-ex', Path(content).resolve(), '-o', output, '--size', '1080x1920',
            '--fps', '60', '--codec', 'h264', '--bitrate', '12M', '--hard-encode', '1' if hard_encode else '0',
            '--timeout', '900', '-j', '1']
        if bindings:
            arguments.append('--effect-mask-runtime')
        record = self._invoke(arguments, work_directory, 960, cancel)
        if bindings:
            from .effect_masks import RUNTIME_TREE_SHA256
            marker = 'effect-mask-runtime: initialized tree=' + RUNTIME_TREE_SHA256
            if marker not in (Path(work_directory) / 'engine.log').read_text('utf-8', errors='replace').splitlines():
                raise ValueError('后台未返回特效蒙版初始化证明，停止归档')
        if bindings and verify_mask_runtime(self.runtime_directory) != runtime:
            raise ValueError('特效蒙版脚本资源在渲染时改变，停止归档')
        if sha(content) != before or not output.is_file():
            raise ValueError('后台输入发生变化或没有生成视频')
        record.update(input_sha256=before, output=str(output), output_sha256=sha(output))
        record['effect_mask_runtime'] = render_mask_receipt(bindings, runtime, before, self.verify()['tool_sha256'])
        return record


def _adopt_bundle(folder, session, job, report, record, persist=None):
    if not report.get('complete') or set(report.get('variants', {})) != set(VARIANTS):
        raise ValueError('后台交付报告未证明四版均完成')
    from .delivery_contract import omitted_srt
    omission=omitted_srt(job)
    if omission:
        if report.get('srt') is not None or report.get('omissions',{}).get('srt')!=omission:
            raise ValueError('交付报告没有证明已确认的字幕缺项')
    elif not report.get('srt'):raise ValueError('后台交付报告缺少字幕文件')
    grouped='archive_layout' in job
    if grouped:
        from .archive_layout import validate_layout,output_path_allowed
        validate_layout(folder,job)
    items=[*report['variants'].items(), *([('SRT',report['srt'])] if job['want_srt'] else [])]
    for key,item in items:
        path = Path(item.get('file', '')).resolve()
        allowed=output_path_allowed(job,key,path) if grouped else path.is_relative_to(Path(job['directory']).resolve())
        if not allowed or not path.is_file() or sha(path) != item.get('sha256'):
            raise ValueError('后台归档结果缺失、改变或路径越界')
    if any(not item.get('probe', {}).get('full_decode') for item in report['variants'].values()):
        raise ValueError('后台结果缺少完整解码证明')
    for key, item in report['variants'].items():
        existing = job['variants'][key]
        reviewed = bool(existing.get('reviewed') and existing.get('sha256') == item['sha256'])
        existing.update(item, source='剪映引擎后台渲染' if key in ('V2','V3') else '两母版媒体流复用')
        if reviewed:
            existing.update(reviewed=True, status='已审核')
    if job['want_srt']:
        old_srt = job.get('srt') or {}
        reviewed_srt = bool(old_srt.get('reviewed') and old_srt.get('sha256') == report['srt']['sha256'])
        job['srt'] = {**report['srt'], 'source': '最终草稿时间线字幕', 'reviewed': reviewed_srt}
        if reviewed_srt:job['srt']['status'] = '已审核'
    else:job['srt']={'file':None,'status':omission['status'],'omitted':True}
    record.update(state='complete', complete=True, reviewed=False)
    job['headless'] = record
    job['status'] = '后台四版导出完成·待声画审核'
    event(session, '后台四版导出完成', job=job['id'], run_directory=record['run_directory'])
    (persist or save_session)(folder, session)
    return record


def _process_alive(pid, executable):
    """Read process identity. This never sends input or signals to the editor."""
    if os.name != 'nt':
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        if code.value != 259:
            return False
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).resolve() == Path(executable).resolve()
        return True
    finally:
        kernel.CloseHandle(handle)


def _render_job(folder, session, job, adapter, app_directory, parallel=2, cancel=None, log=lambda value: None, hard_encode=True):
    """Render the four requested variants from a complete, classified backup."""
    folder = Path(folder).resolve()
    validate_session(folder, session)
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        if not (Path(app_directory) / name).is_file():
            raise FileNotFoundError(f'核验工具目录缺少{name}；已在渲染前停止')
    if set(job['variants']) != set(VARIANTS) or not job['want_srt']:
        raise ValueError('当前后台验证模式要求四版视频与SRT全部勾选；单项任务继续使用原生流程')
    if parallel not in (1, 2):
        raise ValueError('后台并发数只接受1或2')
    content = Path(job['backup_content'])
    fingerprint = sha(content)
    if job.get('roles_content_sha256') != fingerprint:
        raise ValueError('请先使用后台引擎分析本集并保存片段分类')
    prior = job.get('headless') or {}
    plan_fingerprint=sha_roles(job['roles'])
    if job.get('media_overrides'):
        from .advanced import digest,override_contract
        plan_fingerprint=digest({'roles':job['roles'],'overrides':override_contract(job['media_overrides'])})
    same_plan = prior.get('source_sha256') == fingerprint and prior.get('plan_sha256') == plan_fingerprint
    if same_plan and prior.get('run_directory') and not prior.get('bundle_report'):
        previous = Path(prior['run_directory']).resolve()
        if not previous.is_relative_to(folder/'后台引擎工作区'):
            raise ValueError('后台恢复目录越界')
        journal = previous/'run.json'
        if journal.is_file():
            saved = json.loads(journal.read_text('utf-8'))
            if saved.get('source_sha256') == fingerprint and saved.get('plan_sha256') == prior.get('plan_sha256') and saved.get('run_directory') == str(previous):
                prior = saved
    if prior.get('source_sha256') == fingerprint and prior.get('plan_sha256') == plan_fingerprint:
        if prior.get('bundle_report') and prior.get('bundle_report_sha256'):
            report_path = Path(prior['bundle_report']).resolve()
            if report_path.is_relative_to(Path(job['directory']).resolve()) and sha(report_path) == prior['bundle_report_sha256']:
                record = copy.deepcopy(prior)
                if not has_safe_audio_diagnostics(record):
                    run_directory = Path(record.get('run_directory', '')).resolve()
                    if not run_directory.is_relative_to(folder / '后台引擎工作区'):
                        raise ValueError('后台恢复目录越界')
                    for key in ('V2', 'V3'):
                        diagnostics = assert_audio_safe(run_directory / (key + '.mp4'), app_directory, key)
                        record.setdefault('renders', {}).setdefault(key, {})['audio_diagnostics'] = diagnostics
                return _adopt_bundle(folder, session, job, json.loads(report_path.read_text('utf-8')), record)
    if any(item.get('file') for item in job['variants'].values()) or (job.get('srt') or {}).get('file'):
        raise ValueError('本集已有归档文件，不能自动覆盖；请核对已有结果或创建新批次')
    verify_backup(session, job)
    profile = adapter.verify()
    data = adapter.read(content)
    timeline_id=ensure_timeline_identity(job,data)
    assert_simple(data)
    assert_classified(data, job['roles'])
    if job.get('media_overrides'):
        from .advanced import apply_overrides
        data=apply_overrides(data,job['media_overrides'])
    prepared, resources = relocate_document(data, job['draft']['folder'], job['backup'], adapter.cache_directory)
    lookup = materials(prepared)
    used = {s.get('material_id') for t in prepared['tracks'] for s in t.get('segments', [])}
    missing = [m.get('path') for mid, m in lookup.items() if mid in used and m.get('path') and not Path(m['path']).exists()]
    if missing or resources['unresolved_outer_cache_paths']:
        raise FileNotFoundError('所用素材或外层缓存缺失，停止后台渲染：\n' + '\n'.join((missing + resources['unresolved_outer_cache_paths'])[:12]))
    from .media_inputs import assert_readable_wave_paths,probe_media_inputs
    assert_readable_wave_paths(prepared)
    input_stream_checks=probe_media_inputs(prepared,app_directory,cancel)
    resource_snapshot = external_resource_snapshot(prepared, job['backup'])
    resume = False
    if same_plan and prior.get('run_directory'):
        previous_directory = Path(prior['run_directory']).resolve()
        if not previous_directory.is_relative_to(folder / '后台引擎工作区'):
            raise ValueError('后台恢复目录越界')
        for process_file in previous_directory.glob('process-*/process.json'):
            process_record = json.loads(process_file.read_text('utf-8'))
            if process_record.get('state') == 'running' and process_record.get('pid') and _process_alive(process_record['pid'], adapter.executable):
                raise ValueError('该集仍有后台引擎进程在执行，请等待原任务结束后恢复')
        if prior.get('renders') and prior.get('profile') == profile and prior.get('external_resources') is not None and prior.get('hard_encode',True)==hard_encode:
            resume = all((previous_directory / (key + '.json')).is_file()
                         and json.loads((previous_directory / (key + '.json')).read_text('utf-8')) == transform(prepared, job['roles'], key)
                         for key in ('V2','V3'))
    run_directory = previous_directory if resume else folder / '后台引擎工作区' / (job['id'] + '_' + uuid.uuid4().hex[:10])
    if not resume:
        run_directory.mkdir(parents=True, exist_ok=False)
    record = copy.deepcopy(prior) if resume else {'state': 'preparing', 'complete': False, 'run_directory': str(run_directory),
        'source_sha256': fingerprint, 'plan_sha256': plan_fingerprint, 'profile': profile,
        'resources': resources, 'compatibility': copy.deepcopy(adapter.last_compatibility),
        'timeline_id':timeline_id,'hard_encode':hard_encode,'started': now(), 'reviewed': False}
    record.pop('error', None)
    if resume and record.get('external_resources') and record['external_resources'] != resource_snapshot:
        raise ValueError('上次运行后外部素材发生变化，请新建批次核对')
    record['external_resources'] = resource_snapshot
    record['input_stream_checks'] = input_stream_checks
    record['resumed_existing_run'] = resume
    job['headless'] = record
    save_session(folder, session)
    started = time.monotonic()
    try:
        documents = {}
        for key in ('V2', 'V3'):
            path = run_directory / (key + '.json')
            if not resume:
                atomic_json(path, transform(prepared, job['roles'], key))
            documents[key] = path
        subtitles = timeline_srt(prepared, job['roles'])
        if not subtitles:
            raise ValueError('本集没有确认的有效字幕，不能完成SRT交付')
        srt_path = run_directory / '字幕.srt'
        if not srt_path.exists():
            srt_path.write_text(subtitles, 'utf-8-sig', newline='\n')
        elif srt_path.read_text('utf-8-sig') != subtitles:
            raise ValueError('后台工作区字幕已改变，停止恢复')
        record.update(state='rendering')
        atomic_json(run_directory / 'run.json', record)
        save_session(folder, session)
        log(f'后台渲染V2/V3，并发{parallel}；字幕富文本内容保持原样')
        render_started = time.monotonic()
        cached = {}
        for key, result in record.get('renders', {}).items():
            output = run_directory / (key + '.mp4')
            if key in documents and output.is_file() and result.get('input_sha256') == sha(documents[key]) and result.get('output_sha256') == sha(output):
                probe(output, app_directory, data['duration'], decode=False)
                cached[key] = result
        def render(key):
            if key in cached:
                log(f'{key}：复用上次已完成且哈希一致的母版')
                return key, cached[key]
            output = run_directory / (key + '.mp4')
            if output.exists():
                output.rename(run_directory / (key + '.未完成_' + uuid.uuid4().hex[:8] + '.mp4'))
            return key, adapter.render(documents[key], run_directory / (key + '.mp4'),
                run_directory / ('process-' + key + '_' + uuid.uuid4().hex[:8]), hard_encode=hard_encode,cancel=cancel)
        record['renders'] = dict(cached)
        failures = []
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = [pool.submit(render,key) for key in ('V2','V3')]
            for future in as_completed(futures):
                try:
                    key, result = future.result()
                    record['renders'][key] = result
                    atomic_json(run_directory/'run.json',record)
                    save_session(folder,session)
                except Exception as exc:
                    failures.append(exc)
        record['render_wall_seconds_this_attempt'] = time.monotonic() - render_started
        if not cached:
            record['render_wall_seconds'] = record['render_wall_seconds_this_attempt']
        record['reused_masters'] = sorted(cached)
        if failures:
            raise failures[0]
        for key in ('V2','V3'):
            diagnostics=assert_audio_safe(run_directory/(key+'.mp4'),app_directory,key)
            record['renders'][key]['audio_diagnostics']=diagnostics
        atomic_json(run_directory/'run.json',record)
        save_session(folder,session)
        verify_backup(session, job)
        verify_external_resources(resource_snapshot)
        if cancel is not None and cancel.is_set():
            raise Cancelled('已停止；两份渲染结果保留，尚未归档')
        log('两份母版已生成，正在完整解码、逐包复用并核验四版')
        for key in ('V2','V3'):
            probe(run_directory / (key + '.mp4'), app_directory, data['duration'], decode=False)
        identity = job['draft']['name'] + '__' + job['draft']['timeline_name']
        report = compose_bundle(run_directory/'V2.mp4', run_directory/'V3.mp4', srt_path,
            job['directory'], app_directory, identity, True, log)
        report_path = Path(report['directory']) / '两母版交付报告.json'
        record.update(state='delivery_ready', bundle_report=str(report_path),
                      bundle_report_sha256=sha(report_path), total_seconds=time.monotonic()-started)
        atomic_json(run_directory / 'run.json', record)
        job['headless'] = record
        save_session(folder, session)
        result = _adopt_bundle(folder, session, job, report, record)
        atomic_json(run_directory / 'run.json', result)
        return result
    except Exception as exc:
        record.update(state='failed', complete=False, error=str(exc))
        atomic_json(run_directory / 'run.json', record)
        job['headless'] = record
        job['status'] = '后台导出未完成：' + str(exc)
        save_session(folder, session)
        raise


def sha_roles(roles):
    return hashlib.sha256(json.dumps(roles, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def component_root(base):
    base = Path(base).resolve()
    for candidate in (base / 'engine', base / 'tools/headless-engine'):
        if (candidate / 'bin/profile.json').is_file():
            return candidate
    return base / 'engine'


def _refresh_batch(folder, session, job):
    """Merge current disk state without replacing GUI-held job objects."""
    disk = json.loads((Path(folder) / '任务清单.json').read_text('utf-8'))
    validate_session(folder, disk)
    incoming = next((item for item in disk['jobs'] if item['id'] == job['id']), None)
    if not incoming:
        raise ValueError('本批次已改变，所选任务不存在，请重新打开批次')
    if incoming.get('roles_content_sha256') != job.get('roles_content_sha256') or incoming.get('roles') != job.get('roles'):
        raise ValueError('另一窗口已修改该集分类，请重新打开批次核对')
    references = {item['id']: item for item in session['jobs']}
    references[job['id']] = job
    refreshed = []
    for item in disk['jobs']:
        existing = references.get(item['id'])
        if existing is not None:
            existing.clear()
            existing.update(item)
            refreshed.append(existing)
        else:
            refreshed.append(item)
    disk['jobs'] = refreshed
    session.clear()
    session.update(disk)


def render_job(folder, session, job, adapter, app_directory, parallel=2, cancel=None, log=lambda value: None, hard_encode=True, batch_context=None):
    from .inbox import InboxBusy, _state_lock
    folder = validate_output_root(folder)
    validate_session(folder, session)
    if batch_context is not None:
        if not batch_context.owns(folder,session,job):raise ValueError('后台批次锁上下文不匹配')
        if not getattr(adapter,'advanced',False):raise ValueError('批次并行仅用于已验证的直接交付路径')
        from .advanced import render_advanced_job
        return render_advanced_job(folder,session,job,adapter,app_directory,parallel,cancel,log,hard_encode,
                                   persist=batch_context.persist,budget=adapter.execution_budget)
    try:
        with _state_lock(folder / '后台引擎状态'):
            _refresh_batch(folder, session, job)
            if getattr(adapter,'advanced',False) or job.get('classification_mode')=='scoped_v1':
                from .advanced import AdvancedAdapter, render_advanced_job
                if not getattr(adapter,'advanced',False):
                    adapter=AdvancedAdapter(adapter.tool_directory,adapter.runtime_directory,adapter.cache_directory)
                return render_advanced_job(folder,session,job,adapter,app_directory,parallel,cancel,log,hard_encode)
            return _render_job(folder, session, job, adapter, app_directory, parallel, cancel, log,hard_encode)
    except InboxBusy as exc:
        raise ValueError('同一批次正在另一窗口或进程处理，未重复渲染') from exc


def classify_standard_job(folder, session, job, adapter, confirmed=False):
    from .inbox import InboxBusy, _state_lock
    folder = validate_output_root(folder)
    validate_session(folder, session)
    try:
        with _state_lock(folder / '后台引擎状态'):
            _refresh_batch(folder, session, job)
            return _classify_standard_job(folder, session, job, adapter, confirmed)
    except InboxBusy as exc:
        raise ValueError('本批次正在后台执行，请完成后再更改分类') from exc


def persist_manual_roles(folder, session, job, roles, fingerprint, classification_mode=None,direct_confirmed=None,delivery_options=None):
    from .inbox import InboxBusy, _state_lock
    folder = validate_output_root(folder)
    validate_session(folder, session)
    if classification_mode is not None and classification_mode not in ('flat_v1','scoped_v1'):
        raise ValueError('未知分类模式')
    try:
        with _state_lock(folder / '后台引擎状态'):
            _refresh_batch(folder, session, job)
            if fingerprint != sha(job['backup_content']):
                raise ValueError('分类分析后备份发生变化，请重新分析')
            if job.get('roles') != roles and (any(item.get('file') for item in job['variants'].values()) or (job.get('srt') or {}).get('file')):
                raise ValueError('该集已有归档结果，请新建批次更换分类，原成片保留')
            if delivery_options is not None:
                if classification_mode!='scoped_v1' or direct_confirmed is not True or delivery_options.get('source_sha256')!=fingerprint:
                    raise ValueError('缺项确认不属于当前备份或直接交付计划')
                want_srt=not delivery_options.get('no_subtitles_confirmed',False)
                if job['want_srt']!=want_srt and any(item.get('file') for item in [*job['variants'].values(),job.get('srt') or {}]):
                    raise ValueError('已有归档结果，不能更改字幕交付计划；请创建新批次。')
                job['delivery_options']=copy.deepcopy(delivery_options)
                if want_srt and not job['want_srt']:job['srt']=None
                job['want_srt']=want_srt
                if not want_srt:
                    job['srt']={'file':None,'status':delivery_options['subtitle_notice'],'omitted':True}
            job['roles'] = dict(roles)
            job['roles_content_sha256'] = fingerprint
            if classification_mode is not None:
                job['classification_mode']=classification_mode
            if direct_confirmed is not None:
                if classification_mode!='scoped_v1':raise ValueError('直接交付确认需要逐片段分类')
                job['easy_confirmed']=bool(direct_confirmed)
            event(session, '保存片段分类', job=job['id'], classified=len(roles))
            if delivery_options is not None:event(session,'确认本集缺项与交付范围',job=job['id'],options=delivery_options)
            save_session(folder, session)
    except InboxBusy as exc:
        raise ValueError('本批次正在后台执行，请完成后再更改分类') from exc


def cli(argv=None):
    import argparse
    import sys
    from .discovery import load_json, resolve_app_dir, resource_dirs
    from .workflow import get_job, read_batch
    parser=argparse.ArgumentParser(description='剪映独立引擎：检查、按公司规范分类、后台批量导出')
    parser.add_argument('operation',choices=['doctor','classify','run'])
    parser.add_argument('--runtime-dir')
    parser.add_argument('--tool-dir')
    parser.add_argument('--cache-dir')
    parser.add_argument('--app-dir',help='含ffmpeg.exe与ffprobe.exe的核验工具目录')
    parser.add_argument('--batch')
    parser.add_argument('--job-id',action='append')
    parser.add_argument('--parallel',type=int,choices=[1,2],default=2)
    parser.add_argument('--software',action='store_true',help='实验性软件编码，当前码率较低，需复核画质')
    parser.add_argument('--advanced-experimental',action='store_true',help='实验性复合片段与最终处理声音路径，首次需原生对照')
    parser.add_argument('--standard-tracks-confirmed',action='store_true')
    parser.add_argument('--report',help='新建结果JSON文件，拒绝覆盖既有文件')
    args=parser.parse_args(argv)
    base=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
    component=component_root(base)
    from .deployment import configuration_directory
    user=configuration_directory(base)
    settings=load_json(user/'settings.json',{})
    runtime=load_json(user/'engine_runtime.local.json',{}) or load_json(component/'runtime.local.json',{})
    result={'operation':args.operation,'started':now(),'success':False}
    report_path=Path(args.report).resolve() if args.report else None
    if report_path:
        validate_output_root(report_path.parent)
        if report_path.exists():raise FileExistsError('结果报告已存在，拒绝覆盖')
        report_path.parent.mkdir(parents=True,exist_ok=True)
    try:
        runtime_path=args.runtime_dir or settings.get('engine_runtime') or runtime.get('runtime_directory')
        if not runtime_path:raise ValueError('请指定--runtime-dir或先在界面准备独立引擎')
        adapter_type=EngineAdapter
        if args.advanced_experimental:
            from .advanced import AdvancedAdapter
            adapter_type=AdvancedAdapter
        adapter=adapter_type(args.tool_dir or component/'bin',runtime_path,
            args.cache_dir or settings.get('cache') or resource_dirs()['cache'])
        if args.operation=='doctor':
            result.update(success=True,profile=adapter.verify())
        else:
            if not args.batch:raise ValueError('该操作需要--batch')
            folder,session=read_batch(args.batch)
            jobs=[get_job(session,identifier) for identifier in dict.fromkeys(args.job_id)] if args.job_id else list(session['jobs'])
            app=resolve_app_dir(args.app_dir,base)
            rows=[]
            for job in jobs:
                try:
                    if args.operation=='classify':
                        value=classify_standard_job(folder,session,job,adapter,args.standard_tracks_confirmed)
                    else:
                        value=render_job(folder,session,job,adapter,app,args.parallel,hard_encode=not args.software)
                    rows.append({'job':job['id'],'ok':True,'result':value})
                except Exception as exc:
                    rows.append({'job':job['id'],'ok':False,'error':str(exc)})
            result.update(success=bool(rows) and all(row['ok'] for row in rows),jobs=rows,
                completed=sum(row['ok'] for row in rows),failed=sum(not row['ok'] for row in rows))
    except Exception as exc:
        result['error']=str(exc)
    if report_path:atomic_json(report_path,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['success'] else 1
