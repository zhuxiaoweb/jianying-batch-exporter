"""Scheduling/state fault tests; files here are receipts, not rendered media."""
import copy
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from jyexport.delivery_service import prepare,confirm,run,analyze,final_folder
from jyexport.discovery import Draft
from jyexport.engine import EngineAdapter,persist_manual_roles
from jyexport.execution import BatchContext,ExecutionBudget
from jyexport.inbox import _state_lock
from jyexport.storage import atomic_json,sha,event,Cancelled
from jyexport.advanced import REVISION,digest
from tests.test_advanced import document,Reader


class BatchExecutionTests(unittest.TestCase):
    def setUp(self):
        # These are synthetic state/scheduler tests, not hardware admission tests.
        # Keep unrelated applications' RAM/VRAM use from changing their result.
        for name, value in [('memory_status', {'physical_available_mib': 8192,
                'physical_total_mib': 16384, 'commit_available_mib': 16384}),
                ('gpu_status', [])]:
            mocked = patch('jyexport.resources.' + name, return_value=value)
            mocked.start(); self.addCleanup(mocked.stop)
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        source=self.root/'source';source.mkdir();content=source/'draft_content.json'
        content.write_text(json.dumps(document(source)),'utf-8')
        self.adapter=Reader(self.root,self.root)
        drafts=[Draft('case'+str(i),'case',str(source),str(content),'episode',str(i),False,3000000) for i in (1,2)]
        self.folder,self.session=prepare(drafts,self.root/'output',self.adapter)
        self.refs=list(self.session['jobs'])
        for job in self.session['jobs']:
            music=[r['key'] for r in job['easy_analysis']['rows'] if r['name']=='music']
            confirm(self.folder,self.session,job,music,job['easy_analysis']['caption_keys'])
        self.lock=threading.Lock()
    def tearDown(self):self.temp.cleanup()

    def publish(self,job):
        target=Path(job['directory'])/'state-only-delivery';target.mkdir(exist_ok=True)
        report={'complete':True,'variants':{}}
        for key,value in job['variants'].items():
            path=target/(key+'.mp4');path.write_bytes((job['id']+key+'state-only').encode())
            value.update(file=str(path),sha256=sha(path),probe={'width':1080,'height':1920,'fps':'60',
                'container':'MP4','duration':3.,'full_decode':True})
            report['variants'][key]=copy.deepcopy(value)
        path=target/'字幕.srt';path.write_text('1\n00:00:00,000 --> 00:00:01,000\nFinal\n','utf-8')
        job['srt']={'file':str(path),'sha256':sha(path)};report['srt']=copy.deepcopy(job['srt'])
        rp=target/'report.json';atomic_json(rp,report)
        job['headless']={'state':'complete','complete':True,'revision':REVISION,
            'source_sha256':sha(job['backup_content']),
            'plan_sha256':digest({'revision':REVISION,'roles':job['roles'],'overrides':{},'hardware':True}),
            'bundle_report':str(rp),'bundle_report_sha256':sha(rp)}
        report['directory']=str(target)
        return report

    def fake_render(self,folder,local,job,adapter,*args,**kwargs):self.publish(job)

    def test_two_workers_interleave_without_losing_jobs_events_or_ui_references(self):
        barrier=threading.Barrier(2);locals_seen=set()
        def render(folder,local,job,adapter,*args,**kwargs):
            with self.lock:locals_seen.add(id(local))
            self.assertIsNot(local,self.session)
            with adapter.execution_budget.native.slot():
                barrier.wait(timeout=3)
                for index in range(3):
                    job['worker_marker']=job['id']+str(index)
                    event(local,'thread checkpoint',job=job['id'],checkpoint=index)
                    kwargs['batch_context'].persist(folder,local);time.sleep(.005)
                self.publish(job)
        with patch('jyexport.delivery_service.render_job',side_effect=render):
            result=run(self.folder,self.session,self.adapter,self.root,max_jobs=2,native_limit=2)
        self.assertEqual(result['completed'],2);self.assertEqual(len(locals_seen),2)
        self.assertEqual(result['resources']['native']['peak'],2)
        saved=json.loads((self.folder/'任务清单.json').read_text('utf-8'))
        self.assertEqual(sum(e['action']=='thread checkpoint' for e in saved['events']),6)
        for index,job in enumerate(self.session['jobs']):
            self.assertIs(job,self.refs[index]);self.assertEqual(job['worker_marker'],job['id']+'2')
            self.assertTrue(final_folder(job).is_dir())
            for key,item in job['variants'].items():self.assertIn(job['id'],Path(item['file']).read_text())

    def test_bad_episode_does_not_stop_the_other_episode(self):
        bad=self.session['jobs'][0]['id']
        def render(folder,local,job,*args,**kwargs):
            if job['id']==bad:raise ValueError('missing test media')
            self.publish(job)
        with patch('jyexport.delivery_service.render_job',side_effect=render):result=run(self.folder,self.session,self.adapter,self.root)
        self.assertEqual((result['completed'],result['failed']),(1,1))
        self.assertEqual(result['jobs'][0]['job'],bad);self.assertFalse(result['jobs'][0]['ok'])
        self.assertTrue(result['jobs'][1]['ok'])

    def test_resource_failure_retries_once_and_reduces_future_native_concurrency(self):
        calls=[]
        def render(folder,local,job,adapter,app,parallel,*args,**kwargs):
            with adapter.execution_budget.native.slot():
                with self.lock:
                    first=not calls;calls.append((job['id'],parallel))
                if first:raise RuntimeError('NVENC out of memory')
                time.sleep(.01);self.publish(job)
        with patch('jyexport.delivery_service.render_job',side_effect=render):result=run(self.folder,self.session,self.adapter,self.root,native_limit=2)
        self.assertEqual(result['completed'],2);self.assertEqual(len(calls),3)
        self.assertTrue(result['resources']['downgraded']);self.assertEqual(result['resources']['native']['limit'],1)
        self.assertIn(1,[parallel for _,parallel in calls])

    def test_nonresource_errors_are_not_retried(self):
        with patch('jyexport.delivery_service.render_job',side_effect=ValueError('unsupported audio format')) as render:
            result=run(self.folder,self.session,self.adapter,self.root)
        self.assertEqual(render.call_count,2);self.assertEqual(result['failed'],2)
        self.assertFalse(result['resources']['downgraded'])

    def test_high_limit_resource_failures_reduce_in_steps_and_preserve_other_jobs(self):
        calls=[]
        def render(folder,local,job,adapter,*args,**kwargs):
            limit=adapter.execution_budget.native.snapshot()['limit'];calls.append(limit)
            if limit>2:raise RuntimeError('NV_ENC_ERR_OUT_OF_MEMORY' if limit==8 else '后台引擎未完成（退出码3221225495）')
            self.publish(job)
        with patch('jyexport.delivery_service.render_job',side_effect=render):
            result=run(self.folder,self.session,self.adapter,self.root,max_jobs=1,native_limit=8)
        self.assertEqual(result['completed'],2);self.assertEqual(calls,[8,4,2,2])
        self.assertEqual([(r['before'],r['after']) for r in result['resources']['reductions']],[(8,4),(4,2)])
        self.assertEqual(result['resources']['native']['limit'],2)

    def test_stop_does_not_submit_another_episode(self):
        cancel=threading.Event();calls=[]
        def render(folder,local,job,*args,**kwargs):calls.append(job['id']);cancel.set();raise Cancelled('test stop')
        with patch('jyexport.delivery_service.render_job',side_effect=render):
            result=run(self.folder,self.session,self.adapter,self.root,cancel,max_jobs=1)
        self.assertEqual(len(calls),1);self.assertEqual((result['completed'],result['failed'],result['pending']),(0,0,2))

    def test_completed_deliveries_resume_without_engine_or_media_read(self):
        with patch('jyexport.delivery_service.render_job',side_effect=self.fake_render):run(self.folder,self.session,self.adapter,self.root)
        before={j['id']:{k:v['sha256'] for k,v in j['variants'].items()} for j in self.session['jobs']}
        with patch.object(self.adapter,'verify',side_effect=AssertionError('engine should not load')),patch.object(self.adapter,'read',side_effect=AssertionError('media should not load')),patch('jyexport.delivery_service.render_job') as render:
            analyze(self.folder,self.session,self.adapter)
            result=run(self.folder,self.session,self.adapter,self.root)
        render.assert_not_called();self.assertTrue(all(j['reused_delivery'] for j in result['jobs']))
        self.assertEqual(before,{j['id']:{k:v['sha256'] for k,v in j['variants'].items()} for j in self.session['jobs']})

    def test_changed_completed_output_stops_only_that_episode_without_rerender(self):
        with patch('jyexport.delivery_service.render_job',side_effect=self.fake_render):run(self.folder,self.session,self.adapter,self.root)
        Path(self.session['jobs'][0]['variants']['V1']['file']).write_bytes(b'changed')
        with patch('jyexport.delivery_service.render_job') as render:result=run(self.folder,self.session,self.adapter,self.root)
        self.assertEqual((result['completed'],result['failed']),(1,1));render.assert_not_called()
        self.assertNotIn('easy_delivery_folder',self.session['jobs'][0])

    def test_running_batch_rejects_analysis_and_classification_from_another_window(self):
        def render(folder,local,job,*args,**kwargs):
            with self.assertRaisesRegex(ValueError,'其他窗口'):analyze(folder,self.session,self.adapter)
            with self.assertRaisesRegex(ValueError,'另一窗口|正在'):persist_manual_roles(folder,self.session,self.refs[0],self.refs[0]['roles'],self.refs[0]['roles_content_sha256'])
            self.publish(job)
        with patch('jyexport.delivery_service.render_job',side_effect=render):result=run(self.folder,self.session,self.adapter,self.root,max_jobs=1)
        self.assertEqual(result['completed'],2)

    def test_writer_rejects_plan_changes_and_foreign_run_without_overwriting(self):
        with _state_lock(self.folder/'后台引擎状态'):
            context=BatchContext(self.folder,self.session);local,job=context.worker(self.refs[0]['id'])
            before=sha(self.folder/'任务清单.json');job['roles']['unapproved']='背景音乐'
            with self.assertRaisesRegex(ValueError,'不得改变'):context.persist(self.folder,local)
            self.assertEqual(before,sha(self.folder/'任务清单.json'))
            local,job=context.worker(self.refs[0]['id'])
            disk=json.loads((self.folder/'任务清单.json').read_text('utf-8'));disk['active_execution']['id']='foreign';atomic_json(self.folder/'任务清单.json',disk)
            before=sha(self.folder/'任务清单.json')
            with self.assertRaisesRegex(ValueError,'其他程序'):context.persist(self.folder,local)
            context.close();self.assertEqual(before,sha(self.folder/'任务清单.json'))

    def test_worker_copies_only_its_own_job_and_backup_without_old_event_history(self):
        with _state_lock(self.folder/'后台引擎状态'):
            context=BatchContext(self.folder,self.session);local,job=context.worker(self.refs[0]['id'])
            self.assertEqual([j['id'] for j in local['jobs']],[job['id']]);self.assertEqual(local['events'],[])
            self.assertEqual([b['backup'] for b in local['backups']],[job['backup']])
            job['worker_marker']='independent';self.assertNotIn('worker_marker',self.refs[0])
            event(local,'small worker event',job=job['id']);context.persist(self.folder,local)
            self.assertEqual(self.refs[0]['worker_marker'],'independent');self.assertNotIn('worker_marker',self.refs[1])
            self.assertEqual(sum(e['action']=='small worker event' for e in self.session['events']),1)
            context.close()

    def test_csv_in_use_does_not_abort_video_delivery_and_notice_clears_later(self):
        with patch('jyexport.storage.export_csv',side_effect=PermissionError('test spreadsheet lock')):
            with patch('jyexport.delivery_service.render_job',side_effect=self.fake_render):
                result=run(self.folder,self.session,self.adapter,self.root)
        self.assertEqual(result['completed'],2);self.assertIn('csv_notice',self.session)
        with patch('jyexport.delivery_service.render_job') as render:again=run(self.folder,self.session,self.adapter,self.root)
        render.assert_not_called();self.assertEqual(again['completed'],2);self.assertNotIn('csv_notice',self.session)

    def test_resource_retry_preserves_completed_master_through_real_batch_writer(self):
        from tests.test_processed_audio import encoded_wave,wave_bytes
        source=self.root/'source';(source/'Resources/audioAlg').mkdir(parents=True)
        (source/'Resources/audioAlg/voice.wav').write_bytes(encoded_wave(wave_bytes(1.)))
        for filename in ('music.mp3','effect.mp3'):(source/filename).write_bytes(b'state-only media')
        # Recreate the complete backup now that state-only media exists.
        drafts=[Draft('fault','fault',str(source),str(source/'draft_content.json'),'episode','1',False,3000000)]
        folder,session=prepare(drafts,self.root/'fault-output',self.adapter)
        job=session['jobs'][0];music=[r['key'] for r in job['easy_analysis']['rows'] if r['name']=='music']
        confirm(folder,session,job,music,job['easy_analysis']['caption_keys'])
        for name in ('ffmpeg.exe','ffprobe.exe'):(self.root/name).write_bytes(b'state-only validator')
        calls=[]
        def render(content,output,*args,**kwargs):
            key=Path(content).stem;calls.append(key)
            if key=='V3' and calls.count('V3')==1:raise RuntimeError('NVENC out of memory')
            Path(output).write_bytes((key+' state-only master').encode())
            return {'input_sha256':sha(content),'output_sha256':sha(output),'exit_code':0}
        def compose(v2,v3,srt,output,*args,**kwargs):
            receipt=copy.deepcopy(job);receipt['directory']=str(output)
            return self.publish(receipt)
        with patch.object(self.adapter,'render',side_effect=render),patch('jyexport.advanced.probe',return_value={}),patch('jyexport.advanced.probe_media_inputs',return_value={}),patch('jyexport.advanced.assert_audio_safe',return_value={}),patch('jyexport.advanced.compose_bundle',side_effect=compose):
            result=run(folder,session,self.adapter,self.root)
        self.assertEqual(result['completed'],1);self.assertEqual(calls.count('V2'),1);self.assertEqual(calls.count('V3'),2)
        self.assertEqual(job['headless']['reused_masters'],['V2']);self.assertTrue(result['resources']['downgraded'])

    def test_analysis_failure_is_isolated_and_not_sent_to_renderer(self):
        read=self.adapter.read;calls=0
        def one_bad(path):
            nonlocal calls
            calls+=1
            if calls==1:raise ValueError('unsupported child')
            return read(path)
        with patch.object(self.adapter,'read',side_effect=one_bad):analyze(self.folder,self.session,self.adapter)
        self.assertFalse(self.refs[0]['easy_confirmed']);self.assertTrue(self.refs[1]['easy_confirmed'])
        with patch('jyexport.delivery_service.render_job',side_effect=self.fake_render) as render:
            result=run(self.folder,self.session,self.adapter,self.root)
        self.assertEqual(render.call_count,1);self.assertEqual((result['completed'],result['failed']),(1,1))


