"""Authored child captions on verified complete, unit-speed video instances.

Only offset mapping is supported. Trimmed, reversed, retimed or ambiguous
instances fail closed. No speech recognition or cached ASR text is used.
"""
from __future__ import annotations

from .draft import materials,text_content
from .timeline_graph import is_compound


def _time(value,field,default=0):
    result=value.get(field,default)
    if type(result) is not int or result<0:
        raise ValueError('复合字幕的时间范围无效')
    return result


def child_caption_cues(data,roles,needed_scopes):
    from .scoped_variants import scoped_key
    cues=set()
    def needed(scope):return any(s[:len(scope)]==scope for s in needed_scopes)
    def visit(document,scope,offset,depth):
        if depth>16:raise ValueError('复合字幕嵌套过深')
        lookup=materials(document)
        for ti,track in enumerate(document['tracks']):
            for si,segment in enumerate(track.get('segments',[])):
                if segment.get('visible') is False:continue
                if scope and track.get('type')=='text':
                    key=scoped_key(scope,f'{track.get("id",ti)}:{segment.get("id",si)}')
                    if roles.get(key)=='字幕':
                        interval=segment.get('target_timerange') or {}
                        start=_time(interval,'start');duration=_time(interval,'duration')
                        end=min(start+duration,_time(document,'duration'))
                        text=text_content(lookup.get(segment.get('material_id'),{}))
                        if text and end>start:cues.add((offset+start,offset+end,text))
                refs=[segment.get('material_id'),*segment.get('extra_material_refs',[])]
                children={mid:lookup[mid] for mid in refs if mid in lookup and is_compound(lookup[mid])
                          and needed(scope+(mid,))}
                if not children:continue
                all_children={mid for mid in refs if mid in lookup and is_compound(lookup[mid])}
                if len(children)!=1 or len(all_children)!=1 or track.get('type')!='video':
                    raise ValueError('复合字幕的实例引用不明确，须走原生SRT导出')
                mid,material=next(iter(children.items()));child=material.get('draft')
                if not isinstance(child,dict):raise ValueError('复合字幕缺少内嵌时间线')
                source=segment.get('source_timerange') or {};target=segment.get('target_timerange') or {}
                duration=_time(child,'duration')
                if (duration<=0 or _time(source,'start')!=0 or _time(source,'duration')!=duration
                        or _time(target,'duration')!=duration or _time(target,'start')+duration>_time(document,'duration')):
                    raise ValueError('子草稿字幕仅支持完整、等速的复合片段；实例裁切及变速映射尚未验收，请走原生SRT导出')
                if (segment.get('speed',1)!=1 or isinstance(segment.get('speed',1),bool)
                        or any(segment.get(key) for key in ('reverse','is_reverse','curve_speed','speed_curve'))):
                    raise ValueError('子草稿字幕的倒放或变速映射尚未验收，请走原生SRT导出')
                for ref in refs:
                    value=lookup.get(ref,{})
                    if value.get('type')=='speed':
                        speed=value.get('speed',1)
                        if (isinstance(speed,bool) or speed!=1 or any(v not in (None,False,0,'',[],{})
                                for k,v in value.items() if k not in ('id','type','speed'))):
                            raise ValueError('子草稿字幕的变速映射尚未验收，请走原生SRT导出')
                visit(child,scope+(mid,),offset+_time(target,'start'),depth+1)
    visit(data,(),0,0)
    end=_time(data,'duration')
    return {(max(0,start),min(finish,end),text) for start,finish,text in cues if min(finish,end)>max(0,start)}
