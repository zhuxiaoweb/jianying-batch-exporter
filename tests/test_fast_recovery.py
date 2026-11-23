from __future__ import annotations

import copy
import csv
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jyexport.fast import compose_bundle, compose_csv, compose_pair
from jyexport.storage import sha
from jyexport.workflow import complete_fast, native_plan_hash, next_steps, record_plan


def valid_probe():
    return {'full_decode': True, 'width': 1080, 'height': 1920, 'fps': '60',
            'audio_streams': 1, 'duration': .5, 'container': 'MP4'}


class NativeFastRecoveryTests(unittest.TestCase):
    def setUp(self):
        helpers = importlib.import_module('test_workflow')
        self.fixture = helpers.NativeWorkflowTests()
        self.fixture.setUp()
        self.fixture.master_receipts()
        self.folder, self.session, self.job = self.fixture.folder, self.fixture.session, self.fixture.job
        self.job['want_srt'] = False

    def tearDown(self):
        self.fixture.tearDown()

    def test_missing_registered_variant_stops_instead_of_compose_loop(self):
        item = self.job['variants']['V1']
        item.update(file=str(Path(self.job['directory'])/'missing.mp4'), sha256='previous-hash')
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'repair_required')
        with patch('jyexport.fast.compose_pair') as compose:
            with self.assertRaisesRegex(ValueError,'已登记文件缺失'):
                complete_fast(self.folder,self.session,self.job,'unused')
        compose.assert_not_called()
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'repair_required')

    def test_changed_registered_variant_stops_without_overwriting(self):
        path=Path(self.job['directory'])/'old.mp4';path.write_bytes(b'old-archive'*200)
        expected=sha(path);path.write_bytes(b'external-change'*200)
        self.job['variants']['V1'].update(file=str(path),sha256=expected,probe=valid_probe())
        original=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'哈希已变化'):
            complete_fast(self.folder,self.session,self.job,'unused')
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'repair_required')

    def test_unregistered_existing_variant_is_not_recomposed(self):
        item=self.job['variants']['V1']
        path=Path(self.job['directory'])/('V1_'+item['label']+'.mp4')
        path.write_bytes(b'previous-interrupted-run'*100)
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'register_existing_export')
        with self.assertRaisesRegex(ValueError,'未登记'):
            complete_fast(self.folder,self.session,self.job,'unused')
        self.assertEqual(path.read_bytes(),b'previous-interrupted-run'*100)

    def test_legacy_master_requires_explicit_plan_review(self):
        self.job['variants']['V2'].pop('native_plan_hash')
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'review_plan_required')
        with self.assertRaisesRegex(ValueError,'旧版母版证据'):
            complete_fast(self.folder,self.session,self.job,'unused')
        self.assertNotIn('native_plan_hash',self.job['variants']['V2'])

    def test_complete_legacy_batch_is_noop_without_upgrading_receipts(self):
        self.job['variants']['V2'].pop('native_plan_hash')
        self.job['variants']['V3'].pop('native_plan_hash')
        for code in ('V1','V4'):
            path=Path(self.job['directory'])/(code+'_native.mp4');path.write_bytes(b'prior native export'*200)
            self.job['variants'][code].update(file=str(path),sha256=sha(path),probe=valid_probe())
        with patch('jyexport.fast.compose_pair') as compose:
            result=complete_fast(self.folder,self.session,self.job,'unused')
        self.assertEqual(result['generated'],[])
        compose.assert_not_called()
        self.assertNotIn('native_plan_hash',self.job['variants']['V2'])
        self.assertTrue(next_steps(self.folder,self.session)['all_exports_present'])

    def test_unreadable_registered_file_becomes_repair_action(self):
        with patch('jyexport.workflow.sha',side_effect=PermissionError('file is locked')):
            action=next_steps(self.folder,self.session)['next'][0]
        self.assertEqual(action['action'],'repair_required')
        self.assertIn('无法读取',action['problems']['V2'])

    def test_changed_classification_invalidates_receipts_but_keeps_files(self):
        source=self.job['variants']['V2']['file'];old_hash=sha(source)
        self.job['variants']['V2']['reviewed']=True
        changed=self.fixture.plan()
        changed['bgm_targets'][0]['label']='different confirmed background music'
        record_plan(self.folder,self.session,self.job,changed)
        self.assertTrue(self.job['variants']['V2']['plan_review_required'])
        self.assertFalse(self.job['variants']['V2']['reviewed'])
        self.assertEqual(sha(source),old_hash)
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'review_plan_required')
        with self.assertRaisesRegex(ValueError,'分类计划已变更'):
            complete_fast(self.folder,self.session,self.job,'unused')

    def test_prose_and_observation_changes_do_not_invalidate_same_plan(self):
        previous=native_plan_hash(self.job['ui']['plan'])
        changed=self.fixture.plan()
        changed['observations']=['a fresh screen observation']
        changed['bgm_targets'][0]['evidence']='same classification, clearer explanation'
        record_plan(self.folder,self.session,self.job,changed)
        self.assertEqual(previous,native_plan_hash(self.job['ui']['plan']))
        self.assertNotIn('plan_review_required',self.job['variants']['V2'])
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'compose_missing_variants')

    def test_failed_second_variant_keeps_first_and_resumes_only_second(self):
        def generated(code):
            item=self.job['variants'][code]
            path=Path(self.job['directory'])/(code+'_'+item['label']+'.mp4')
            path.write_bytes((code+'-verified').encode()*200)
            return {'file':str(path),'sha256':sha(path),'probe':valid_probe(),
                'reviewed':False,'status':'规格通过·待声画审核',
                'video_source':'V3' if code=='V1' else 'V2',
                'audio_source':'V2' if code=='V1' else 'V3'}
        sources={code:{'file':self.job['variants'][code]['file'],
                       'sha256':self.job['variants'][code]['sha256']} for code in ('V2','V3')}
        def first_attempt(*args):
            args[7]('V1',generated('V1'),sources)
            raise ValueError('second variant decode failed')
        with patch('jyexport.fast.compose_pair',side_effect=first_attempt):
            with self.assertRaisesRegex(ValueError,'second variant'):
                complete_fast(self.folder,self.session,self.job,'unused')
        first_hash=self.job['variants']['V1']['sha256']
        self.assertIn('fast_failure',self.job['ui'])
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'compose_missing_variants')
        def second_attempt(*args):
            self.assertEqual(args[5],['V4'])
            item=generated('V4');args[7]('V4',item,sources)
            return {'variants':{'V4':item},'total_seconds':0.0}
        with patch('jyexport.fast.compose_pair',side_effect=second_attempt):
            complete_fast(self.folder,self.session,self.job,'unused')
        self.assertEqual(self.job['variants']['V1']['sha256'],first_hash)
        self.assertNotIn('fast_failure',self.job['ui'])
        self.assertTrue(next_steps(self.folder,self.session)['all_exports_present'])
        self.assertTrue(all(not item['reviewed'] for item in self.job['variants'].values()))


class PortableBatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jy_fast_recovery_')
        self.base=Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def listing(self,name,rows):
        path=self.base/name
        with path.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(['剧名','集号','V2','V3','SRT'])
            writer.writerows(rows)
        return path

    def test_batch_cannot_write_failure_summary_inside_draft(self):
        draft=self.base/'draft';draft.mkdir();(draft/'draft_content.json').write_text('original')
        listing=self.listing('tasks.csv',[['test','1','missing2.mp4','missing3.mp4','missing.srt']])
        with self.assertRaisesRegex(ValueError,'草稿内部'):
            compose_csv(listing,draft,'unused',True)
        self.assertEqual([path.name for path in draft.iterdir()],['draft_content.json'])

    def test_output_equal_to_source_file_is_rejected_before_writes(self):
        source=self.base/'source.mp4';source.write_bytes(b'original input')
        before=sha(source)
        with self.assertRaisesRegex(ValueError,'输入输出重叠'):
            compose_bundle(source,'other.mp4','missing.srt',source,'unused','test',True)
        self.assertEqual(sha(source),before)

    def test_short_csv_row_isolated_and_later_valid_row_continues(self):
        listing=self.listing('short.csv',[['short-row'],['test','2','2.mp4','3.mp4','s.srt']])
        with patch('jyexport.fast.compose_bundle',return_value={'directory':str(self.base/'complete')}) as compose:
            result=compose_csv(listing,self.base/'out','unused',True)
        self.assertEqual((result['completed'],result['failed']),(1,1))
        self.assertEqual(compose.call_count,1)
        self.assertIn('空字段',result['jobs'][0]['error'])
        self.assertTrue(Path(result['report']).is_file())

    def test_previous_output_cannot_feed_back_into_same_batch(self):
        output=self.base/'out'
        generated=output/'already-produced'
        listing=self.listing('feedback.csv',[
            ['test','1','2.mp4','3.mp4','s.srt'],
            ['test','2',str(generated/'V2.mp4'),'3b.mp4','b.srt']])
        with patch('jyexport.fast.compose_bundle',return_value={'directory':str(generated)}) as compose:
            result=compose_csv(listing,output,'unused',True)
        self.assertEqual((result['completed'],result['failed']),(1,1))
        self.assertEqual(compose.call_count,1)
        self.assertIn('同一批输入',result['jobs'][1]['error'])

    def test_changed_source_is_rejected_before_publish_or_registration(self):
        v2=self.base/'V2.mp4';v2.write_bytes(b'original2')
        v3=self.base/'V3.mp4';v3.write_bytes(b'original3')
        destination=self.base/'out'
        def evidence(path,app):
            if Path(path)==v2:return {'video':{'packet_and_time_sha256':'v2v'},'audio':{'packet_and_time_sha256':'v2a'}}
            if Path(path)==v3:return {'video':{'packet_and_time_sha256':'v3v'},'audio':{'packet_and_time_sha256':'v3a'}}
            return {'video':{'packet_and_time_sha256':'v3v'},'audio':{'packet_and_time_sha256':'v2a'}}
        def render(app,executable,arguments):
            Path(arguments[-1]).write_bytes(b'validated output')
            v2.write_bytes(b'input changed during render')
            return ''
        with patch('jyexport.fast.probe',return_value=valid_probe()), \
             patch('jyexport.fast.check_pair'), \
             patch('jyexport.fast.stream_evidence',side_effect=evidence), \
             patch('jyexport.fast._run',side_effect=render):
            callback=unittest.mock.Mock()
            with self.assertRaisesRegex(RuntimeError,'发生变化'):
                compose_pair(v2,v3,destination,'unused',variants=('V1',),on_variant=callback)
        callback.assert_not_called()
        self.assertFalse((destination/'V1_无背景音乐_无字幕.mp4').exists())

    def bundle_inputs(self):
        v2=self.base/'V2.mp4';v2.write_bytes(b'original2')
        v3=self.base/'V3.mp4';v3.write_bytes(b'original3')
        srt=self.base/'subtitle.srt'
        srt.write_text('1\n00:00:00,000 --> 00:00:00,400\noriginal\n',encoding='utf-8')
        sources={}
        for code,path in [('V2',v2),('V3',v3)]:
            stat=path.stat()
            sources[code]={'file':str(path),'sha256':sha(path),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns}
        report={'sources':sources,'variants':{
            code:{'file':'verified-derived','sha256':'verified','probe':valid_probe(),
                  'reviewed':False,'status':'规格通过·待声画审核'} for code in ('V1','V4')}}
        return v2,v3,srt,report

    def test_audit_write_failure_cannot_leave_success_receipt(self):
        v2,v3,srt,report=self.bundle_inputs()
        with patch('jyexport.fast.compose_pair',return_value=report), \
             patch('jyexport.fast.probe',return_value=valid_probe()), \
             patch('csv.writer',side_effect=PermissionError('audit file locked')):
            with self.assertRaisesRegex(PermissionError,'audit file locked'):
                compose_bundle(v2,v3,srt,self.base/'out','unused','test',True)
        folders=list((self.base/'out').iterdir());self.assertEqual(len(folders),1)
        status=json.loads((folders[0]/'交付状态.json').read_text(encoding='utf-8'))
        self.assertFalse(status['complete'])
        self.assertIn('失败',status['status'])
        self.assertFalse((folders[0]/'两母版交付报告.json').exists())

    def test_srt_change_during_processing_stops_bundle(self):
        v2,v3,srt,report=self.bundle_inputs()
        def changed(*args,**kwargs):
            srt.write_text('1\n00:00:00,000 --> 00:00:00,400\nchanged\n',encoding='utf-8')
            return report
        with patch('jyexport.fast.compose_pair',side_effect=changed), \
             patch('jyexport.fast.probe',return_value=valid_probe()):
            with self.assertRaisesRegex(RuntimeError,'发生变化'):
                compose_bundle(v2,v3,srt,self.base/'out','unused','test',True)
        folder=next((self.base/'out').iterdir())
        status=json.loads((folder/'交付状态.json').read_text(encoding='utf-8'))
        self.assertFalse(status['complete'])
        self.assertFalse((folder/'两母版交付报告.json').exists())


if __name__=='__main__':
    unittest.main(verbosity=2)
