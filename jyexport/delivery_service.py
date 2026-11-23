"""One continuous path: selected drafts -> reviewed plan -> final delivery folder."""
from __future__ import annotations
import json
import time
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from pathlib import Path
from . import __version__

from .advanced import AdvancedAdapter
from .draft import VARIANTS
from .engine import ensure_timeline_identity,persist_manual_roles,render_job
from .scoped_variants import assert_classified
from .storage import create_session,save_session,sha,Cancelled,event
from .workflow import read_batch,verify_backup
from .delivery_contract import NO_SUBTITLES,NO_AUDIO_TRACKS,missing_features,omitted_srt,delivery_items,completion_state
from .bgm_memory import ReferenceIndex
from .error_messages import describe_failure


def selection_roles(rows,bgm_keys,caption_keys,no_bgm_confirmed=False,no_captions_confirmed=False):
    bgm_keys,caption_keys=set(bgm_keys),set(caption_keys)
    audio={row['key'] for row in rows if row['kind']=='audio' and not row['is_container']}
    texts={row['key'] for row in rows if row['kind']=='text' and not row['is_container']}
    if not bgm_keys<=audio or not caption_keys<=texts:raise ValueError('选择不属于当前集或指向复合容器，请重新确认')
    if bgm_keys and no_bgm_confirmed:raise ValueError('已勾选长配乐时，请取消“没有独立背景音乐”')
    if not bgm_keys and not no_bgm_confirmed:raise ValueError('请勾选要移除的长配乐，或明确确认本集没有独立背景音乐')
    if not caption_keys and not no_captions_confirmed:raise ValueError('未选择最终字幕，请检查普通文字中是否有手工字幕，或确认按无字幕草稿继续。')
    return {row['key']:('字幕' if row['key'] in caption_keys else '标题/其他文字') if row['kind']=='text'
            else ('背景音乐' if row['key'] in bgm_keys else '保留原声音') for row in rows}


def prepare(drafts,output,adapter,cancel=None,log=lambda value:None,on_prepared=lambda folder,session:None,*,archive_mode='legacy'):
    adapter.verify()
    folder,session=create_session(drafts,output,list(VARIANTS),True,cancel,log,archive_mode=archive_mode)
    on_prepared(folder,session)
    analyze(folder,session,adapter,cancel,log)
    return folder,session


def analyze(folder,session,adapter,cancel=None,log=lambda value:None):
    from .inbox import _state_lock,InboxBusy
    try:
        with _state_lock(Path(folder)/'后台引擎状态'):
            return _analyze_unlocked(folder,session,adapter,cancel,log)
    except InboxBusy as exc:raise ValueError('本批次正在其他窗口处理，未重新分析或覆盖任务') from exc


