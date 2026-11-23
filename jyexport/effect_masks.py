"""Prove the fixed engine's session-local effect-mask resource binding."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .storage import sha
from .timeline_graph import documents, local_references
from .draft import materials

RUNTIME_TREE_SHA256 = '07214b002e53deef19c97b7a6c74f128b3f99212073828acb966d85b26716bd6'
RUNTIME_FILES = 63
SCHEMA = 'native_effect_mask_session_runtime_v1'


def _redundant_mask_curve(curve, masks, duration):
    fields = {'KFTypeCommonMaskRotation': 'rotation', 'KFTypeCommonMaskFeather': 'feather',
              'KFTypeCommonMaskSizeWidth': 'width', 'KFTypeCommonMaskSizeHeight': 'height'}
    field = fields.get(curve.get('property_type'))
    points = curve.get('keyframe_list', [])
    if field is None or not isinstance(points, list) or len(points) > 1:
        return False
    if set(curve) - {'id', 'material_id', 'property_type', 'keyframe_list'}:
        return False
    if not points:
        return True
    point = points[0]
    if not isinstance(point, dict) or set(point) - {
            'id', 'time_offset', 'values', 'left_control', 'right_control',
            'curveType', 'graphID', 'string_value'}:
        return False
    offset = point.get('time_offset', 0)
    if type(offset) is not int or offset not in (0, 16666):
        return False
    if type(duration) is not int or not 0 <= offset < duration:
        return False
    values = point.get('values')
    if not isinstance(values, list) or len(values) != 1:
        return False
    value = values[0]
    if type(value) not in (int, float) or not math.isfinite(value):
        return False
    if (point.get('curveType', 'Line') != 'Line' or point.get('graphID', '') != ''
            or point.get('string_value', '') != ''):
        return False
    for key in ('left_control', 'right_control'):
        control = point.get(key, {'x': 0.0, 'y': 0.0})
        if not isinstance(control, dict) or set(control) != {'x', 'y'}:
            return False
        if any(type(v) not in (int, float) or v != 0 for v in control.values()):
            return False
    bound = [mask for mask in masks if curve.get('material_id') in
             (mask.get('id'), mask.get('constant_material_id'))]
    return bool(bound) and all(value == mask['config'].get(field, 0) for mask in bound)


def effect_mask_bindings(data):
    result = []
    for scope, document in documents(data):
        lookup = materials(document)
        for mid in sorted(local_references(document)):
            material = lookup[mid]
            masks = material.get('effect_mask')
            if not masks:
                continue
            if not isinstance(masks, list):
                raise ValueError('特效蒙版结构尚未验证：' + str(mid))
            for mask in masks:
                if not isinstance(mask, dict) or mask.get('resource_type') not in ('circle', 'rectangle'):
                    raise ValueError('此特效蒙版形状尚未验证，请使用剪映原生导出：' + str(mid))
                config = mask.get('config')
                defaults = {'aspectRatio': 1.0, 'expansion': 0.0, 'roundCorner': 0.0}
                allowed = {'width', 'height', 'centerX', 'centerY', 'rotation', 'feather', 'invert', *defaults}
                if not isinstance(config, dict) or set(config) - allowed:
                    raise ValueError('此特效蒙版参数尚未验证，请使用剪映原生导出：' + str(mid))
                for key in ('width', 'height'):
                    value = config.get(key)
                    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                        raise ValueError('特效蒙版缺少有效大小：' + str(mid))
                for key, value in config.items():
                    if key == 'invert':
                        valid = type(value) is bool
                    else:
                        valid = type(value) in (int, float) and math.isfinite(value)
                    if not valid or (key == 'feather' and not 0 <= value <= 1):
                        raise ValueError('特效蒙版参数无效：' + str(mid) + '/' + key)
                    if key in defaults and value != defaults[key]:
                        raise ValueError('此特效蒙版参数尚未验证，请使用剪映原生导出：' + str(mid) + '/' + key)
            references = []
            ids = {value for mask in masks for value in (mask.get('id'), mask.get('constant_material_id')) if value}
            for track in document['tracks']:
                for segment in track.get('segments', []):
                    if mid not in (segment.get('material_id'), *segment.get('extra_material_refs', [])):
                        continue
                    for curve in segment.get('common_keyframes', []):
                        if (curve.get('material_id') in ids and curve.get('property_type') not in (
                                'KFTypeCommonMaskPositionX', 'KFTypeCommonMaskPositionY')
                                and not _redundant_mask_curve(curve, masks,
                                    (segment.get('target_timerange') or {}).get('duration'))):
                            raise ValueError('此特效蒙版关键帧尚未验证，请使用剪映原生导出：' + str(mid))
                    references.append({'track_id': track.get('id'), 'segment_id': segment.get('id'),
                        'target_timerange': segment.get('target_timerange'),
                        'mask_keyframes': [curve for curve in segment.get('common_keyframes', [])
                                           if curve.get('material_id') in ids]})
            result.append({'scope': list(scope), 'material_id': mid, 'name': material.get('name'),
                           'masks': masks, 'references': references})
    return result


def bindings_digest(bindings):
    return hashlib.sha256(json.dumps(bindings, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def verify_mask_runtime(runtime_directory):
    root = Path(runtime_directory) / 'Resources/lumi_js_resources'
    current = root
    while current != Path(runtime_directory).parent:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('特效蒙版脚本资源不能通过链接提供')
        if current == Path(runtime_directory):
            break
        current = current.parent
    if not root.is_dir():
        raise ValueError('缺少精确版本的特效蒙版脚本资源，请重新准备独立引擎')
    rows = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('特效蒙版脚本资源包含链接')
        if path.is_file():
            rows.append((path.relative_to(root).as_posix(), path.stat().st_size, sha(path)))
    encoded = ''.join(f'{name}\t{size}\t{digest}\n' for name, size, digest in sorted(rows)).encode('utf-8')
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if len(rows) != RUNTIME_FILES or fingerprint != RUNTIME_TREE_SHA256:
        raise ValueError('特效蒙版脚本资源校验失败，已停止以免导出全屏模糊')
    return {'root': str(root.resolve()), 'files': len(rows), 'tree_sha256': fingerprint}


def render_mask_receipt(bindings, runtime, input_sha256, helper_sha256):
    return {'schema': SCHEMA, 'bindings_sha256': bindings_digest(bindings),
            'bindings': bindings, 'input_sha256': input_sha256, 'helper_sha256': helper_sha256,
            'native_initialization_verified': bool(bindings), 'runtime': runtime}


def assert_cached_effect_masks_safe(job, app_directory=None):
    """Inspect sealed master inputs; never reuse the known uninitialized path."""
    record = job.get('headless') or {}
    renders = record.get('renders') or {}
    if not renders:
        return
    # New unmasked and initialized-mask receipts remain verifiable after cleanup.
    for key in ('V2', 'V3'):
        render = renders.get(key) or {}
        receipt = render.get('effect_mask_runtime') or {}
        if (receipt.get('schema') != SCHEMA or receipt.get('input_sha256') != render.get('input_sha256')
                or receipt.get('helper_sha256') != (record.get('profile') or {}).get('tool_sha256')):
            break
        bindings = receipt.get('bindings')
        if not isinstance(bindings, list) or bindings_digest(bindings) != receipt.get('bindings_sha256'):
            raise ValueError('既有特效蒙版核验记录改变，保留原文件并停止')
        if bindings and (receipt.get('native_initialization_verified') is not True
                or (receipt.get('runtime') or {}).get('tree_sha256') != RUNTIME_TREE_SHA256):
            raise ValueError('既有交付缺少特效蒙版初始化证明，请新建批次重新导出')
    else:
        return
    data = None
    for key in ('V2', 'V3'):
        path = Path(record.get('run_directory', '')) / (key + '.json')
        if path.is_file():
            expected = (renders.get(key) or {}).get('input_sha256')
            if not expected or sha(path) != expected:
                raise ValueError('既有后台渲染输入改变，不能复用交付')
            data = json.loads(path.read_bytes())
            if effect_mask_bindings(data):
                raise ValueError('既有成片使用了未初始化的特效蒙版，可能变成全屏模糊；原文件已保留，请新建批次重新导出')
    if data is not None:
        return
    path = Path(job['backup_content'])
    before = sha(path)
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        if app_directory is None:
            raise ValueError('旧渲染输入已清理，请继续任务以核对备份中的特效蒙版')
        from .codec import Codec
        data = json.loads(Codec(app_directory).decrypt(raw.decode('utf-8-sig')))
    if sha(path) != before:
        raise ValueError('核对特效蒙版时备份发生改变')
    if effect_mask_bindings(data):
        raise ValueError('既有成片缺少特效蒙版初始化证明，请新建批次重新导出；原文件已保留')
