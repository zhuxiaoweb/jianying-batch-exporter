"""Narrow final background-removal profile; synthetic temporary media only.

These tests validate routing, preserved parameters and strict sample bounds. They
do not claim a synthetic model reader is a native render-equivalence oracle.
"""
import copy
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from jyexport.advanced import ProcessingCompatibilityError
from jyexport.processed_audio import (bind_referenced_results, decode_referenced_wave,
                                     final_audio_profile, map_processed_source_range)
from jyexport.scoped_variants import classify_standard_tracks, transform
from jyexport.storage import sha
from jyexport.timeline_graph import documents
from tests.test_advanced import Reader, document


def pcm_wave(frames, rate=44100):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setparams((2, 2, rate, 0, 'NONE', 'not compressed'))
        # Non-silent, deterministic stereo PCM; no real user audio is included.
        stream.writeframes(struct.pack('<hh', 2301, -1703) * frames)
    return buffer.getvalue()


def background_node(path='explicit-final.wav', **extra):
    return {'id': 'background-result', 'type': 'vocal_separation',
            'removed_sounds': ['background'], 'final_algorithm': 'instrument_separate',
            'production_path': str(path), 'time_range': {'start': 4483333, 'duration': 6566667},
            'enter_from': 'timeline_menu', **extra}


