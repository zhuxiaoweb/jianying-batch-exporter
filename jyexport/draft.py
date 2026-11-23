"""Conservative transformations: explicitly chosen segments only."""
from __future__ import annotations
import copy
import json
import re

ROLES = ('待确认', '人声', '环境音', '音效', '背景音乐', '字幕', '标题/其他文字', '保留原声音')
VARIANTS = {
    'V1': {'label': '无背景音乐_无字幕', 'bgm': False, 'subtitles': False},
    'V2': {'label': '无背景音乐_有字幕', 'bgm': False, 'subtitles': True},
    'V3': {'label': '有背景音乐_无字幕', 'bgm': True, 'subtitles': False},
    'V4': {'label': '有背景音乐_有字幕', 'bgm': True, 'subtitles': True},
}

def materials(data):
    return {x['id']: x for group in data.get('materials', {}).values() if isinstance(group, list) for x in group if isinstance(x, dict) and 'id' in x}

def text_content(material):
    content = material.get('content', '')
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                content = parsed.get('text', '')
        except ValueError:
            pass
    elif isinstance(content, dict):
        content = content.get('text', '')
    return str(content).replace('\\n', '\n').replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '').strip()

def inspect(data):
    lookup, rows = materials(data), []
    for track_index, track in enumerate(data.get('tracks', [])):
        for index, seg in enumerate(track.get('segments', [])):
            mat = lookup.get(seg.get('material_id'), {})
            kind = track.get('type')
            if kind not in ('audio', 'text'):
                continue
            key = f'{track.get("id", track_index)}:{seg.get("id", index)}'
            name = mat.get('name') or mat.get('material_name') or mat.get('path', '').replace('\\', '/').rsplit('/', 1)[-1]
            suggestion = '待确认'
            if kind == 'text':
                suggestion = '字幕' if mat.get('type') in ('subtitle', 'caption') else '标题/其他文字'
                name = text_content(mat)[:100]
            elif mat.get('type') in ('sound', 'sound_effect'):
                suggestion = '音效'
            elif re.search(r'bgm|背景音乐|配乐', track.get('name', ''), re.I):
                suggestion = '背景音乐'
            rows.append({'key': key, 'track': track.get('name') or f'{kind}轨道{track_index + 1}', 'kind': kind,
                'name': name, 'path': mat.get('path', ''), 'start': int(seg.get('target_timerange', {}).get('start', 0)),
                'duration': int(seg.get('target_timerange', {}).get('duration', 0)), 'suggestion': suggestion})
    return rows

def assert_classified(data, roles, include_audio=True):
    for row in inspect(data):
        if row['kind'] == 'audio' and not include_audio:
            continue
        role = roles.get(row['key'], '')
        allowed = ('字幕', '标题/其他文字') if row['kind'] == 'text' else ('人声', '环境音', '音效', '背景音乐', '保留原声音')
        if role not in allowed:
            raise ValueError(f'请确认每个音频片段和文字片段的类别：{row["track"]} / {row["name"]}')

def assert_simple(data):
    for mat in materials(data).values():
        if mat.get('type') in ('draft', 'compound', 'combination') or mat.get('draft') or mat.get('draft_id'):
            raise ValueError('存在复合片段/子草稿，无法保证内部音乐和字幕被全部处理。请在剪映副本内展开后操作。')

def transform(data, roles, variant):
    assert_classified(data, roles)
    assert_simple(data)
    result = copy.deepcopy(data)
    settings = VARIANTS[variant]
    for track_index, track in enumerate(result.get('tracks', [])):
        kept = []
        for index, seg in enumerate(track.get('segments', [])):
            key = f'{track.get("id", track_index)}:{seg.get("id", index)}'
            role = roles.get(key)
            if (role == '背景音乐' and not settings['bgm']) or (role == '字幕' and not settings['subtitles']):
                continue
            kept.append(seg)
        track['segments'] = kept
    result.setdefault('canvas_config', {}).update({'width': 1080, 'height': 1920, 'ratio': '9:16'})
    result['fps'] = 60.0
    return result

def timestamp(us):
    millis = max(0, (int(us) + 500) // 1000)
    seconds, ms = divmod(millis, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f'{hours:02}:{minute:02}:{sec:02},{ms:03}'

def srt(data, roles):
    lookup, cues, seen = materials(data), [], set()
    assert_simple(data)
    for ti, track in enumerate(data.get('tracks', [])):
        if track.get('type') != 'text':
            continue
        for si, seg in enumerate(track.get('segments', [])):
            key = f'{track.get("id", ti)}:{seg.get("id", si)}'
            if roles.get(key) != '字幕' or seg.get('visible') is False:
                continue
            tr = seg.get('target_timerange', {})
            start = max(0, int(tr.get('start', 0)))
            end = int(tr.get('start', 0)) + int(tr.get('duration', 0))
            if data.get('duration'):
                end = min(end, int(data['duration']))
            text = text_content(lookup.get(seg.get('material_id'), {}))
            cue = (start, end, text)
            if text and end > start and cue not in seen:
                cues.append(cue)
                seen.add(cue)
    cues.sort(key=lambda x: (x[0], x[1], x[2]))
    return ''.join(f'{i}\n{timestamp(start)} --> {timestamp(max(end, start + 1000))}\n{text}\n\n' for i, (start, end, text) in enumerate(cues, 1))
