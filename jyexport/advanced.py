"""Experimental batch integration for embedded timelines and final audio results.

All reads start from verified backups. The user's library/index is never written.
"""
from __future__ import annotations
import copy
import hashlib
import json
import time
import uuid
from contextlib import contextmanager,nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from . import __version__

from .draft import VARIANTS, materials
from .engine import (EngineAdapter, _adopt_bundle, _process_alive, external_resource_snapshot,
    inspect_capabilities, meaningful_changes, model_changes, relocate_document, render_projection,
    verify_external_resources, version_tuple, ensure_timeline_identity)
from .fast import compose_bundle
from .media import probe,decode_scheduling
from .media_inputs import required_media_snapshot, probe_media_inputs
from .native_prepare import read_native_document
from .processed_audio import bind_referenced_media_results, bind_referenced_results, final_audio_profile
from .encoded_audio import bind_referenced_aac, has_footer
from .scoped_variants import assert_classified, classify_standard_tracks, inspect, subtitle_srt, transform
from .storage import Cancelled, atomic_json, event, now, save_session, sha, validate_session
from .timeline_graph import documents, local_references
from .workflow import verify_backup
from .execution import resource_failure
from .delivery_contract import omitted_srt
from .audio_diagnostics import assert_audio_safe, has_safe_audio_diagnostics

REVISION='scoped-audio-periodic-v1'


class ModelCompatibilityError(ValueError):
    def __init__(self,scope,changes,doc,normalized):
        self.compatibility_details={'scope':list(scope),'changes':changes,
            'source_channel_mappings':doc.get('materials',{}).get('sound_channel_mappings',[]),
            'normalized_channel_mappings':normalized.get('materials',{}).get('sound_channel_mappings',[])}
        message='当前引擎不能完整保留该草稿的部分参数，已停止本集；原草稿未改动。'
        if any('/sound_channel_mappings/' in path for path in changes):
            message+=' 检测到声道映射参数不兼容；请查看本集“兼容性诊断.json”。'
        if any('/color_match_info/' in path for path in changes):
            source=[item for item in doc.get('materials',{}).get('effects',[]) if item.get('type')=='color_match']
            self.compatibility_details.update(issue='color_match_reference_fields',source_color_matches=source,
                normalized_color_matches=[item for item in normalized.get('materials',{}).get('effects',[]) if item.get('type')=='color_match'])
            names=[item.get('color_match_info',{}).get('reference_name') for item in source]
            message+=' 检测到色彩匹配参考参数不兼容'+('（'+ '、'.join(name for name in names if name)+'）' if any(names) else '')+'；效果未被移除，请查看本集“兼容性诊断.json”。'
        super().__init__(message+' 模型不能保留有效字段：'+str(scope)+' '+','.join(changes[:5]))


class ProcessingCompatibilityError(ValueError):
    def __init__(self,scope,capability,document):
        active=local_references(document)
        nodes=[]
        for mid,material in materials(document).items():
            if mid not in active or material.get('type')!='vocal_separation':continue
            refs=[{'track_id':track.get('id'),'segment_id':segment.get('id'),
                   'source_timerange':segment.get('source_timerange'),
                   'target_timerange':segment.get('target_timerange')}
                  for track in document['tracks'] for segment in track.get('segments',[])
                  if mid in segment.get('extra_material_refs',[]) or mid==segment.get('material_id')]
            nodes.append({'material_id':mid,'choice':material.get('choice'),
                'final_algorithm':material.get('final_algorithm'),'removed_sounds':material.get('removed_sounds'),
                'has_production_path':bool(material.get('production_path')),
                'time_range':material.get('time_range'),'recognized_profile':final_audio_profile(material),
                'references':refs})
        self.compatibility_details={'issue':'unsupported_active_processing','scope':list(scope),
            'declared_version':document.get('new_version'),'blocked':capability['blocked'],
            'vocal_processing':nodes,'source_draft_modified':False}
        super().__init__('未验证的处理须走原生流程：'+'；'.join(capability['blocked'][:5]))


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


