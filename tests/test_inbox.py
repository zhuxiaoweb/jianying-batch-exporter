from __future__ import annotations
import json
import csv
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jyexport.inbox import InboxBusy, InboxWorker, _state_lock
from jyexport.storage import atomic_json, sha


class Crash(BaseException):
    pass


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jy_inbox_test_')
        self.base=Path(self.temp.name)
        self.inbox=self.base/'inbox';self.inbox.mkdir()
        self.output=self.base/'output'
        self.tick=100.0
        self.calls=[]

    def tearDown(self):
        self.temp.cleanup()

    def task(self,name='episode-1',identifier=None):
        folder=self.inbox/name;folder.mkdir()
        for key in ('V2','V3'):(folder/(key+'.mp4')).write_bytes((key+'-native-data').encode())
        (folder/'subtitle.srt').write_text('1\n00:00:00,000 --> 00:00:00,400\nhello\n',encoding='utf-8')
        data={'schema':1,'id':identifier or name,'identity':'剧名__'+name,
              'same_timeline_confirmed':True,'V2':'V2.mp4','V3':'V3.mp4','SRT':'subtitle.srt'}
        atomic_json(folder/'task.json',data)
        return folder,data

    def worker(self,processor=None,**kwargs):
        return InboxWorker(self.inbox,self.output,'unused',stable_seconds=2,
                           clock=lambda:self.tick,processor=processor or self.process,**kwargs)

    def process(self,v2,v3,srt,root,app_dir,identity,confirmed_pair=False,log=lambda value:None):
        self.calls.append(identity)
        self.assertTrue(confirmed_pair)
        result_dir=Path(root)/'result';result_dir.mkdir()
        result={'complete':True,'method':'two_native_masters_stream_copy','identity':identity,'directory':str(result_dir),
                'sources':{'V2':{'sha256':sha(v2)},'V3':{'sha256':sha(v3)}},'variants':{},
                'video_reencoded':False,'audio_reencoded':False,'sources_unchanged':True,
                'reviewed':False,'status':'五项文件完成·待声画审核'}
        for code in ('V1','V2','V3','V4'):
            target=result_dir/(code+'.mp4')
            if code in ('V2','V3'):shutil.copy2(v2 if code=='V2' else v3,target)
            else:target.write_bytes((code+'-generated').encode())
            item={'file':str(target),'sha256':sha(target),'reviewed':False,
                  'probe':{'width':1080,'height':1920,'fps':'60','container':'MP4',
                           'audio_streams':1,'full_decode':True}}
            if code in ('V1','V4'):
                item['video_source'],item['audio_source']=('V3','V2') if code=='V1' else ('V2','V3')
            result['variants'][code]=item
        target=result_dir/'subtitle.srt';shutil.copy2(srt,target)
        result['srt']={'file':str(target),'sha256':sha(target),'cues':1}
        with (result_dir/'审核归档总表.csv').open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['项目/集','版本','文件','SHA256','状态'])
            for code in ('V1','V2','V3','V4'):
                item=result['variants'][code]
                writer.writerow([identity,code,item['file'],item['sha256'],'规格通过·待声画审核'])
            writer.writerow([identity,'SRT',result['srt']['file'],result['srt']['sha256'],'待审核'])
        atomic_json(result_dir/'交付状态.json',{'status':result['status']})
        atomic_json(result_dir/'两母版交付报告.json',result)
        return result

    def settled_scan(self,worker=None):
        worker=worker or self.worker()
        first=worker.scan_once()
        self.assertTrue(all(item['state']=='settling' for item in first['results']))
        self.tick+=3
        return worker.scan_once()

    def test_waits_for_two_stable_observations(self):
        folder,_=self.task();worker=self.worker()
        self.assertEqual(worker.scan_once()['results'][0]['state'],'settling')
        self.tick+=1
        self.assertEqual(worker.scan_once()['results'][0]['state'],'settling')
        self.assertEqual(self.calls,[])
        (folder/'V2.mp4').write_bytes(b'new-version')
        self.tick+=2
        self.assertEqual(worker.scan_once()['results'][0]['state'],'settling')
        self.tick+=3
        self.assertEqual(worker.scan_once()['completed'],1)

    def test_missing_file_waits_without_invoking_processor(self):
        folder,_=self.task();(folder/'V3.mp4').unlink()
        result=self.worker().scan_once()
        self.assertEqual(result['results'][0]['state'],'waiting_files')
        self.assertEqual(self.calls,[])
        self.assertTrue(list((self.output/'.inbox-state/errors').glob('*.json')))

    def test_restart_audits_receipt_without_duplicate_processing(self):
        self.task()
        result=self.settled_scan()
        self.assertEqual(result['completed'],1)
        before=list(self.output.rglob('V1.mp4'))
        again=self.worker().scan_once()
        self.assertEqual(again['results'][0]['state'],'already_completed')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(before,list(self.output.rglob('V1.mp4')))

    def test_content_change_with_preserved_size_and_mtime_is_not_reused(self):
        folder,_=self.task();self.settled_scan()
        path=folder/'V2.mp4';stat=path.stat()
        path.write_bytes(b'x'*stat.st_size)
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        result=self.worker().scan_once()
        self.assertEqual(result['results'][0]['state'],'source_changed')
        self.assertEqual(len(self.calls),1)

    def test_output_tampering_is_not_hidden_by_completed_status(self):
        self.task();result=self.settled_scan()
        path=Path(result['results'][0]['directory'])/'V4.mp4'
        path.write_bytes(b'corrupted')
        again=self.worker().scan_once()
        self.assertEqual(again['results'][0]['state'],'output_changed')
        self.assertEqual(len(self.calls),1)

    def test_source_change_during_processing_blocks_receipt(self):
        folder,_=self.task()
        def change(*args,**kwargs):
            result=self.process(*args,**kwargs)
            (folder/'subtitle.srt').write_text('1\n00:00:00,000 --> 00:00:00,400\nchanged\n',encoding='utf-8')
            return result
        result=self.settled_scan(self.worker(change))
        self.assertEqual(result['results'][0]['state'],'failed')
        self.assertIn('来源变化',result['results'][0]['error'])

    def test_completed_result_before_crash_is_recovered_not_reencoded(self):
        self.task()
        def crash(*args,**kwargs):
            self.process(*args,**kwargs)
            raise Crash()
        worker=self.worker(crash);worker.scan_once();self.tick+=3
        with self.assertRaises(Crash):worker.scan_once()
        result=self.worker().scan_once()
        self.assertEqual(result['results'][0]['state'],'recovered')
        self.assertEqual(len(self.calls),1)

    def test_partial_crash_requires_attention_instead_of_duplicate_run(self):
        self.task()
        def crash(*args,**kwargs):
            self.calls.append(args[5]);(Path(args[3])/'partial.mp4').write_bytes(b'partial')
            raise Crash()
        worker=self.worker(crash);worker.scan_once();self.tick+=3
        with self.assertRaises(Crash):worker.scan_once()
        result=self.worker().scan_once()
        self.assertEqual(result['results'][0]['state'],'interrupted')
        self.assertEqual(len(self.calls),1)

    def test_one_failure_does_not_block_other_tasks_or_repeat_automatically(self):
        self.task('a');self.task('b')
        def selective(*args,**kwargs):
            if args[5].endswith('__a'):
                self.calls.append(args[5]);raise ValueError('bad media')
            return self.process(*args,**kwargs)
        worker=self.worker(selective)
        result=self.settled_scan(worker)
        self.assertEqual([r['state'] for r in result['results']],['failed','completed'])
        worker.scan_once()
        self.assertEqual(len(self.calls),2)

    def test_duplicate_id_in_another_folder_is_blocked(self):
        self.task('a','same-id');self.task('b','same-id')
        result=self.worker().scan_once()
        self.assertEqual([r['state'] for r in result['results']],['settling','duplicate_id'])

    def test_relative_path_escape_is_rejected_and_persisted(self):
        folder,data=self.task();data['V2']='../outside.mp4';atomic_json(folder/'task.json',data)
        result=self.worker().scan_once()
        self.assertEqual(result['results'][0]['state'],'invalid_task')
        self.assertEqual(self.calls,[])

    def test_absolute_windows_path_is_rejected(self):
        folder,data=self.task();data['V2']='C:\\private\\video.mp4';atomic_json(folder/'task.json',data)
        self.assertEqual(self.worker().scan_once()['results'][0]['state'],'invalid_task')

    def test_input_output_overlap_and_draft_output_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'重叠'):
            InboxWorker(self.inbox,self.inbox/'output','unused')
        with self.assertRaisesRegex(ValueError,'重叠'):
            InboxWorker(self.inbox,self.base,'unused')
        draft=self.base/'draft';draft.mkdir();(draft/'draft_content.json').write_text('cipher')
        with self.assertRaisesRegex(ValueError,'草稿内部'):
            InboxWorker(self.inbox,draft/'out','unused')

    def test_corrupt_state_does_not_start_a_new_attempt(self):
        self.task();self.settled_scan()
        record,=list((self.output/'.inbox-state').glob('*.json'))
        record.write_text('{broken',encoding='utf-8')
        self.assertEqual(self.worker().scan_once()['results'][0]['state'],'invalid_task')
        self.assertEqual(len(self.calls),1)

    def test_output_and_inbox_folders_can_move_with_receipts(self):
        self.task();self.settled_scan()
        new_inbox=self.base/'moved-inbox';new_output=self.base/'moved-output'
        self.inbox.rename(new_inbox);self.output.rename(new_output)
        worker=InboxWorker(new_inbox,new_output,'unused',stable_seconds=0,
                           clock=lambda:self.tick,processor=self.process)
        result=worker.scan_once()
        self.assertEqual(result['results'][0]['state'],'already_completed')
        self.assertTrue(Path(result['results'][0]['directory']).is_relative_to(new_output))
        self.assertEqual(len(self.calls),1)

    def test_live_os_lock_prevents_duplicate_worker(self):
        worker=self.worker()
        with _state_lock(worker.state_dir):
            with self.assertRaises(InboxBusy):worker.scan_once()

    def test_stop_event_ends_loop_without_starting_processing(self):
        self.task();worker=self.worker();stop=threading.Event();seen=[]
        def on_scan(result):seen.append(result);stop.set()
        worker.run(stop,poll_seconds=.01,on_scan=on_scan)
        self.assertEqual(len(seen),1)
        self.assertEqual(self.calls,[])

    def test_same_file_hardlinks_are_rejected(self):
        folder,_=self.task();(folder/'V3.mp4').unlink()
        try:os.link(folder/'V2.mp4',folder/'V3.mp4')
        except OSError as exc:self.skipTest(str(exc))
        self.assertEqual(self.worker().scan_once()['results'][0]['state'],'invalid_task')

    def test_output_jobs_link_cannot_write_back_into_inbox(self):
        self.task();self.output.mkdir()
        try:os.symlink(self.inbox,self.output/'jobs',target_is_directory=True)
        except OSError as exc:self.skipTest(str(exc))
        result=self.settled_scan()
        self.assertEqual(result['results'][0]['state'],'invalid_task')
        self.assertEqual([p.name for p in self.inbox.iterdir()],['episode-1'])
        self.assertEqual(self.calls,[])

    def test_state_directory_link_to_inbox_is_rejected_before_writing(self):
        self.output.mkdir()
        try:os.symlink(self.inbox,self.output/'.inbox-state',target_is_directory=True)
        except OSError as exc:self.skipTest(str(exc))
        with self.assertRaisesRegex(ValueError,'重叠'):self.worker()
        self.assertEqual(list(self.inbox.iterdir()),[])

    def test_manifest_changes_after_parsing_do_not_execute_stale_paths(self):
        folder,data=self.task();worker=self.worker();worker.scan_once();self.tick+=3
        from jyexport import inbox as module
        original=module._fingerprint
        def change(paths,stats):
            data['identity']='different-episode'
            atomic_json(folder/'task.json',data)
            return original(paths,module._stats(paths))
        with patch('jyexport.inbox._fingerprint',side_effect=change):
            result=worker.scan_once()
        self.assertEqual(result['results'][0]['state'],'settling')
        self.assertEqual(self.calls,[])

    def test_completed_source_disappearance_is_attention_not_waiting(self):
        folder,_=self.task();self.settled_scan();(folder/'V3.mp4').unlink()
        result=self.worker().scan_once()
        self.assertEqual(result['results'][0]['state'],'source_changed')
        self.assertEqual(result['attention'],1)

    def test_recovery_does_not_accept_partial_audit_csv(self):
        self.task()
        def crash(*args,**kwargs):
            result=self.process(*args,**kwargs)
            (Path(result['directory'])/'审核归档总表.csv').write_text('项目/集,版本,文件,SHA256,状态\n',encoding='utf-8')
            raise Crash()
        worker=self.worker(crash);worker.scan_once();self.tick+=3
        with self.assertRaises(Crash):worker.scan_once()
        result=self.worker().scan_once()
        self.assertEqual(result['results'][0]['state'],'interrupted')
        self.assertEqual(len(self.calls),1)

    def test_processor_return_and_saved_report_must_match(self):
        self.task()
        def inconsistent(*args,**kwargs):
            result=self.process(*args,**kwargs)
            result['srt']['cues']=999
            return result
        result=self.settled_scan(self.worker(inconsistent))
        self.assertEqual(result['results'][0]['state'],'failed')
        self.assertIn('落盘交付报告不一致',result['results'][0]['error'])


