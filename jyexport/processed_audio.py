"""Candidate reader for user-owned audioAlg results explicitly referenced by drafts.

This is not enabled by the 1.4 GUI. It does not recognize speech, separate
voices, rebuild a timeline from caches, or touch the source media.
"""
from __future__ import annotations

import hashlib
import copy
import math
import os
import re
import uuid
import wave
from pathlib import Path

from .fast import validate_output_root
from .storage import sha
from .media_crypto import wave_plan, transform_block
from .float_wave import inspect_float_wave


def final_audio_profile(material):
    """Recognize only final-result modes exercised with the pinned engine.

    Recognition allows validation to continue. The exact referenced WAVE,
    integrity commitment, duration and every used sample still need checking.
    """
    if material.get('type') != 'vocal_separation':
        return None
    if (not isinstance(material.get('production_path'), str) or not material['production_path']
            or int((material.get('time_range') or {}).get('duration', 0)) <= 0):
        return None
    if (not material.get('choice') and material.get('removed_sounds') == ['human']
            and material.get('final_algorithm') == 'instrument_separate'):
        return 'instrument_separate'
    # This is the authored final background-removal result, not a request to
    # remove additional sound during export. Keep its mode and source timing;
    # only its explicit, fully verified PCM path is rebound. Both zero and
    # nonzero video-segment results were compared with independent PCM oracles.
    if (('choice' not in material or
            (type(material['choice']) is int and material['choice'] == 0))
            and material.get('removed_sounds') == ['background']
            and material.get('final_algorithm') == 'instrument_separate'):
        return 'instrument_separate_background'
    if (type(material.get('choice')) is int and material['choice'] in (1,2)
            and not material.get('removed_sounds')
            and material.get('final_algorithm') == 'vocal_separate'):
        return 'vocal_separate_choice'+str(material['choice'])
    return None


def decode_referenced_wave(source, destination, expected_duration_us=0):
    source=Path(source).resolve(strict=True)
    destination=Path(destination).resolve()
    if source==destination or destination.exists():raise FileExistsError('处理结果输出已存在或指向源文件')
    validate_output_root(destination.parent)
    destination.parent.mkdir(parents=True,exist_ok=True)
    before=source.stat()
    try:plan=wave_plan(source)
    except ValueError as exc:raise ValueError(source.name+'：'+str(exc)) from exc
    temporary=destination.with_name(destination.name+'.partial-'+uuid.uuid4().hex)
    digest=hashlib.sha256()
    with source.open('rb') as incoming,temporary.open('xb') as outgoing:
        offset=0
        for block in iter(lambda:incoming.read(1024*1024),b''):
            digest.update(block)
            body=block[:max(0,min(len(block),plan['body_size']-offset))]
            outgoing.write(transform_block(body,offset,plan));offset+=len(block)
        outgoing.flush();os.fsync(outgoing.fileno())
    after=source.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or sha(source)!=digest.hexdigest():
        raise ValueError('处理结果读取时改变，未发布解码文件')
    planned_source_sha = plan.get('source_sha256')
    if planned_source_sha and planned_source_sha != digest.hexdigest():
        raise ValueError('处理结果与已验证的来源不一致，未发布解码文件')
    witness=plan.get('pcm_witness')
    if witness and sha(witness['path'])!=witness['sha256']:
        raise ValueError('处理声音的明文核对副本已改变，未发布解码文件')
    sample_format='integer_pcm'
    try:
        with wave.open(str(temporary),'rb') as decoded_wave:
            rate=decoded_wave.getframerate();frames=decoded_wave.getnframes()
            channels=decoded_wave.getnchannels();width=decoded_wave.getsampwidth()
            if decoded_wave.getcomptype()!='NONE' or rate<=0 or frames<=0 or channels not in (1,2) or width not in (2,3,4):
                raise ValueError('处理结果不是当前已支持的PCM WAVE')
            pcm_digest=hashlib.sha256();pcm_bytes=0
            while block:=decoded_wave.readframes(65536):
                pcm_digest.update(block);pcm_bytes+=len(block)
            if pcm_bytes!=frames*channels*width:raise ValueError('PCM数据不完整')
            pcm_hash=pcm_digest.hexdigest()
    except (wave.Error,EOFError):
        info=inspect_float_wave(temporary)
        rate=info['rate'];frames=info['frames'];channels=info['channels'];width=info['sample_width']
        sample_format=info['sample_format'];pcm_hash=info['pcm_sha256']
    duration=frames/rate
    if witness and (pcm_hash!=witness['pcm_sha256'] or frames!=witness['frames']
            or rate!=witness['rate'] or channels!=witness['channels'] or width!=witness['sample_width']
            or sha(witness['path'])!=witness['sha256']):
        raise ValueError('处理声音与完整明文PCM核对副本不一致，未发布解码文件')
    from .verified_pcm import verify_profile_output
    verify_profile_output(plan,digest.hexdigest(),temporary,rate,frames,channels,width,pcm_hash)
    if expected_duration_us and abs(duration-expected_duration_us/1e6)>max(.05,expected_duration_us/1e6*.001):
        raise ValueError('处理结果时长与草稿引用范围不符')
    os.rename(temporary,destination)
    decoded_digest=sha(destination)
    return {'source':str(source),'source_sha256':digest.hexdigest(),'decoded':str(destination),
            'decoded_sha256':decoded_digest,'pcm_sha256':pcm_hash,'sample_format':sample_format,
            'pcm_data_complete':True,
            'plain_source_identity_verified':plan['mode']=='plain' and decoded_digest==digest.hexdigest(),
            'rate':rate,'frames':frames,'channels':channels,'sample_width':width,
            'duration_seconds':duration,'source_unchanged':True,'render_equivalence_verified':False,
            'media_format_plan':plan}