def _analyze_unlocked(folder,session,adapter,cancel=None,log=lambda value:None):
    reference_index=ReferenceIndex(cancel)
    for job in session['jobs']:
        if cancel and cancel.is_set():raise Cancelled('已停止，任务与备份已保存')
        job.pop('easy_resume_verified',None)
        try:
            ready=verified_delivery(job)
            if ready is not None:
                approved=all(item.get('reviewed') for item in delivery_items(job))
                job.update(easy_delivery_folder=str(ready),easy_confirmed=True,easy_resume_verified=True,
                           easy_delivery_state=completion_state(job,approved))
                job.pop('easy_failure',None);job.pop('easy_analysis_error',None)
                save_session(folder,session)
                log('第'+job['draft']['timeline_name']+'集：已核验既有完整交付');continue
            verify_backup(session,job)
            fingerprint=sha(job['backup_content'])
            log('分析：'+job['draft']['name']+' / 第'+job['draft']['timeline_name']+'集')
            data=adapter.read(job['backup_content']);ensure_timeline_identity(job,data)
            if not data.get('duration') or not any(track.get('segments') for track in data['tracks']):
                raise ValueError('该草稿尚无可导出的时间线内容；请先在剪映添加素材并保存，再刷新列表。')
            if sha(job['backup_content'])!=fingerprint:raise ValueError('分析时备份内容改变，请重新创建任务')
            rows=adapter.inspect_tracks(data)
            from .advanced import media_rows
            from .timeline_graph import documents
            references=media_rows(data,job,getattr(adapter,'cache_directory',None))
            scopes=dict(documents(data))
            for item in references:
                document=scopes[tuple(item['scope'])]
                item['positions']=[{'track':track.get('name') or track.get('id') or '未命名',
                    'segment_id':segment.get('id'),'start':int((segment.get('target_timerange') or {}).get('start',0))}
                    for track in document['tracks'] for segment in track.get('segments',[])
                    if item['material_id'] in [segment.get('material_id'),*segment.get('extra_material_refs',[])]]
            reference_index.enrich(data,rows,references,job)
            from .scoped_variants import exportable_caption_keys
            job['easy_analysis']={'content_sha256':fingerprint,'rows':rows,'exportable_caption_keys':exportable_caption_keys(data),
                'media_references':references,
                'caption_keys':[row['key'] for row in rows if row['kind']=='text' and row['suggestion']=='字幕' and not row['is_container']]}
            if job.get('roles_content_sha256')==fingerprint and job.get('classification_mode')=='scoped_v1':
                try:
                    assert_classified(data,job['roles'])
                    missing=missing_features(job['easy_analysis'],[key for key,value in job['roles'].items() if value=='字幕'])
                    options=job.get('delivery_options') or {}
                    job['easy_confirmed']=(not missing['no_subtitles'] or bool(options.get('no_subtitles_confirmed') and not job['want_srt'])) and (not missing['no_audio_tracks'] or bool(options.get('no_audio_tracks_confirmed')))
                except ValueError:job['easy_confirmed']=False
            else:job['easy_confirmed']=False
            job.pop('easy_analysis_error',None);job.pop('easy_delivery_state',None);job.pop('easy_failure',None)
        except Cancelled:raise
        except Exception as exc:
            if hasattr(exc,'compatibility_details'):
                from .storage import atomic_json
                diagnostic=Path(job['directory'])/'兼容性诊断.json'
                atomic_json(diagnostic,{'program_version':__version__,'draft':job['draft']['name'],
                    'episode':job['draft']['timeline_name'],'source_sha256':sha(job['backup_content']),**exc.compatibility_details})
                job['compatibility_diagnostics']=str(diagnostic)
            info=describe_failure(exc,job,'analysis');job['easy_failure']=info
            job['easy_analysis_error']=info['summary'];job['easy_confirmed']=False
            job.pop('easy_delivery_folder',None)
            job['easy_delivery_state']='分析未完成：'+info['summary']
            log('第'+job['draft']['timeline_name']+'集分析失败，继续其他集：'+str(exc))
        save_session(folder,session)
    return session


def confirm(folder,session,job,bgm_keys,caption_keys,no_bgm_confirmed=False,no_captions_confirmed=False,no_audio_confirmed=False):
    analysis=job['easy_analysis']
    if sha(job['backup_content'])!=analysis['content_sha256']:raise ValueError('备份已改变，停止保存分类')
    missing=missing_features(analysis,caption_keys)
    if missing['no_subtitles'] and not no_captions_confirmed:raise ValueError('未识别出可导出字幕，请确认是否按无字幕草稿继续。')
    if missing['no_audio_tracks'] and not no_audio_confirmed:raise ValueError('未识别出独立音频轨；仍会保留视频原声，请确认是否继续。')
    if no_captions_confirmed and not missing['no_subtitles']:raise ValueError('已选择有效字幕，请取消无字幕确认。')
    roles=selection_roles(analysis['rows'],bgm_keys,caption_keys,no_bgm_confirmed,no_captions_confirmed)
    options={'source_sha256':analysis['content_sha256'],'no_subtitles_confirmed':missing['no_subtitles'],
             'no_audio_tracks_confirmed':missing['no_audio_tracks'],'subtitle_notice':NO_SUBTITLES if missing['no_subtitles'] else ''}
    persist_manual_roles(folder,session,job,roles,analysis['content_sha256'],'scoped_v1',direct_confirmed=True,delivery_options=options)
    return roles


