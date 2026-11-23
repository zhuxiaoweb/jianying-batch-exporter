"""Round-scoped exact-reference BGM hints and truthful actionable errors."""
import copy
import errno
import json
import os
import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jyexport.advanced import AdvancedAdapter, ModelCompatibilityError, media_rows
from jyexport.bgm_memory import ReferenceIndex, RoundBgmMemory, canonical_reference, confirmation_counts
from jyexport.delivery_service import analyze, confirm, prepare, run
from jyexport.discovery import Draft
from jyexport.easy_ui import EasyApp
from jyexport.error_messages import describe_failure
from jyexport.resources import ResourcePressure
from jyexport.scoped_variants import inspect
from jyexport.storage import Cancelled, sha
from tests.test_advanced import Reader, document


def descendants(parent):
    for child in parent.winfo_children():
        yield child
        yield from descendants(child)


class ReferenceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.music = self.root/'music.mp3'
        self.music.write_bytes(b'full BGM reference bytes')
        self.data = document(self.root)
        self.job = {'draft': {'folder': str(self.root)}, 'backup': str(self.root)}
        self.index = ReferenceIndex()

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, data=None, job=None):
        data, job = data or self.data, job or self.job
        rows = inspect(data)
        self.index.enrich(data, rows, media_rows(data, job, None), job)
        return rows

    def music_row(self, data=None, job=None):
        return next(r for r in self.rows(data, job) if r['name'] == 'music')

    def test_same_reference_with_different_material_and_clip_ids_matches(self):
        a = self.music_row()
        data = copy.deepcopy(self.data)
        data['materials']['audios'][1].update(id='other-material', type='extract_music')
        data['tracks'][1]['segments'][0].update(id='other-clip', material_id='other-material')
        b = self.music_row(data)
        self.assertNotEqual(a['key'], b['key'])
        self.assertEqual(a['bgm_reference_token'], b['bgm_reference_token'])
        memory = RoundBgmMemory();memory.remember([a], {a['key']})
        self.assertEqual(memory.suggest([b]), {b['key']})

    def test_same_basename_or_same_bytes_at_different_paths_never_match(self):
        a = self.music_row()
        other = self.root/'other'/'music.mp3';other.parent.mkdir();other.write_bytes(self.music.read_bytes())
        self.data['materials']['audios'][1]['path'] = str(other)
        self.assertNotEqual(a['bgm_reference_token'], self.music_row()['bgm_reference_token'])

    def test_changed_bytes_at_same_path_do_not_match_and_hint_reads_are_cached_per_operation(self):
        with patch('jyexport.bgm_memory.sha', wraps=sha) as hashing:
            a = self.music_row();self.music_row()
            self.assertEqual(hashing.call_count, 1)
            self.music.write_bytes(b'different full BGM reference bytes')
            b = self.music_row()
            self.assertEqual(hashing.call_count, 2)
        self.assertNotEqual(a['bgm_reference_token'], b['bgm_reference_token'])

    def test_vocal_processing_modes_of_same_media_are_distinct(self):
        result = self.root/'final.wav';result.write_bytes(b'processed PCM cache identity')
        processor = {'id': 'processor', 'type': 'vocal_separation', 'choice': 1,
                     'final_algorithm': 'vocal_separate', 'production_path': str(result),
                     'time_range': {'start': 0, 'duration': 3000000}}
        self.data['materials']['vocal_separations'] = [processor]
        self.data['tracks'][1]['segments'][0]['extra_material_refs'] = ['processor']
        a = self.music_row();processor['choice'] = 2;b = self.music_row()
        self.assertNotEqual(a['bgm_reference_token'], b['bgm_reference_token'])

    def test_project_placeholders_match_only_same_original_project(self):
        self.data['materials']['audios'][1]['path'] = '##_draftpath_placeholder_UUID_##/music.mp3'
        a = self.music_row()
        job = copy.deepcopy(self.job);job['draft']['folder'] = str(self.root/'different-project')
        b = self.music_row(job=job)
        self.assertNotEqual(a['bgm_reference_token'], b['bgm_reference_token'])
        self.assertEqual(canonical_reference('D:/Music/../Music/BGM.mp3', ''),
                         canonical_reference('d:\\music\\bgm.mp3', ''))

    def test_missing_file_or_giant_video_carrier_has_no_hint_and_does_not_fail_analysis(self):
        self.music.unlink();self.assertNotIn('bgm_reference_token', self.music_row())
        self.music.write_bytes(b'not actually large')
        stat = self.music.stat()
        with patch('jyexport.bgm_memory.Path.stat', return_value=SimpleNamespace(
                st_size=513*1024*1024, st_mtime_ns=stat.st_mtime_ns, st_ctime_ns=stat.st_ctime_ns, st_mode=stat.st_mode)):
            self.assertIsNone(self.index.fingerprint(self.music))

    def test_stop_is_not_swallowed_by_optional_hints(self):
        cancel = threading.Event();cancel.set();self.index = ReferenceIndex(cancel)
        with self.assertRaises(Cancelled):self.music_row()


class RoundDialogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.path = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'LOCALAPPDATA': str(self.path/'profile')});self.env.start()
        self.base = patch('jyexport.easy_ui.base_directory', return_value=self.path);self.base.start()
        self.root = tk.Tk();self.root.withdraw();self.app = EasyApp(self.root)
        for token in self.root.tk.call('after', 'info'):self.root.after_cancel(token)
        self.errors = [];self.root.report_callback_exception = lambda k,v,t:self.errors.append(str(v))
        self.jobs = [self.job(i) for i in range(1,4)]
        self.app.session = {'jobs': self.jobs};self.app.folder = self.path/'batch'
        self.app.begin_render = Mock(side_effect=self.app.bgm_memory.reset)

    def tearDown(self):
        self.app.windows.close_all()
        for token in self.root.tk.call('after', 'info'):self.root.after_cancel(token)
        self.root.destroy();self.base.stop();self.env.stop();self.temp.cleanup()
        self.assertEqual(self.errors, [])

    def job(self, i):
        return {'id': str(i), 'draft': {'name': 'test project', 'timeline_name': str(i)},
                'easy_analysis': {'caption_keys': [f'caption:{i}'], 'rows': [
                    {'key': f'bgm:{i}', 'kind': 'audio', 'is_container': False, 'name': 'same music',
                     'suggestion': '背景音乐', 'track': 'BGM', 'start': 0, 'duration': 1000000,
                     'bgm_reference_token': 'same-full-reference'},
                    {'key': f'other:{i}', 'kind': 'audio', 'is_container': False, 'name': 'music.mp3',
                     'suggestion': '待确认', 'track': 'FX', 'start': 0, 'duration': 1000000,
                     'bgm_reference_token': 'different-full-reference'},
                    {'key': f'caption:{i}', 'kind': 'text', 'is_container': False, 'name': 'Final',
                     'suggestion': '字幕', 'track': 'text', 'start': 0, 'duration': 1000000}]}}

    def button(self, label):
        return next(w for w in descendants(self.app.dialog) if w.winfo_class() == 'TButton' and w.cget('text') == label)

    def tree(self):
        return next(w for w in descendants(self.app.dialog) if w.winfo_class() == 'Treeview')

    def toggle(self, iid='0'):
        tree = self.tree();tree.identify_column = lambda x:'#1';tree.identify_row = lambda y:iid
        # Dispatch the Tk event through the widget binding, without OS input.
        tree.event_generate('<Button-1>', x=8, y=35)

    def accepted(self, folder, session, job, bgm, captions, no_bgm, **flags):
        job['easy_confirmed'] = True;job['roles'] = {key:'背景音乐' for key in bgm}

    def test_exact_confirmed_bgm_defaults_next_dialog_and_previous_choices_never_change(self):
        app = self.app;app.confirm_next();first = app.dialog
        self.assertEqual(self.tree().set('0','check'), '□')
        # Event generation requires a mapped own-app window, with no OS input.
        first.deiconify();first.update_idletasks();self.toggle()
        with patch('jyexport.easy_ui.confirm', side_effect=self.accepted):self.button('确认并继续制作').invoke()
        self.assertEqual(self.tree().set('0','check'), '☑');self.assertEqual(self.tree().set('1','check'), '□')
        saved = copy.deepcopy(self.jobs[0])
        labels = [str(w.cget('text')) for w in descendants(app.dialog) if w.winfo_class() == 'TLabel']
        self.assertTrue(any('已确认 1 集，待确认 2 集' in t for t in labels))
        app.dialog.update_idletasks();self.toggle()
        # This episode can override the default, leaving first episode intact.
        check = next(w for w in descendants(app.dialog) if w.winfo_class() == 'TCheckbutton')
        check.invoke()
        with patch('jyexport.easy_ui.confirm', side_effect=self.accepted):self.button('确认并继续制作').invoke()
        self.assertEqual(self.jobs[0], saved);self.assertEqual(self.jobs[1]['roles'], {})
        self.assertEqual(self.tree().set('0','check'), '☑')
        self.assertIsNone(self.root.grab_current())

    def test_failed_confirmation_never_seeds_memory_and_remains_editable(self):
        self.app.confirm_next();self.app.dialog.update_idletasks();self.toggle()
        with patch('jyexport.easy_ui.confirm', side_effect=ValueError('reject')),patch('jyexport.easy_ui.messagebox.showerror'):
            self.button('确认并继续制作').invoke()
        self.assertFalse(self.app.bgm_memory.tokens);self.assertIsNotNone(self.app.dialog)
        self.assertFalse(self.jobs[0].get('easy_confirmed'))

    def test_later_and_stop_clear_only_suggestion_memory(self):
        self.app.bgm_memory.remember(self.jobs[0]['easy_analysis']['rows'], {'bgm:1'})
        self.jobs[0]['easy_confirmed'] = True;before = copy.deepcopy(self.jobs[0])
        self.app.confirm_next();self.button('稍后继续').invoke()
        self.assertFalse(self.app.bgm_memory.tokens);self.assertEqual(self.jobs[0], before)
        self.app.bgm_memory.remember(self.jobs[0]['easy_analysis']['rows'], {'bgm:1'})
        self.app.confirm_next();self.app.stop()
        self.assertFalse(self.app.bgm_memory.tokens);self.assertEqual(self.jobs[0], before)

    def test_resume_starts_a_new_round_and_does_not_reconstruct_memory_from_saved_roles(self):
        self.app.bgm_memory.remember(self.jobs[0]['easy_analysis']['rows'], {'bgm:1'})
        self.app.environment_ready = True;self.app.dispatch = Mock()
        with patch('jyexport.easy_ui.read_batch', return_value=(self.app.folder,self.app.session)), \
             patch('jyexport.easy_ui.filedialog.askopenfilename', return_value='batch/任务清单.json'):
            self.app.resume()
        self.assertFalse(self.app.bgm_memory.tokens)

    def test_counts_include_saved_confirmations_and_separate_failed_episodes(self):
        self.jobs[0]['easy_confirmed'] = True;self.jobs[2]['easy_analysis_error'] = 'unsupported'
        text = confirmation_counts(self.jobs)
        self.assertIn('已确认 1 集，待确认 1 集', text);self.assertIn('另有 1 集分析未通过', text)

    def test_new_batch_render_and_preferences_never_carry_confirmed_bgm_memory(self):
        app=self.app;rows=self.jobs[0]['easy_analysis']['rows']
        app.bgm_memory.remember(rows, {'bgm:1'});app.pending_drafts=[]
        app.engine_adapter=Mock();app.dispatch=Mock();app.begin_selected()
        self.assertFalse(app.bgm_memory.tokens)
        app.bgm_memory.remember(rows, {'bgm:1'});app.media_directory=Mock(return_value=str(self.path))
        EasyApp.begin_render(app);self.assertFalse(app.bgm_memory.tokens)
        app.bgm_memory.remember(rows, {'bgm:1'});app.save_preferences()
        saved=json.loads((app.state_dir/'settings.json').read_text('utf-8'))
        self.assertNotIn('same-full-reference',json.dumps(saved))
        self.assertNotIn('bgm_memory',saved)


