"""Core adoption admits only private staging or the frozen per-version target."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from jyexport.advanced import REVISION,delivery_plan,digest,override_contract
from jyexport.discovery import Draft
from jyexport.draft import VARIANTS
from jyexport.engine import _adopt_bundle
from jyexport.storage import sha


class LayoutPlanDigestTests(unittest.TestCase):
    def job(self):
        return {'roles':{'voice':'保留原声音','bgm':'背景音乐'},'want_srt':True}

    def test_existing_batch_digest_is_byte_identical_without_layout_field(self):
        job=self.job()
        expected=digest({'revision':REVISION,'roles':job['roles'],
            'overrides':override_contract({}),'hardware':True})
        self.assertEqual(delivery_plan(job),expected)

    def test_grouped_destinations_are_frozen_into_plan_digest(self):
        job=self.job();before=delivery_plan(job)
        job['archive_layout']={'mode':'series_v1','targets':{'V1':'target-a.mp4'}}
        grouped=delivery_plan(job);self.assertNotEqual(before,grouped)
        job['archive_layout']['targets']['V1']='target-b.mp4'
        self.assertNotEqual(grouped,delivery_plan(job))

    def test_existing_confirmed_srt_omission_retains_the_legacy_digest(self):
        job=self.job();job.update(want_srt=False,roles_content_sha256='content',
            delivery_options={'no_subtitles_confirmed':True,'source_sha256':'content'})
        from jyexport.delivery_contract import omitted_srt
        expected=digest({'revision':REVISION,'roles':job['roles'],'overrides':{},
            'hardware':False,'srt_omission':omitted_srt(job)})
        self.assertEqual(delivery_plan(job,False),expected)


class GroupedAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.folder=self.root/'batch';self.folder.mkdir()
        self.draft=Draft('case','series',str(self.root/'source'),str(self.root/'source/draft_content.json'),
            'timeline','时间线01',False,1000000)
        from jyexport.archive_layout import prepare_layout
        self.layout=prepare_layout(self.folder,self.draft,'0123456789',[self.draft])
        self.directory=self.folder/'任务记录'/'0123456789';self.directory.mkdir(parents=True)
        self.job={'id':'0123456789','draft':self.draft.dict(),'directory':str(self.directory),
            'archive_layout':self.layout,'want_srt':True,
            'variants':{key:{'reviewed':False} for key in VARIANTS},'srt':None}
        self.session={'events':[]};self.persist=Mock()
        self.report={'complete':True,'variants':{},'srt':None}
        for key in VARIANTS:
            path=Path(self.layout['targets'][key]);path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes((key+' state-only verified receipt').encode())
            self.report['variants'][key]={'file':str(path),'sha256':sha(path),
                'probe':{'full_decode':True},'status':'待审核','reviewed':False}
        path=Path(self.layout['targets']['SRT']);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('1\n00:00:00,000 --> 00:00:01,000\nCaption\n','utf-8')
        self.report['srt']={'file':str(path),'sha256':sha(path),'status':'待审核'}
        self.record={'run_directory':str(self.folder/'后台引擎工作区/case')}

    def tearDown(self):self.temp.cleanup()

    def adopt(self):
        return _adopt_bundle(self.folder,self.session,self.job,self.report,self.record,persist=self.persist)

    def test_exact_final_targets_are_adopted_and_review_is_not_implicitly_approved(self):
        self.adopt();self.persist.assert_called_once()
        self.assertTrue(self.job['headless']['complete'])
        self.assertFalse(self.job['variants']['V1']['reviewed'])
        self.assertFalse(self.job['srt']['reviewed'])

    def test_private_staging_receipt_remains_valid_before_publication(self):
        for key,item in [*self.report['variants'].items(),('SRT',self.report['srt'])]:
            path=self.directory/(key+('.srt' if key=='SRT' else '.mp4'))
            path.write_bytes(Path(item['file']).read_bytes());item['file']=str(path)
        self.adopt();self.assertTrue(self.record['complete'])

    def test_other_episode_path_in_shared_project_is_rejected_even_with_matching_sha(self):
        item=self.report['variants']['V1'];path=Path(item['file']).with_name('another-episode.mp4')
        path.write_bytes(Path(item['file']).read_bytes());item['file']=str(path)
        with self.assertRaisesRegex(ValueError,'路径越界'):self.adopt()
        self.persist.assert_not_called()

    def test_cross_version_path_is_rejected_by_its_exact_variant_key(self):
        self.report['variants']['V1']=copy.deepcopy(self.report['variants']['V2'])
        with self.assertRaisesRegex(ValueError,'路径越界'):self.adopt()
        self.persist.assert_not_called()

    def test_tampered_layout_is_rejected_before_adopting_files(self):
        self.job['archive_layout']['targets']['V1']=str(self.root/'outside.mp4')
        with self.assertRaises(ValueError):self.adopt()
        self.persist.assert_not_called()

    def test_file_hash_and_full_decode_evidence_still_required(self):
        self.report['variants']['V1']['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'缺失、改变或路径越界'):self.adopt()
        self.persist.assert_not_called()
        self.report['variants']['V1']['sha256']=sha(self.report['variants']['V1']['file'])
        self.report['variants']['V1']['probe']['full_decode']=False
        with self.assertRaisesRegex(ValueError,'完整解码证明'):self.adopt()

    def test_legacy_adoption_still_rejects_files_outside_private_episode_directory(self):
        self.job.pop('archive_layout')
        with self.assertRaisesRegex(ValueError,'路径越界'):self.adopt()
        self.persist.assert_not_called()


if __name__=='__main__':unittest.main()
