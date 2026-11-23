import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from jyexport.storage import atomic_json,sha


class ManifestSharingTests(unittest.TestCase):
    def test_transient_windows_reader_does_not_abort_atomic_write(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name)/'manifest.json';atomic_json(path,{'value':0});before=sha(path)
            replace=os.replace;calls=[]
            def sharing(source,target):
                calls.append(1)
                if len(calls)==1:
                    self.assertEqual(json.loads(path.read_text('utf-8')),{'value':0})
                    error=PermissionError('transient reader');error.winerror=32;raise error
                return replace(source,target)
            with patch('jyexport.storage.os.replace',side_effect=sharing),patch('jyexport.storage.time.sleep'):
                atomic_json(path,{'value':1},expected_sha256=before)
            self.assertEqual(json.loads(path.read_text('utf-8')),{'value':1});self.assertEqual(len(calls),2)
    def test_foreign_change_during_retry_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name)/'manifest.json';atomic_json(path,{'value':0});before=sha(path)
            def sharing(source,target):
                path.write_text('{"foreign":true}',encoding='utf-8')
                error=PermissionError('foreign reader');error.winerror=32;raise error
            with patch('jyexport.storage.os.replace',side_effect=sharing) as replace,patch('jyexport.storage.time.sleep'):
                with self.assertRaisesRegex(ValueError,'其他程序更新'):atomic_json(path,{'value':1},expected_sha256=before)
            self.assertEqual(json.loads(path.read_text('utf-8')),{'foreign':True});self.assertEqual(replace.call_count,1)
    def test_permanent_lock_keeps_original_and_complete_recovery_payload(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name)/'manifest.json';atomic_json(path,{'value':0});before=sha(path)
            error=PermissionError('held reader');error.winerror=32
            with patch('jyexport.storage.os.replace',side_effect=error),patch('jyexport.storage.time.sleep'):
                with self.assertRaises(PermissionError):atomic_json(path,{'value':1},expected_sha256=before)
            self.assertEqual(sha(path),before)
            temporary=list(path.parent.glob('.j-*.tmp'));self.assertEqual(len(temporary),1)
            self.assertEqual(json.loads(temporary[0].read_text('utf-8')),{'value':1})


if __name__=='__main__':unittest.main()
