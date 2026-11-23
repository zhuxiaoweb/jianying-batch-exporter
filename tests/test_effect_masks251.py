import copy
import unittest

from jyexport.effect_masks import effect_mask_bindings, bindings_digest
from tests.test_effect_masks250 import document


FIELDS = {'Rotation': ('rotation', 9.0), 'Feather': ('feather', 0.0),
          'SizeWidth': ('width', 0.1), 'SizeHeight': ('height', 0.05)}


def with_curve(name='Rotation', points=None):
    data = document()
    field, value = FIELDS[name]
    mask = data['materials']['video_effects'][0]['effect_mask'][0]
    mask['config'][field] = value
    mask['constant_material_id'] = 'native-mask'
    curve = {'id': 'curve', 'material_id': 'native-mask', 'property_type': 'KFTypeCommonMask' + name,
             'keyframe_list': [{'values': [value]}] if points is None else points}
    data['tracks'][0]['segments'][0]['common_keyframes'] = [curve]
    return data, curve, mask


class RedundantMaskCurves251Tests(unittest.TestCase):
    def test_omitted_empty_points_match_native_default_but_explicit_null_rejects(self):
        data, curve, _ = with_curve()
        curve.pop('keyframe_list')
        self.assertTrue(effect_mask_bindings(data))
        curve['keyframe_list'] = None
        with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_empty_known_curves_preserved(self):
        for name in FIELDS:
            data, curve, _ = with_curve(name, [])
            before = copy.deepcopy(data)
            self.assertEqual(effect_mask_bindings(data)[0]['references'][0]['mask_keyframes'], [curve])
            self.assertEqual(data, before)

    def test_single_point_at_each_verified_offset_preserved(self):
        for name in FIELDS:
            for offset in (0, 16666):
                data, curve, _ = with_curve(name)
                curve['keyframe_list'][0]['time_offset'] = offset
                self.assertTrue(effect_mask_bindings(data))

    def test_native_roundtrip_default_point_fields(self):
        data, curve, _ = with_curve()
        curve['keyframe_list'][0].update(curveType='Line', graphID='', string_value='',
            left_control={'x': 0.0, 'y': 0.0}, right_control={'x': 0.0, 'y': 0.0})
        self.assertTrue(effect_mask_bindings(data))

    def test_absent_static_rotation_and_feather_use_default_zero(self):
        for name in ('Rotation', 'Feather'):
            data, curve, mask = with_curve(name)
            mask['config'].pop(FIELDS[name][0])
            curve['keyframe_list'][0]['values'] = [0.0]
            self.assertTrue(effect_mask_bindings(data))

    def test_non_equal_single_point_stays_blocked(self):
        for name in FIELDS:
            data, curve, _ = with_curve(name)
            curve['keyframe_list'][0]['values'][0] += .001
            with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_multiple_points_even_identical_stay_blocked(self):
        data, curve, _ = with_curve()
        curve['keyframe_list'] *= 2
        with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_bad_values_stay_blocked(self):
        for value in (True, '9', float('nan'), float('inf'), None):
            data, curve, _ = with_curve()
            curve['keyframe_list'][0]['values'] = [value]
            with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_missing_empty_or_vector_values_stay_blocked(self):
        for value in (None, [], [9.0, 9.0], '9'):
            data, curve, _ = with_curve()
            curve['keyframe_list'][0]['values'] = value
            with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_unverified_offsets_stay_blocked(self):
        for value in (True, '0', 0.0, -1, 16667, 1000000):
            data, curve, _ = with_curve()
            curve['keyframe_list'][0]['time_offset'] = value
            with self.assertRaises(ValueError): effect_mask_bindings(data)
        data, curve, _ = with_curve()
        curve['keyframe_list'][0]['time_offset'] = 16666
        data['tracks'][0]['segments'][0]['target_timerange']['duration'] = 1000
        with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_nondefault_interpolation_fields_stay_blocked(self):
        for key, value in [('curveType', 'Bezier'), ('graphID', 'custom'), ('string_value', 'expression'),
                           ('left_control', {'x': .1, 'y': 0}), ('right_control', {'x': False, 'y': 0})]:
            data, curve, _ = with_curve()
            curve['keyframe_list'][0][key] = value
            with self.assertRaises(ValueError): effect_mask_bindings(data)
        for key in ('graphID', 'string_value'):
            for value in (False, 0, None, [], {}):
                data, curve, _ = with_curve()
                curve['keyframe_list'][0][key] = value
                with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_unknown_point_or_curve_field_stays_blocked(self):
        for where in ('point', 'curve'):
            data, curve, _ = with_curve()
            (curve if where == 'curve' else curve['keyframe_list'][0])['future'] = 1
            with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_unknown_property_even_empty_stays_blocked(self):
        data, curve, _ = with_curve(points=[])
        curve['property_type'] = 'KFTypeCommonMaskFuture'
        with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_shared_constant_id_is_local_to_each_segment(self):
        data, curve, mask = with_curve()
        other = copy.deepcopy(data['materials']['video_effects'][0]); other['id'] = 'other'
        other['effect_mask'][0]['config']['rotation'] = 33.0
        data['materials']['video_effects'].append(other)
        segment = copy.deepcopy(data['tracks'][0]['segments'][0]); segment.update(id='s2', material_id='other')
        segment['common_keyframes'][0]['keyframe_list'][0]['values'] = [33.0]
        data['tracks'][0]['segments'].append(segment)
        self.assertEqual(len(effect_mask_bindings(data)), 2)

    def test_shared_constant_multiple_masks_must_all_match(self):
        data, curve, mask = with_curve()
        second = copy.deepcopy(mask); second['id'] = 'second'; second['config']['rotation'] = 33.0
        data['materials']['video_effects'][0]['effect_mask'].append(second)
        with self.assertRaises(ValueError): effect_mask_bindings(data)

    def test_receipt_digest_includes_redundant_curves(self):
        data, curve, _ = with_curve()
        first = bindings_digest(effect_mask_bindings(data))
        curve['keyframe_list'][0]['time_offset'] = 16666
        self.assertNotEqual(bindings_digest(effect_mask_bindings(data)), first)


if __name__ == '__main__': unittest.main()
