import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jyexport.preflight import check_all
from jyexport.storage import Cancelled,sha
from jyexport.workflow import verify_backup
from jyexport.media_inputs import probe_media_inputs


class BoundedPreflightTests(unittest.TestCase):
    def setUp(self):
        self.memory=patch('jyexport.preflight.memory_status',return_value={'physical_available_mib':8192,'commit_available_mib':8192})
        self.memory.start();self.addCleanup(self.memory.stop)

    def test_all_checks_run_once_with_ordered_results_and_four_worker_cap(self):
        lock=threading.Lock();active=0;peak=0;seen=[]
        def check(i):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active);seen.append(i)
            time.sleep(.002*(4-i%4))
            with lock:active-=1
            return i*i
        with patch('jyexport.preflight.os.cpu_count',return_value=128):
            result=check_all(check,range(40))
        self.assertEqual(result,[i*i for i in range(40)])
        self.assertEqual(sorted(seen),list(range(40)))
        self.assertLessEqual(peak,4);self.assertGreater(peak,1);self.assertEqual(active,0)

    def test_failure_stops_submission_and_joins_readers(self):
        seen=[];finished=[]
        def check(i):
            seen.append(i)
            if i==0:raise ValueError('invalid source')
            time.sleep(.01);finished.append(i)
        with patch('jyexport.preflight.os.cpu_count',return_value=12):
            with self.assertRaisesRegex(ValueError,'invalid source'):check_all(check,range(100))
        self.assertTrue(set(seen).issubset(range(4)))
        self.assertEqual(set(finished),set(seen)-{0})

    def test_cancellation_does_not_start_later_checks(self):
        cancel=threading.Event();seen=[]
        def check(i):seen.append(i);cancel.set();return i
        with patch('jyexport.preflight.os.cpu_count',return_value=12):
            with self.assertRaises(Cancelled):check_all(check,range(100),cancel)
        self.assertEqual(seen,[0])
        with self.assertRaises(Cancelled):check_all(check,range(100),cancel)
        self.assertEqual(seen,[0])

    def test_low_core_or_unknown_cpu_has_serial_fallback(self):
        for count in (1,2,None):
            threads=[]
            with patch('jyexport.preflight.os.cpu_count',return_value=count):
                self.assertEqual(check_all(lambda i:(threads.append(threading.get_ident()),i)[1],range(7)),list(range(7)))
            self.assertEqual(set(threads),{threading.get_ident()})

    def test_low_or_unknown_memory_uses_serial_checks(self):
        for memory in ({},{'physical_available_mib':1024},{'physical_available_mib':8192,'commit_available_mib':1024}):
            seen=[]
            with patch('jyexport.preflight.memory_status',return_value=memory):
                check_all(lambda i:seen.append(threading.get_ident()),range(8))
            self.assertEqual(set(seen),{threading.get_ident()})

    def test_intermediate_memory_has_two_worker_cap(self):
        lock=threading.Lock();active=0;peak=0
        def check(i):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            time.sleep(.003)
            with lock:active-=1
        with patch('jyexport.preflight.memory_status',return_value={'physical_available_mib':2500}),patch('jyexport.preflight.os.cpu_count',return_value=12):
            check_all(check,range(20))
        self.assertEqual(peak,2)

    def test_every_backup_member_is_rehashed_on_every_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);files=[]
            for i in range(24):
                p=root/str(i);p.write_text('original');files.append({'path':str(i),'sha256':sha(p)})
            record={'backup':str(root),'files':files};session={'backups':[record]};job={'backup':str(root)}
            self.assertIs(verify_backup(session,job),record)
            (root/'23').write_text('tampered')
            with self.assertRaisesRegex(ValueError,'23'):verify_backup(session,job)
            (root/'23').unlink()
            with self.assertRaisesRegex(ValueError,'23'):verify_backup(session,job)

    def test_late_invalid_media_blocks_all_results_without_skipping_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'ffprobe.exe').touch()
            audios=[];segments=[]
            for i in range(12):
                p=root/(f'{i:02}.mp3');p.touch()
                audios.append({'id':str(i),'path':str(p)})
                segments.append({'id':str(i),'material_id':str(i),'source_timerange':{'start':0,'duration':1000000}})
            data={'tracks':[{'type':'audio','segments':segments}],'materials':{'audios':audios}}
            calls=[]
            def run(args,**kwargs):
                name=Path(args[-1]).name;calls.append(name)
                duration='.5' if name=='11.mp3' else '1.0'
                return SimpleNamespace(returncode=0,stdout=json.dumps({'streams':[{'codec_type':'audio'}],'format':{'duration':duration}}).encode())
            with patch('jyexport.media_inputs.subprocess.run',side_effect=run):
                with self.assertRaisesRegex(ValueError,'源范围'):probe_media_inputs(data,root)
            self.assertEqual(sorted(calls),[f'{i:02}.mp3' for i in range(12)])

    def test_metadata_evidence_order_and_values_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'ffprobe.exe').touch();audios=[];segments=[]
            for i in reversed(range(9)):
                p=root/(str(i)+'.mp3');p.touch();audios.append({'id':str(i),'path':str(p)})
                segments.append({'id':str(i),'material_id':str(i)})
            data={'tracks':[{'type':'audio','segments':segments}],'materials':{'audios':audios}}
            parsed={'streams':[{'codec_type':'audio','channels':2}],'format':{'duration':'1.0'}}
            with patch('jyexport.media_inputs.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(parsed).encode())):
                result=probe_media_inputs(data,root)
            self.assertEqual(list(result),sorted(result))
            self.assertEqual(len(result),9)
            self.assertTrue(all(row=={'required_stream_types':['audio'],'streams':parsed['streams'],'duration':'1.0','source_ranges':[]} for row in result.values()))

if __name__=='__main__':unittest.main()
