import copy,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock

from jyexport.delivery_service import prepare,confirm,analyze,verified_delivery
from jyexport.delivery_contract import missing_features,omitted_srt
from jyexport.discovery import Draft
from jyexport.engine import model_changes
from jyexport.storage import SourceChanged,stable_snapshot,sha
from jyexport.easy_ui import current_drafts,refresh_selection
from tests.test_advanced import document,Reader


class MissingTrackPlans(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir();self.content=self.source/'draft_content.json'
        self.adapter=Reader(self.root,self.root)
    def tearDown(self):self.temp.cleanup()
    def setup_batch(self,mutation):
        data=document(self.source);mutation(data);self.content.write_text(json.dumps(data),'utf-8')
        draft=Draft('key','draft',str(self.source),str(self.content),'episode','1',False)
        self.folder,self.session=prepare([draft],self.root/'out',self.adapter)
        self.job=self.session['jobs'][0]
        return self.job.get('easy_analysis',{})
    def test_no_subtitles_requires_confirmation_and_survives_resume(self):
        analysis=self.setup_batch(lambda d:d.update(tracks=[t for t in d['tracks'] if t['type']!='text']))
        original=sha(self.content)
        with self.assertRaisesRegex(ValueError,'可导出字幕'):confirm(self.folder,self.session,self.job,[],[],True)
        self.assertTrue(self.job['want_srt']);self.assertFalse(self.job['easy_confirmed'])
        confirm(self.folder,self.session,self.job,[],[],True,no_captions_confirmed=True)
        self.assertFalse(self.job['want_srt']);self.assertTrue(omitted_srt(self.job)['confirmed'])
        self.assertFalse(any(Path(self.folder).rglob('*.srt')))
        self.assertIn('不生成SRT',(self.folder/'审核归档总表.csv').read_text('utf-8-sig'))
        analyze(self.folder,self.session,self.adapter)
        self.assertTrue(self.job['easy_confirmed']);self.assertEqual(original,sha(self.content))
    def test_video_original_audio_is_not_mistaken_for_missing_sound(self):
        a=self.setup_batch(lambda d:d.update(tracks=[t for t in d['tracks'] if t['type']!='audio']))
        self.assertTrue(missing_features(a,a['caption_keys'])['no_audio_tracks'])
        with self.assertRaisesRegex(ValueError,'视频原声'):confirm(self.folder,self.session,self.job,[],a['caption_keys'],True)
        confirm(self.folder,self.session,self.job,[],a['caption_keys'],True,no_audio_confirmed=True)
        self.assertTrue(self.job['want_srt']);self.assertTrue(self.job['delivery_options']['no_audio_tracks_confirmed'])
    def test_invisible_empty_or_outside_timeline_text_does_not_promise_srt(self):
        for mutation in ('hidden','empty','outside'):
            with self.subTest(mutation=mutation):
                def change(d):
                    segment=d['tracks'][-1]['segments'][0]
                    if mutation=='hidden':segment['visible']=False
                    elif mutation=='empty':d['materials']['texts'][0]['content']=''
                    else:segment['target_timerange']['start']=9000000
                a=self.setup_batch(change)
                self.assertTrue(missing_features(a,a['caption_keys'])['no_subtitles'])
                confirm(self.folder,self.session,self.job,[],a['caption_keys'],True,no_captions_confirmed=True)
                self.assertFalse(self.job['want_srt'])
    def test_empty_draft_has_actionable_message(self):
        self.setup_batch(lambda d:d.update(tracks=[],duration=0))
        self.assertIn('添加素材并保存',self.job['easy_analysis_error'])
    def test_subtitle_omission_cannot_be_forged_by_toggling_want_srt(self):
        self.setup_batch(lambda d:None);self.job['want_srt']=False
        with self.assertRaisesRegex(ValueError,'尚未确认'):omitted_srt(self.job)


class ChannelDefaults(unittest.TestCase):
    def doc(self,type_value='',mapping=0):
        return {'materials':{'sound_channel_mappings':[{'id':'a','type':type_value,'audio_channel_mapping':mapping,'is_config_open':False}]}}
    def test_known_inactive_aliases_only(self):
        for alias in ('','sound_channel_mapping'):
            source=self.doc(alias);before=copy.deepcopy(source)
            changes,evidence=model_changes(source,self.doc('none'))
            self.assertEqual(changes,[]);self.assertEqual(source,before);self.assertEqual(evidence[0]['before'],alias)

    def test_missing_default_fields_are_still_inactive_aliases(self):
        source={'materials':{'sound_channel_mappings':[{'id':'a','type':''}]}}
        before=copy.deepcopy(source)
        changes,evidence=model_changes(source,self.doc('none'))
        self.assertEqual(changes,[]);self.assertEqual(source,before);self.assertEqual(evidence[0]['before'],'')
    def test_unknown_types_and_actual_channel_changes_remain_blocked(self):
        for source,target in ((self.doc('unexpected'),self.doc('none')),
                              (self.doc('',1),self.doc('none',1)),
                              (self.doc('none',1),self.doc('none',0))):
            self.assertTrue(model_changes(source,target)[0])
        source=self.doc();source['materials']['sound_channel_mappings'][0]['extra_active_field']=12
        self.assertTrue(model_changes(source,self.doc('none'))[0])


class LiveDraftRefresh(unittest.TestCase):
    def test_rescan_after_save_uses_current_metadata_and_preserves_identity(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);project=root/'project';project.mkdir();(project/'draft_content.json').write_text('{"duration":100}')
            (project/'draft_meta_info.json').write_text('{"draft_name":"before"}')
            with patch('jyexport.easy_ui.roots_and_index',return_value=([],{})) as read_index:
                first=current_drafts([root],root)[0][0]
                (project/'draft_meta_info.json').write_text('{"draft_name":"after"}')
                (project/'draft_content.json').write_text('{"duration":200}')
                latest=refresh_selection([first],[root],root)[0]
                self.assertEqual((latest.name,latest.duration),('after',200));self.assertEqual(read_index.call_count,2)
                (project/'draft_content.json').unlink()
                with self.assertRaisesRegex(ValueError,'尚未保存'):refresh_selection([first],[root],root)
    def test_transient_save_retries_without_reusing_partial_backup(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);source=root/'source';source.mkdir();(source/'draft_content.json').write_text('unchanged')
            from jyexport.storage import snapshot
            calls=[]
            def saving(src,dst,cancel,log):
                calls.append(Path(dst))
                if len(calls)==1:
                    Path(dst).mkdir();(Path(dst)/'partial').write_bytes(b'partial')
                    raise SourceChanged('saving')
                return snapshot(src,dst,cancel,log)
            with patch('jyexport.storage.snapshot',side_effect=saving),patch('jyexport.storage.time.sleep'):
                result=stable_snapshot(source,root/'backup')
            self.assertNotEqual(result['backup'],str(calls[0]));self.assertTrue((calls[0]/'partial').is_file())
            self.assertEqual(sha(source/'draft_content.json'),sha(Path(result['backup'])/'draft_content.json'))


if __name__=='__main__':unittest.main()