def final_folder(job):
    omitted_srt(job)
    expected=delivery_items(job)
    folders=set()
    for item in expected:
        path=Path(item.get('file') or '')
        if not path.is_file() or not item.get('sha256') or sha(path)!=item['sha256']:
            raise ValueError('交付文件缺失或改变，不能显示完成')
        folders.add(path.resolve().parent)
    from .archive_layout import is_grouped,validate_layout
    if is_grouped(job):
        private=Path(job['directory']).resolve();validate_layout(private.parent.parent,job)
        if not (job.get('headless') or {}).get('grouped_published'):raise ValueError('本集已核验成片尚未完成按剧归档，可继续本批次')
        entries=[(key,job['variants'][key]) for key in VARIANTS]
        if job['want_srt']:entries.append(('SRT',job['srt']))
        for key,item in entries:
            if Path(item['file']).resolve()!=Path(job['archive_layout']['targets'][key]).resolve():
                raise ValueError('按剧交付文件不属于本集精确目标')
        return Path(job['archive_layout']['project_directory']).resolve()
    if len(folders)!=1:raise ValueError('本集交付文件不在同一个结果目录')
    target=folders.pop()
    if not target.is_relative_to(Path(job['directory']).resolve()):raise ValueError('交付目录超出本集任务范围')
    return target


def verified_delivery(job, app_directory=None):
    """Reuse a sealed delivery without loading the editor engine or old media."""
    from .advanced import REVISION,delivery_plan,assert_decoder_cache_safe
    from .media import read_srt
    record=job.get('headless') or {}
    if not record.get('complete') or record.get('state')!='complete':return None
    assert_decoder_cache_safe(record)
    source=sha(job['backup_content'])
    if source!=job.get('roles_content_sha256') or source!=record.get('source_sha256'):return None
    from .effect_masks import assert_cached_effect_masks_safe
    assert_cached_effect_masks_safe(job, app_directory)
    plan=delivery_plan(job)
    if record.get('revision')!=REVISION or record.get('plan_sha256')!=plan:return None
    report_path=Path(record.get('bundle_report','')).resolve()
    if not report_path.is_relative_to(Path(job['directory']).resolve()) or sha(report_path)!=record.get('bundle_report_sha256'):
        raise ValueError('既有交付报告改变，保留原文件并停止该集')
    report=json.loads(report_path.read_text('utf-8'))
    if not report.get('complete') or set(report.get('variants',{}))!=set(VARIANTS):raise ValueError('既有四版交付证明不完整')
    for key in VARIANTS:
        item=job['variants'][key];saved=report['variants'][key]
        if item.get('sha256')!=saved.get('sha256') or item.get('file')!=saved.get('file') or not saved.get('probe',{}).get('full_decode'):
            raise ValueError('既有成片记录改变或缺少完整解码证明')
        info=saved['probe']
        if (info.get('width'),info.get('height'),str(info.get('fps')),info.get('container'))!=(1080,1920,'60','MP4'):
            raise ValueError('既有成片记录缺少要求的1080×1920/60fps/MP4规格证明')
    if app_directory is not None and (Path(app_directory).resolve() / 'ffmpeg.exe').is_file():
        from .audio_diagnostics import assert_audio_safe
        for key in ('V2', 'V3'):
            diagnostics = assert_audio_safe(report['variants'][key]['file'], app_directory, key)
            record.setdefault('renders', {}).setdefault(key, {})['audio_diagnostics'] = diagnostics
        job['headless'] = record
    if job['want_srt']:
        if (job.get('srt') or {}).get('sha256')!=(report.get('srt') or {}).get('sha256'):raise ValueError('既有字幕记录改变')
    elif report.get('srt') is not None or report.get('omissions',{}).get('srt')!=omitted_srt(job):raise ValueError('既有字幕缺项证明改变')
    from .series_archive import publish_grouped
    publish_grouped(job)
    folder=final_folder(job)
    if job['want_srt']:read_srt(job['srt']['file'],round(report['variants']['V2']['probe']['duration']*1000000))
    return folder


