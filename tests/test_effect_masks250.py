import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jyexport.effect_masks import (SCHEMA, RUNTIME_TREE_SHA256, assert_cached_effect_masks_safe,
    bindings_digest, effect_mask_bindings, render_mask_receipt, verify_mask_runtime)
from jyexport.engine import EngineAdapter
from jyexport.storage import sha


def document():
    return {'id': 'doc', 'new_version': '189.0.0', 'duration': 1000000,
        'tracks': [{'id': 'fx', 'type': 'effect', 'segments': [{'id': 's', 'material_id': 'e',
            'target_timerange': {'start': 0, 'duration': 1000000}}]}],
        'materials': {'video_effects': [{'id': 'e', 'type': 'video_effect', 'name': 'blur',
            'effect_mask': [{'id': 'mask', 'resource_type': 'circle',
                'config': {'width': .1, 'height': .05, 'centerX': -.6, 'centerY': -.8, 'feather': .13}}]}]}}


class MaskSemantics250Tests(unittest.TestCase):
    def test_native_constant_material_keyframes_are_bound_and_unknown_curves_block(self):
        data = document()
        data['materials']['video_effects'][0]['effect_mask'][0]['constant_material_id'] = 'native-key'
        curve = {'material_id': 'native-key', 'property_type': 'KFTypeCommonMaskPositionX',
                 'keyframe_list': [{'values': [.2]}]}
        data['tracks'][0]['segments'][0]['common_keyframes'] = [curve]
        rows = effect_mask_bindings(data)
        self.assertEqual(rows[0]['references'][0]['mask_keyframes'], [curve])
        curve['property_type'] = 'KFTypeUnknownMaskCurve'
        with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_native_model_defaults_are_exact_and_nondefault_geometry_still_blocks(self):
        data = document()
        config = data['materials']['video_effects'][0]['effect_mask'][0]['config']
        config.update(aspectRatio=1.0, expansion=0.0, roundCorner=0.0)
        before = copy.deepcopy(data)
        self.assertTrue(effect_mask_bindings(data))
        self.assertEqual(data, before)
        for field, value in [('aspectRatio', 2.0), ('expansion', .1), ('roundCorner', .1), ('expansion', False)]:
            changed = copy.deepcopy(data)
            changed['materials']['video_effects'][0]['effect_mask'][0]['config'][field] = value
            with self.assertRaises(ValueError):
                effect_mask_bindings(changed)

    def test_effect_geometry_and_sources_are_not_rewritten(self):
        data = document(); before = copy.deepcopy(data)
        rows = effect_mask_bindings(data)
        self.assertEqual(rows[0]['masks'], data['materials']['video_effects'][0]['effect_mask'])
        self.assertEqual(data, before)

    def test_unused_mask_library_entries_do_not_initialize_or_block(self):
        data = document(); data['tracks'] = []
        data['materials']['video_effects'][0]['effect_mask'][0]['resource_type'] = 'unknown'
        self.assertEqual(effect_mask_bindings(data), [])

    def test_child_namespace_masks_are_found(self):
        child = document()
        data = {'id': 'root', 'tracks': [{'id': 'v', 'type': 'video',
            'segments': [{'id': 'c', 'material_id': 'child'}]}],
            'materials': {'drafts': [{'id': 'child', 'type': 'combination', 'draft': child}]}}
        self.assertEqual(effect_mask_bindings(data)[0]['scope'], ['child'])

    def test_multiple_shapes_rotation_and_authored_position_curves_are_preserved(self):
        data = document(); mask = data['materials']['video_effects'][0]['effect_mask'][0]
        rectangle = copy.deepcopy(mask); rectangle.update(id='rectangle', resource_type='rectangle')
        rectangle['config'].update(width=1.32, height=.26, rotation=35.5, invert=True)
        data['materials']['video_effects'][0]['effect_mask'].append(rectangle)
        data['tracks'][0]['segments'][0]['common_keyframes'] = [{'material_id': 'mask',
            'property_type': 'KFTypeCommonMaskPositionX', 'keyframe_list': [
                {'time_offset': 0, 'values': [.5]}, {'time_offset': 700000, 'values': [-.3]}]}]
        original = copy.deepcopy(data); rows = effect_mask_bindings(data)
        self.assertEqual(data, original)
        self.assertEqual(len(rows[0]['masks']), 2)
        self.assertTrue(rows[0]['references'][0]['mask_keyframes'])
        digest = bindings_digest(rows)
        data['tracks'][0]['segments'][0]['common_keyframes'][0]['keyframe_list'][1]['values'] = [-.2]
        self.assertNotEqual(bindings_digest(effect_mask_bindings(data)), digest)

    def test_unknown_shape_keyframe_or_config_is_rejected(self):
        for kind in ('shape', 'curve', 'config'):
            data = document()
            if kind == 'shape': data['materials']['video_effects'][0]['effect_mask'][0]['resource_type'] = 'heart'
            elif kind == 'config': data['materials']['video_effects'][0]['effect_mask'][0]['config']['future'] = 2
            else: data['tracks'][0]['segments'][0]['common_keyframes'] = [
                {'material_id': 'mask', 'property_type': 'KFTypeCommonMaskFuture'}]
            with self.subTest(kind=kind), self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_invalid_geometry_is_not_coerced(self):
        for value in (True, '1', 0, -1, float('nan'), float('inf')):
            data = document(); data['materials']['video_effects'][0]['effect_mask'][0]['config']['width'] = value
            with self.subTest(value=value), self.assertRaises(ValueError): effect_mask_bindings(data)


