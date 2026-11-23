from __future__ import annotations
import io
import json
import os
import tempfile
import threading
import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from jyexport.inbox import InboxWorker,read_task
from jyexport.storage import sha
from jyexport.submission import cli,discover_exports,process_once,submit_folder,summarize_scan


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jy_submission_');self.base=Path(self.temp.name)
        self.inbox=self.base/'inbox';self.inbox.mkdir();self.output=self.base/'output'

    def tearDown(self):self.temp.cleanup()

    def exports(self,folder,nested=False):
        folder.mkdir(parents=True)
        for key in ('V2','V3'):
            parent=folder/key if nested else folder
            parent.mkdir(exist_ok=True)
            (parent/(key+'.mp4')).write_bytes((key+'-native').encode())
        target=folder/'V2/V2.srt' if nested else folder/'字幕.srt'
        target.write_text('1\n00:00:00,000 --> 00:00:00,400\nhello\n',encoding='utf-8')
        return folder

    def test_external_folder_is_copied_and_published_once(self):
        source=self.exports(self.base/'editor-export')
        before={p.name:sha(p) for p in source.iterdir()}
        result=submit_folder(source,self.inbox,'剧名__01',confirmed_pair=True,output_root=self.output)
        self.assertTrue(result['copied']);self.assertFalse(result['already_submitted'])
        task,paths=read_task(result['folder'])
        self.assertEqual(task['source_sha256'],{key:sha(paths[key]) for key in ('V2','V3','SRT')})
        self.assertEqual(before,{p.name:sha(p) for p in source.iterdir()})
        again=submit_folder(source,self.inbox,'剧名__01',confirmed_pair=True,output_root=self.output)
        self.assertTrue(again['already_submitted']);self.assertEqual(result['id'],again['id'])
        self.assertEqual(len(list(self.inbox.glob('*/task.json'))),1)

    def test_direct_child_can_submit_in_place_with_nested_native_paths(self):
        source=self.exports(self.inbox/'episode',nested=True)
        result=submit_folder(source,self.inbox,'剧名__02',confirmed_pair=True,output_root=self.output)
        self.assertFalse(result['copied']);self.assertEqual(Path(result['folder']),source)
        task,_=read_task(source)
        self.assertEqual((task['V2'],task['V3'],task['SRT']),('V2/V2.mp4','V3/V3.mp4','V2/V2.srt'))

    def test_ambiguous_master_is_rejected_instead_of_guessing(self):
        source=self.exports(self.base/'source',nested=True)
        (source/'V2.mp4').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError,'2个候选'):discover_exports(source)
        self.assertEqual(list(self.inbox.iterdir()),[])

    def test_ambiguous_subtitle_is_rejected(self):
        source=self.exports(self.base/'source',nested=True)
        (source/'字幕.srt').write_bytes((source/'V2/V2.srt').read_bytes())
        with self.assertRaisesRegex(ValueError,'SRT存在2个候选'):discover_exports(source)

    def test_confirmation_is_required_before_creating_anything(self):
        source=self.exports(self.base/'source');inbox=self.base/'new-inbox'
        with self.assertRaisesRegex(ValueError,'明确确认'):submit_folder(source,inbox,'01')
        self.assertFalse(inbox.exists())

    def test_source_containing_inbox_is_rejected_without_creating_it(self):
        source=self.exports(self.base/'source');nested=source/'new-inbox'
        with self.assertRaisesRegex(ValueError,'包含收件箱'):submit_folder(source,nested,'01',confirmed_pair=True)
        self.assertFalse(nested.exists())

    def test_draft_folder_is_not_used_as_export_submission(self):
        draft=self.base/'draft';draft.mkdir();(draft/'draft_content.json').write_text('cipher')
        source=self.exports(draft/'exports')
        with self.assertRaisesRegex(ValueError,'草稿内部'):submit_folder(source,self.inbox,'01',confirmed_pair=True)

    def test_copy_failure_never_publishes_task_and_preserves_source(self):
        source=self.exports(self.base/'source')
        hashes={p.name:sha(p) for p in source.iterdir()}
        def fail(*_):raise OSError('disk full')
        with self.assertRaisesRegex(OSError,'disk full'):
            submit_folder(source,self.inbox,'01',confirmed_pair=True,copy_file=fail)
        self.assertEqual(list(self.inbox.rglob('task.json')),[])
        self.assertTrue(list(self.inbox.rglob('提交未完成.json')))
        self.assertEqual(hashes,{p.name:sha(p) for p in source.iterdir()})

    def test_submission_hash_blocks_later_source_replacement(self):
        folder=self.exports(self.inbox/'episode')
        submit_folder(folder,self.inbox,'01',confirmed_pair=True,output_root=self.output)
        (folder/'V2.mp4').write_bytes(b'changed after confirmation')
        worker=InboxWorker(self.inbox,self.output,'unused',stable_seconds=0,
                           processor=lambda *_args,**_kwargs:self.fail('changed input must not be processed'))
        worker.scan_once();result=worker.scan_once()
        self.assertEqual(result['results'][0]['state'],'source_changed')

    def test_different_submitted_content_creates_new_revision_not_old_receipt(self):
        source=self.exports(self.base/'source')
        first=submit_folder(source,self.inbox,'01',confirmed_pair=True)
        (source/'V2.mp4').write_bytes(b'different revision')
        second=submit_folder(source,self.inbox,'01',confirmed_pair=True)
        self.assertNotEqual(first['id'],second['id'])
        self.assertTrue(Path(first['folder'],'task.json').is_file())
        self.assertTrue(Path(second['folder'],'task.json').is_file())

    def test_concurrent_duplicate_submissions_publish_only_one_task(self):
        source=self.exports(self.base/'source');gate=threading.Barrier(2);results=[];errors=[]
        def work():
            try:
                gate.wait()
                results.append(submit_folder(source,self.inbox,'01',confirmed_pair=True))
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=work) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors,[]);self.assertEqual(len(results),2)
        self.assertEqual(results[0]['id'],results[1]['id'])
        self.assertEqual(len(list(self.inbox.glob('*/task.json'))),1)


