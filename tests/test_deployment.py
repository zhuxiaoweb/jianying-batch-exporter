import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jyexport import deployment as d


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.base=self.root/'tool';self.base.mkdir()
        self.local=self.root/'system-profile';self.local.mkdir()
        self.environment=patch.dict(os.environ,{'LOCALAPPDATA':str(self.local)})
        self.environment.start()
    def tearDown(self):self.environment.stop();self.temp.cleanup()

    def test_full_system_configuration_moves_to_writable_tool_directory_and_preserves_settings(self):
        legacy=self.local/'JianyingExportAssistant';legacy.mkdir()
        raw=json.dumps({'engine_runtime':'previous runtime','custom':'retained'})
        (legacy/'settings.json').write_text(raw,'utf-8')
        def available(path):return 0 if self.local in Path(path).parents or Path(path)==self.local else 20*1024**3
        with patch.object(d,'free_bytes',side_effect=available):
            selected=d.configuration_directory(self.base)
        self.assertEqual(selected,self.base/'user-config')
        self.assertEqual((selected/'settings.json').read_text('utf-8'),raw)
        self.assertEqual((legacy/'settings.json').read_text('utf-8'),raw)
        with patch.object(d,'free_bytes',return_value=20*1024**3):
            self.assertEqual(d.configuration_directory(self.base),selected)

    def test_low_space_stops_before_download_or_extraction(self):
        with patch.object(d,'free_bytes',return_value=10*1024**2):
            with self.assertRaisesRegex(ValueError,'空间不足'):
                d.check_storage(self.base/'runtime')
        self.assertFalse((self.base/'runtime').exists())

    def test_known_completed_download_is_reused_from_previous_config_directory(self):
        candidate=self.local/'JianyingExportAssistant/HeadlessRuntime/downloads/Jianying_11_6_0_14552.exe'
        candidate.parent.mkdir(parents=True)
        # Sparse size-only selection; real SHA256 validation remains in setup PS.
        with candidate.open('wb') as stream:stream.truncate(d.INSTALLER_BYTES)
        self.assertEqual(d.cached_installer(self.base/'new runtime',self.base/'user-config'),candidate)
        with patch.object(d,'free_bytes',return_value=d.RUNTIME_BYTES+d.RESERVE_BYTES):
            result=d.check_storage(self.base/'new runtime',candidate)
            self.assertTrue(result['installer_reused'])
            with self.assertRaises(ValueError):d.check_storage(self.base/'new runtime')

    def test_custom_choice_is_not_silently_redirected(self):
        requested=self.root/'chosen'
        self.assertEqual(d.recommend_storage(self.base,self.local,requested),requested)
        with patch.object(d,'free_bytes',return_value=0):
            with self.assertRaises(ValueError):d.check_storage(requested)

    def test_runtime_cannot_be_placed_inside_an_existing_draft(self):
        source=self.root/'draft';source.mkdir();(source/'draft_content.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'已有剪映草稿'):
            d.check_storage(source/'runtime')

    def test_adequate_data_location_is_chosen_over_full_legacy_location(self):
        chosen=self.base/'runtime'
        with patch.object(d,'storage_candidates',return_value=[self.local/'HeadlessRuntime',chosen]),patch.object(d,'free_bytes',side_effect=[0,d.REQUIRED_BYTES]):
            self.assertEqual(d.recommend_storage(self.base,self.local),chosen)


if __name__=='__main__':unittest.main()