class BackgroundProfileTests(unittest.TestCase):
    def test_only_missing_choice_or_strict_integer_zero_matches_background_profile(self):
        missing = background_node()
        for node in (missing, {**missing, 'choice': 0}):
            with self.subTest(choice=node.get('choice', '<absent>')):
                before = copy.deepcopy(node)
                self.assertEqual(final_audio_profile(node), 'instrument_separate_background')
                self.assertEqual(node, before)

    def test_mixed_duplicate_unknown_or_ambiguous_profile_parameters_remain_rejected(self):
        original = background_node()
        mutations = [
            {'removed_sounds': ['background', 'human']},
            {'removed_sounds': ['human', 'background']},
            {'removed_sounds': ['background', 'background']},
            {'removed_sounds': 'background'}, {'removed_sounds': ('background',)},
            {'removed_sounds': ['unknown']}, {'removed_sounds': []},
            {'final_algorithm': 'unknown'}, {'final_algorithm': 'vocal_separate'},
            {'final_algorithm': 'INSTRUMENT_SEPARATE'}, {'type': 'audio'},
            {'choice': True}, {'choice': False}, {'choice': None}, {'choice': 0.0},
            {'choice': ''}, {'choice': '0'}, {'choice': 1}, {'choice': 2}, {'choice': -1},
            {'production_path': ''}, {'production_path': None},
            {'time_range': {'duration': 0}}, {'time_range': {'duration': -1}},
            {'time_range': {}},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertIsNone(final_audio_profile({**original, **mutation}))

    def test_original_human_and_choice_one_two_profiles_keep_their_distinct_names(self):
        original = background_node()
        human = {**original, 'removed_sounds': ['human']}
        self.assertEqual(final_audio_profile(human), 'instrument_separate')
        for choice in (1, 2):
            vocal = {**original, 'removed_sounds': [], 'choice': choice, 'final_algorithm': 'vocal_separate'}
            self.assertEqual(final_audio_profile(vocal), 'vocal_separate_choice' + str(choice))
        self.assertIsNone(final_audio_profile({**human, 'choice': 1}))
        self.assertIsNone(final_audio_profile({**original, 'removed_sounds': ['human'], 'final_algorithm': 'vocal_separate'}))


class BackgroundTemporaryCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='jy_background238_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / 'project'
        self.audio = self.project / 'Resources' / 'audioAlg'
        self.audio.mkdir(parents=True)
        self.source = self.audio / 'explicit-final.wav'
        self.source.write_bytes(pcm_wave(289792))
        self.data = self.shared_document()

    def shared_document(self):
        first = background_node('##_draftpath_placeholder_test_##/Resources/audioAlg/explicit-final.wav', id='p1')
        second = copy.deepcopy(first); second['id'] = 'p2'
        return {
            'id': 'episode', 'new_version': '191.0.0', 'fps': 60, 'duration': 76450000,
            'canvas_config': {'width': 1080, 'height': 1920, 'ratio': '9:16'},
            'tracks': [{'id': 'video', 'type': 'video', 'segments': [
                {'id': 'cut1', 'material_id': 'source-video', 'extra_material_refs': ['p1'],
                 'source_timerange': {'start': 7483333, 'duration': 983333},
                 'target_timerange': {'start': 71433333, 'duration': 983333},
                 'volume': .35, 'common_keyframes': [{'id': 'k1', 'property_type': 'KFTypeVolume'}]},
                {'id': 'cut2', 'material_id': 'source-video', 'extra_material_refs': ['p2'],
                 'source_timerange': {'start': 8466666, 'duration': 1983334},
                 'target_timerange': {'start': 72416666, 'duration': 1983334},
                 'volume': .8, 'reverse': False}]}],
            'materials': {'videos': [{'id': 'source-video', 'type': 'video', 'path': 'source.mp4'}],
                          'vocal_separations': [first, second]},
        }


class BackgroundBindingTests(BackgroundTemporaryCase):
    def test_shared_result_decodes_once_and_only_production_paths_change(self):
        before = copy.deepcopy(self.data)
        original_sha = sha(self.source)
        bound, evidence = bind_referenced_results(self.data, self.project, self.root / 'bound')
        self.assertEqual(evidence['decoded_files'], 1)
        self.assertEqual(len(evidence['bindings']), 2)
        self.assertEqual(self.data, before)
        nodes = bound['materials']['vocal_separations']
        self.assertEqual(nodes[0]['production_path'], nodes[1]['production_path'])
        self.assertNotEqual(nodes[0]['production_path'], before['materials']['vocal_separations'][0]['production_path'])
        self.assertEqual(Path(nodes[0]['production_path']).read_bytes(), self.source.read_bytes())
        mappings = [entry['mappings'][0] for entry in evidence['bindings']]
        self.assertEqual([m['cached_source_start_us'] for m in mappings], [3000000, 3983333])
        self.assertEqual([(m['first_sample_floor'], m['end_sample_ceil']) for m in mappings],
                         [(132300, 175665), (175664, 263131)])
        self.assertTrue(all(m['final_pcm_covers_authored_range'] for m in mappings))
        self.assertTrue(all(m['authored_eof_tail_samples'] == 0 for m in mappings))
        for node, original in zip(nodes, before['materials']['vocal_separations']):
            node['production_path'] = original['production_path']
        self.assertEqual(bound, before)
        self.assertEqual(sha(self.source), original_sha)
        self.assertTrue(evidence['processing_parameters_preserved'])
        self.assertFalse(evidence['native_render_comparison_passed'])

    def test_zero_start_single_node_preserves_actual_first_clip_range(self):
        self.source.write_bytes(pcm_wave(231424))
        node = background_node(str(self.source), id='p1', time_range={'duration': 5233333})
        segment = self.data['tracks'][0]['segments'][0]
        segment.update(source_timerange={'duration': 2233333}, target_timerange={'duration': 2233333})
        self.data['tracks'][0]['segments'] = [segment]
        self.data['materials']['vocal_separations'] = [node]
        before = copy.deepcopy(self.data)
        bound, evidence = bind_referenced_results(self.data, self.project, self.root / 'single')
        mapping = evidence['bindings'][0]['mappings'][0]
        self.assertEqual((mapping['first_sample_floor'], mapping['end_sample_ceil']), (0, 98490))
        self.assertTrue(mapping['target_timerange_unchanged'])
        self.assertEqual(bound['tracks'], before['tracks'])
        self.assertEqual(self.data, before)

    def test_missing_corrupt_or_truncated_explicit_result_stops_without_changing_document(self):
        before = copy.deepcopy(self.data)
        for index, raw in enumerate((None, b'not wave', self.source.read_bytes()[:-4])):
            with self.subTest(index=index):
                if raw is None:
                    self.source.unlink()
                else:
                    self.source.write_bytes(raw)
                with self.assertRaises((OSError, ValueError)):
                    bind_referenced_results(self.data, self.project, self.root / ('bad-' + str(index)))
                self.assertEqual(self.data, before)
                self.assertFalse(list((self.root / ('bad-' + str(index))).glob('*.wav')))

    def test_actual_pcm_shortfall_is_rejected_without_padding(self):
        node = background_node(time_range={'duration': 100000})
        segment = {'id': 'edge', 'source_timerange': {'duration': 100000}}
        with self.assertRaisesRegex(ValueError, '实际PCM'):
            map_processed_source_range(segment, node, {'rate': 44100, 'frames': 4409})

    def test_background_keeps_two_microsecond_metadata_tolerance_not_choice_two_rounding(self):
        node = background_node(time_range={'duration': 1000000})
        segment = {'id': 'edge', 'source_timerange': {'duration': 1000002}}
        mapping = map_processed_source_range(segment, node, {'rate': 44100, 'frames': 50000})
        self.assertEqual(mapping['declared_tail_overrun_us'], 2)
        for extra in (3, 8334, 16667):
            with self.subTest(extra=extra):
                segment['source_timerange']['duration'] = 1000000 + extra
                with self.assertRaisesRegex(ValueError, '源范围'):
                    map_processed_source_range(segment, node, {'rate': 44100, 'frames': 50000})

    def test_referenced_source_change_during_complete_read_is_rejected(self):
        output = self.root / 'changed-source.wav'
        actual = sha
        def changed(path):
            if Path(path) == self.source:
                raw = bytearray(self.source.read_bytes()); raw[-1] ^= 1; self.source.write_bytes(raw)
            return actual(path)
        with patch('jyexport.processed_audio.sha', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '改变'):
                decode_referenced_wave(self.source, output, 6566667)
        self.assertFalse(output.exists())


class BackgroundReadAndVariantTests(BackgroundTemporaryCase):
    def read_fixture(self, scope):
        data = document(self.project)
        target = data if scope == 'root' else data['materials']['drafts'][0]['draft']
        target['materials']['vocal_separations'] = [background_node('MISSING-but-explicit.wav')]
        target['tracks'][0]['segments'][0]['extra_material_refs'] = ['background-result']
        path = self.root / (scope + '.json')
        path.write_text(json.dumps(data), encoding='utf-8')
        return data, target, path

    def test_root_and_child_read_only_mark_final_result_pending_file_validation(self):
        for scope in ('root', 'child'):
            with self.subTest(scope=scope):
                data, target, path = self.read_fixture(scope)
                adapter = Reader(self.root, self.root)
                before = copy.deepcopy(data)
                self.assertEqual(adapter.read(path), before)
                evidence = next(item for item in adapter.last_compatibility['scopes']
                                if item['scope'] == ([] if scope == 'root' else ['compound']))
                self.assertEqual(evidence['final_audio_results_pending_validation'], ['background-result'])
                self.assertFalse(adapter.last_compatibility['native_comparison_passed'])
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), before)

    def test_supported_profile_does_not_hide_model_field_loss_in_root_or_child(self):
        for scope in ('root', 'child'):
            with self.subTest(scope=scope):
                data, target, path = self.read_fixture(scope)
                adapter = Reader(self.root, self.root)
                def lose(value):
                    normalized = copy.deepcopy(value)
                    doc = normalized if scope == 'root' else normalized['materials']['drafts'][0]['draft']
                    doc['materials']['vocal_separations'][0]['removed_sounds'] = []
                    return normalized
                with patch.object(adapter, '_roundtrip_model', side_effect=lose):
                    with self.assertRaisesRegex(ValueError, '有效字段'):
                        adapter.read(path)

    def test_other_ai_denoise_processing_remains_blocked_in_root_or_child(self):
        for scope in ('root', 'child'):
            with self.subTest(scope=scope):
                data, target, path = self.read_fixture(scope)
                source_id = target['tracks'][0]['segments'][0]['material_id']
                material = next(item for item in target['materials']['audios'] if item['id'] == source_id)
                material['enable_ai_denoise'] = True
                path.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(ProcessingCompatibilityError) as raised:
                    Reader(self.root, self.root).read(path)
                self.assertTrue(any('enable_ai_denoise' in item
                                    for item in raised.exception.compatibility_details['blocked']))

    def test_four_variants_only_remove_confirmed_music_or_captions_and_preserve_processing(self):
        data, target, path = self.read_fixture('root')
        child = data['materials']['drafts'][0]['draft']
        child['materials']['vocal_separations'] = [background_node('another-explicit.wav', id='child-process')]
        child['tracks'][0]['segments'][0]['extra_material_refs'] = ['child-process']
        before = copy.deepcopy(data)
        roles = classify_standard_tracks(data)
        expected_music = {'V1': False, 'V2': False, 'V3': True, 'V4': True}
        expected_captions = {'V1': False, 'V2': True, 'V3': False, 'V4': True}
        for variant in ('V1', 'V2', 'V3', 'V4'):
            with self.subTest(variant=variant):
                output = transform(data, roles, variant)
                current = {tuple(scope): doc for scope, doc in documents(output)}
                for scope, original in documents(before):
                    self.assertEqual(current[scope]['materials'], original['materials'])
                root_tracks = {track['id']: track for track in output['tracks']}
                self.assertEqual(root_tracks['voice'], before['tracks'][0])
                self.assertEqual(root_tracks['fx'], before['tracks'][2])
                self.assertEqual(bool(root_tracks['music']['segments']), expected_music[variant])
                self.assertEqual(bool(root_tracks['captions']['segments']), expected_captions[variant])
                self.assertEqual(current[('compound',)], before['materials']['drafts'][0]['draft'])
        self.assertEqual(data, before)


if __name__ == '__main__':
    unittest.main()