class ScanSummaryTests(unittest.TestCase):
    def test_failed_count_sets_failure_exit_even_if_scan_count_claims_success(self):
        result=summarize_scan({'completed':999,'results':[{'state':'completed'},{'state':'failed'}]})
        self.assertEqual((result['completed'],result['failed'],result['exit_code']),(1,1,1))
        self.assertFalse(result['all_complete'])

    def test_pending_is_distinct_from_success(self):
        result=summarize_scan({'results':[{'state':'settling'}]})
        self.assertEqual(result['exit_code'],2);self.assertFalse(result['all_complete'])
        self.assertFalse(summarize_scan({'results':[]})['all_complete'])

    def test_once_waits_for_stability_and_reports_processing_failure(self):
        class Worker:
            stable_seconds=2
            def __init__(self):self.count=0
            def scan_once(self,event):
                self.count+=1
                return {'results':[{'state':'settling' if self.count==1 else 'failed'}]}
        worker=Worker();waits=[]
        result=process_once(worker,wait=lambda delay:waits.append(delay))
        self.assertEqual(worker.count,2);self.assertEqual(waits,[2])
        self.assertEqual(result['exit_code'],1)

    def test_stop_during_stability_does_not_start_second_scan(self):
        stop=threading.Event()
        class Worker:
            stable_seconds=2
            def __init__(self):self.count=0
            def scan_once(self,event):self.count+=1;return {'results':[{'state':'settling'}]}
        worker=Worker();result=process_once(worker,stop_event=stop,wait=lambda _:stop.set())
        self.assertEqual(worker.count,1);self.assertTrue(result['stopped'])

    def test_cli_returns_nonzero_and_writes_failed_count(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);inbox=base/'inbox';inbox.mkdir();output=base/'out';app=base/'app';app.mkdir()
            for name in ('ffmpeg.exe','ffprobe.exe'):(app/name).touch()
            worker=unittest.mock.Mock();worker.stable_seconds=0
            worker.scan_once.return_value={'results':[{'state':'failed','error':'bad media'}]}
            with patch('jyexport.submission.InboxWorker',return_value=worker),patch('jyexport.submission.resolve_app_dir',return_value=str(app)),patch('sys.stdout',io.StringIO()):
                code=cli(['--inbox',str(inbox),'--out',str(output)])
            result=json.loads((output/'收件箱运行状态.json').read_text(encoding='utf-8'))
            self.assertEqual(code,1);self.assertEqual(result['failed'],1);self.assertFalse(result['all_complete'])

    def test_existing_custom_report_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);inbox=base/'inbox';inbox.mkdir();app=base/'app';app.mkdir();report=base/'custom.json'
            for name in ('ffmpeg.exe','ffprobe.exe'):(app/name).touch()
            report.write_text('{"keep":"user data"}',encoding='utf-8');before=sha(report)
            with patch('jyexport.submission.resolve_app_dir',return_value=str(app)),patch('sys.stderr',io.StringIO()):
                code=cli(['--inbox',str(inbox),'--out',str(base/'out'),'--report',str(report)])
            self.assertEqual(code,1);self.assertEqual(sha(report),before)

    def test_watch_with_existing_stop_file_never_runs_queue(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);inbox=base/'inbox';inbox.mkdir();app=base/'app';app.mkdir();stop=base/'STOP';stop.touch()
            for name in ('ffmpeg.exe','ffprobe.exe'):(app/name).touch()
            class Worker:
                def run(self,event,*args):
                    if not event.is_set():raise AssertionError('existing stop signal must be honored')
            with patch('jyexport.submission.InboxWorker',return_value=Worker()),patch('jyexport.submission.resolve_app_dir',return_value=str(app)),patch('sys.stdout',io.StringIO()):
                code=cli(['--inbox',str(inbox),'--out',str(base/'out'),'--stop-file',str(stop)],watch=True)
            report=json.loads((base/'out/收件箱运行状态.json').read_text(encoding='utf-8'))
            self.assertEqual(code,2);self.assertTrue(report['stopped']);self.assertFalse(report['all_complete'])

    def test_watch_keeps_seen_failure_when_next_scan_is_empty(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);inbox=base/'inbox';inbox.mkdir();app=base/'app';app.mkdir()
            for name in ('ffmpeg.exe','ffprobe.exe'):(app/name).touch()
            class Worker:
                def run(self,event,interval,observed):
                    observed({'results':[{'task':'bad','state':'failed','error':'bad media'}]})
                    observed({'results':[]});event.set()
            with patch('jyexport.submission.InboxWorker',return_value=Worker()),patch('jyexport.submission.resolve_app_dir',return_value=str(app)),patch('sys.stdout',io.StringIO()):
                code=cli(['--inbox',str(inbox),'--out',str(base/'out'),'--stop-file',str(base/'STOP')],watch=True)
            report=json.loads((base/'out/收件箱运行状态.json').read_text(encoding='utf-8'))
            self.assertEqual(code,1);self.assertEqual(report['failed'],1)

    def test_main_process_failure_exit_is_not_zero(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);inbox=base/'inbox';task=inbox/'bad';task.mkdir(parents=True)
            (task/'task.json').write_text('{"schema":99}',encoding='utf-8')
            app=base/'app';app.mkdir()
            for name in ('videoeditor.dll','ffmpeg.exe','ffprobe.exe'):(app/name).touch()
            main=Path(__file__).resolve().parents[1]/'main.py'
            result=subprocess.run([sys.executable,'-B','-X','utf8',str(main),'--inbox-once',
                '--inbox',str(inbox),'--out',str(base/'out'),'--app-dir',str(app)],capture_output=True)
            self.assertEqual(result.returncode,1,result.stdout.decode('utf-8','replace'))
            report=json.loads((base/'out/收件箱运行状态.json').read_text(encoding='utf-8'))
            self.assertEqual(report['failed'],1);self.assertFalse(report['all_complete'])