class MaskRuntime250Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.runtime = self.root / 'runtime'
        self.assets = self.runtime / 'Resources/lumi_js_resources'; self.assets.mkdir(parents=True)
        p = self.assets / 'config.json'; p.write_bytes(b'fixture-official-resource')
        signature = hashlib.sha256(('config.json\t' + str(p.stat().st_size) + '\t' + sha(p) + '\n').encode()).hexdigest()
        self.count = patch('jyexport.effect_masks.RUNTIME_FILES', 1); self.count.start(); self.addCleanup(self.count.stop)
        self.digest = patch('jyexport.effect_masks.RUNTIME_TREE_SHA256', signature); self.digest.start(); self.addCleanup(self.digest.stop)

    def test_complete_resource_inventory_passes(self):
        self.assertEqual(verify_mask_runtime(self.runtime)['files'], 1)

    def test_modified_or_extra_file_rejects(self):
        (self.assets / 'extra.js').write_bytes(b'not-reviewed')
        with self.assertRaisesRegex(ValueError, '校验失败'): verify_mask_runtime(self.runtime)
        (self.assets / 'extra.js').unlink()
        (self.assets / 'config.json').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '校验失败'): verify_mask_runtime(self.runtime)

    def test_missing_resource_does_not_pass(self):
        with self.assertRaisesRegex(ValueError, '缺少'): verify_mask_runtime(self.root / 'missing')

    def test_linked_resource_is_rejected_before_reading(self):
        with patch.object(Path, 'is_symlink', return_value=True), self.assertRaisesRegex(ValueError, '链接'):
            verify_mask_runtime(self.runtime)