class AdvancedAdapter(EngineAdapter):
    advanced=True
    inspect_tracks=staticmethod(inspect)
    classify_tracks=staticmethod(classify_standard_tracks)

    def read(self,path):
        self.verify()
        data,_=read_native_document(self,path)
        from .effect_masks import effect_mask_bindings
        effect_mask_bindings(data)
        scopes=list(documents(data))
        for scope,doc in scopes:
            version=version_tuple(doc.get('new_version'))
            if version != (193,0,0) and (not version_tuple('137.0.0')<=version<=version_tuple('191.0.0')
                                       or version==version_tuple('190.0.0')):
                location='根草稿' if not scope else '子草稿 '+str(scope)
                raise ValueError(location+'结构尚未验证（'+str(doc.get('new_version'))+'），请走原生流程')
        normalized=dict(documents(self._roundtrip_model(data)))
        evidence=[]
        for scope,doc in scopes:
            active=local_references(doc)
            if scope not in normalized:raise ValueError('模型丢失被引用的子草稿')
            changes,aliases=model_changes(render_projection(doc,active),render_projection(normalized[scope],active))
            if changes:raise ModelCompatibilityError(scope,changes,doc,normalized[scope])
            allowed=set()
            for mid in active:
                mat=materials(doc)[mid]
                if final_audio_profile(mat):
                    allowed.add(mid)
            capability=inspect_capabilities(normalized[scope],active,allowed)
            if capability['blocked']:raise ProcessingCompatibilityError(scope,capability,doc)
            evidence.append({'scope':list(scope),'capabilities':capability,'final_audio_results_pending_validation':sorted(allowed),
                             'inactive_channel_type_aliases':aliases})
        self.last_compatibility={'mode':REVISION,'declared_version':data['new_version'],
            'version_rewritten':False,'all_active_nondefault_fields_preserved':True,'scopes':evidence,
            'native_comparison_passed':False}
        return data


def override_key(scope,material_id,field='path'):
    return json.dumps([*scope,material_id,field],ensure_ascii=False,separators=(',',':'))


def override_contract(overrides):
    return {key:{field:item[field] for field in ('scope','material_id','field','original_path','replacement','sha256')}
            for key,item in overrides.items()}


def media_rows(data,job,cache_directory):
    relocated,_=relocate_document(data,job['draft']['folder'],job['backup'],cache_directory)
    resolved=dict(documents(relocated));rows=[]
    for scope,document in documents(data):
        lookup=materials(document);bound=materials(resolved[scope])
        direct={segment.get('material_id') for track in document['tracks'] if track.get('type') in ('audio','video')
                for segment in track.get('segments',[])}
        for mid in sorted(local_references(document)):
            mat=lookup[mid]
            fields=['path'] if mid in direct and mat.get('path') else []
            if mat.get('type')=='vocal_separation' and mat.get('production_path') and (mat.get('choice') or mat.get('removed_sounds') or mat.get('final_algorithm')):
                fields.append('production_path')
            for field in fields:
                current=bound[mid][field]
                override=(job.get('media_overrides') or {}).get(override_key(scope,mid,field))
                if override:current=override['replacement']
                rows.append({'scope':list(scope),'scope_name':document.get('name') or ('复合片段' if scope else '当前集'),
                    'material_id':mid,'field':field,'name':mat.get('name') or Path(current).name,
                    'original_path':mat[field],'resolved_path':current,'exists':Path(current).is_file(),
                    'relocated':bool(override)})
    return rows


def apply_overrides(data,overrides):
    result=copy.deepcopy(data)
    scopes=dict(documents(result))
    for key,item in overrides.items():
        if key!=override_key(item['scope'],item['material_id'],item['field']):raise ValueError('素材定位键不匹配')
        material=materials(scopes[tuple(item['scope'])])[item['material_id']]
        if material.get(item['field'])!=item['original_path']:raise ValueError('素材来源已经改变，请重新分析定位')
        path=Path(item['replacement']).resolve(strict=True)
        if not path.is_file() or sha(path)!=item['sha256']:raise ValueError('已定位素材改变或缺失，请重新选择')
        material[item['field']]=str(path)
    return result


