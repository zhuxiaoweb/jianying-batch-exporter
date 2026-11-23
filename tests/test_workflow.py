from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from jyexport.discovery import Draft, _path_value, app_dirs, resolve_app_dir, roots_and_index
from jyexport.storage import sha
from jyexport.workflow import bind_native_copy, complete_fast, native_plan_hash, next_steps, prepare_native, record_native_export, record_plan, select_drafts, verify_backup

class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jy_discovery_test_');self.base=Path(self.temp.name)
        self.install=self.base/'apps/jianying/JianyingPro'
        for version in ('11.7.0.14652','11.8.0.15000'):
            path=self.install/version;path.mkdir(parents=True);(path/'videoeditor.dll').touch()

    def tearDown(self):self.temp.cleanup()

    def test_registry_install_root_resolves_even_from_desktop(self):
        with patch('jyexport.discovery.registry_install_roots',return_value=[self.install]):
            found=resolve_app_dir('',self.base/'Desktop')
        self.assertEqual(found,str(self.install/'11.8.0.15000'))

    def test_packaged_exe_ancestor_discovers_install_sibling(self):
        with patch('jyexport.discovery.registry_install_roots',return_value=[]):
            found=app_dirs(self.install.parent/'JianyingExportAssistant/dist/剪映交付助手')
        self.assertEqual(found[0],str(self.install/'11.8.0.15000'))

    def test_selecting_install_root_finds_version_child(self):
        self.assertEqual(resolve_app_dir(self.install),str(self.install/'11.8.0.15000'))

    def test_selecting_dll_or_exe_is_normalized(self):
        self.assertEqual(_path_value(str(self.install/'uninst.exe')+',0'),self.install)
        self.assertEqual(resolve_app_dir(self.install/'11.7.0.14652/videoeditor.dll'),str(self.install/'11.7.0.14652'))

    def test_missing_install_location_uses_registry_uninstall_path(self):
        path=_path_value('"'+str(self.install/'uninst.exe')+'" /S')
        with patch('jyexport.discovery.registry_install_roots',return_value=[path]):
            self.assertEqual(resolve_app_dir(),str(self.install/'11.8.0.15000'))

    def test_desktop_is_not_a_library_due_to_exe_location(self):
        user=self.base/'user';desktop=self.base/'Desktop';desktop.mkdir()
        with patch('jyexport.discovery.user_data',return_value=user):
            roots,index=roots_and_index(desktop)
        self.assertNotIn(str(desktop),roots)

    def test_indexed_project_does_not_promote_parent_directory(self):
        user=self.base/'user';default=user/'Projects/com.lveditor.draft';default.mkdir(parents=True)
        project=self.base/'Desktop/项目';project.mkdir(parents=True);(project/'draft_content.json').write_text('cipher')
        (default/'root_meta_info.json').write_text(json.dumps({'all_draft_store':[{'draft_fold_path':str(project),'draft_name':'项目'}]}),'utf-8')
        with patch('jyexport.discovery.user_data',return_value=user):roots,index=roots_and_index()
        self.assertIn(str(project),roots);self.assertNotIn(str(project.parent),roots)

class NativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jy_native_test_');self.base=Path(self.temp.name)
        self.source=self.base/'drafts/original';self.source.mkdir(parents=True)
        (self.source/'draft_content.json').write_text('encrypted-root-content','utf-8')
        content=self.source/'Timelines/one/draft_content.json';content.parent.mkdir(parents=True);content.write_text('encrypted-timeline-content','utf-8')
        (self.source/'Timelines/project.json').write_text(json.dumps({'timelines':[{'id':'one','name':'1'}]}),'utf-8')
        self.draft=Draft('k','剧名',str(self.source),str(content),'one','1',True,0,0)
        self.index=[{'draft_name':'剧名','draft_id':'original-id','draft_fold_path':str(self.source)}]
        self.fake_user=self.base/'user';self.fake_user.mkdir()
        self.patcher=patch('jyexport.workflow.current_index',side_effect=lambda:self.index);self.patcher.start()
        self.user_patcher=patch('jyexport.workflow.user_data',return_value=self.fake_user);self.user_patcher.start()
        self.folder,self.session=prepare_native([self.draft],self.base/'out',['V1','V2','V3','V4'],True,{'app_dir':'not-used'})
        self.job=self.session['jobs'][0]

    def tearDown(self):
        self.user_patcher.stop();self.patcher.stop();self.temp.cleanup()

    def make_copy(self):
        dest=self.base/'drafts/native-copy';dest.mkdir()
        (dest/'draft_content.json').write_text('encrypted-copy','utf-8')
        timeline=dest/'Timelines/new-timeline';timeline.mkdir(parents=True);(timeline/'draft_content.json').write_text('encrypted-copy-timeline','utf-8')
        (dest/'Timelines/project.json').write_text(json.dumps({'timelines':[{'id':'new-timeline','name':'1'}]}),'utf-8')
        self.index.append({'draft_id':'new-id','draft_name':self.job['ui']['copy_name'],'draft_fold_path':str(dest)})
        bind_native_copy(self.folder,self.session,self.job,self.job['ui']['copy_name'])
        return dest

    def plan(self):
        return {'copy_name':self.job['ui']['copy_name'],'timeline_name':'1','duration_us':500000,'ratio':'9:16','bgm_present':True,'subtitles_present':True,
            'bgm_targets':[{'label':'BGM 1','scope':'track','contains_other_content':False,'evidence':'synthetic test role'}],
            'subtitle_targets':[{'label':'字幕1','scope':'track','contains_other_content':False,'evidence':'synthetic test role'}],
            'observations':['synthetic test observation']}

    def test_encrypted_backup_and_queue_do_not_need_decryption(self):
        self.assertTrue(verify_backup(self.session,self.job))
        self.assertEqual(Path(self.job['backup_content']).read_text(),'encrypted-timeline-content')
        self.assertFalse(next_steps(self.folder,self.session)['all_exports_present'])
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'create_native_copy')

    def test_original_id_cannot_be_bound_as_native_copy(self):
        self.index[0]['draft_name']=self.job['ui']['copy_name']
        with self.assertRaises(ValueError):bind_native_copy(self.folder,self.session,self.job,self.job['ui']['copy_name'])

    def test_new_id_at_original_path_is_rejected(self):
        self.index.append({'draft_id':'new-id','draft_name':self.job['ui']['copy_name'],'draft_fold_path':str(self.source)})
        with self.assertRaises(ValueError):bind_native_copy(self.folder,self.session,self.job,self.job['ui']['copy_name'])

    def test_native_copy_mapping_and_plan_resume(self):
        dest=self.make_copy()
        self.assertEqual(self.job['ui']['copy']['timeline_id'],'new-timeline')
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'inspect_native_timeline')
        record_plan(self.folder,self.session,self.job,self.plan())
        next_item=next_steps(self.folder,self.session)['next'][0]
        self.assertEqual(next_item['variant'],'V3');self.assertEqual(next_item['pending'],['V3','V2','SRT','V1','V4'])
        self.assertEqual((self.source/'Timelines/one/draft_content.json').read_text(),'encrypted-timeline-content')

    def test_plan_for_other_episode_is_rejected(self):
        self.make_copy();plan=self.plan();plan['timeline_name']='2'
        with self.assertRaises(ValueError):record_plan(self.folder,self.session,self.job,plan)

    def test_mixed_audio_track_cannot_be_muted_whole(self):
        self.make_copy();plan=self.plan();plan['bgm_targets'][0]['contains_other_content']=True
        with self.assertRaises(ValueError):record_plan(self.folder,self.session,self.job,plan)

    def test_unbound_plan_and_export_are_rejected(self):
        with self.assertRaises(ValueError):record_plan(self.folder,self.session,self.job,self.plan())
        with self.assertRaises(ValueError):record_native_export(self.folder,self.session,self.job,'V4','missing.mp4','',{})

    def test_wrong_variant_state_stops_before_media_archive(self):
        self.make_copy();record_plan(self.folder,self.session,self.job,self.plan())
        evidence={'copy_name':self.job['ui']['copy_name'],'timeline_name':'1','export_finished':True,'observations':['test'],'bgm_enabled':False,'subtitles_enabled':True,'settings':self.session['specification']}
        with self.assertRaises(ValueError):record_native_export(self.folder,self.session,self.job,'V4','missing.mp4','',evidence)
        self.assertIsNone(self.job['variants']['V4']['file'])

    def test_placeholder_files_do_not_count_as_complete(self):
        self.make_copy();record_plan(self.folder,self.session,self.job,self.plan())
        for key,item in self.job['variants'].items():
            path=Path(self.job['directory'])/(key+'.mp4');path.touch();item.update(file=str(path),sha256=sha(path))
        self.assertFalse(next_steps(self.folder,self.session)['all_exports_present'])

    def test_pending_music_decision_blocks_no_bgm_export(self):
        self.make_copy();plan=self.plan();plan['pending_decisions']=['短音乐归类待确认']
        record_plan(self.folder,self.session,self.job,plan)
        self.assertEqual(self.job['variants']['V1']['status'],'待短音乐分类')
        with self.assertRaisesRegex(ValueError,'短音乐分类'):
            record_native_export(self.folder,self.session,self.job,'V1','missing.mp4','',{})

    def master_receipts(self):
        self.make_copy();record_plan(self.folder,self.session,self.job,self.plan())
        for key,bgm,sub in [('V2',False,True),('V3',True,False)]:
            path=Path(self.job['directory'])/(key+'.mp4');path.write_bytes(b'test-master'*200)
            self.job['variants'][key].update(file=str(path),sha256=sha(path),probe={
                'full_decode':True,'width':1080,'height':1920,'fps':'60','audio_streams':1},
                native_plan_hash=native_plan_hash(self.job['ui']['plan']),
                native_ui_evidence={'copy_name':self.job['ui']['copy_name'],'timeline_name':'1',
                'settings':self.session['specification'],'bgm_enabled':bgm,'subtitles_enabled':sub,'export_finished':True})

    def test_fast_completion_requires_native_master_receipts(self):
        self.master_receipts();self.job['variants']['V3'].pop('native_ui_evidence')
        with self.assertRaisesRegex(ValueError,'原生导出依据'):
            complete_fast(self.folder,self.session,self.job,'unused')
        self.assertIsNone(self.job['variants']['V1']['file'])

    def test_fast_route_requires_present_srt_before_composition(self):
        self.master_receipts()
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['variant'],'SRT')
        path=Path(self.job['directory'])/'subtitle.srt';path.write_text('fixture')
        self.job['srt']={'file':str(path),'sha256':sha(path)}
        self.assertEqual(next_steps(self.folder,self.session)['next'][0]['action'],'compose_missing_variants')

    def test_partial_fast_success_is_saved_before_next_variant_failure(self):
        self.master_receipts()
        def partial(*args):
            target=Path(self.job['directory'])/'V1_无背景音乐_无字幕.mp4';target.write_bytes(b'fixture'*200)
            item={'file':str(target),'sha256':sha(target),'probe':{'full_decode':True},'reviewed':False,'status':'规格通过·待声画审核',
                  'video_source':'V3','audio_source':'V2'}
            sources={key:{'file':self.job['variants'][key]['file'],'sha256':self.job['variants'][key]['sha256']} for key in ('V2','V3')}
            args[7]('V1',item,sources)
            raise ValueError('V4 decode failed')
        with patch('jyexport.fast.compose_pair',side_effect=partial):
            with self.assertRaisesRegex(ValueError,'V4 decode'):
                complete_fast(self.folder,self.session,self.job,'unused')
        saved=json.loads((self.folder/'任务清单.json').read_text(encoding='utf-8'))['jobs'][0]
        self.assertIsNotNone(saved['variants']['V1']['file'])
        self.assertFalse(saved['variants']['V1']['reviewed'])
        self.assertIsNone(saved['variants']['V4']['file'])

    def test_multiepisode_selection_requires_explicit_scope(self):
        another=Draft('other','剧名',str(self.source),'unused','other','2',True)
        with self.assertRaises(ValueError):select_drafts([self.draft,another],'剧名')
        self.assertEqual(select_drafts([self.draft,another],'剧名',['2']),[another])

if __name__=='__main__':unittest.main(verbosity=2)