def map_processed_source_range(segment,processing,wave_info):
    timerange=processing.get('time_range') or {}
    original=segment.get('source_timerange') or {}
    start=int(original.get('start',0));duration=int(original.get('duration',0))
    processed_start=int(timerange.get('start',0));processed_duration=int(timerange.get('duration',0))
    relative=start-processed_start
    overrun=relative+duration-processed_duration
    # A real choice=2 clip uses 8.334 ms beyond its algorithm metadata, and
    # its explicitly referenced final WAVE contains those samples. Permit at
    # most one 60-fps frame of metadata tail rounding for that tested mode;
    # the actual sample bound below remains strict. Never extend or pad audio.
    tolerance=16667 if final_audio_profile(processing)=='vocal_separate_choice2' else 2
    if duration<=0 or processed_duration<=0 or relative<0 or overrun>tolerance:
        raise ValueError('片段超出已处理的源范围，不能套用缓存')
    rate=int(wave_info['rate']);frames=int(wave_info['frames'])
    first=relative*rate//1000000
    end=math.ceil((relative+duration)*rate/1000000)
    shortfall=max(0,end-frames)
    plan=wave_info.get('media_format_plan') or {}
    # Plain audioAlg WAVE results can also end on the final complete 1024-frame
    # buffer. Require the complete original bytes, not just plausible metadata:
    # wave_plan checked exact RIFF size, decode_referenced_wave read every PCM
    # byte, and source/destination SHA256 must match. Reject a missing whole
    # buffer, unaligned/truncated data, unknown conversion or metadata overruns.
    plain_pcm_tail=(plan.get('mode')=='plain' and
        wave_info.get('sample_format')=='integer_pcm' and
        wave_info.get('pcm_data_complete') is True and
        wave_info.get('plain_source_identity_verified') is True and
        wave_info.get('source_unchanged') is True and
        bool(wave_info.get('source_sha256')) and
        wave_info['source_sha256']==wave_info.get('decoded_sha256') and
        frames==(processed_duration*rate//1000000//1024)*1024)
    witnessed_choice2_tail=(final_audio_profile(processing)=='vocal_separate_choice2' and
        plan.get('validation')=='complete_plaintext_pcm_witness_v1' and
        plan.get('mode')=='full_xor' and
        plan.get('parameter_commitment_verified') is False and
        wave_info.get('pcm_data_complete') is True and
        wave_info.get('source_unchanged') is True and
        isinstance(plan.get('pcm_witness'),dict) and
        plan['pcm_witness'].get('entire_pcm_equal') is True and
        plan['pcm_witness'].get('pcm_sha256')==wave_info.get('pcm_sha256') and
        plan['pcm_witness'].get('frames')==frames and frames%1024==0)
    # Some authored audioAlg results end on a complete 1024-sample buffer
    # before their declared video-frame duration. Preserve their real EOF and
    # the authored clip; do not invent samples or shorten the timeline. This
    # narrow allowance needs a validated BDVE commitment and an aligned EOF.
    final_buffer_tail=(0<shortfall<=1024 and frames%1024==0 and first<frames and
        (plan.get('parameter_commitment_verified') is True or plain_pcm_tail or witnessed_choice2_tail) and
        0<=processed_duration*rate/1000000-frames<=1024 and overrun<=2)
    if first<0 or (end>frames and not final_buffer_tail):
        raise ValueError('片段超出实际PCM数据：'+str(segment.get('id',''))+
                         f'（需要{end}采样，实际{frames}采样）')
    return {'original_source_start_us':start,'processed_source_start_us':processed_start,
            'cached_source_start_us':relative,'source_duration_us':duration,
            'declared_tail_overrun_us':max(0,overrun),
            'final_pcm_covers_authored_range':not shortfall,
            'authored_eof_tail_samples':shortfall,
            'eof_tail_validation':('plain_pcm_complete_buffer' if final_buffer_tail and plain_pcm_tail
                else 'witnessed_choice2_complete_buffer' if final_buffer_tail and witnessed_choice2_tail
                else 'committed_complete_buffer' if final_buffer_tail else None),
            'first_sample_floor':first,'end_sample_ceil':end,'target_timerange_unchanged':True,
            'render_equivalence_verified':False}


def bind_referenced_results(data,project_folder,output_directory):
    """Candidate relocation of explicit processed results, with parameters kept.

    Does not remove a processing control or change source/target ranges, gains,
    fades, keyframes, material identities, or the original document.
    """
    from .timeline_graph import documents, local_references
    project_folder=Path(project_folder).resolve(strict=True)
    output_directory=validate_output_root(output_directory)
    if output_directory.exists():raise FileExistsError('处理结果工作目录已存在，避免混用旧结果')
    output_directory.mkdir(parents=True)
    result=copy.deepcopy(data)
    converted={};bindings=[]
    for scope,document in documents(result):
        definitions={}
        for group in document.get('materials',{}).values():
            if isinstance(group,list):
                for candidate in group:
                    if isinstance(candidate,dict) and candidate.get('id'):
                        definitions.setdefault(candidate['id'],[]).append(candidate)
        active=local_references(document)
        for group in document.get('materials',{}).values():
            if not isinstance(group,list):continue
            for material in group:
                if not isinstance(material,dict) or material.get('id') not in active or material.get('type')!='vocal_separation':continue
                enabled=material.get('choice') or material.get('removed_sounds') or material.get('final_algorithm')
                if not enabled:continue
                value=material.get('production_path')
                if not isinstance(value,str) or not value:
                    raise ValueError('启用的声音处理没有最终结果引用')
                if not final_audio_profile(material):
                    raise ValueError('该声音处理模式尚未验证，不能替换最终结果：'+str(material.get('id')))
                rewritten=re.sub(r'^##_draftpath_placeholder_[^#]+_##',lambda _:project_folder.as_posix(),value,count=1)
                path=Path(rewritten)
                if not path.is_absolute():raise ValueError('处理结果不是已解析的绝对路径')
                path=path.resolve(strict=True)
                declared=material.get('time_range') or {}
                duration=int(declared.get('duration',0))
                if duration<=0:raise ValueError('处理结果缺少明确的源时间范围')
                key=(str(path),duration)
                if key not in converted:
                    target=output_directory/(str(uuid.uuid4())+'.wav')
                    converted[key]=decode_referenced_wave(path,target,duration)
                evidence=converted[key]
                mappings=[]
                for track in document['tracks']:
                    for segment in track.get('segments',[]):
                        if material['id'] in segment.get('extra_material_refs',[]):
                            mappings.append({'segment_id':segment['id'],**map_processed_source_range(segment,material,evidence)})
                bindings.append({'scope':list(scope),'material_id':material['id'],'original_production_path':value,
                                 'bound_production_path':evidence['decoded'],'wave':evidence,'mappings':mappings})
                for candidate in definitions.get(material['id'],[material]):
                    candidate['production_path']=evidence['decoded']
    return result,{'bindings':bindings,'decoded_files':len(converted),'source_document_unchanged':True,
                   'processing_parameters_preserved':True,'native_render_comparison_passed':False}


def bind_referenced_media_results(data, project_folder, output_directory):
    """Decode final audioAlg assets explicitly used as audio material paths.

    These are authored timeline media, separate from vocal_separation nodes.
    Only the referenced project-owned files are read; no cache search occurs.
    """
    from .draft import materials
    from .timeline_graph import documents
    project_folder = Path(project_folder).resolve(strict=True)
    resource_root = (project_folder/'Resources'/'audioAlg').resolve()
    output_directory = validate_output_root(output_directory)
    if output_directory.exists():
        raise FileExistsError('处理素材工作目录已存在，避免混用旧结果')
    output_directory.mkdir(parents=True)
    result = copy.deepcopy(data)
    converted, bindings = {}, []
    for scope, document in documents(result):
        definitions = {}
        for group in document.get('materials', {}).values():
            if isinstance(group, list):
                for candidate in group:
                    if isinstance(candidate, dict) and candidate.get('id'):
                        definitions.setdefault(candidate['id'], []).append(candidate)
        lookup = materials(document)
        segments = {}
        for track in document['tracks']:
            if track.get('type') == 'audio':
                for segment in track.get('segments', []):
                    segments.setdefault(segment.get('material_id'), []).append(segment)
        for mid, instances in segments.items():
            material = lookup.get(mid, {})
            value = material.get('path')
            if not isinstance(value, str) or not value:
                continue
            rewritten = re.sub(r'^##_draftpath_placeholder_[^#]+_##',
                lambda _: project_folder.as_posix(), value, count=1)
            path = Path(rewritten)
            if not path.is_absolute():
                continue
            owned=path.resolve().is_relative_to(resource_root)
            if not owned:
                # Copied/preset clips may retain an explicit final WAV reference
                # outside this project's audioAlg directory. Do not search for
                # alternatives: only a typed audio material's exact file is used.
                if material.get('type') not in ('extract_music','music','sound','sound_effect') or path.suffix.casefold()!='.wav':
                    continue
                with path.open('rb') as stream:header=stream.read(12)
                if header[:4]==b'RIFF' and header[8:]==b'WAVE':
                    continue
            path = path.resolve(strict=True)
            duration = int(material.get('duration', 0))
            if duration <= 0:
                raise ValueError('处理素材缺少明确时长：' + str(mid))
            key = (str(path), duration)
            if key not in converted:
                converted[key] = decode_referenced_wave(path,
                    output_directory/(str(uuid.uuid4())+'.wav'), duration)
            evidence = converted[key]
            mappings = [{'segment_id': segment['id'], **map_processed_source_range(segment,
                {'time_range': {'start': 0, 'duration': duration}}, evidence)} for segment in instances]
            for candidate in definitions.get(mid, [material]):
                candidate['path'] = evidence['decoded']
            bindings.append({'scope': list(scope), 'material_id': mid, 'field': 'path',
                'original_path': value, 'bound_path': evidence['decoded'], 'wave': evidence,
                'mappings': mappings})
    return result, {'bindings': bindings, 'decoded_files': len(converted),
        'source_document_unchanged': True, 'source_ranges_and_processing_preserved': True,
        'native_render_comparison_passed': False}
