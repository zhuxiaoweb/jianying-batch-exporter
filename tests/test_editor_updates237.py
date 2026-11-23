"""Update policy safety checks; every profile and preference lives in a temp dir."""
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from jyexport import editor_updates as updates


class TemporaryProfile(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='jy_updates_test_')
        self.base = Path(self.temporary.name)
        self.root = self.base / 'JianyingPro' / 'User Data'
        self.config = self.root / 'Config'
        self.config.mkdir(parents=True)
        self.global_setting = self.config / 'globalSetting'
        self.beta = self.config / 'updateInfo'
        self.backups = self.base / 'backups'
        self.global_setting.write_bytes(b'[General]\r\nother=keep\r\nenableAutoUpdate=true\r\n')
        self.beta.write_bytes(b'[General]\r\njoinBeta=true\r\nneed_show_automatic_updates_popup=true\r\n')

    def tearDown(self):
        self.temporary.cleanup()

    def apply(self, disabled=True):
        return updates.apply_update_policy(self.root, self.backups, disabled)


class EditorPolicyTests(TemporaryProfile):
    def test_default_disables_auto_updates_and_beta_with_full_verified_backups(self):
        originals = {path: path.read_bytes() for path in (self.global_setting, self.beta)}
        result = self.apply()
        self.assertTrue(result['success'])
        for name, path in [('beta', self.beta), ('auto_update', self.global_setting)]:
            item = result[name]
            self.assertTrue(item['ok'])
            self.assertTrue(item['changed'])
            self.assertFalse(item['value'])
            self.assertTrue(item['before'])
            self.assertNotIn('error', item)
            self.assertEqual(item['path'], str(path))
            self.assertEqual(Path(item['backup']).read_bytes(), originals[path])
            metadata = json.loads((Path(item['backup']).parent / '备份记录.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['sha256'], updates._digest(originals[path]))
        self.assertIn(b'enableAutoUpdate=false\r\n', self.global_setting.read_bytes())
        self.assertIn(b'joinBeta=false\r\n', self.beta.read_bytes())
        self.assertFalse(list(self.config.glob('*.lock')))
        self.assertFalse(list(self.config.glob('*.tmp')))

    def test_allowing_auto_updates_still_disables_beta(self):
        self.global_setting.write_bytes(b'[General]\r\nenableAutoUpdate=false\r\n')
        result = self.apply(False)
        self.assertTrue(result['success'])
        self.assertTrue(result['auto_update']['value'])
        self.assertFalse(result['beta']['value'])
        self.assertIn(b'enableAutoUpdate=true', self.global_setting.read_bytes())
        self.assertIn(b'joinBeta=false', self.beta.read_bytes())

    def test_policy_applies_beta_before_automatic_setting(self):
        actual = updates._apply_one
        calls = []
        def record(*args):
            calls.append(args[2])
            return actual(*args)
        with patch.object(updates, '_apply_one', side_effect=record):
            self.assertTrue(self.apply()['success'])
        self.assertEqual(calls, ['updateInfo', 'globalSetting'])

    def test_noop_means_no_lock_backup_replace_or_timestamp_change(self):
        self.global_setting.write_bytes(b'[General]\r\nenableAutoUpdate=FALSE\r\n')
        self.beta.write_bytes(b'[General]\r\njoinBeta=false\r\n')
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (self.global_setting, self.beta)}
        with patch.object(updates, '_advisory_lock', side_effect=AssertionError('unexpected lock/write')):
            result = self.apply()
        self.assertTrue(result['success'])
        self.assertFalse(result['beta']['changed'])
        self.assertFalse(result['auto_update']['changed'])
        self.assertFalse(self.backups.exists())
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})

    def test_edit_preserves_bom_crlf_comments_spacing_unicode_and_unrelated_sections(self):
        original = b'\xef\xbb\xbf' + (';注释\r\n[General]\r\nother=中文\r\n  enableAutoUpdate = true\r\n'
                                         '[Else]\r\nenableAutoUpdate=unknown-and-unrelated\r\n').encode('utf-8')
        self.global_setting.write_bytes(original)
        result = self.apply()
        self.assertTrue(result['success'])
        self.assertEqual(self.global_setting.read_bytes(), original.replace(b' = true\r\n', b' = false\r\n', 1))

    def test_missing_update_info_created_only_with_existing_valid_profile(self):
        self.beta.unlink()
        result = self.apply()
        self.assertTrue(result['success'])
        self.assertFalse(result['beta']['before_exists'])
        self.assertIsNone(result['beta']['before'])
        self.assertEqual(self.beta.read_bytes(), b'[General]\r\njoinBeta=false\r\n')
        self.assertFalse(json.loads(Path(result['beta']['backup']).read_text(encoding='utf-8'))['existed'])

    def test_missing_target_key_is_appended_to_general_and_keeps_other_bytes(self):
        original = b'[General]\r\nother=keep\r\n[Else]\r\nfoo=bar\r\n'
        self.global_setting.write_bytes(original)
        result = self.apply()
        self.assertTrue(result['success'])
        self.assertEqual(self.global_setting.read_bytes(), original.replace(b'[Else]', b'enableAutoUpdate=false\r\n[Else]'))

    def test_existing_other_section_in_update_info_gets_general_appended(self):
        original = b'[Other]\r\nkept=yes'
        self.beta.write_bytes(original)
        result = self.apply()
        self.assertTrue(result['success'])
        self.assertEqual(self.beta.read_bytes(), original + b'\r\n[General]\r\njoinBeta=false\r\n')

    def test_missing_global_setting_is_not_created_and_neither_file_changes(self):
        self.global_setting.unlink()
        original = self.beta.read_bytes()
        result = self.apply()
        self.assertFalse(result['success'])
        self.assertFalse(result['beta']['ok'])
        self.assertFalse(result['auto_update']['ok'])
        self.assertFalse(self.global_setting.exists())
        self.assertEqual(self.beta.read_bytes(), original)
        self.assertFalse(self.backups.exists())

    def test_missing_config_is_not_created(self):
        self.global_setting.unlink(); self.beta.unlink(); self.config.rmdir()
        self.assertFalse(self.apply()['success'])
        self.assertFalse(self.config.exists())

    def test_unrelated_root_is_never_treated_as_jianying(self):
        unrelated = self.base / 'arbitrary'
        result = updates.apply_update_policy(unrelated, self.backups)
        self.assertFalse(result['success'])
        self.assertFalse(unrelated.exists())

    def test_unknown_beta_value_blocks_only_beta(self):
        original = b'[General]\r\njoinBeta=maybe\r\n'
        self.beta.write_bytes(original)
        result = self.apply()
        self.assertFalse(result['success'])
        self.assertFalse(result['beta']['ok'])
        self.assertTrue(result['auto_update']['ok'])
        self.assertEqual(self.beta.read_bytes(), original)

    def test_unknown_auto_value_does_not_block_beta(self):
        original = b'[General]\r\nenableAutoUpdate=1\r\n'
        self.global_setting.write_bytes(original)
        result = self.apply()
        self.assertFalse(result['success'])
        self.assertTrue(result['beta']['ok'])
        self.assertFalse(result['auto_update']['ok'])
        self.assertEqual(self.global_setting.read_bytes(), original)

    def test_duplicate_sections_or_keys_and_invalid_utf8_are_never_rewritten(self):
        for original in [b'[General]\r\njoinBeta=true\r\njoinBeta=false\r\n',
                         b'[General]\r\njoinBeta=true\r\n[general]\r\nfoo=bar\r\n',
                         b'[General]\r\njoinBeta=true\r\nJOINBETA=false\r\n',
                         b'[General]\r\njoinBeta=true\r\nother=\xff\r\n',
                         b'[General]\r\njoinBeta=true\r\ninvalid_line\r\n']:
            with self.subTest(original=original):
                self.beta.write_bytes(original)
                result = self.apply()
                self.assertFalse(result['beta']['ok'])
                self.assertTrue(result['auto_update']['ok'])
                self.assertEqual(self.beta.read_bytes(), original)

    def test_oversized_beta_is_not_loaded_or_replaced(self):
        original = b'x' * (updates.MAX_CONFIG_BYTES + 1)
        self.beta.write_bytes(original)
        result = self.apply()
        self.assertFalse(result['beta']['ok'])
        self.assertTrue(result['auto_update']['ok'])
        self.assertEqual(self.beta.read_bytes(), original)

    def test_foreign_or_stale_lock_is_retained_and_other_setting_continues(self):
        lock = Path(str(self.beta) + '.lock')
        original_lock = b'1\nstale-process\nother-machine\n'
        lock.write_bytes(original_lock)
        original = self.beta.read_bytes()
        with patch.object(updates, 'LOCK_WAIT_SECONDS', 0):
            result = self.apply()
        self.assertFalse(result['beta']['ok'])
        self.assertTrue(result['auto_update']['ok'])
        self.assertEqual(lock.read_bytes(), original_lock)
        self.assertEqual(self.beta.read_bytes(), original)

    def test_lock_payload_matches_qt_three_line_format_and_is_removed_after_release(self):
        lock = self.base / 'sample.lock'
        with patch.dict(os.environ, {'COMPUTERNAME': 'UNITTEST-HOST'}):
            with updates._advisory_lock(lock):
                self.assertEqual(lock.read_text(encoding='utf-8').splitlines(),
                                 [str(os.getpid()), updates._process_name(), 'UNITTEST-HOST'])
                with patch.object(updates, 'LOCK_WAIT_SECONDS', 0):
                    with self.assertRaises(OSError):
                        with updates._advisory_lock(lock):
                            self.fail('lock was acquired twice')
        self.assertFalse(lock.exists())

    def test_lock_fsync_failure_cleans_only_our_complete_lock(self):
        lock = self.base / 'fsync-failure.lock'
        with patch.object(updates.os, 'fsync', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                with updates._advisory_lock(lock):
                    self.fail('incomplete lock must not be acquired')
        self.assertFalse(lock.exists())

    def test_partial_lock_write_failure_cleans_own_prefix_but_keeps_foreign_content(self):
        fdopen = os.fdopen
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                lock = self.base / ('partial-' + str(foreign) + '.lock')
                class BrokenWriter:
                    def __init__(self, stream): self.stream = stream
                    def __enter__(self): return self
                    def __exit__(self, *args): self.stream.close()
                    def write(self, payload):
                        self.stream.write(b'foreign-writer-content' if foreign else payload[:3])
                        self.stream.flush()
                        raise OSError('partial write: disk full')
                with patch.object(updates.os, 'fdopen', side_effect=lambda fd, mode: BrokenWriter(fdopen(fd, mode))):
                    with self.assertRaisesRegex(OSError, 'partial write'):
                        with updates._advisory_lock(lock):
                            self.fail('incomplete lock must not be acquired')
                if foreign:
                    self.assertEqual(lock.read_bytes(), b'foreign-writer-content')
                else:
                    self.assertFalse(lock.exists())

    def test_short_lock_write_is_an_error_and_its_prefix_is_removed(self):
        fdopen = os.fdopen
        lock = self.base / 'short-write.lock'
        class ShortWriter:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def write(self, payload):
                return self.stream.write(payload[:3])
        with patch.object(updates.os, 'fdopen', side_effect=lambda fd, mode: ShortWriter(fdopen(fd, mode))):
            with self.assertRaises(OSError):
                with updates._advisory_lock(lock):
                    self.fail('short lock writes must not succeed')
        self.assertFalse(lock.exists())

    def test_failed_lock_cleanup_never_deletes_replacement_inode_even_if_payload_is_prefix(self):
        lock = self.base / 'replaced.lock'
        replacement = self.base / 'foreign.lock'
        replacement.write_bytes(b'')  # Empty is a prefix, but this inode is not ours.
        close = os.close
        def replace_after_close(descriptor):
            close(descriptor)
            os.replace(replacement, lock)
        with patch.object(updates.os, 'fsync', side_effect=OSError('disk full')):
            with patch.object(updates.os, 'close', side_effect=replace_after_close):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    with updates._advisory_lock(lock):
                        self.fail('incomplete lock must not be acquired')
        self.assertEqual(lock.read_bytes(), b'')

    def test_external_change_after_backup_is_preserved_by_compare_before_replace(self):
        backup = updates._backup_original
        external = b'[General]\r\njoinBeta=true\r\nexternal=newer\r\n'
        def changed(path, data, root):
            result = backup(path, data, root)
            if path == self.beta:
                path.write_bytes(external)
            return result
        with patch.object(updates, '_backup_original', side_effect=changed):
            result = self.apply()
        self.assertFalse(result['beta']['ok'])
        self.assertFalse(result['beta']['changed'])
        self.assertEqual(self.beta.read_bytes(), external)
        self.assertTrue(Path(result['beta']['backup']).exists())
        self.assertTrue(result['auto_update']['ok'])

    def test_new_file_appearing_before_replace_is_not_overwritten(self):
        self.beta.unlink()
        backup = updates._backup_original
        external = b'[General]\r\njoinBeta=true\r\nexternal=appeared\r\n'
        def appeared(path, data, root):
            result = backup(path, data, root)
            if path == self.beta:
                path.write_bytes(external)
            return result
        with patch.object(updates, '_backup_original', side_effect=appeared):
            result = self.apply()
        self.assertFalse(result['beta']['ok'])
        self.assertFalse(result['beta']['changed'])
        self.assertEqual(self.beta.read_bytes(), external)

    def test_backup_failure_prevents_replacement_but_other_item_is_attempted(self):
        original = self.beta.read_bytes()
        backup = updates._backup_original
        def failed(path, data, root):
            if path == self.beta:
                raise OSError('backup unavailable')
            return backup(path, data, root)
        with patch.object(updates, '_backup_original', side_effect=failed):
            result = self.apply()
        self.assertFalse(result['beta']['ok'])
        self.assertFalse(result['beta']['changed'])
        self.assertEqual(self.beta.read_bytes(), original)
        self.assertTrue(result['auto_update']['ok'])

    def test_readback_failure_is_not_reported_success_or_rolled_over_external_data(self):
        verify = updates._verify_readback
        external = b'[General]\r\njoinBeta=true\r\nexternal=after-replace\r\n'
        def failed(path, data, maximum):
            if path == self.beta:
                path.write_bytes(external)
                raise OSError('readback failed')
            return verify(path, data, maximum)
        with patch.object(updates, '_verify_readback', side_effect=failed):
            result = self.apply()
        self.assertFalse(result['success'])
        self.assertFalse(result['beta']['ok'])
        self.assertTrue(result['beta']['changed'])
        self.assertEqual(self.beta.read_bytes(), external)
        self.assertTrue(Path(result['beta']['backup']).exists())
        self.assertTrue(result['auto_update']['ok'])

    def test_backup_under_jianying_user_data_is_rejected(self):
        original = self.beta.read_bytes()
        result = updates.apply_update_policy(self.root, self.root / 'backups')
        self.assertFalse(result['success'])
        self.assertEqual(self.beta.read_bytes(), original)
        self.assertFalse((self.root / 'backups').exists())

    def test_linked_target_and_linked_ancestor_are_rejected(self):
        info = self.beta.lstat()
        actual = Path.lstat
        class Linked:
            st_mode = info.st_mode
            st_file_attributes = 0x400
        for linked in [self.beta, self.root.parent]:
            with self.subTest(linked=linked):
                def lstat(path):
                    return Linked() if path == linked else actual(path)
                with patch.object(Path, 'lstat', lstat):
                    result = self.apply()
                self.assertFalse(result['beta']['ok'])
                self.assertIn('链接', result['beta']['error'])

    def test_non_boolean_policy_argument_is_rejected(self):
        with self.assertRaises(TypeError):
            self.apply('false')


class PreferenceMergeTests(TemporaryProfile):
    def setUp(self):
        super().setUp()
        self.preferences = self.base / 'assistant' / 'settings.json'

    def load(self):
        return json.loads(self.preferences.read_text(encoding='utf-8'))

    def test_default_and_only_literal_false_opt_out(self):
        for value in [None, {}, {updates.PREFERENCE: None}, {updates.PREFERENCE: 0},
                      {updates.PREFERENCE: ''}, {updates.PREFERENCE: 'false'}, {updates.PREFERENCE: True}]:
            self.assertTrue(updates.disables_auto_update(value))
        self.assertFalse(updates.disables_auto_update({updates.PREFERENCE: False}))

    def test_first_save_adds_default_and_preserves_values(self):
        result = updates.merge_preferences(self.preferences, {'output': 'D:/delivery', 'custom': {'a': 1}})
        self.assertTrue(result[updates.PREFERENCE])
        self.assertEqual(result, self.load())
        self.assertFalse(Path(str(self.preferences) + '.lock').exists())

    def test_stale_window_or_advanced_save_cannot_erase_explicit_false(self):
        updates.merge_preferences(self.preferences, {'unknown': 'kept', 'first': 1}, explicit_disable=False)
        stale = {'output': 'new', updates.PREFERENCE: True}
        result = updates.merge_preferences(self.preferences, stale)
        self.assertFalse(result[updates.PREFERENCE])
        result = updates.merge_preferences(self.preferences, {'advanced_setting': 3})
        self.assertFalse(result[updates.PREFERENCE])
        self.assertEqual(result['unknown'], 'kept')
        self.assertEqual(result['first'], 1)
        self.assertEqual(result, self.load())

    def test_only_explicit_bool_changes_preference_in_either_direction(self):
        updates.merge_preferences(self.preferences, {}, explicit_disable=False)
        self.assertTrue(updates.merge_preferences(self.preferences, {}, explicit_disable=True)[updates.PREFERENCE])
        self.assertTrue(updates.merge_preferences(self.preferences, {updates.PREFERENCE: False})[updates.PREFERENCE])
        self.assertFalse(updates.merge_preferences(self.preferences, {}, explicit_disable=False)[updates.PREFERENCE])
        with self.assertRaises(TypeError):
            updates.merge_preferences(self.preferences, {}, explicit_disable=0)

    def test_ordinary_save_keeps_current_true_despite_stale_false_snapshot(self):
        updates.merge_preferences(self.preferences, {}, explicit_disable=True)
        self.assertTrue(updates.merge_preferences(self.preferences, {updates.PREFERENCE: False})[updates.PREFERENCE])

    def test_semantically_unchanged_preferences_do_not_rewrite_file(self):
        updates.merge_preferences(self.preferences, {'a': 1})
        original = self.preferences.read_bytes(), self.preferences.stat().st_mtime_ns
        with patch.object(updates, '_atomic_replace', side_effect=AssertionError('unexpected rewrite')):
            result = updates.merge_preferences(self.preferences, {'a': 1})
        self.assertTrue(result[updates.PREFERENCE])
        self.assertEqual(original, (self.preferences.read_bytes(), self.preferences.stat().st_mtime_ns))

    def test_bad_json_invalid_utf8_nonobject_duplicate_keys_and_nan_are_preserved(self):
        self.preferences.parent.mkdir()
        for original in [b'{broken', b'{"x":"\xff"}', b'[]', b'{"x":1,"x":2}', b'{"x":NaN}']:
            with self.subTest(original=original):
                self.preferences.write_bytes(original)
                with self.assertRaises(ValueError):
                    updates.merge_preferences(self.preferences, {'other': 1})
                self.assertEqual(self.preferences.read_bytes(), original)
                self.assertFalse(Path(str(self.preferences) + '.lock').exists())

    def test_preference_lock_is_never_stolen(self):
        self.preferences.parent.mkdir()
        lock = Path(str(self.preferences) + '.lock')
        original = b'42\nother\nhost\n'
        lock.write_bytes(original)
        with patch.object(updates, 'LOCK_WAIT_SECONDS', 0):
            with self.assertRaises(OSError):
                updates.merge_preferences(self.preferences, {})
        self.assertEqual(lock.read_bytes(), original)
        self.assertFalse(self.preferences.exists())

    def test_uncooperative_external_preference_write_is_not_overwritten(self):
        updates.merge_preferences(self.preferences, {'a': 1}, explicit_disable=False)
        actual = updates._require_unchanged
        external = b'{"outside":"new","disable_jianying_auto_update":false}'
        def changed(path, expected, maximum):
            if path == self.preferences:
                path.write_bytes(external)
            return actual(path, expected, maximum)
        with patch.object(updates, '_require_unchanged', side_effect=changed):
            with self.assertRaises(ValueError):
                updates.merge_preferences(self.preferences, {'b': 2})
        self.assertEqual(self.preferences.read_bytes(), external)
        self.assertFalse(list(self.preferences.parent.glob('*.tmp')))

    def test_two_windows_merge_unknown_keys_and_preserve_false(self):
        updates.merge_preferences(self.preferences, {'initial': 1}, explicit_disable=False)
        barrier = threading.Barrier(2)
        errors = []
        def save(index):
            try:
                barrier.wait(timeout=2)
                updates.merge_preferences(self.preferences, {str(index): index, updates.PREFERENCE: True})
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=save, args=(index,)) for index in (1, 2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(timeout=3)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertFalse(errors)
        self.assertEqual(self.load(), {'initial': 1, '1': 1, '2': 2, updates.PREFERENCE: False})


class SavedPolicyConcurrencyTests(TemporaryProfile):
    def setUp(self):
        super().setUp()
        self.preferences = self.base / 'assistant' / 'settings.json'

    def saved_policy(self):
        return updates.apply_saved_update_policy(self.root, self.backups, self.preferences)

    def test_delayed_old_worker_reads_the_new_explicit_choice_instead_of_captured_bool(self):
        updates.merge_preferences(self.preferences, {}, explicit_disable=False)
        scheduled = threading.Event()
        execute = threading.Event()
        results, errors = [], []
        def old_worker():
            try:
                # This worker was scheduled when updates were allowed. Its API
                # cannot carry that old bool into the later execution.
                scheduled.set()
                if not execute.wait(2): raise TimeoutError('worker not released')
                results.append(self.saved_policy())
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=old_worker)
        worker.start()
        self.assertTrue(scheduled.wait(2))
        try:
            updates.merge_preferences(self.preferences, {}, explicit_disable=True)
        finally:
            execute.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertTrue(results[0]['success'])
        self.assertTrue(results[0]['disable_auto_update'])
        self.assertTrue(results[0]['preference_verified'])
        self.assertFalse(results[0]['auto_update']['value'])
        self.assertIn(b'enableAutoUpdate=false', self.global_setting.read_bytes())

    def test_save_waits_for_active_policy_then_last_explicit_choice_and_policy_win(self):
        updates.merge_preferences(self.preferences, {}, explicit_disable=False)
        executing = threading.Event()
        release = threading.Event()
        saving = threading.Event()
        saved = threading.Event()
        reports, errors = {}, []
        actual = updates.apply_update_policy
        def controlled(root, backups, disabled=True):
            if not disabled:
                executing.set()
                if not release.wait(2): raise TimeoutError('policy not released')
            return actual(root, backups, disabled)
        def first_policy():
            try: reports['first'] = self.saved_policy()
            except Exception as exc: errors.append(exc)
        def new_choice():
            try:
                saving.set()
                updates.merge_preferences(self.preferences, {'other': 'preserved'}, explicit_disable=True)
                saved.set()
                reports['last'] = self.saved_policy()
            except Exception as exc:
                errors.append(exc)
        with patch.object(updates, 'apply_update_policy', side_effect=controlled):
            first = threading.Thread(target=first_policy)
            first.start()
            self.assertTrue(executing.wait(2))
            last = threading.Thread(target=new_choice)
            last.start()
            self.assertTrue(saving.wait(2))
            try:
                self.assertFalse(saved.wait(.1), 'preference save crossed the active policy lock')
                self.assertTrue(Path(str(self.preferences) + '.lock').exists())
            finally:
                release.set()
                first.join(timeout=3)
                last.join(timeout=3)
        self.assertFalse(first.is_alive() or last.is_alive())
        self.assertFalse(errors)
        self.assertTrue(saved.is_set())
        self.assertTrue(reports['first']['success'])
        self.assertFalse(reports['first']['disable_auto_update'])
        self.assertTrue(reports['last']['success'])
        self.assertTrue(reports['last']['disable_auto_update'])
        self.assertFalse(reports['last']['auto_update']['value'])
        self.assertTrue(json.loads(self.preferences.read_text(encoding='utf-8'))[updates.PREFERENCE])
        self.assertIn(b'enableAutoUpdate=false', self.global_setting.read_bytes())
        self.assertIn(b'joinBeta=false', self.beta.read_bytes())

    def test_corrupt_or_ambiguous_preferences_never_guess_auto_and_still_close_beta(self):
        self.preferences.parent.mkdir()
        for original in [b'{broken', b'[]', b'{"x":NaN}', b'{"x":Infinity}',
                         b'{"disable_jianying_auto_update":false,"disable_jianying_auto_update":true}',
                         b'{"x":"\xff"}']:
            with self.subTest(original=original):
                self.preferences.write_bytes(original)
                self.beta.write_bytes(b'[General]\r\njoinBeta=true\r\n')
                global_before = self.global_setting.read_bytes(), self.global_setting.stat().st_mtime_ns
                result = self.saved_policy()
                self.assertFalse(result['success'])
                self.assertFalse(result['preference_verified'])
                self.assertIsNone(result['disable_auto_update'])
                self.assertTrue(result['beta']['ok'])
                self.assertFalse(result['beta']['value'])
                self.assertFalse(result['auto_update']['ok'])
                self.assertFalse(result['auto_update']['changed'])
                self.assertIsNone(result['auto_update']['value'])
                self.assertEqual(self.preferences.read_bytes(), original)
                self.assertEqual(global_before, (self.global_setting.read_bytes(), self.global_setting.stat().st_mtime_ns))
                self.assertIn(b'joinBeta=false', self.beta.read_bytes())

    def test_unavailable_preference_lock_is_preserved_and_does_not_block_beta(self):
        self.preferences.parent.mkdir()
        lock = Path(str(self.preferences) + '.lock')
        content = b'100\nother-app\nhost\n'
        lock.write_bytes(content)
        original = self.global_setting.read_bytes()
        with patch.object(updates, 'LOCK_WAIT_SECONDS', 0):
            result = self.saved_policy()
        self.assertFalse(result['success'])
        self.assertFalse(result['preference_verified'])
        self.assertTrue(result['beta']['ok'])
        self.assertIsNone(result['auto_update']['value'])
        self.assertFalse(result['auto_update']['ok'])
        self.assertEqual(self.global_setting.read_bytes(), original)
        self.assertEqual(lock.read_bytes(), content)
        self.assertFalse(self.preferences.exists())
        self.assertIn(b'joinBeta=false', self.beta.read_bytes())

    def test_missing_preferences_use_default_under_lock_without_creating_json(self):
        result = self.saved_policy()
        self.assertTrue(result['success'])
        self.assertTrue(result['disable_auto_update'])
        self.assertTrue(result['preference_verified'])
        self.assertEqual(result['preference_source'], 'default')
        self.assertFalse(self.preferences.exists())
        self.assertFalse(Path(str(self.preferences) + '.lock').exists())


if __name__ == '__main__':
    unittest.main()