def run(folder,session,adapter,app_directory,cancel=None,log=lambda value:None,on_job=lambda job:None,
        max_jobs=4,native_limit=4,validation_limit=None):
    from .execution import BatchContext,ExecutionBudget,resource_failure,concurrency
    from .resources import RenderAdmission,machine_render_lock
    from .inbox import InboxBusy,_state_lock
    from .fast import validate_output_root
    concurrency(max_jobs,'同时处理集数')
    checks=validation_limit if validation_limit is not None else (2 if native_limit>=4 and max_jobs>=2 else 1)
    budget=ExecutionBudget(native_limit,cancel,RenderAdmission(log=log),checks)
    folder=validate_output_root(folder)
    if not session['jobs']:raise ValueError('没有需要交付的集')
    if len({job['id'] for job in session['jobs']})!=len(session['jobs']):raise ValueError('批次包含重复的集任务')
    for job in session['jobs']:
        if set(job['variants'])!=set(VARIANTS):raise ValueError('直接交付入口要求四版视频；请创建新的完整交付任务')
        omitted_srt(job)
        if not job.get('easy_confirmed') and not job.get('easy_analysis_error'):raise ValueError('尚未确认配乐与字幕：'+job['draft']['timeline_name'])
    started=time.monotonic();rows=[];peak_jobs=0
    def worker(context,job_id):
        local,job=context.worker(job_id)
        worker_adapter=adapter.worker_copy(budget)
        try:
            if job.get('easy_analysis_error'):raise ValueError('分析未完成：'+job['easy_analysis_error'])
            log('正在制作交付包：'+job['draft']['name']+' / 第'+job['draft']['timeline_name']+'集')
            previous=job.get('headless') or {}
            check_gate=budget.validation if previous.get('complete') and previous.get('state')=='complete' else budget.prepare
            with check_gate.slot(cancel):directory=verified_delivery(job, app_directory)
            reused=directory is not None
            if reused:
                log('第'+job['draft']['timeline_name']+'集：复用已核验的完整交付')
                event(local,'复用已核验完整交付',job=job_id)
            else:
                job['easy_delivery_state']='正在准备';context.persist(folder,local)
                single_retry_used=False
                for attempt in range(5):
                    observed_limit=budget.native.snapshot()['limit']
                    try:
                        render_job(folder,local,job,worker_adapter,app_directory,min(2,observed_limit),cancel,log,batch_context=context)
                        break
                    except Cancelled:raise
                    except Exception as exc:
                        if attempt==4 or not resource_failure(exc):raise
                        if observed_limit==1:
                            if single_retry_used:raise
                            single_retry_used=True
                        budget.reduce(observed_limit,str(exc))
                        limit=budget.native.snapshot()['limit']
                        log('硬件资源不足，整批渲染上限为'+str(limit)+'；保留合格母版，重试第'+job['draft']['timeline_name']+'集')
                        event(local,'资源不足降低渲染上限并重试',job=job_id,limit=limit,error=str(exc))
                from .series_archive import publish_grouped
                publish_grouped(job)
                directory=final_folder(job)
            job['easy_delivery_folder']=str(directory)
            approved=all(item.get('reviewed') for item in delivery_items(job))
            job['easy_delivery_state']=completion_state(job,approved)
            job.pop('easy_failure',None)
            result={'job':job_id,'ok':True,'directory':str(directory),'reused_delivery':reused}
        except Cancelled:
            job['easy_delivery_state']='已停止，可继续'
            context.persist(folder,local)
            return {'job':job_id,'cancelled':True}
        except Exception as exc:
            job.pop('easy_delivery_folder',None)
            info=job.get('easy_failure') if job.get('easy_analysis_error') else None
            info=info or describe_failure(exc,job,'delivery');job['easy_failure']=info
            job['easy_delivery_state']='需要处理：'+info['summary']
            result={'job':job_id,'ok':False,'error':str(exc)}
        context.persist(folder,local)
        return result
    try:
        with _state_lock(folder/'后台引擎状态'),machine_render_lock():
            context=BatchContext(folder,session,on_job)
            try:
                pending=iter(job['id'] for job in session['jobs'])
                with ThreadPoolExecutor(max_workers=max_jobs) as pool:
                    futures={}
                    def submit():
                        if cancel is not None and cancel.is_set():return False
                        job_id=next(pending,None)
                        if job_id is None:return False
                        futures[pool.submit(worker,context,job_id)]=job_id
                        return True
                    for _ in range(max_jobs):submit()
                    peak_jobs=max(peak_jobs,len(futures))
                    while futures:
                        done,_=wait(futures,return_when=FIRST_COMPLETED)
                        for future in done:
                            futures.pop(future)
                            value=future.result()
                            if not value.get('cancelled'):rows.append(value)
                        while len(futures)<max_jobs and submit():pass
                        peak_jobs=max(peak_jobs,len(futures))
            finally:context.close(budget.snapshot())
    except InboxBusy as exc:raise ValueError('同一批次正在其他窗口处理，未重复导出') from exc
    order={job['id']:i for i,job in enumerate(session['jobs'])};rows.sort(key=lambda row:order[row['job']])
    return {'completed':sum(row['ok'] for row in rows),'failed':sum(not row['ok'] for row in rows),
            'pending':len(session['jobs'])-len(rows),'jobs':rows,'batch':str(folder),'program_version':__version__,
            'max_jobs':max_jobs,'peak_jobs':peak_jobs,'resources':budget.snapshot(),
            'elapsed_seconds':round(time.monotonic()-started,3)}


