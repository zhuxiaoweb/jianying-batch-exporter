import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jyexport.advanced import AdvancedAdapter
from jyexport.engine import EngineAdapter
from jyexport.storage import sha
from tests.test_advanced import document
from tests.test_engine import document as simple_document


class Schema193Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.content = self.root/'draft_content.json'

    def read(self, data, advanced=True, normalized=None):
        self.content.write_text(json.dumps(data), 'utf-8')
        before = sha(self.content)
        adapter = (AdvancedAdapter if advanced else EngineAdapter)(self.root, self.root)
        with patch.object(adapter, 'verify', return_value={}), patch.object(adapter, '_roundtrip_model',
                side_effect=lambda value: copy.deepcopy(value) if normalized is None else normalized):
            result = adapter.read(self.content)
        self.assertEqual(sha(self.content), before)
        self.assertEqual(result, data)
        self.assertFalse(adapter.last_compatibility['version_rewritten'])
        return adapter

    def test_standard_193_root_preserved(self):
        data = simple_document(); data.update(new_version='193.0.0', version=360000)
        adapter = self.read(data, False)
        self.assertEqual(adapter.last_compatibility['declared_version'], '193.0.0')

    def test_advanced_193_root_and_child_preserved(self):
        data = document(self.root); data.update(new_version='193.0.0', version=360000)
        data['materials']['drafts'][0]['draft']['new_version'] = '193.0.0'
        self.assertEqual(len(self.read(data).last_compatibility['scopes']), 2)

    def test_mixed_accepted_parent_child_versions(self):
        for parent, child in [('193.0.0','189.0.0'),('191.0.0','193.0.0')]:
            data = document(self.root); data['new_version'] = parent
            data['materials']['drafts'][0]['draft']['new_version'] = child
            self.read(data)

    def test_unverified_standard_versions_still_reject_before_model(self):
        for version in ('136.0.0','190.0.0','192.0.0','193.0.1','193.1.0','194.0.0'):
            data = simple_document(); data['new_version'] = version
            self.content.write_text(json.dumps(data), 'utf-8')
            adapter = EngineAdapter(self.root,self.root)
            with patch.object(adapter,'verify',return_value={}), patch.object(adapter,'_roundtrip_model') as model:
                with self.assertRaisesRegex(ValueError, '结构'): adapter.read(self.content)
                model.assert_not_called()

    def test_unverified_advanced_child_version_reports_scope_and_version(self):
        for version in ('190.0.0','192.0.0','193.0.1','194.0.0'):
            data=document(self.root);data['new_version']='193.0.0'
            data['materials']['drafts'][0]['draft']['new_version']=version
            self.content.write_text(json.dumps(data),'utf-8')
            adapter=AdvancedAdapter(self.root,self.root)
            with patch.object(adapter,'verify',return_value={}), patch.object(adapter,'_roundtrip_model') as model:
                with self.assertRaisesRegex(ValueError, '子草稿.*'+version): adapter.read(self.content)
                model.assert_not_called()

    def test_unverified_advanced_root_reports_actual_version(self):
        data=document(self.root);data['new_version']='192.0.0'
        self.content.write_text(json.dumps(data),'utf-8')
        adapter=AdvancedAdapter(self.root,self.root)
        with patch.object(adapter,'verify',return_value={}), patch.object(adapter,'_roundtrip_model'):
            with self.assertRaisesRegex(ValueError,'根草稿.*192.0.0'):adapter.read(self.content)

    def test_malformed_versions_still_reject(self):
        for version in (193, None, '193', '193.0', '193.0.0-extra', ''):
            data=simple_document();data['new_version']=version
            with self.assertRaises(ValueError):self.read(data,False)

    def test_standard_nondefault_field_loss_still_rejects(self):
        data=simple_document();data['new_version']='193.0.0'
        normalized=copy.deepcopy(data);normalized['tracks'][0]['segments'][0]['volume']=0
        with self.assertRaisesRegex(ValueError,'有效字段'):self.read(data,False,normalized)

    def test_child_field_loss_still_rejects(self):
        data=document(self.root);data['new_version']='193.0.0'
        data['materials']['drafts'][0]['draft']['new_version']='193.0.0'
        normalized=copy.deepcopy(data);normalized['materials']['drafts'][0]['draft']['fps']=30
        with self.assertRaisesRegex(ValueError,'有效字段'):self.read(data,True,normalized)

    def test_unknown_child_audio_processing_still_rejects(self):
        data=document(self.root);data['new_version']='193.0.0'
        data['materials']['drafts'][0]['draft']['new_version']='193.0.0'
        data['materials']['drafts'][0]['draft']['materials']['audios'][0]['enable_ai_denoise']=True
        with self.assertRaisesRegex(ValueError,'人声处理'):self.read(data)

    def test_unknown_effect_mask_still_rejects(self):
        data=document(self.root);data['new_version']='193.0.0'
        data['materials']['audios'][0]['effect_mask']=[{'resource_type':'heart'}]
        with self.assertRaisesRegex(ValueError,'蒙版形状'):self.read(data)

    def test_previously_supported_standard_versions_keep_behavior(self):
        for version in ('137.0.0','189.0.0','191.0.0'):
            data=simple_document();data['new_version']=version
            self.read(data,False)


if __name__=='__main__':unittest.main()