class InboxCacheTests(unittest.TestCase):
    setUp=InboxTests.setUp
    tearDown=InboxTests.tearDown
    task=InboxTests.task
    process=InboxTests.process
    settled_scan=InboxTests.settled_scan

    def worker(self,processor=None,**kwargs):
        kwargs.setdefault('completed_audit_interval',3600)
        return InboxWorker(self.inbox,self.output,'unused',stable_seconds=2,clock=lambda:self.tick,
                           processor=processor or self.process,**kwargs)

    def test_cache_repeated_watch_scans_do_not_hash_media_or_rewrite_receipts(self):
        self.task();worker=self.worker();self.settled_scan(worker)
        receipt,=list((self.output/'.inbox-state').glob('*.json'))
        before=receipt.stat().st_mtime_ns
        from jyexport import inbox as module
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            for _ in range(3):
                self.tick+=1;row=worker.scan_once()['results'][0]
                self.assertEqual(row['state'],'completed_cached')
                self.assertFalse(row['hash_verified_this_scan']);self.assertTrue(row['metadata_checked_this_scan'])
                self.assertEqual(row['audit_due_epoch'],3703)
            self.assertEqual(digest.call_count,0)
        self.assertEqual(receipt.stat().st_mtime_ns,before)

    def test_cache_source_metadata_change_forces_immediate_full_audit(self):
        folder,_=self.task();worker=self.worker();self.settled_scan(worker)
        path=folder/'V2.mp4';stat=path.stat();os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000))
        from jyexport import inbox as module
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            row=worker.scan_once()['results'][0]
        self.assertEqual(row['state'],'already_completed');self.assertTrue(row['hash_verified_this_scan'])
        self.assertGreater(digest.call_count,0)

    def test_cache_output_metadata_change_forces_immediate_full_audit(self):
        self.task();worker=self.worker();done=self.settled_scan(worker)
        path=Path(done['results'][0]['directory'])/'V4.mp4';stat=path.stat()
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000))
        from jyexport import inbox as module
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            row=worker.scan_once()['results'][0]
        self.assertEqual(row['state'],'already_completed');self.assertGreater(digest.call_count,0)

    def test_cache_hidden_source_change_is_found_when_audit_expires(self):
        folder,_=self.task();worker=self.worker();done=self.settled_scan(worker)
        path=folder/'V2.mp4';stat=path.stat();path.write_bytes(b'x'*stat.st_size)
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        row=worker.scan_once()['results'][0]
        self.assertEqual(row['state'],'completed_cached');self.assertFalse(row['hash_verified_this_scan'])
        self.assertEqual(row['last_verified'],done['results'][0]['last_verified'])
        self.tick+=3601
        row=worker.scan_once()['results'][0]
        self.assertEqual(row['state'],'source_changed');self.assertEqual(len(self.calls),1)

    def test_cache_hidden_output_change_is_found_when_audit_expires(self):
        self.task();worker=self.worker();done=self.settled_scan(worker)
        path=Path(done['results'][0]['directory'])/'V1.mp4';stat=path.stat()
        path.write_bytes(b'z'*stat.st_size);os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        self.assertEqual(worker.scan_once()['results'][0]['state'],'completed_cached')
        self.tick+=3601
        self.assertEqual(worker.scan_once()['results'][0]['state'],'output_changed')

    def test_cache_force_flag_bypasses_live_window(self):
        self.task();worker=self.worker();self.settled_scan(worker)
        from jyexport import inbox as module
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            row=worker.scan_once(force_audit=True)['results'][0]
        self.assertEqual(row['state'],'already_completed');self.assertTrue(row['hash_verified_this_scan'])
        self.assertGreater(digest.call_count,0)

    def test_cache_missing_audit_evidence_on_restart_runs_strict(self):
        self.task();self.settled_scan(self.worker())
        path,=list((self.output/'.inbox-state').glob('*.json'))
        record=json.loads(path.read_text(encoding='utf-8'));record.pop('audit');atomic_json(path,record)
        from jyexport import inbox as module
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            row=self.worker().scan_once()['results'][0]
        self.assertEqual(row['state'],'already_completed');self.assertTrue(row['hash_verified_this_scan'])
        self.assertGreater(digest.call_count,0)

    def test_cache_restart_with_complete_evidence_can_return_labeled_cache(self):
        self.task();self.settled_scan(self.worker())
        row=self.worker().scan_once()['results'][0]
        self.assertEqual(row['state'],'completed_cached');self.assertFalse(row['hash_verified_this_scan'])

    def test_cache_moving_input_and_output_roots_runs_strict_first(self):
        self.task();self.settled_scan(self.worker())
        new_in=self.base/'moved-in';new_out=self.base/'moved-out'
        self.inbox.rename(new_in);self.output.rename(new_out)
        worker=InboxWorker(new_in,new_out,'unused',stable_seconds=0,clock=lambda:self.tick,
                           completed_audit_interval=3600,processor=self.process)
        from jyexport import inbox as module
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            row=worker.scan_once()['results'][0]
        self.assertEqual(row['state'],'already_completed');self.assertTrue(row['hash_verified_this_scan'])
        self.assertGreater(digest.call_count,0)

    def test_cache_default_api_remains_strict(self):
        self.task();worker=InboxWorker(self.inbox,self.output,'unused',stable_seconds=2,
                                      clock=lambda:self.tick,processor=self.process)
        self.settled_scan(worker)
        from jyexport import inbox as module
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            row=worker.scan_once()['results'][0]
        self.assertEqual(row['state'],'already_completed');self.assertTrue(row['hash_verified_this_scan'])
        self.assertEqual(worker.completed_audit_interval,0);self.assertGreater(digest.call_count,0)

    def test_cache_clock_rollback_forces_strict_audit(self):
        self.task();worker=self.worker();self.settled_scan(worker);self.tick=10
        row=worker.scan_once()['results'][0]
        self.assertEqual(row['state'],'already_completed');self.assertTrue(row['hash_verified_this_scan'])

    def test_cache_failure_is_not_washed_or_rehashed_every_poll(self):
        self.task();worker=self.worker(lambda *_args,**_kwargs:(_ for _ in ()).throw(ValueError('failure')))
        self.settled_scan(worker)
        from jyexport import inbox as module
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            row=worker.scan_once()['results'][0]
        self.assertEqual(row['state'],'failed');self.assertEqual(digest.call_count,0)

    def test_cache_rejects_invalid_intervals(self):
        for value in (-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError):self.worker(completed_audit_interval=value)

    def test_cache_first_completion_and_processing_recovery_stay_strict(self):
        self.task();worker=self.worker()
        from jyexport import inbox as module
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            first=self.settled_scan(worker)['results'][0]
        self.assertEqual(first['state'],'completed');self.assertTrue(first['hash_verified_this_scan'])
        self.assertGreater(digest.call_count,0)
        path,=list((self.output/'.inbox-state').glob('*.json'))
        record=json.loads(path.read_text(encoding='utf-8'));record['state']='processing';atomic_json(path,record)
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            recovered=worker.scan_once()['results'][0]
        self.assertEqual(recovered['state'],'recovered');self.assertTrue(recovered['hash_verified_this_scan'])
        self.assertGreater(digest.call_count,0);self.assertEqual(len(self.calls),1)

    def test_cache_manual_force_finds_hidden_change_before_due(self):
        folder,_=self.task();worker=self.worker();self.settled_scan(worker)
        path=folder/'V2.mp4';stat=path.stat();path.write_bytes(b'm'*stat.st_size)
        os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        self.assertEqual(worker.scan_once(force_audit=True)['results'][0]['state'],'source_changed')

    def test_cache_malformed_source_evidence_is_not_reused(self):
        self.task();worker=self.worker();self.settled_scan(worker)
        path,=list((self.output/'.inbox-state').glob('*.json'))
        record=json.loads(path.read_text(encoding='utf-8'))
        record['fingerprint']['V2']=None;record['audit']['fingerprint']['V2']=None
        atomic_json(path,record)
        self.assertEqual(worker.scan_once()['results'][0]['state'],'source_changed')

    def test_cache_artifact_inventory_must_name_existing_outputs(self):
        self.task();worker=self.worker();self.settled_scan(worker)
        path,=list((self.output/'.inbox-state').glob('*.json'))
        record=json.loads(path.read_text(encoding='utf-8'))
        digest=record['artifacts'].pop('V1.mp4');record['artifacts']['missing.mp4']=digest
        record['audit']['artifacts']=dict(record['artifacts']);atomic_json(path,record)
        self.assertEqual(worker.scan_once()['results'][0]['state'],'output_changed')

    def test_cache_unsafe_output_link_is_not_traversed_or_rehashed_each_poll(self):
        self.task();worker=self.worker();done=self.settled_scan(worker)
        outside=self.base/'outside';outside.mkdir();(outside/'两母版交付报告.json').write_text('{"outside":true}',encoding='utf-8')
        try:os.symlink(outside,Path(done['results'][0]['directory'])/'linked',target_is_directory=True)
        except OSError as exc:self.skipTest(str(exc))
        from jyexport import inbox as module
        with patch('jyexport.inbox._read_json',wraps=module._read_json) as reader:
            row=worker.scan_once()['results'][0]
        self.assertEqual(row['state'],'output_changed')
        self.assertFalse(any(outside in Path(call.args[0]).parents for call in reader.call_args_list))
        with patch('jyexport.inbox.sha',wraps=module.sha) as digest:
            self.assertEqual(worker.scan_once()['results'][0]['state'],'output_changed')
        self.assertEqual(digest.call_count,0)


if __name__=='__main__':unittest.main(verbosity=2)
