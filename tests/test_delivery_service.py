import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from jyexport.delivery_service import prepare,confirm,run,selection_roles,final_folder,analyze
from jyexport.discovery import Draft
from jyexport.storage import sha
from tests.test_advanced import document,Reader


class DirectDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir()
        self.content=self.source/'draft_content.json';self.content.write_text(json.dumps(document(self.source)),'utf-8')
        self.adapter=Reader(self.root,self.root)
        self.draft=Draft('case','case',str(self.source),str(self.content),'episode','18',False,3000000)
        self.folder,self.session=prepare([self.draft],self.root/'output',self.adapter)
        self.job=self.session['jobs'][0];self.rows=self.job['easy_analysis']['rows']
        self.music=next(row['key'] for row in self.rows if row['name']=='music')
        self.caption=self.job['easy_analysis']['caption_keys']
    def tearDown(self):self.temp.cleanup()

    def test_prepare_contains_all_five_outputs_and_no_source_changes(self):
        self.assertEqual(set(self.job['variants']),{'V1','V2','V3','V4'})
        self.assertTrue(self.job['want_srt']);self.assertFalse(self.job['easy_confirmed'])
        self.assertEqual(sha(self.content),sha(self.job['backup_content']))
        self.assertEqual(len(self.caption),1)

    def test_one_bgm_selection_preserves_all_other_audio_and_internal_effect(self):
        roles=confirm(self.folder,self.session,self.job,[self.music],self.caption)
        self.assertEqual(roles[self.music],'背景音乐')
        self.assertEqual(sum(value=='背景音乐' for value in roles.values()),1)
        for row in self.rows:
            if row['kind']=='audio' and row['key']!=self.music:self.assertEqual(roles[row['key']],'保留原声音')
        self.assertTrue(self.job['easy_confirmed'])

    def test_empty_bgm_selection_requires_explicit_no_music_confirmation(self):
        with self.assertRaisesRegex(ValueError,'没有独立背景音乐'):selection_roles(self.rows,[],self.caption)
        roles=selection_roles(self.rows,[],self.caption,True)
        self.assertNotIn('背景音乐',roles.values())
        with self.assertRaisesRegex(ValueError,'取消'):selection_roles(self.rows,[self.music],self.caption,True)

    def test_no_captions_and_container_selection_cannot_silently_continue(self):
        with self.assertRaisesRegex(ValueError,'未选择最终字幕'):selection_roles(self.rows,[self.music],[])
        container=next(row['key'] for row in self.rows if row['is_container'])
        with self.assertRaisesRegex(ValueError,'复合容器'):selection_roles(self.rows,[container],self.caption)

    def test_saved_exact_source_plan_resumes_without_another_confirmation(self):
        confirm(self.folder,self.session,self.job,[self.music],self.caption)
        analyze(self.folder,self.session,self.adapter)
        self.assertTrue(self.job['easy_confirmed'])

    def test_analysis_error_names_episode_and_is_cleared_on_successful_resume(self):
        from jyexport.workflow import read_batch
        with patch.object(self.adapter,'read',side_effect=ValueError('unverified voice mode')):
            analyze(self.folder,self.session,self.adapter)
        _,saved=read_batch(self.folder)
        self.assertEqual(saved['jobs'][0]['easy_analysis_error'],'unverified voice mode')
        self.assertFalse(saved['jobs'][0]['easy_confirmed'])
        analyze(self.folder,self.session,self.adapter)
        self.assertNotIn('easy_analysis_error',self.job)

    def publish(self,*args,**kwargs):
        job=args[2] if len(args)>2 else self.job
        directory=Path(job['directory'])/'快速交付';directory.mkdir()
        for key,value in job['variants'].items():
            file=directory/(key+'.mp4');file.write_bytes((key+'state only receipt').encode())
            value.update(file=str(file),sha256=sha(file))
        caption=directory/'字幕.srt';caption.write_text('1\n00:00:00,000 --> 00:00:01,000\nFinal\n')
        job['srt']={'file':str(caption),'sha256':sha(caption)}

    def test_single_operation_calls_renderer_and_returns_final_folder(self):
        confirm(self.folder,self.session,self.job,[self.music],self.caption)
        with patch('jyexport.delivery_service.render_job',side_effect=self.publish) as render:
            result=run(self.folder,self.session,self.adapter,self.root)
        render.assert_called_once()
        self.assertEqual(result['completed'],1);self.assertEqual(result['failed'],0)
        self.assertEqual(Path(result['jobs'][0]['directory']),final_folder(self.job))
        self.assertEqual(len(list(final_folder(self.job).glob('*.mp4'))),4)
        self.assertEqual(len(list(final_folder(self.job).glob('*.srt'))),1)

    def test_unconfirmed_batch_never_renders(self):
        with patch('jyexport.delivery_service.render_job') as render:
            with self.assertRaisesRegex(ValueError,'尚未确认'):run(self.folder,self.session,self.adapter,self.root)
        render.assert_not_called()

    def test_changed_output_is_not_presented_as_finished_delivery(self):
        confirm(self.folder,self.session,self.job,[self.music],self.caption)
        with patch('jyexport.delivery_service.render_job',side_effect=self.publish):run(self.folder,self.session,self.adapter,self.root)
        Path(self.job['variants']['V1']['file']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'改变'):final_folder(self.job)

    def test_long_but_valid_report_destination_does_not_grow_temp_name_past_windows_limit(self):
        from jyexport.storage import atomic_json
        count=210-len(str(self.root.resolve()))-1
        if count<=0:self.skipTest('test temporary root already too long')
        directory=self.root/('p'*count);directory.mkdir()
        target=directory/'两母版交付报告.json'
        self.assertLess(len(str(target)),260)
        atomic_json(target,{'complete':True,'message':'中文报告'})
        self.assertTrue(json.loads(target.read_text('utf-8'))['complete'])


if __name__=='__main__':unittest.main()
