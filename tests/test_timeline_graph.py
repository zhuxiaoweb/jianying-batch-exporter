import copy
import unittest

from jyexport.timeline_graph import documents, local_references


def timeline(identifier='root', referenced=(), entries=()):
    return {'id': identifier, 'tracks': [{'id': 'track', 'type': 'audio',
        'segments': [{'id': str(i), 'material_id': mid} for i, mid in enumerate(referenced)]}],
        'materials': {'items': list(entries)}}


class TimelineGraphTests(unittest.TestCase):
    def test_only_referenced_inline_children_are_visited_and_input_is_unchanged(self):
        child = timeline('child')
        root = timeline(referenced=['active'], entries=[
            {'id': 'active', 'type': 'combination', 'draft': child},
            {'id': 'unused', 'type': 'combination', 'draft_file_path': 'missing.json'}])
        before = copy.deepcopy(root)
        self.assertEqual([(scope, doc['id']) for scope, doc in documents(root)],
                         [((), 'root'), (('active',), 'child')])
        self.assertEqual(root, before)

    def test_child_ids_cannot_activate_parent_library_entries(self):
        root = timeline(referenced=['active'], entries=[
            {'id': 'active', 'type': 'combination', 'draft': timeline('child', ['collision'])},
            {'id': 'collision', 'type': 'combination', 'draft_file_path': 'missing.json'}])
        self.assertEqual(local_references(root), {'active'})
        self.assertEqual(len(list(documents(root))), 2)

    def test_transitive_material_reference_and_repeated_instances_visit_one_definition(self):
        root = timeline(referenced=['outer', 'outer'], entries=[
            {'id': 'outer', 'extra_material_refs': ['child']},
            {'id': 'child', 'type': 'compound', 'draft': timeline('child')}])
        self.assertEqual([scope for scope, _ in documents(root)], [(), ('child',)])

    def test_external_only_active_child_is_rejected(self):
        root = timeline(referenced=['child'], entries=[{'id': 'child', 'type': 'combination'}])
        with self.assertRaisesRegex(ValueError, '外部文件引用'):
            list(documents(root))

    def test_cycles_and_depth_are_explicit_errors(self):
        root = timeline(referenced=['child'])
        root['materials']['items'] = [{'id': 'child', 'type': 'draft', 'draft': root}]
        with self.assertRaisesRegex(ValueError, '循环'):
            list(documents(root))
        root = timeline(referenced=['child'], entries=[{'id': 'child', 'type': 'draft', 'draft': timeline('child')}])
        with self.assertRaisesRegex(ValueError, '嵌套过深'):
            list(documents(root, max_depth=0))

    def test_conflicting_duplicate_ids_are_rejected(self):
        root = timeline(referenced=['same'], entries=[{'id': 'same', 'path': 'a'}, {'id': 'same', 'path': 'b'}])
        with self.assertRaisesRegex(ValueError, '重复素材ID'):
            list(documents(root))


if __name__ == '__main__':
    unittest.main()