class NativeGateTests(unittest.TestCase):
    def test_all_native_invoke_calls_obey_one_shared_budget(self):
        budget=ExecutionBudget(2);adapter=EngineAdapter('.', '.').worker_copy(budget)
        lock=threading.Lock();active=0;peak=0
        def invoke(*args,**kwargs):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            try:time.sleep(.02);return {'ok':True}
            finally:
                with lock:active-=1
        with patch.object(adapter,'_invoke_unbounded',side_effect=invoke):
            with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(lambda n:adapter._invoke(['-dc'],'unused',1),range(8)))
        self.assertEqual(peak,2);self.assertEqual(budget.native.snapshot()['active'],0)

    def test_waiting_native_call_cancels_without_starting_and_slot_recovers(self):
        cancel=threading.Event();budget=ExecutionBudget(1,cancel);started=threading.Event();errors=[]
        def wait_slot():
            started.set()
            try:
                with budget.native.slot():errors.append('incorrectly started')
            except Cancelled:errors.append('cancelled')
        with budget.native.slot():
            thread=threading.Thread(target=wait_slot);thread.start();started.wait(1);cancel.set();thread.join(1)
        self.assertEqual(errors,['cancelled']);self.assertEqual(budget.native.snapshot()['active'],0)

    def test_waiters_are_served_in_arrival_order(self):
        budget=ExecutionBudget(1);order=[];threads=[]
        def queued(index):
            with budget.native.slot():order.append(index)
        with budget.native.slot():
            for index in range(3):
                thread=threading.Thread(target=queued,args=(index,));thread.start();threads.append(thread)
                deadline=time.monotonic()+2
                while budget.native.snapshot()['queued']<index+1 and time.monotonic()<deadline:time.sleep(.005)
                self.assertEqual(budget.native.snapshot()['queued'],index+1)
        for thread in threads:thread.join(2)
        self.assertEqual(order,[0,1,2])


if __name__=='__main__':unittest.main()
