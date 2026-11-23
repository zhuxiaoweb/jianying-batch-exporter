import copy
import json
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from jyexport.advanced import (AdvancedAdapter, apply_overrides, digest, media_rows,
    override_contract, override_key, prepare_resources, save_media_override)
from jyexport.discovery import Draft
from jyexport.engine import persist_manual_roles, render_job
from jyexport.scoped_variants import inspect
from jyexport.storage import create_session, sha
from tests.test_processed_audio import encoded_wave, wave_bytes


def document(root):
    def segment(sid,mid,start=0,duration=1000000,refs=()):
        return {'id':sid,'material_id':mid,'source_timerange':{'start':0,'duration':duration},
                'target_timerange':{'start':start,'duration':duration},'extra_material_refs':list(refs)}
    child={'id':'child-timeline','new_version':'189.0.0','duration':1000000,'fps':24,
        'name':'音效复合','canvas_config':{'width':640,'height':480,'ratio':'4:3'},
        'tracks':[{'id':'child-track','type':'audio','name':'SFX 1','segments':[segment('child-s','child-m')]}],
        'materials':{'audios':[{'id':'child-m','path':str(root/'effect.mp3'),'type':'extract_music','name':'effect','duration':1000000}]}}
    return {'id':'episode','new_version':'189.0.0','duration':3000000,'fps':60,
        'canvas_config':{'width':1080,'height':1920,'ratio':'9:16'},'tracks':[
            {'id':'voice','type':'audio','name':'VOICE 1','segments':[segment('voice-s','voice-m')]},
            {'id':'music','type':'audio','name':'BGM 1','segments':[segment('music-s','music-m',duration=3000000)]},
            {'id':'fx','type':'audio','name':'SFX 2','segments':[segment('fx-s','shell',start=1000000,refs=['compound'])]},
            {'id':'captions','type':'text','segments':[segment('caption-s','caption-m')]}],
        'materials':{'audios':[
            {'id':'voice-m','type':'extract_music','name':'voice','duration':1000000,'path':str(root/'Resources/audioAlg/voice.wav')},
            {'id':'music-m','type':'music','name':'music','duration':3000000,'path':str(root/'music.mp3')},
            {'id':'shell','type':'music','path':'','duration':1000000}],
            'texts':[{'id':'caption-m','type':'subtitle','content':'Final edited caption'}],
            'drafts':[{'id':'compound','type':'combination','draft':child}]}}


class Reader(AdvancedAdapter):
    def verify(self):return {'test_profile':True}
    def _roundtrip_model(self,data):return copy.deepcopy(data)


class AdvancedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source';(self.source/'Resources/audioAlg').mkdir(parents=True)
        (self.source/'Resources/audioAlg/voice.wav').write_bytes(encoded_wave(wave_bytes(1.)))
        for name in ('effect.mp3','music.mp3'):(self.source/name).write_bytes(b'state-only media placeholder')
        self.data=document(self.source);self.content=self.source/'draft_content.json';self.write()
        self.adapter=Reader(self.root,self.root)
        self.gui_root=None
    def tearDown(self):
        if self.gui_root is not None:self.gui_root.destroy()
        self.temp.cleanup()
    def write(self):self.content.write_text(json.dumps(self.data),'utf-8')

    def test_recursive_read_and_company_classification_keep_child_scope(self):
        read=self.adapter.read(self.content);roles=self.adapter.classify_tracks(read)
        self.assertEqual(len(roles),5)
        rows=self.adapter.inspect_tracks(read)
        child=next(row for row in rows if row['scope'])
        self.assertIn('音效复合',child['track'])
        self.assertEqual(roles[child['key']],'音效')
        self.assertFalse(self.adapter.last_compatibility['native_comparison_passed'])

    def test_unknown_active_child_processing_still_blocks(self):
        self.data['materials']['drafts'][0]['draft']['materials']['audios'][0]['matting']={'flag':1};self.write()
        with self.assertRaisesRegex(ValueError,'抠像'):self.adapter.read(self.content)

    def test_compound_container_cannot_remove_retained_child_audio(self):
        self.data['tracks'][2]['name']='BGM 2'
        self.data['materials']['drafts'][0]['draft']['tracks'][0]['name']='VOICE 1'
        roles=self.adapter.classify_tracks(self.data)
        container=next(row for row in self.adapter.inspect_tracks(self.data) if row['is_container'])
        self.assertEqual(roles[container['key']],'保留原声音')
        roles[container['key']]='背景音乐'
        from jyexport.scoped_variants import transform
        with self.assertRaisesRegex(ValueError,'复合容器'):transform(self.data,roles,'V2')

    def test_known_result_is_pending_validation_and_other_voice_algorithms_are_blocked(self):
        node={'id':'processor','type':'vocal_separation','removed_sounds':['human'],
            'final_algorithm':'instrument_separate','production_path':str(self.root/'missing.wav'),
            'time_range':{'duration':1000000}}
        self.data['materials']['vocal_separations']=[node]
        self.data['tracks'][0]['segments'][0]['extra_material_refs']=['processor'];self.write()
        self.adapter.read(self.content)
        self.assertEqual(self.adapter.last_compatibility['scopes'][0]['final_audio_results_pending_validation'],['processor'])
        node['choice']=1;self.write()
        with self.assertRaisesRegex(ValueError,'人声处理'):self.adapter.read(self.content)

    def test_choice2_final_vocal_result_can_proceed_to_file_validation(self):
        node={'id':'processor','type':'vocal_separation','choice':2,
            'final_algorithm':'vocal_separate','production_path':str(self.root/'missing.wav'),
            'time_range':{'start':15083333,'duration':3208333}}
        self.data['materials']['vocal_separations']=[node]
        self.data['tracks'][0]['segments'][0]['extra_material_refs']=['processor'];self.write()
        before=copy.deepcopy(self.data)
        self.assertEqual(self.adapter.read(self.content),before)
        self.assertEqual(self.adapter.last_compatibility['scopes'][0]['final_audio_results_pending_validation'],['processor'])
        self.assertFalse(self.adapter.last_compatibility['native_comparison_passed'])
        for mutation in ({'choice':3},{'final_algorithm':'unknown'},
                         {'removed_sounds':['human']},{'production_path':''}):
            with self.subTest(mutation=mutation):
                node.clear();node.update(copy.deepcopy(before['materials']['vocal_separations'][0]));node.update(mutation);self.write()
                with self.assertRaisesRegex(ValueError,'人声处理'):self.adapter.read(self.content)

    def test_model_loss_in_child_is_not_hidden_by_supported_audio_path(self):
        def lose(data):
            result=copy.deepcopy(data);result['materials']['drafts'][0]['draft']['fps']=30;return result
        with patch.object(self.adapter,'_roundtrip_model',side_effect=lose):
            with self.assertRaisesRegex(ValueError,'有效字段'):self.adapter.read(self.content)

    def test_preparation_reuses_same_decoded_paths_and_rejects_changed_copy(self):
        work=self.root/'work';work.mkdir();before=sha(self.source/'Resources/audioAlg/voice.wav')
        first,evidence=prepare_resources(self.data,self.source,work)
        second,again=prepare_resources(self.data,self.source,work)
        self.assertEqual(first,second);self.assertEqual(evidence,again)
        self.assertEqual(len(list(work.glob('prepare_*'))),1)
        decoded=Path(evidence['media_results']['bindings'][0]['wave']['decoded'])
        decoded.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'改变'):prepare_resources(self.data,self.source,work)
        self.assertEqual(sha(self.source/'Resources/audioAlg/voice.wav'),before)

    def test_explicit_relocation_is_scope_bound_and_metadata_does_not_invalidate_plan(self):
        original=self.data['materials']['audios'][1]['path'];replacement=self.root/'replacement.mp3';replacement.write_bytes(b'correct chosen media')
        record={'scope':[],'material_id':'music-m','field':'path','original_path':original,
                'replacement':str(replacement),'sha256':sha(replacement),'at':'first'}
        key=override_key([],'music-m');overrides={key:record};before=copy.deepcopy(self.data)
        adjusted=apply_overrides(self.data,overrides)
        self.assertEqual(adjusted['materials']['audios'][1]['path'],str(replacement));self.assertEqual(self.data,before)
        contract=digest(override_contract(overrides));record['at']='later'
        self.assertEqual(contract,digest(override_contract(overrides)))
        replacement.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'改变'):apply_overrides(self.data,overrides)

    def batch(self):
        draft=Draft('case','Synthetic state case',str(self.source),str(self.content),'episode','1',False,3000000)
        return create_session([draft],self.root/'output',['V1','V2','V3','V4'],True)

    def test_failed_second_master_reuses_first_and_decoded_media_across_retry(self):
        folder,session=self.batch();job=session['jobs'][0]
        data=self.adapter.read(job['backup_content']);roles=self.adapter.classify_tracks(data)
        persist_manual_roles(folder,session,job,roles,sha(job['backup_content']),'scoped_v1')
        media=self.root/'tools';media.mkdir()
        for name in ('ffmpeg.exe','ffprobe.exe'):(media/name).write_bytes(b'placeholder')
        calls=[]
        def render(content,output,*args,**kwargs):
            key=Path(content).stem;calls.append(key)
            if key=='V3':raise RuntimeError('second master deliberately failed')
            Path(output).write_bytes(b'state-only test master')
            return {'input_sha256':sha(content),'output_sha256':sha(output),'exit_code':0}
        with patch.object(self.adapter,'render',side_effect=render),patch('jyexport.advanced.probe',return_value={'test':'metadata-only'}),patch('jyexport.advanced.probe_media_inputs',return_value={'test':'fake input stream validator'}):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError,'deliberately'):render_job(folder,session,job,self.adapter,media,1)
        self.assertEqual(calls.count('V2'),1);self.assertEqual(calls.count('V3'),2)
        run=Path(job['headless']['run_directory'])
        self.assertEqual(len(list(run.glob('prepare_*'))),1)
        self.assertIn('V2',json.loads((run/'run.json').read_text('utf-8'))['renders'])
        self.assertEqual(job['variants']['V2']['file'],None)

    def test_media_rows_cover_child_and_override_is_saved_without_touching_source(self):
        folder,session=self.batch();job=session['jobs'][0]
        data=self.adapter.read(job['backup_content']);rows=media_rows(data,job,None)
        child=next(row for row in rows if row['scope']);self.assertTrue(child['exists'])
        original=sha(self.content);replacement=self.root/'chosen.mp3';replacement.write_bytes(b'replacement')
        save_media_override(folder,session,job,child['scope'],child['material_id'],child['field'],child['original_path'],replacement,sha(job['backup_content']))
        relocated=next(row for row in media_rows(data,job,None) if row['scope'])
        self.assertEqual(relocated['resolved_path'],str(replacement));self.assertEqual(sha(self.content),original)
        self.assertTrue(relocated['relocated'])

    def gui(self):
        import tkinter as tk
        from jyexport.gui import App
        folder,session=self.batch();job=session['jobs'][0]
        self.gui_root=tk.Tk();self.gui_root.withdraw()
        with patch.dict(os.environ,{'LOCALAPPDATA':str(self.root/'user')}):app=App(self.gui_root)
        self.gui_root.update_idletasks()
        app.session_folder=folder;app.session=session;app.show_tasks()
        app.selected_task=lambda:(job,'V2');app.engine_adapter=lambda:self.adapter
        app.dispatch=lambda function,callback=None:callback(function()) if callback else function()
        return app,job

    def test_gui_analyzes_child_and_saves_scoped_classification_for_restart(self):
        from jyexport.workflow import read_batch
        app,job=self.gui();app.analyze(True)
        self.assertEqual(app.current_analysis_mode,'scoped_v1')
        self.assertEqual(len(app.classification),5)
        self.assertEqual(sum(bool(row['scope']) for row in app.classification),1)
        app.role_map=self.adapter.classify_tracks(self.adapter.read(job['backup_content']))
        app.save_roles()
        _,loaded=read_batch(app.session_folder)
        self.assertEqual(loaded['jobs'][0]['classification_mode'],'scoped_v1')
        self.assertEqual(loaded['jobs'][0]['roles'],app.role_map)

    def test_gui_media_selection_persists_override_and_updates_row(self):
        import tkinter as tk
        from tkinter import ttk
        app,job=self.gui();app.analyze(True)
        chosen=self.root/'chosen.mp3';chosen.write_bytes(b'operator chosen media')
        original_factory=tk.Toplevel
        def hidden_window(*args,**kwargs):
            window=original_factory(*args,**kwargs);window.withdraw();return window
        with patch('jyexport.gui.tk.Toplevel',side_effect=hidden_window):app.locate_media()
        window=next(widget for widget in self.gui_root.winfo_children() if isinstance(widget,original_factory))
        def descendants(parent):
            for widget in parent.winfo_children():
                yield widget
                yield from descendants(widget)
        widgets=list(descendants(window))
        tree=next(widget for widget in widgets if isinstance(widget,ttk.Treeview))
        button=next(widget for widget in widgets if isinstance(widget,ttk.Button) and '选择正确文件' in widget.cget('text'))
        selected=next(iid for iid in tree.get_children() if tree.set(iid,'name')=='music')
        tree.selection_set(selected)
        with patch('jyexport.gui.filedialog.askopenfilename',return_value=str(chosen)):button.invoke()
        self.assertEqual(tree.set(selected,'state'),'已定位')
        self.assertEqual(next(iter(job['media_overrides'].values()))['sha256'],sha(chosen))
