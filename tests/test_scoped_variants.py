import copy
import unittest

from jyexport.scoped_variants import inspect, scoped_key, subtitle_srt, transform


def fixture():
    def doc(identifier):
        return {'id': identifier, 'duration': 10000000, 'fps': 24,
            'canvas_config': {'width': 640, 'height': 480, 'ratio': '4:3'},
            'tracks': [
                {'id': 'a', 'type': 'audio', 'segments': [
                    {'id': 's', 'material_id': 'music', 'volume': .23,
                     'target_timerange': {'start': 400000, 'duration': 2000000}}]},
                {'id': 't', 'type': 'text', 'segments': [
                    {'id': 's', 'material_id': 'caption',
                     'target_timerange': {'start': 1234567, 'duration': 2000000}}]}],
            'materials': {'audios': [{'id': 'music', 'name': 'music'}],
                          'texts': [{'id': 'caption', 'type': 'subtitle', 'content': '{"text":"台词"}'}]}}
    root, child = doc('root'), doc('child')
    root['materials']['drafts'] = [{'id': 'child', 'type': 'combination', 'draft': child}]
    root['tracks'].append({'id': 'compound', 'type': 'video', 'segments': [
        {'id': 'one', 'material_id': 'child'}, {'id': 'two', 'material_id': 'child'}]})
    return root


class ScopedVariantTests(unittest.TestCase):
    def test_colliding_local_keys_have_separate_scoped_roles(self):
        data = fixture()
        rows = inspect(data)
        self.assertEqual(len(rows), 4)
        roles = {row['key']: '字幕' if row['kind'] == 'text' else '保留原声音' for row in rows}
        roles[scoped_key(('child',), 'a:s')] = '背景音乐'
        before = copy.deepcopy(data)
        result = transform(data, roles, 'V2')
        self.assertEqual(data, before)
        self.assertEqual(result['tracks'][0], data['tracks'][0])
        child = result['materials']['drafts'][0]['draft']
        self.assertEqual(child['tracks'][0]['segments'], [])
        self.assertEqual(child['fps'], 24)
        self.assertEqual(child['canvas_config'], data['materials']['drafts'][0]['draft']['canvas_config'])
        self.assertEqual(len(result['tracks'][2]['segments']), 2)
        self.assertEqual(result['fps'], 60)

    def test_no_subtitle_variant_removes_child_captions_but_keeps_titles_and_audio_parameters(self):
        data = fixture()
        roles = {row['key']: '字幕' if row['kind'] == 'text' else '保留原声音' for row in inspect(data)}
        roles[scoped_key((), 't:s')] = '标题/其他文字'
        result = transform(data, roles, 'V3')
        self.assertEqual(result['tracks'][1], data['tracks'][1])
        self.assertEqual(result['tracks'][0], data['tracks'][0])
        self.assertEqual(result['materials']['drafts'][0]['draft']['tracks'][1]['segments'], [])

    def test_unclassified_child_stops_preparation(self):
        data = fixture()
        with self.assertRaisesRegex(ValueError, '片段类别'):
            transform(data, {scoped_key((), 'a:s'): '背景音乐'}, 'V1')

    def test_nested_caption_srt_is_explicitly_rejected(self):
        data = fixture()
        roles = {row['key']: '字幕' if row['kind'] == 'text' else '保留原声音' for row in inspect(data)}
        with self.assertRaisesRegex(ValueError, '实例裁切及变速'):
            subtitle_srt(data, roles)

    def test_audio_only_compound_keeps_root_caption_text_and_truncated_times(self):
        data = fixture()
        data['materials']['drafts'][0]['draft']['tracks'][1]['segments'] = []
        roles = {row['key']: '字幕' if row['kind'] == 'text' else '保留原声音' for row in inspect(data)}
        self.assertEqual(subtitle_srt(data, roles), '1\n00:00:01,234 --> 00:00:03,234\n台词\n\n')


if __name__ == '__main__':
    unittest.main()