def decoder_cache_issues(record):
    """Find obsolete unproved PCM in receipts, including a stale session's journal.

    A readable RIFF and a safe output peak do not prove a method-3 decoder.
    Keep committed plans and the independent complete-PCM proof paths reusable.
    """
    evidence=[record]
    if record.get('run_directory'):
        run=Path(record['run_directory'])
        for name in ('run.json','advanced-preparation.json'):
            path=run/name
            if path.is_file():
                value=json.loads(path.read_text('utf-8'))
                if not isinstance(value,dict):raise ValueError('旧声音准备记录不完整，停止复用')
                evidence.append(value)
    issues=[];seen=set()
    for receipt in evidence:
        preparation=receipt.get('preparation') or receipt
        for group in ('processing','media_results'):
            for item in preparation.get(group,{}).get('bindings',[]):
                wave=item.get('wave') or {};plan=wave.get('media_format_plan') or {}
                validation=plan.get('validation')
                structural=(plan.get('self_describing_full_xor') or
                            validation=='self_describing_full_xor_pcm_v1')
                witness=plan.get('pcm_witness') or {}
                proven=(validation=='complete_plaintext_pcm_witness_v1' and
                        witness.get('entire_pcm_equal') is True) or (
                        validation=='verified_complete_plaintext_fingerprint_v1' and
                        bool(plan.get('pcm_profile')))
                unproved=(plan.get('method')==3 and plan.get('mode')=='full_xor' and
                          plan.get('parameter_commitment_verified') is not True and not proven)
                if not structural and not unproved:continue
                key=(wave.get('source'),wave.get('source_sha256'),wave.get('decoded_sha256'))
                if key in seen:continue
                seen.add(key)
                issues.append({'group':group,'source':wave.get('source'),
                    'source_sha256':wave.get('source_sha256'),'decoded':wave.get('decoded'),
                    'decoded_sha256':wave.get('decoded_sha256'),'validation':validation,
                    'reason':'unverified_full_xor_pcm'})
    return issues


def assert_decoder_cache_safe(record):
    if decoder_cache_issues(record):
        raise ValueError('旧声音缓存缺少可靠解码证明，已停止复用旧PCM、母版和交付；原结果保留，请重新选集创建新批次。')


def prepare_resources(data,project_folder,run_directory,app_directory=None,cancel=None):
    """Keep verified decoded paths stable across process restarts and retries."""
    run=Path(run_directory).resolve()
    manifest=run/'advanced-preparation.json'
    authored=digest(data)
    if manifest.exists():
        record=json.loads(manifest.read_text('utf-8'))
        assert_decoder_cache_safe(record)
        payload=Path(record['prepared_document']).resolve()
        if record.get('revision')!=REVISION or record.get('authored_sha256')!=authored or not payload.is_relative_to(run) or sha(payload)!=record['prepared_sha256']:
            raise ValueError('声音准备记录不匹配，原有结果保留，请创建新批次')
        prepared=json.loads(payload.read_text('utf-8'))
        for group in ('processing','media_results'):
            for item in record[group]['bindings']:
                wave=item['wave'];decoded=Path(wave['decoded']).resolve()
                if not decoded.is_relative_to(run) or sha(decoded)!=wave['decoded_sha256'] or sha(wave['source'])!=wave['source_sha256']:
                    raise ValueError('处理声音的来源或可读副本已改变')
                witness=wave.get('media_format_plan',{}).get('pcm_witness')
                if witness and sha(witness['path'])!=witness['sha256']:
                    raise ValueError('处理声音的明文核对副本已改变')
        for item in record.get('encoded_media',{}).get('bindings',[]):
            audio=item['audio'];decoded=Path(audio['decoded']).resolve();witness=audio['witness']
            if (not decoded.is_relative_to(run) or sha(decoded)!=audio['decoded_sha256']
                    or sha(audio['source'])!=audio['source_sha256']
                    or sha(witness['source'])!=witness['source_sha256']):
                raise ValueError('预设声音的来源、核对副本或可读媒体已改变')
        if required_media_snapshot(prepared)!=record['required_media']:raise ValueError('声音准备后所用媒体改变')
        # Migrate a pre-2.3.1 preparation only when it still contains an unreadable
        # encrypted AAC. Retain the prior payload and publish a new attempt only
        # after every new check succeeds; do not invalidate completed deliveries.
        if not any(has_footer(path) for path in record['required_media']):
            return prepared,record
    attempt=run/('prepare_'+uuid.uuid4().hex[:8]);attempt.mkdir()
    prepared,processing=bind_referenced_results(data,project_folder,attempt/'处理节点')
    prepared,media=bind_referenced_media_results(prepared,project_folder,attempt/'处理素材')
    prepared,encoded=bind_referenced_aac(prepared,project_folder,attempt/'预设声音',app_directory,cancel)
    required=required_media_snapshot(prepared)
    payload=attempt/'prepared.json';atomic_json(payload,prepared)
    record={'revision':REVISION,'authored_sha256':authored,'prepared_document':str(payload),
        'prepared_sha256':sha(payload),'processing':processing,'media_results':media,
        'encoded_media':encoded,'required_media':required}
    atomic_json(manifest,record)
    return prepared,record


