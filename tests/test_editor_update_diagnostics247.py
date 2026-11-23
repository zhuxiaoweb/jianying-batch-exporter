"""Read-only update observations preserve the two-field policy contract."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from jyexport import editor_updates as updates
from jyexport.easy_ui import check_editor_update_policy, editor_update_summary


class UpdateDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='jy_update_diagnostics_')
        self.base = Path(self.temporary.name)
        self.profile = self.base / 'JianyingPro/User Data'
        self.config = self.profile / 'Config'
        self.config.mkdir(parents=True)
        self.global_path = self.config / 'globalSetting'
        self.update_path = self.config / 'updateInfo'
        self.global_path.write_bytes(b'[General]\r\nenableAutoUpdate=false\r\nother=keep\r\n')
        self.update_path.write_bytes(b'[General]\r\njoinBeta=false\r\n'
                                     b'need_show_automatic_updates_popup=false\r\n'
                                     b'11.8.0_lastTimeShow=@DateTime(\\0\\0\\0\\x10)\r\n')
        self.state = self.base / 'assistant-state'
        self.state.mkdir()
        self.preferences = self.state / 'settings.json'
        self.preferences.write_text(json.dumps({updates.PREFERENCE: True}), encoding='utf-8')
        self.process_patch = patch.object(updates, 'editor_process_state',
                                          return_value={'ok': True, 'running': False, 'process_ids': []})
        self.process_patch.start()

    def tearDown(self):
        self.process_patch.stop()
        self.temporary.cleanup()

    def apply(self):
        return updates.apply_saved_update_policy(self.profile, self.base / 'backups', self.preferences)

    def test_observation_captures_exact_bytes_and_native_records_without_writes(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.global_path, self.update_path)}
        with patch.object(updates, '_advisory_lock', side_effect=AssertionError('read-only observation acquired a writer lock')), \
                patch.object(updates, '_backup_original', side_effect=AssertionError('read-only observation backed up files')), \
                patch.object(updates, '_atomic_replace', side_effect=AssertionError('read-only observation wrote files')):
            report = updates.inspect_update_settings(self.profile)
        self.assertTrue(report['ok'])
        self.assertEqual(report['profile_root'], str(self.profile))
        self.assertRegex(report['sampled_at'], r'^\d{4}-\d{2}-\d{2}T')
        for filename, path in (('globalSetting', self.global_path), ('updateInfo', self.update_path)):
            item = report['files'][filename]
            self.assertEqual(item['sha256'], hashlib.sha256(before[path][0]).hexdigest())
            self.assertFalse(item['value'])
            self.assertEqual(item['path'], str(path))
        notice = report['version_reminders'][0]
        self.assertEqual(notice['version'], '11.8.0')
        self.assertTrue(notice['last_time_show']['present'])
        self.assertFalse(notice['nonotify']['present'])
        self.assertIsNone(notice['nonotify']['raw'])
        self.assertFalse(report['reminder_suppression_verified'])
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})

    def test_raw_nonotify_marker_carries_no_claim_of_suppression(self):
        self.update_path.write_bytes(self.update_path.read_bytes() + b'11.8.0_nonotify=true\r\n11.8.0_checkednotify=false\r\n')
        report = self.apply()
        observed = report['disk_observation']
        notice = observed['version_reminders'][0]
        self.assertEqual(notice['nonotify'], {'present': True, 'raw': 'true'})
        self.assertEqual(notice['checkednotify'], {'present': True, 'raw': 'false'})
        self.assertFalse(observed['reminder_suppression_verified'])
        summary = editor_update_summary(report)
        self.assertIn('弹窗效果待原生核验', summary)
        self.assertNotIn('已屏蔽', summary)
        self.assertIn('自动下载/安装设置', summary)

    def test_observation_failure_preserves_successful_policy_and_other_fields(self):
        before_global = self.global_path.read_bytes().replace(b'false', b'true', 1)
        before_update = self.update_path.read_bytes().replace(b'joinBeta=false', b'joinBeta=true', 1)
        self.global_path.write_bytes(before_global)
        self.update_path.write_bytes(before_update)
        with patch.object(updates, 'inspect_update_settings', side_effect=OSError('diagnostic fixture failure')):
            result = self.apply()
        self.assertTrue(result['success'])
        self.assertFalse(result['disk_observation']['ok'])
        self.assertIn('diagnostic fixture failure', result['disk_observation']['error'])
        self.assertEqual(self.global_path.read_bytes(), before_global.replace(b'enableAutoUpdate=true', b'enableAutoUpdate=false'))
        self.assertEqual(self.update_path.read_bytes(), before_update.replace(b'joinBeta=true', b'joinBeta=false'))
        self.assertIn('升级提醒诊断未完成', editor_update_summary(result))

    def test_observation_runs_after_the_preference_lock_is_released(self):
        actual = updates.inspect_update_settings
        def inspect(profile):
            self.assertFalse(Path(str(self.preferences) + '.lock').exists())
            return actual(profile)
        with patch.object(updates, 'inspect_update_settings', side_effect=inspect):
            result = self.apply()
        self.assertTrue(result['success'])
        self.assertTrue(result['disk_observation']['matches_policy'])

    def test_changed_readback_is_visible_without_reporting_closed_state(self):
        observation = updates.inspect_update_settings(self.profile)
        observation['files']['globalSetting']['value'] = True
        with patch.object(updates, 'inspect_update_settings', return_value=observation):
            result = self.apply()
        self.assertTrue(result['success'])
        self.assertFalse(result['disk_observation']['matches_policy'])
        summary = editor_update_summary(result)
        self.assertIn('自动更新：磁盘值已变化', summary)
        self.assertNotIn('自动更新：已关闭', summary)

    def test_running_editor_requires_native_reopen_verification(self):
        with patch.object(updates, 'editor_process_state',
                          return_value={'ok': True, 'running': True, 'process_ids': [123]}):
            result = self.apply()
        self.assertEqual(result['disk_observation']['editor_process']['process_ids'], [123])
        self.assertIn('检测到剪映正在运行', editor_update_summary(result))
        self.assertIn('重新打开剪映后核验', editor_update_summary(result))

    def test_corrupt_update_info_is_recorded_without_altering_either_file(self):
        self.update_path.write_bytes(b'\xff\x00\x01')
        before = {path: path.read_bytes() for path in (self.global_path, self.update_path)}
        result = updates.inspect_update_settings(self.profile)
        self.assertFalse(result['ok'])
        self.assertTrue(result['files']['globalSetting']['ok'])
        self.assertFalse(result['files']['updateInfo']['ok'])
        self.assertEqual(result['files']['updateInfo']['sha256'], hashlib.sha256(before[self.update_path]).hexdigest())
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_helper_receipt_contains_observed_profile_sha_and_time(self):
        with patch('jyexport.easy_ui.user_data', return_value=self.profile):
            result = check_editor_update_policy(self.state)
        receipt = json.loads((self.state / '剪映更新设置核验.json').read_text('utf-8'))
        self.assertEqual(receipt, result)
        self.assertEqual(receipt['disk_observation']['profile_root'], str(self.profile))
        self.assertIn('sampled_at', receipt['disk_observation'])
        self.assertEqual(receipt['disk_observation']['files']['updateInfo']['sha256'],
                         hashlib.sha256(self.update_path.read_bytes()).hexdigest())

    def test_process_probe_failure_is_independent_of_disk_observation(self):
        with patch.object(updates, 'editor_process_state',
                          return_value={'ok': False, 'running': None, 'error': 'process fixture failure'}):
            result = self.apply()
        self.assertTrue(result['success'])
        self.assertTrue(result['disk_observation']['ok'])
        self.assertIsNone(result['disk_observation']['editor_process']['running'])


class ProcessObservationTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows process-list format')
    def test_process_list_is_read_once_with_a_bounded_hidden_command(self):
        process = Mock(returncode=0, stdout=b'"JianyingPro.exe","2468","Console","1","200 K"\r\n')
        with patch.object(updates.subprocess, 'run', return_value=process) as run:
            observed = updates.editor_process_state()
        self.assertEqual(observed, {'ok': True, 'running': True, 'process_ids': [2468]})
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(run.call_args.args[0][-1], 'IMAGENAME eq JianyingPro.exe')


if __name__ == '__main__':
    unittest.main()