def cli(argv=None):
    import argparse,sys
    from .deployment import configuration_directory
    from .discovery import load_json,resource_dirs,resolve_app_dir
    from .engine import component_root
    from .storage import atomic_json
    parser=argparse.ArgumentParser(description='直接制作最终交付包，自动完成两母版、SRT、四版合成和核验')
    parser.add_argument('--batch',required=True);parser.add_argument('--runtime-dir');parser.add_argument('--app-dir');parser.add_argument('--report',required=True)
    parser.add_argument('--tool-dir',help='后台组件目录；通常由便携程序自动定位')
    parser.add_argument('--jobs',type=int,choices=range(1,9));parser.add_argument('--native-limit',type=int,choices=range(1,9))
    parser.add_argument('--validation-limit',type=int,choices=(1,2),help='完整核验上限；默认随并发档位自动选择1或2')
    args=parser.parse_args(argv)
    report=Path(args.report).resolve()
    if report.exists():raise FileExistsError('结果报告已存在')
    base=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
    state=configuration_directory(base);settings=load_json(state/'settings.json',{})
    runtime=load_json(state/'engine_runtime.local.json',{})
    adapter=AdvancedAdapter(args.tool_dir or component_root(base)/'bin',args.runtime_dir or settings.get('engine_runtime') or runtime.get('runtime_directory',''),
                            settings.get('cache') or resource_dirs()['cache'])
    folder,session=read_batch(args.batch)
    from .execution import performance_limits
    jobs_limit,render_limit=performance_limits(settings)
    result=run(folder,session,adapter,resolve_app_dir(args.app_dir or settings.get('app_dir'),base),
               max_jobs=args.jobs or jobs_limit,native_limit=args.native_limit or render_limit,validation_limit=args.validation_limit)
    result['success']=bool(result['jobs']) and not result['failed'] and not result['pending']
    report.parent.mkdir(parents=True,exist_ok=True);atomic_json(report,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['success'] else 1
