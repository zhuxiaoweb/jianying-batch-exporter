"""Candidate variant preparation for explicitly classified embedded timelines.

Kept separate from the released 1.4 pipeline until native comparison passes.
"""
from __future__ import annotations

import copy
import json

from .draft import VARIANTS, inspect as inspect_document, materials, text_content
from .timeline_graph import documents, is_compound


def scoped_key(scope, local_key):
    return json.dumps([*scope, local_key], ensure_ascii=False, separators=(',', ':'))


def inspect(data):
    rows = []
    for number,(scope, document) in enumerate(documents(data)):
        lookup=materials(document)
        segments={f'{track.get("id",ti)}:{segment.get("id",si)}':segment
            for ti,track in enumerate(document['tracks']) for si,segment in enumerate(track.get('segments',[]))}
        for row in inspect_document(document):
            segment=segments[row['key']]
            container=any(is_compound(lookup.get(ref,{})) for ref in [segment.get('material_id'),*segment.get('extra_material_refs',[])])
            rows.append({**row, 'key': scoped_key(scope, row['key']),
                         'local_key': row['key'], 'scope': list(scope),'material_id':segment.get('material_id'),
                         'is_container':container,
                         'suggestion':('保留原声音' if row['kind']=='audio' else '标题/其他文字') if container else row['suggestion'],
                         'track':('复合'+str(number)+'·'+(document.get('name') or '未命名')+' / ' if scope else '')+row['track']})
    if len({row['key'] for row in rows}) != len(rows):
        raise ValueError('同一时间线内轨道或片段标识重复，不能唯一分类')
    return rows


def classify_standard_tracks(data):
    from .engine import classify_standard_tracks as classify_local
    from .timeline_graph import is_compound
    roles={};rows=inspect(data)
    for scope,document in documents(data):
        # The local naming classifier only needs audio/text materials. Compound
        # definitions are classified in their own namespace by this outer loop.
        local=copy.deepcopy(document)
        local['materials']={key:[item for item in value if not isinstance(item,dict) or not is_compound(item)]
            if isinstance(value,list) else value for key,value in local.get('materials',{}).items()}
        containers={row['local_key']:row for row in rows if row['scope']==list(scope) and row['is_container']}
        for key,row in containers.items():
            roles[row['key']]='保留原声音' if row['kind']=='audio' else '标题/其他文字'
        for ti,track in enumerate(local['tracks']):
            kept=[]
            for si,segment in enumerate(track.get('segments',[])):
                key=f'{track.get("id",ti)}:{segment.get("id",si)}'
                segment.setdefault('id',si)
                if key not in containers:kept.append(segment)
            track['segments']=kept
        roles.update({scoped_key(scope,key):role for key,role in classify_local(local).items()})
    assert_classified(data,roles)
    return roles


def assert_classified(data, roles):
    for row in inspect(data):
        if row['is_container'] and roles.get(row['key']) in ('背景音乐','字幕'):
            raise ValueError('复合容器请保留，在内部片段中区分配乐和字幕，避免丢失其他声画：'+row['key'])
        allowed = ('字幕', '标题/其他文字') if row['kind'] == 'text' else (
            '人声', '环境音', '音效', '背景音乐', '保留原声音')
        if roles.get(row['key']) not in allowed:
            raise ValueError('请确认含子草稿在内的片段类别：' + row['key'] + ' / ' + row['name'])


def transform(data, roles, variant):
    assert_classified(data, roles)
    settings = VARIANTS[variant]
    result = copy.deepcopy(data)
    # Material definitions are visited once, even if used by several instances.
    for scope, document in documents(result):
        for ti, track in enumerate(document['tracks']):
            kept = []
            for si, segment in enumerate(track.get('segments', [])):
                local_key = f'{track.get("id", ti)}:{segment.get("id", si)}'
                role = roles.get(scoped_key(scope, local_key))
                if role == '背景音乐' and not settings['bgm']:
                    continue
                if role == '字幕' and not settings['subtitles']:
                    continue
                kept.append(segment)
            track['segments'] = kept
    # Child coordinate systems and their frame rates retain their authored values.
    result.setdefault('canvas_config', {}).update(width=1080, height=1920, ratio='9:16')
    result['fps'] = 60.0
    return result


def subtitle_srt(data, roles):
    """Authored root captions and complete, unit-speed child video instances."""
    assert_classified(data, roles)
    lookup, cues = materials(data), set()
    scopes={tuple(row['scope']) for row in inspect(data)
            if row['scope'] and row['kind']=='text' and roles[row['key']]=='字幕'}
    if scopes:
        from .nested_subtitles import child_caption_cues
        cues.update(child_caption_cues(data,roles,scopes))
    for ti, track in enumerate(data['tracks']):
        if track.get('type') != 'text':
            continue
        for si, segment in enumerate(track.get('segments', [])):
            local_key = f'{track.get("id", ti)}:{segment.get("id", si)}'
            if roles.get(scoped_key((), local_key)) != '字幕' or segment.get('visible') is False:
                continue
            tr = segment.get('target_timerange') or {}
            start = max(0, int(tr.get('start', 0)))
            end = int(tr.get('start', 0)) + int(tr.get('duration', 0))
            if data.get('duration'):
                end = min(end, int(data['duration']))
            text = text_content(lookup.get(segment.get('material_id'), {}))
            if text and end > start:
                cues.add((start, end, text))

    def stamp(us):
        seconds, ms = divmod(max(0, int(us)) // 1000, 1000)
        minutes, sec = divmod(seconds, 60)
        hours, minute = divmod(minutes, 60)
        return f'{hours:02}:{minute:02}:{sec:02},{ms:03}'

    return ''.join(f'{i}\n{stamp(start)} --> {stamp(max(end, start + 1000))}\n{text}\n\n'
                   for i, (start, end, text) in enumerate(sorted(cues), 1))


def exportable_caption_keys(data):
    """Describe visible, nonempty text cues before the operator chooses roles."""
    keys=[]
    for scope,document in documents(data):
        lookup=materials(document)
        for ti,track in enumerate(document['tracks']):
            if track.get('type')!='text':continue
            for si,segment in enumerate(track.get('segments',[])):
                tr=segment.get('target_timerange') or {}
                start=max(0,int(tr.get('start',0)));end=int(tr.get('start',0))+int(tr.get('duration',0))
                if document.get('duration'):end=min(end,int(document['duration']))
                if segment.get('visible') is not False and end>start and text_content(lookup.get(segment.get('material_id'),{})):
                    keys.append(scoped_key(scope,f'{track.get("id",ti)}:{segment.get("id",si)}'))
    return keys