def render_advanced_job(folder,session,job,adapter,app_directory,parallel=2,cancel=None,log=lambda value:None,hard_encode=True,persist=None,budget=None):
    """Called under the same OS batch lock as the standard engine pipeline."""
    folder=Path(folder).resolve();validate_session(folder,session)
    persist=persist or save_session;attempt_started=time.monotonic();timings={};phase_waits={}
    @contextmanager
    def phase(name,gate=None):
        queued=time.monotonic();entered=False
        try:
            with gate.slot(cancel) if gate else nullcontext():
                started=time.monotonic();entered=True
                if gate is not None:phase_waits[name]=phase_waits.get(name,0.)+started-queued
                try:yield
                finally:timings[name]=timings.get(name,0.)+time.monotonic()-started
        finally:
            if gate is not None and not entered:
                phase_waits[name]=phase_waits.get(name,0.)+time.monotonic()-queued
    for filename in ('ffmpeg.exe','ffprobe.exe'):
        if not (Path(app_directory)/filename).is_file():raise FileNotFoundError('缺少核验工具：'+filename)
    if parallel not in (1,2):raise ValueError('并发数只接受1或2')
    if set(job['variants'])!=set(VARIANTS):raise ValueError('当前复合模式要求四版视频全部勾选')
    omission=omitted_srt(job)
    source=Path(job['backup_content']);fingerprint=sha(source)
    if job.get('roles_content_sha256')!=fingerprint:raise ValueError('请先分析当前备份并保存分类')
    with phase('backup_and_model',budget.prepare if budget else None):
        verify_backup(session,job)
        profile=adapter.verify();data=adapter.read(source);ensure_timeline_identity(job,data)
    assert_classified(data,job['roles'])
    # Fail unsupported nested caption timing before rendering either master.
    captions=subtitle_srt(data,job['roles'])
    if not captions and job['want_srt']:raise ValueError('未识别出可导出字幕，请返回本集确认是否按无字幕草稿继续。')
    if captions and not job['want_srt']:raise ValueError('字幕缺项确认与实际时间线不一致，请重新确认。')
    overrides=job.get('media_overrides') or {}
    adjusted=apply_overrides(data,overrides)
    relocated,paths=relocate_document(adjusted,job['draft']['folder'],job['backup'],adapter.cache_directory)
    if paths['unresolved_outer_cache_paths']:raise FileNotFoundError('所用缓存缺失，请重新定位素材')
    plan=delivery_plan(job,hard_encode)
    prior=job.get('headless') or {}
    matching=prior.get('revision')==REVISION and prior.get('source_sha256')==fingerprint and prior.get('plan_sha256')==plan and prior.get('profile')==profile
    run=Path(prior['run_directory']).resolve() if matching else folder/'后台引擎工作区'/(job['id']+'_'+uuid.uuid4().hex[:10])
    if not run.is_relative_to(folder/'后台引擎工作区'):raise ValueError('后台恢复目录越界')
    if matching and (run/'run.json').exists():
        saved=json.loads((run/'run.json').read_text('utf-8'))
        if saved.get('plan_sha256')!=plan or saved.get('source_sha256')!=fingerprint or saved.get('run_directory')!=str(run):raise ValueError('恢复记录不属于本集计划')
        prior=saved
    rejected=decoder_cache_issues(job.get('headless') or {}) if matching else []
    if rejected:
        if any(x.get('file') for x in job['variants'].values()) or (job.get('srt') or {}).get('file'):
            assert_decoder_cache_safe(job.get('headless') or {})
    if not matching and (any(x.get('file') for x in job['variants'].values()) or (job.get('srt') or {}).get('file')):
        raise ValueError('本集已有归档结果，不能覆盖；请使用新批次更改计划')
    if run.exists():
        for item in run.glob('process-*/process.json'):
            process=json.loads(item.read_text('utf-8'))
            if process.get('state')=='running' and process.get('pid') and _process_alive(process['pid'],adapter.executable):raise ValueError('本集仍有后台进程，未重复渲染')
    if rejected:
        rejected_run=run
        run=folder/'后台引擎工作区'/(job['id']+'_'+uuid.uuid4().hex[:10])
        matching=False
    run.mkdir(parents=True,exist_ok=True)
    record=copy.deepcopy(prior) if matching else {'revision':REVISION,'advanced':True,'job':job['id'],
        'run_directory':str(run),'source_sha256':fingerprint,'plan_sha256':plan,'profile':profile,
        'started':now(),'compatibility':copy.deepcopy(adapter.last_compatibility),'renders':{}}
    if rejected:
        record['decoder_cache_migration']={'previous_run_directory':str(rejected_run),
            'previous_record_sha256':digest(prior),'rejected_bindings':rejected,
            'previous_results_preserved':True,'at':now()}
    record.update(state='preparing',complete=False);record.pop('error',None)
    def save():
        record['last_attempt_program_version']=__version__
        record['phase_seconds_this_attempt']={k:round(v,3) for k,v in timings.items()}
        record['phase_wait_seconds_this_attempt']={k:round(v,3) for k,v in phase_waits.items()}
        record['attempt_seconds']=round(time.monotonic()-attempt_started,3)
        job['easy_delivery_state']={'preparing':'准备素材','rendering':'正在渲染母版',
            'composing_and_decoding':'核验并归档','failed':'导出失败，可继续','cancelled':'已停止，可继续'}.get(record['state'],job.get('easy_delivery_state','正在制作'))
        atomic_json(run/'run.json',record);job['headless']=record;persist(folder,session)
    save();started=time.monotonic()
    try:
        with phase('prepare_and_probe',budget.prepare if budget else None):
            prepared,preparation=prepare_resources(relocated,job['backup'],run,app_directory,cancel)
            record['preparation']=preparation
            record['input_stream_checks']=probe_media_inputs(prepared,app_directory,cancel)
            external=external_resource_snapshot(prepared,job['backup'])
        if matching and record.get('external_resources') is not None and external!=record['external_resources']:raise ValueError('上次导出后外部素材或字体发生变化')
        record['external_resources']=external
        if matching and record.get('bundle_report'):
            report_path=Path(record['bundle_report']).resolve()
            if not report_path.is_relative_to(Path(job['directory']).resolve()) or sha(report_path)!=record['bundle_report_sha256']:raise ValueError('既有交付报告改变')
            if not has_safe_audio_diagnostics(record):
                with phase('restore_audio_peak_diagnostics',budget.validation if budget else None):
                    for key in ('V2', 'V3'):
                        diagnostics = assert_audio_safe(run / (key + '.mp4'), app_directory, key)
                        record.setdefault('renders', {}).setdefault(key, {})['audio_diagnostics'] = diagnostics
                save()
            result=_adopt_bundle(folder,session,job,json.loads(report_path.read_text('utf-8')),record,persist=persist)
            atomic_json(run/'run.json',result);return result
        inputs={}
        for key in ('V2','V3'):
            document=transform(prepared,job['roles'],key);path=run/(key+'.json')
            if path.exists():
                if json.loads(path.read_text('utf-8'))!=document:raise ValueError('母版准备文档改变，停止恢复')
            else:atomic_json(path,document)
            inputs[key]=path
        srt=run/'字幕.srt' if job['want_srt'] else None
        if srt is not None:
            if srt.exists() and srt.read_text('utf-8-sig')!=captions:raise ValueError('字幕改变，停止恢复')
            if not srt.exists():srt.write_text(captions,encoding='utf-8-sig',newline='\n')
        cached={}
        for key,value in record.get('renders',{}).items():
            movie=run/(key+'.mp4')
            if key in inputs and movie.is_file() and sha(movie)==value.get('output_sha256') and sha(inputs[key])==value.get('input_sha256'):
                probe(movie,app_directory,data['duration']);cached[key]=value
        def render(key):
            if key in cached:
                log(key+'：复用已核验的母版');return key,cached[key]
            movie=run/(key+'.mp4')
            if movie.exists():movie.rename(run/(key+'.未完成_'+uuid.uuid4().hex[:8]+'.mp4'))
            return key,adapter.render(inputs[key],movie,run/('process-'+key+'_'+uuid.uuid4().hex[:8]),hard_encode=hard_encode,cancel=cancel)
        record.update(state='rendering',renders=dict(cached));save();render_started=time.monotonic();failures=[]
        observed_limit=budget.native.snapshot()['limit'] if budget else None
        with phase('render'):
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures=[pool.submit(render,key) for key in ('V2','V3')]
                for future in as_completed(futures):
                    try:
                        key,value=future.result();record['renders'][key]=value;save()
                    except Exception as exc:
                        failures.append(exc)
                        if budget and resource_failure(exc) and budget.reduce(observed_limit,str(exc)):
                            log('硬件资源不足，整批引擎进程上限已降为'+str(budget.native.snapshot()['limit']))
        record.update(reused_masters=sorted(cached),render_wall_seconds_this_attempt=time.monotonic()-render_started)
        if not cached:record['render_wall_seconds']=record['render_wall_seconds_this_attempt']
        if failures:raise failures[0]
        with phase('audio_peak_diagnostics',budget.validation if budget else None):
            for key in ('V2','V3'):
                diagnostics=assert_audio_safe(run/(key+'.mp4'),app_directory,key)
                record['renders'][key]['audio_diagnostics']=diagnostics
            save()
        with phase('verify_sources_after_render',budget.prepare if budget else None):
            verify_backup(session,job);verify_external_resources(external)
            if required_media_snapshot(prepared)!=preparation['required_media']:raise ValueError('渲染过程中所用媒体改变')
        if cancel is not None and cancel.is_set():raise Cancelled('已停止，母版保留')
        record['state']='composing_and_decoding';save()
        with phase('compose_decode_and_publish',budget.validation if budget else None),decode_scheduling(lambda:budget.native.snapshot()['active_render'] if budget else 0):
            report=compose_bundle(run/'V2.mp4',run/'V3.mp4',srt,job['directory'],app_directory,
                job['draft']['name']+'__'+job['draft']['timeline_name'],True,log,
                **({'srt_omission':omission} if omission else {}))
            for key in ('V2','V3'):report['variants'][key]['source']='剪映独立引擎：复合与处理声音实验母版'
            report.update(advanced=True,revision=REVISION,native_comparison_passed=False)
            if (job.get('delivery_options') or {}).get('no_audio_tracks_confirmed'):
                from .delivery_contract import NO_AUDIO_TRACKS
                report['track_notices']=[NO_AUDIO_TRACKS]
            report_path=Path(report['directory'])/'两母版交付报告.json';atomic_json(report_path,report)
            record.update(state='delivery_ready',bundle_report=str(report_path),bundle_report_sha256=sha(report_path),total_seconds=time.monotonic()-started)
            save();result=_adopt_bundle(folder,session,job,report,record,persist=persist)
        record['phase_seconds_this_attempt']={k:round(v,3) for k,v in timings.items()}
        record['phase_wait_seconds_this_attempt']={k:round(v,3) for k,v in phase_waits.items()}
        record['attempt_seconds']=round(time.monotonic()-attempt_started,3)
        persist(folder,session);atomic_json(run/'run.json',result);return result
    except Exception as exc:
        record.update(state='cancelled' if isinstance(exc,Cancelled) else 'failed',complete=False,error=str(exc));save();raise