class AdapterMask250Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.content = self.root / 'input.json'
        self.output = self.root / 'output.mp4'; self.work = self.root / 'process'
        self.adapter = EngineAdapter(self.root, self.root)
        self.invocations = []
        def invoke(args, directory, timeout, cancel):
            self.invocations.append(args); self.output.write_bytes(b'controlled-mock-output')
            self.work.mkdir(); (self.work / 'engine.log').write_text(
                'effect-mask-runtime: initialized tree=' + RUNTIME_TREE_SHA256 + '\n', encoding='utf-8')
            return {}
        self.adapter._invoke = invoke
        self.adapter.verify = lambda: {'tool_sha256': 'h' * 64}

    def test_masked_render_requires_verified_resources_and_native_receipt(self):
        data = document(); self.content.write_text(json.dumps(data))
        runtime = {'root': str(self.root), 'files': 63, 'tree_sha256': RUNTIME_TREE_SHA256}
        with patch('jyexport.effect_masks.verify_mask_runtime', return_value=runtime) as checked:
            record = self.adapter.render(self.content, self.output, self.work)
        self.assertEqual(checked.call_count, 2)
        self.assertIn('--effect-mask-runtime', self.invocations[0])
        self.assertTrue(record['effect_mask_runtime']['native_initialization_verified'])
        self.assertEqual(record['effect_mask_runtime']['input_sha256'], sha(self.content))

    def test_unmasked_render_keeps_original_native_arguments(self):
        data = document(); data['tracks'] = []; self.content.write_text(json.dumps(data))
        with patch('jyexport.effect_masks.verify_mask_runtime') as checked:
            record = self.adapter.render(self.content, self.output, self.work)
        checked.assert_not_called()
        self.assertNotIn('--effect-mask-runtime', self.invocations[0])
        self.assertFalse(record['effect_mask_runtime']['bindings'])

    def test_bad_resource_cannot_launch_sdk(self):
        self.content.write_text(json.dumps(document()))
        with patch('jyexport.effect_masks.verify_mask_runtime', side_effect=ValueError('resource changed')):
            with self.assertRaises(ValueError): self.adapter.render(self.content, self.output, self.work)
        self.assertFalse(self.invocations)

    def test_missing_native_initialization_marker_stops_acceptance(self):
        self.content.write_text(json.dumps(document())); original = self.adapter._invoke
        def missing(*args):
            result = original(*args); (self.work / 'engine.log').write_text('export completed')
            return result
        self.adapter._invoke = missing
        with patch('jyexport.effect_masks.verify_mask_runtime', return_value={}):
            with self.assertRaisesRegex(ValueError, '初始化证明'):
                self.adapter.render(self.content, self.output, self.work)


class MaskResume250Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'V2.json'
        self.source.write_text(json.dumps(document()))
        self.job = {'backup_content': str(self.source), 'headless': {'run_directory': str(self.root),
            'profile': {'tool_sha256': 'h' * 64}, 'renders': {
                'V2': {'input_sha256': sha(self.source)}, 'V3': {'input_sha256': sha(self.source)}}}}

    def test_old_masked_delivery_is_not_reused(self):
        with self.assertRaisesRegex(ValueError, '全屏模糊'): assert_cached_effect_masks_safe(self.job)

    def test_old_unmasked_delivery_keeps_reuse(self):
        data = document(); data['tracks'] = []; self.source.write_text(json.dumps(data))
        self.job['headless']['renders']['V2']['input_sha256'] = sha(self.source)
        assert_cached_effect_masks_safe(self.job)

    def test_changed_old_input_is_rejected(self):
        self.source.write_text('{}')
        with self.assertRaisesRegex(ValueError, '输入改变'): assert_cached_effect_masks_safe(self.job)

    def test_new_initialized_receipts_remain_valid_after_work_input_cleanup(self):
        bindings = effect_mask_bindings(document())
        receipt = render_mask_receipt(bindings, {'tree_sha256': RUNTIME_TREE_SHA256}, sha(self.source), 'h' * 64)
        for render in self.job['headless']['renders'].values(): render['effect_mask_runtime'] = copy.deepcopy(receipt)
        self.source.unlink()
        assert_cached_effect_masks_safe(self.job)

    def test_uninitialized_receipt_cannot_be_reused(self):
        bindings = effect_mask_bindings(document())
        receipt = render_mask_receipt(bindings, {'tree_sha256': RUNTIME_TREE_SHA256}, sha(self.source), 'h' * 64)
        receipt['native_initialization_verified'] = False
        self.job['headless']['renders']['V2']['effect_mask_runtime'] = receipt
        with self.assertRaisesRegex(ValueError, '初始化证明'): assert_cached_effect_masks_safe(self.job)


if __name__ == '__main__':
    unittest.main()
