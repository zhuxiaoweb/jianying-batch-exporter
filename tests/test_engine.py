from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jyexport.draft import inspect, transform
from jyexport.engine import EngineAdapter, classify_standard_tracks, ensure_timeline_identity, external_resource_snapshot, inspect_capabilities, meaningful_changes, referenced_material_ids, relocate_document, render_projection, timeline_srt, verify_external_resources


def document():
    def segment(sid, mid, start=0, duration=1000000):
        return {'id': sid, 'material_id': mid, 'target_timerange': {'start':start,'duration':duration},
                'volume': .31, 'common_keyframes': [{'time':10,'value':.4}]}
    return {'id':'episode','new_version':'164.0.0','duration':10000000,
        'tracks':[{'id':'audio','type':'audio','segments':[segment('bgm','music'),segment('short','short')]},
                  {'id':'text','type':'text','segments':[segment('caption','caption',8066666,1000000),segment('title','title')]}],
        'materials':{'audios':[{'id':'music','type':'music'},{'id':'short','type':'extract_music'}],
                     'texts':[{'id':'caption','type':'subtitle','content':json.dumps({'text':'Hola\\nmundo','styles':[]})},
                              {'id':'title','type':'text','content':'Episode title'}]}}


def roles():
    return {'audio:bgm':'背景音乐','audio:short':'保留原声音','text:caption':'字幕','text:title':'标题/其他文字'}


class EngineSemanticTests(unittest.TestCase):
    def test_retained_audio_keeps_keyframes_and_title(self):
        source=document();before=copy.deepcopy(source)
        v1=transform(source,roles(),'V1')
        self.assertEqual(v1['tracks'][0]['segments'],[source['tracks'][0]['segments'][1]])
        self.assertEqual(v1['tracks'][1]['segments'],[source['tracks'][1]['segments'][1]])
        self.assertEqual(source,before)

    def test_native_srt_truncates_millisecond_and_preserves_edited_text(self):
        result=timeline_srt(document(),roles())
        self.assertIn('00:00:08,066 --> 00:00:09,066\nHola\nmundo',result)
        self.assertNotIn('Episode title',result)

    def test_hidden_subtitle_is_not_exported_as_srt(self):
        data=document();data['tracks'][1]['segments'][0]['visible']=False
        self.assertEqual(timeline_srt(data,roles()),'')

    def test_added_native_defaults_are_safe(self):
        source={'enabled':True,'gain':.31,'tracks':[{'id':'a','duration':1000000}]}
        normalized={**source,'native_default':None,'tracks':[{**source['tracks'][0],'new_default':0}]}
        self.assertEqual(meaningful_changes(source,normalized),[])

    def test_unknown_active_property_cannot_disappear(self):
        self.assertEqual(meaningful_changes({'new_effect':{'enable':True}},{}),['/new_effect:missing'])

    def test_integer_timing_and_enum_boolean_type_are_exact(self):
        self.assertTrue(meaningful_changes({'time':1000000000000000},{'time':1000000000000001}))
        self.assertTrue(meaningful_changes({'enabled':True},{'enabled':1}))
        self.assertEqual(meaningful_changes({'volume':.31},{'volume':.31000000000000005}),[])

    def test_rich_text_is_compared_verbatim(self):
        source={'content':'{"text":"Hola","size":14}'}
        self.assertTrue(meaningful_changes(source,{'content':'{"text":"Hola","size":15}'}))

    def test_list_loss_or_reordering_is_rejected(self):
        self.assertTrue(meaningful_changes([{'id':'a'},{'id':'b'}],[{'id':'b'},{'id':'a'}]))
        self.assertTrue(meaningful_changes([1,2],[1]))

    def test_unused_ai_descriptor_does_not_block_render(self):
        data=document()
        data['materials']['videos']=[{'id':'unused','matting':{'flag':1}}]
        self.assertEqual(inspect_capabilities(data)['blocked'],[])

    def test_active_matting_is_blocked_but_inactive_cache_is_not(self):
        data=document();data['materials']['audios'][0]['matting']={'path':'old-cache','custom_matting_id':'old-id','flag':0}
        self.assertEqual(inspect_capabilities(data)['blocked'],[])
        data['materials']['audios'][0]['matting']['flag']=1
        self.assertTrue(inspect_capabilities(data)['blocked'])

    def test_active_vocal_processing_in_extra_reference_is_blocked(self):
        data=document();data['tracks'][0]['segments'][0]['extra_material_refs']=['voice-process']
        data['materials']['vocal_separations']=[{'id':'voice-process','type':'vocal_separation','choice':1}]
        self.assertTrue(inspect_capabilities(data)['blocked'])
        data['materials']['vocal_separations'][0]['choice']=0
        self.assertEqual(inspect_capabilities(data)['blocked'],[])

    def test_missing_referenced_effect_is_not_silently_ignored(self):
        data=document();data['tracks'][0]['segments'][0]['extra_material_refs']=['missing']
        self.assertTrue(inspect_capabilities(data)['blocked'])

    def test_standard_naming_maps_music_voice_ambience_short_music_and_titles(self):
        data=document();music,short=data['tracks'][0]['segments'];caption,title=data['tracks'][1]['segments']
        data['tracks']=[{'id':'bgm','type':'audio','name':'BGM 1','segments':[music]},
                        {'id':'short','type':'audio','name':'短音乐 1','segments':[short]},
                        {'id':'captions','type':'text','name':'台词字幕','segments':[caption]},
                        {'id':'titles','type':'text','name':'标题','segments':[title]}]
        before=copy.deepcopy(data)
        self.assertEqual(classify_standard_tracks(data),{'bgm:bgm':'背景音乐','short:short':'音效','captions:caption':'字幕','titles:title':'标题/其他文字'})
        self.assertEqual(data,before)

    def test_standard_mode_refuses_unnamed_legacy_audio(self):
        with self.assertRaisesRegex(ValueError,'音频轨'):classify_standard_tracks(document())

    def test_standard_mode_refuses_effect_mixed_into_bgm_track(self):
        data=document();data['tracks'][0]['name']='BGM 1';data['materials']['audios'][1]['type']='sound'
        with self.assertRaisesRegex(ValueError,'混入音效'):classify_standard_tracks(data)

    def test_identical_repeated_material_references_do_not_look_like_lost_content(self):
        source=document();source['materials']['audios'].append(copy.deepcopy(source['materials']['audios'][0]))
        normalized=document();active=referenced_material_ids(source)
        self.assertEqual(meaningful_changes(render_projection(source,active),render_projection(normalized,active)),[])

    def test_conflicting_duplicates_of_active_material_are_rejected(self):
        source=document();duplicate=copy.deepcopy(source['materials']['audios'][0]);duplicate['path']='different.wav'
        source['materials']['audios'].append(duplicate)
        with self.assertRaisesRegex(ValueError,'不一致参数'):render_projection(source,referenced_material_ids(source))

    def test_unused_media_and_empty_lanes_may_be_dropped_by_model(self):
        source=document();source['materials']['audios'].append({'id':'unused','type':'music','path':'unused.wav'})
        source['tracks'].append({'id':'empty','type':'audio','segments':[]})
        normalized=document();active=referenced_material_ids(source)
        self.assertEqual(meaningful_changes(render_projection(source,active),render_projection(normalized,active)),[])


class EngineFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        self.source=self.root/'original';self.source.mkdir()
        self.backup=self.root/'backup';self.backup.mkdir()
        self.cache=self.root/'cache'
        self.font=self.cache/'effect/123/font.ttf';self.font.parent.mkdir(parents=True);self.font.write_bytes(b'font')

    def tearDown(self):self.temporary.cleanup()

    def test_resource_relocation_preserves_serialized_font_metrics_and_caption_literals(self):
        font='C:/Users/Old/AppData/Local/JianyingPro/User Data/Cache/effect/123/font.ttf'
        content=json.dumps({'text':'Hola','styles':[{'size':14,'font':{'path':font}}]})
        data={'path':'##_draftpath_placeholder_project_##/clip.mp4','font_path':font,'content':content,
              'text':font,'name':'##_draftpath_placeholder_project_##/literal-title'}
        before=copy.deepcopy(data)
        result,record=relocate_document(data,self.source,self.backup,self.cache)
        self.assertEqual(result['path'],self.backup.as_posix()+'/clip.mp4')
        self.assertEqual(result['font_path'],self.font.as_posix())
        self.assertEqual(result['content'],content)
        self.assertEqual(result['text'],font)
        self.assertEqual(result['name'],before['name'])
        self.assertEqual(data,before)

    def test_cache_escape_is_reported_instead_of_relocated(self):
        value='C:/Users/Old/AppData/Local/JianyingPro/User Data/Cache/../../outside.ttf'
        result,record=relocate_document({'font_path':value},self.source,self.backup,self.cache)
        self.assertEqual(result['font_path'],value)
        self.assertEqual(record['unresolved_outer_cache_paths'],[value])

    def test_mismatched_runtime_fails_before_native_process(self):
        adapter=EngineAdapter(self.root/'tool',self.root/'runtime')
        adapter.tool_directory.mkdir();adapter.runtime_directory.mkdir()
        adapter.executable.write_bytes(b'executable')
        from jyexport.storage import sha
        adapter.profile.write_text(json.dumps({'schema':1,'engine_version':'11.6.0.14552','tool_sha256':sha(adapter.executable)}),'utf-8')
        (adapter.runtime_directory/'videoeditor.dll').write_bytes(b'wrong dll')
        with patch('jyexport.engine.subprocess.Popen') as process:
            with self.assertRaisesRegex(ValueError,'11.6.0.14552'):adapter.verify()
            process.assert_not_called()

    def test_ciphertext_copied_with_exit_zero_is_not_decrypted_json(self):
        source=self.root/'encrypted.json';source.write_bytes(b'CIPHERTEXT')
        adapter=EngineAdapter(self.root,self.root)
        def copy_cipher(arguments,*unused):Path(arguments[3]).write_bytes(b'CIPHERTEXT')
        with patch.object(adapter,'verify',return_value={}),patch.object(adapter,'_invoke',side_effect=copy_cipher):
            with self.assertRaisesRegex(ValueError,'JSON'):adapter.read(source)
        self.assertEqual(source.read_bytes(),b'CIPHERTEXT')

    def test_new_schema_requires_value_preservation_and_keeps_version(self):
        data=document();data['new_version']='191.0.0'
        source=self.root/'new.json';source.write_text(json.dumps(data),'utf-8')
        adapter=EngineAdapter(self.root,self.root)
        with patch.object(adapter,'verify',return_value={}),patch.object(adapter,'_roundtrip_model',return_value=copy.deepcopy(data)):
            self.assertEqual(adapter.read(source)['new_version'],'191.0.0')
            self.assertFalse(adapter.last_compatibility['version_rewritten'])
        broken=copy.deepcopy(data);broken['tracks'][0]['segments'][0]['volume']=0
        with patch.object(adapter,'verify',return_value={}),patch.object(adapter,'_roundtrip_model',return_value=broken):
            with self.assertRaisesRegex(ValueError,'有效字段'):adapter.read(source)

    def test_unseen_future_schema_stops_before_model_call(self):
        data=document();data['new_version']='192.0.0'
        source=self.root/'future.json';source.write_text(json.dumps(data),'utf-8')
        adapter=EngineAdapter(self.root,self.root)
        with patch.object(adapter,'verify',return_value={}),patch.object(adapter,'_roundtrip_model') as model:
            with self.assertRaisesRegex(ValueError,'结构'):adapter.read(source)
            model.assert_not_called()

    def test_external_audio_changes_block_publication(self):
        audio=self.root/'external.wav';audio.write_bytes(b'audio-before')
        data=document();data['materials']['audios'][0]['path']=str(audio)
        snapshot=external_resource_snapshot(data,self.backup)
        verify_external_resources(snapshot)
        audio.write_bytes(b'audio-after')
        with self.assertRaisesRegex(ValueError,'外部素材'):verify_external_resources(snapshot)

    def test_font_fallback_state_must_stay_stable(self):
        missing=self.root/'missing-font.ttf'
        data=document();data['materials']['texts'][0]['content']=json.dumps({'text':'Hola','styles':[{'font':{'path':str(missing)}}]})
        snapshot=external_resource_snapshot(data,self.backup)
        self.assertIn(str(missing),snapshot['missing'])
        missing.write_bytes(b'new font')
        with self.assertRaisesRegex(ValueError,'替代状态'):verify_external_resources(snapshot)

    def test_root_draft_without_project_timeline_index_binds_to_exact_root_file(self):
        job={'draft':{'timeline_id':''},'backup':str(self.backup),'backup_content':str(self.backup/'draft_content.json')}
        self.assertEqual(ensure_timeline_identity(job,document()),'episode')
        job['backup_content']=str(self.backup/'Timelines/other/draft_content.json')
        with self.assertRaisesRegex(ValueError,'根草稿'):ensure_timeline_identity(job,document())


class EngineBatchAndGuiTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='jy_engine_gui_')
        self.root_path=Path(self.temporary.name)
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.root_path/'user')});self.env.start()
        from jyexport.discovery import Draft
        from jyexport.storage import create_session
        source=self.root_path/'source';source.mkdir()
        content=source/'draft_content.json';content.write_text(json.dumps(document()),'utf-8')
        drafts=[Draft('one','Show',str(source),str(content),'episode','1',False),
                Draft('two','Show',str(source),str(content),'episode','2',False)]
        self.folder,self.session=create_session(drafts,self.root_path/'output',['V1','V2','V3','V4'],True)
        self.job=self.session['jobs'][0]
        self.tk_root=None

    def tearDown(self):
        if self.tk_root is not None:self.tk_root.destroy()
        self.env.stop();self.temporary.cleanup()

    def test_live_batch_lock_blocks_classification_without_changing_it(self):
        from jyexport.engine import persist_manual_roles
        from jyexport.inbox import _state_lock
        from jyexport.storage import sha
        before=copy.deepcopy(self.job)
        with _state_lock(self.folder/'后台引擎状态'):
            with self.assertRaisesRegex(ValueError,'后台执行'):
                persist_manual_roles(self.folder,self.session,self.job,roles(),sha(self.job['backup_content']))
        self.assertEqual(self.job,before)

    def test_refresh_preserves_job_references_and_other_job_updates(self):
        from jyexport.engine import _refresh_batch
        from jyexport.storage import atomic_json
        first,second=self.session['jobs']
        disk=copy.deepcopy(self.session);disk['jobs'][1]['status']='another window updated this job'
        atomic_json(self.folder/'任务清单.json',disk)
        _refresh_batch(self.folder,self.session,first)
        self.assertIs(self.session['jobs'][0],first)
        self.assertIs(self.session['jobs'][1],second)
        self.assertEqual(second['status'],'another window updated this job')

    def test_manual_classification_persists_fingerprint(self):
        from jyexport.engine import persist_manual_roles
        from jyexport.storage import sha
        fingerprint=sha(self.job['backup_content'])
        persist_manual_roles(self.folder,self.session,self.job,roles(),fingerprint)
        saved=json.loads((self.folder/'任务清单.json').read_text('utf-8'))['jobs'][0]
        self.assertEqual(saved['roles'],roles());self.assertEqual(saved['roles_content_sha256'],fingerprint)

    def test_failed_second_master_keeps_first_durably_and_reuses_it_on_retry(self):
        from jyexport.engine import persist_manual_roles,render_job
        from jyexport.storage import sha
        persist_manual_roles(self.folder,self.session,self.job,roles(),sha(self.job['backup_content']))
        media=self.root_path/'media';media.mkdir()
        for name in ('ffmpeg.exe','ffprobe.exe'):(media/name).write_bytes(b'test placeholder')
        calls=[]
        class Renderer:
            cache_directory=None
            executable=media/'native-test.exe'
            last_compatibility={'test':'state-only fake renderer'}
            def verify(self):return {'test_profile':True}
            def read(self,path):return document()
            def render(self,content,output,work_directory,**kwargs):
                calls.append(Path(content).stem)
                if Path(content).stem=='V3':raise RuntimeError('simulated renderer failure')
                Path(output).write_bytes(b'state-only test master')
                return {'input_sha256':sha(content),'output_sha256':sha(output),'exit_code':0}
        adapter=Renderer()
        with patch('jyexport.media_inputs.probe_media_inputs',return_value={'test':'state-only fake input validator'}):
            with self.assertRaisesRegex(RuntimeError,'simulated'):
                render_job(self.folder,self.session,self.job,adapter,media,parallel=1)
        run=Path(self.job['headless']['run_directory'])
        saved=json.loads((run/'run.json').read_text('utf-8'))
        self.assertIn('V2',saved['renders'])
        with patch('jyexport.engine.probe',return_value={'test':'metadata preflight only'}),patch('jyexport.media_inputs.probe_media_inputs',return_value={'test':'state-only fake input validator'}):
            with self.assertRaisesRegex(RuntimeError,'simulated'):
                render_job(self.folder,self.session,self.job,adapter,media,parallel=1)
        self.assertEqual(calls.count('V2'),1)
        self.assertEqual(calls.count('V3'),2)

    def make_gui(self):
        import tkinter as tk
        from jyexport.gui import App
        self.tk_root=tk.Tk();self.tk_root.withdraw()
        app=App(self.tk_root);self.tk_root.update_idletasks()
        app.session_folder=self.folder;app.session=self.session;app.show_tasks()
        app.dispatch=lambda function,callback=None:callback(function()) if callback else function()
        return app

    def test_background_analysis_button_uses_engine_and_shows_classification(self):
        app=self.make_gui()
        app.selected_task=lambda:(self.job,'V2')
        class Reader:
            def read(self,path):return document()
        app.engine_adapter=lambda:Reader()
        app.analyze(True)
        self.assertIs(app.current_job,self.job)
        self.assertEqual(len(app.classification),4)
        self.assertEqual(app.tabs.select(),str(app.class_tab))

    def test_selected_variant_rows_export_each_episode_only_once(self):
        app=self.make_gui();app.engine_adapter=lambda:object()
        rows=app.task_tree.get_children()
        selected=[iid for iid in rows if app.task_rows[iid][1] in ('V1','V2')]
        app.task_tree.selection_set(*selected)
        calls=[]
        with patch('jyexport.gui.render_job',side_effect=lambda folder,session,job,*args,**kwargs:(calls.append(job['id']) or {'complete':True})):
            app.engine_export()
        self.assertEqual(calls,[job['id'] for job in self.session['jobs']])
        self.assertIn('完成2集',app.engine_state.get())


if __name__=='__main__':unittest.main()