def delivery_plan(job,hard_encode=True):
    value={'revision':REVISION,'roles':job['roles'],
           'overrides':override_contract(job.get('media_overrides') or {}),'hardware':hard_encode}
    if 'archive_layout' in job:value['archive_layout']=job['archive_layout']
    omission=omitted_srt(job)
    if omission:value['srt_omission']=omission
    return digest(value)


def save_media_override(folder,session,job,scope,material_id,field,original_path,replacement,fingerprint):
    from .engine import _refresh_batch
    from .inbox import _state_lock
    from .fast import validate_output_root
    folder=validate_output_root(folder)
    if field not in ('path','production_path'):raise ValueError('不支持定位此字段')
    with _state_lock(folder/'后台引擎状态'):
        _refresh_batch(folder,session,job)
        if sha(job['backup_content'])!=fingerprint:raise ValueError('分析后备份改变，请重新分析')
        if any(x.get('file') for x in job['variants'].values()) or (job.get('srt') or {}).get('file'):raise ValueError('本集已有结果，请在新批次重新定位素材')
        path=Path(replacement).resolve(strict=True)
        if not path.is_file():raise ValueError('请选择具体的媒体文件')
        key=override_key(scope,material_id,field)
        job.setdefault('media_overrides',{})[key]={'scope':list(scope),'material_id':material_id,'field':field,
            'original_path':original_path,'replacement':str(path),'sha256':sha(path),'at':now(),'operator_selected':True}
        event(session,'重新定位素材',job=job['id'],material=key)
        save_session(folder,session)