class FriendlyFailureTests(unittest.TestCase):
    def setUp(self):
        self.path = r'D:\jy\JianyingPro Presets\Combination\Resources\f49b2c202038e6db1241822748fb13c0.wav'
        self.job = {'draft': {'name': '30-1', 'timeline_name': '时间线01'}, 'easy_analysis': {
            'media_references': [{'scope': [], 'material_id': 'processor', 'field': 'production_path',
                'name': 'cached voice', 'original_path': self.path, 'resolved_path': self.path,
                'exists': True, 'positions': [{'track': 'VOICE', 'start': 44083333}]}]}}

    def test_missing_media_gives_exact_path_and_distinguishes_restore_from_edited_draft(self):
        error = FileNotFoundError(errno.ENOENT, 'not found', self.path)
        info = describe_failure(error, self.job, 'delivery')
        self.assertEqual(info['kind'], 'missing_media');self.assertEqual(info['raw_error'], str(error))
        self.assertIn(self.path, info['message']);self.assertIn('44.083 秒', info['message'])
        self.assertIn('重新选集', info['message']);self.assertIn('旧任务仍使用原备份', info['message'])

    def test_missing_tool_is_not_mislabeled_as_missing_user_media(self):
        info = describe_failure(FileNotFoundError(2, 'not found', r'D:\engine\ffprobe.exe'),self.job,'delivery')
        self.assertEqual(info['kind'], 'other')

    def test_resources_and_memory_errors_have_resumable_instructions(self):
        for error in (ResourcePressure('资源余量持续不足；物理0.7GB'), MemoryError('allocation'), ValueError('NV_ENC_ERR_OUT_OF_MEMORY')):
            info = describe_failure(error,self.job,'delivery')
            self.assertEqual(info['kind'], 'resources');self.assertIn('继续上次任务', info['message'])
            self.assertIn('合格母版', info['message']);self.assertEqual(info['raw_error'], str(error))

    def test_unverified_wav_has_actual_external_reference_not_a_missing_file_claim(self):
        error = ValueError('f49b2c202038e6db1241822748fb13c0.wav：音频分段参数未通过文件校验，拒绝猜测转换')
        info = describe_failure(error,self.job,'delivery')
        self.assertEqual(info['kind'], 'unverified_audio');self.assertIn(self.path, info['message'])
        self.assertIn('44.083 秒', info['message']);self.assertNotIn('引用丢失', info['summary'])

    def test_unreadable_compound_media_explains_uncompound_workflow(self):
        media_path = self.path.replace('.wav','.mp4')
        self.job['easy_analysis']['media_references'].append({'scope': [], 'material_id': 'compound',
            'field': 'path', 'name': 'compound_video.mp4', 'original_path': media_path,
            'resolved_path': media_path, 'exists': True, 'positions': []})
        error = ValueError('所用素材无法读取，停止以免丢失声画：'+media_path)
        info = describe_failure(error,self.job,'delivery')
        self.assertEqual(info['kind'], 'unreadable_media')
        self.assertIn('解除复合片段', info['message']);self.assertIn('重新选集', info['message'])
        self.assertIn('mp4', info['summary'])

    def test_unverified_wav_requires_matching_download_witness(self):
        error = ValueError('d61c8f042ac5a84c1ea58df383763844_8000000_250000.wav：音频分段参数未通过文件校验，拒绝猜测转换')
        info = describe_failure(error,self.job,'delivery')
        self.assertEqual(info['kind'], 'unverified_audio')
        self.assertIn('_download.wav', info['message']);self.assertIn('同一时间段', info['message'])
        self.assertIn('原生导出',info['message'])
        self.assertIn('草稿副本',info['message'])

    def test_short_processed_audio_has_regeneration_guidance(self):
        info = describe_failure(ValueError('处理结果时长短于草稿声明范围，未发布解码文件'),self.job,'delivery')
        self.assertEqual(info['kind'],'unverified_audio');self.assertIn('重新生成',info['message']);self.assertIn('重新选集',info['message'])

    def test_duplicate_material_reference_has_reanalysis_guidance(self):
        info = describe_failure(ValueError('同一时间线存在参数不同的重复素材ID：abc'),self.job,'delivery')
        self.assertEqual(info['kind'],'duplicate_material_reference');self.assertIn('重新选集',info['message'])

    def test_color_match_has_specific_diagnostics_and_remains_blocked(self):
        effect = {'id': 'color', 'type': 'color_match', 'value': .8,
                  'color_match_info': {'reference_id': 'ref', 'reference_name': '参考01'}}
        doc = {'materials': {'effects': [effect]}}
        normalized = copy.deepcopy(doc);del normalized['materials']['effects'][0]['color_match_info']['reference_id']
        error = ModelCompatibilityError((),['/materials/effects/0/color_match_info/reference_id:missing'],doc,normalized)
        info = describe_failure(error,self.job,'analysis')
        self.assertEqual(info['kind'], 'color_match_compatibility');self.assertIn('参考01', info['message'])
        self.assertEqual(error.compatibility_details['source_color_matches'], [effect])
        self.assertEqual(effect['value'], .8);self.assertNotIn('引用丢失', info['message'])


class HintServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.source = self.root/'source';self.source.mkdir()
        self.content = self.source/'draft_content.json'
        self.content.write_text(json.dumps(document(self.source)),'utf-8')
        self.adapter = Reader(self.root,self.root)
        draft = Draft('case','case',str(self.source),str(self.content),'episode','18',False,3000000)
        self.folder,self.session = prepare([draft],self.root/'output',self.adapter)
        self.job = self.session['jobs'][0]

    def tearDown(self):self.temp.cleanup()

    def test_failure_context_is_saved_and_successful_reanalysis_clears_it(self):
        missing = str(self.source/'lost.wav')
        with patch.object(self.adapter,'read', side_effect=FileNotFoundError(2,'not found',missing)):
            analyze(self.folder,self.session,self.adapter)
        self.assertEqual(self.job['easy_failure']['kind'], 'missing_media')
        self.assertIn('lost.wav', self.job['easy_analysis_error'])
        analyze(self.folder,self.session,self.adapter)
        self.assertNotIn('easy_failure', self.job);self.assertNotIn('easy_analysis_error', self.job)

    def test_resource_guard_failure_retains_plan_and_can_be_continued(self):
        analysis = self.job['easy_analysis'];music = next(r['key'] for r in analysis['rows'] if r['name']=='music')
        confirm(self.folder,self.session,self.job,[music],analysis['caption_keys'])
        roles = copy.deepcopy(self.job['roles']);backup_hash = sha(self.job['backup_content'])
        with patch('jyexport.delivery_service.render_job', side_effect=ResourcePressure('资源余量持续不足')):
            result = run(self.folder,self.session,self.adapter,self.root)
        self.assertEqual(result['failed'],1);self.assertEqual(self.job['easy_failure']['kind'],'resources')
        self.assertEqual(self.job['roles'],roles);self.assertEqual(sha(self.job['backup_content']),backup_hash)
        self.assertTrue(self.job['easy_confirmed']);self.assertNotIn('easy_delivery_folder',self.job)


if __name__ == '__main__':unittest.main()