class SubmissionMediaIntegrationTests(unittest.TestCase):
    def test_real_compose_bundle_contract_and_restart(self):
        app=Path(os.environ.get('JY_TEST_APP_DIR') or '__set_JY_TEST_APP_DIR__')
        if not (app/'ffmpeg.exe').is_file():self.skipTest('需要本机FFmpeg做收件箱媒体集成测试')
        with tempfile.TemporaryDirectory(prefix='jy_inbox_media_') as root:
            base=Path(root);inbox=base/'inbox';source=inbox/'第01集';source.mkdir(parents=True)
            for key,frequency in [('V2',440),('V3',880)]:
                command=[str(app/'ffmpeg.exe'),'-nostdin','-v','error','-n','-f','lavfi','-i',
                         'color=c=black:s=1080x1920:r=60:d=0.5','-f','lavfi','-i',
                         f'sine=frequency={frequency}:sample_rate=48000:duration=0.5',
                         '-c:v','mpeg4','-q:v','10','-c:a','aac','-t','0.5',str(source/(key+'.mp4'))]
                result=subprocess.run(command,capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
            (source/'字幕.srt').write_text('1\n00:00:00,000 --> 00:00:00,400\ntest\n',encoding='utf-8')
            submit_folder(source,inbox,'真实编码集成__01',confirmed_pair=True,output_root=base/'out')
            worker=InboxWorker(inbox,base/'out',app,stable_seconds=0)
            summary=process_once(worker,wait=lambda _:None)
            self.assertEqual((summary['completed'],summary['failed'],summary['exit_code']),(1,0,0),summary)
            self.assertTrue(summary['all_complete'])
            again=worker.scan_once()
            self.assertEqual(again['results'][0]['state'],'already_completed')


class InboxGuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.temp=tempfile.TemporaryDirectory(prefix='jy_inbox_gui_')
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':self.temp.name});self.env.start()
        self.root=tk.Tk();self.root.withdraw()
        from jyexport.gui import App
        self.app=App(self.root);self.root.update_idletasks()

    def tearDown(self):
        self.app.inbox_stop.set();self.app.codec.close()
        self.root.destroy();self.env.stop();self.temp.cleanup()

    def test_page_initializes_without_auto_start_or_saved_confirmation(self):
        self.assertFalse(self.app._inbox_running());self.assertFalse(self.app.inbox_confirm.get())
        self.assertIn('公司收件箱',[self.app.tabs.tab(tab,'text') for tab in self.app.tabs.tabs()])
        self.app.save_preferences()
        data=json.loads((self.app.state_dir/'settings.json').read_text(encoding='utf-8'))
        self.assertIn('inbox_root',data)
        self.assertNotIn('inbox_active',data)

    def test_close_requests_stop_and_waits_for_current_worker(self):
        class Live:
            alive=True
            def is_alive(self):return self.alive
        live=Live();self.app.inbox_thread=live
        with patch.object(self.app,'_finish_close') as finish:
            self.app.close()
            self.assertTrue(self.app.inbox_stop.is_set());finish.assert_not_called()
            live.alive=False;self.app.pump();finish.assert_called_once()


class CacheSummaryTests(unittest.TestCase):
    def test_cached_completion_is_separate_from_this_scan_full_verification(self):
        summary=summarize_scan({'results':[{'state':'completed_cached','hash_verified_this_scan':False,
            'metadata_checked_this_scan':True,'last_verified':'2026-11-05T10:00:00+08:00','audit_due':'2026-11-05T11:00:00+08:00'}]})
        self.assertTrue(summary['all_complete']);self.assertFalse(summary['all_hash_verified_this_scan'])
        self.assertEqual((summary['completed'],summary['cached'],summary['hash_verified']),(1,1,0))
        self.assertEqual(summary['verification_mode'],'cached')

    def test_failure_is_not_washed_by_a_cached_completed_neighbor(self):
        summary=summarize_scan({'results':[{'state':'completed_cached','metadata_checked_this_scan':True,
                                            'hash_verified_this_scan':False},{'state':'failed'}]})
        self.assertEqual((summary['failed'],summary['cached'],summary['exit_code']),(1,1,1))
        self.assertFalse(summary['all_complete'])

    def test_unobserved_history_is_not_current_full_verification(self):
        summary=summarize_scan({'results':[{'state':'already_completed','hash_verified_this_scan':True,
            'metadata_checked_this_scan':True,'present_this_scan':False}]})
        self.assertFalse(summary['all_complete']);self.assertFalse(summary['all_hash_verified_this_scan'])
        self.assertEqual(summary['hash_verified'],0)

    def test_force_audit_once_is_forwarded(self):
        class Worker:
            stable_seconds=0
            def scan_once(self,event,*,force_audit=False):
                if not force_audit:raise AssertionError('force flag not passed')
                return {'results':[{'state':'already_completed','hash_verified_this_scan':True,'metadata_checked_this_scan':True}]}
        summary=process_once(Worker(),force_audit=True)
        self.assertTrue(summary['all_hash_verified_this_scan'])

    def test_cli_once_defaults_strict_and_force_flag_is_forwarded(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);inbox=base/'inbox';inbox.mkdir();app=base/'app';app.mkdir()
            for name in ('ffmpeg.exe','ffprobe.exe'):(app/name).touch()
            worker=unittest.mock.Mock();worker.stable_seconds=0
            worker.scan_once.return_value={'results':[]}
            with patch('jyexport.submission.InboxWorker',return_value=worker) as factory,patch('jyexport.submission.resolve_app_dir',return_value=str(app)),patch('sys.stdout',io.StringIO()):
                self.assertEqual(cli(['--inbox',str(inbox),'--out',str(base/'out')]),0)
                self.assertEqual(factory.call_args.kwargs['completed_audit_interval'],0)
                self.assertEqual(cli(['--inbox',str(inbox),'--out',str(base/'out'),'--completed-audit-seconds','3600','--force-audit']),0)
                self.assertEqual(factory.call_args.kwargs['completed_audit_interval'],3600)
                self.assertTrue(worker.scan_once.call_args.kwargs['force_audit'])

    def test_cli_watch_defaults_to_window_and_labels_cached_rows(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root);inbox=base/'inbox';inbox.mkdir();app=base/'app';app.mkdir()
            for name in ('ffmpeg.exe','ffprobe.exe'):(app/name).touch()
            class Worker:
                def run(self,event,interval,observed):
                    observed({'results':[{'task':'a','state':'completed_cached','hash_verified_this_scan':False,
                        'metadata_checked_this_scan':True,'last_verified':'2026-11-05T10:00:00+08:00','audit_due':'2026-11-05T11:00:00+08:00'}]})
                    event.set()
            with patch('jyexport.submission.InboxWorker',return_value=Worker()) as factory,patch('jyexport.submission.resolve_app_dir',return_value=str(app)),patch('sys.stdout',io.StringIO()):
                code=cli(['--inbox',str(inbox),'--out',str(base/'out'),'--stop-file',str(base/'STOP')],watch=True)
            self.assertEqual(code,2);self.assertEqual(factory.call_args.kwargs['completed_audit_interval'],3600)
            report=json.loads((base/'out/收件箱运行状态.json').read_text(encoding='utf-8'))
            self.assertEqual(report['cached'],1);self.assertFalse(report['all_hash_verified_this_scan'])
            self.assertFalse(report['results'][0]['hash_verified_this_scan'])


class InboxGuiCacheTests(unittest.TestCase):
    setUp=InboxGuiTests.setUp
    tearDown=InboxGuiTests.tearDown

    def test_cache_ui_default_and_label_are_explicit(self):
        self.assertEqual(self.app.inbox_audit_seconds.get(),'3600')
        self.app.update_inbox_row({'id':'a','task':'episode','state':'completed_cached',
            'hash_verified_this_scan':False,'metadata_checked_this_scan':True,
            'last_verified':'2026-11-05T10:00:00+08:00','audit_due':'2026-11-05T11:00:00+08:00'})
        row=self.app.inbox_tree.item(self.app.inbox_tree_rows['task:episode'],'values')
        self.assertIn('缓存',row[1]);self.assertIn('本轮仅元数据',row[2])
        self.assertIn('缓存1',self.app.inbox_counts.get())
        self.assertFalse(self.app.inbox_rows['task:episode']['hash_verified_this_scan'])

    def test_cache_manual_button_requests_strict_single_round(self):
        with patch.object(self.app,'start_inbox') as start:
            self.app.inbox_once_button.invoke()
        start.assert_called_once_with(False,True)

    def test_cache_resubmission_does_not_wash_existing_failure(self):
        self.app.update_inbox_row({'id':'a','task':'episode','state':'failed','error':'bad media','receipt':'original.json'})
        self.app.update_inbox_row({'id':'a','task':'episode','state':'waiting','receipt':'new-task.json'})
        row=self.app.inbox_rows['task:episode']
        self.assertEqual((row['state'],row['error'],row['receipt']),('failed','bad media','original.json'))
        self.assertIn('失败1',self.app.inbox_counts.get())


if __name__=='__main__':unittest.main(verbosity=2)
