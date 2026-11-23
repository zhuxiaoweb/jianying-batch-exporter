import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jyexport.native_prepare import commit_prepared_payload,resolve_copy,variant_for_native,verify_new_native_copy
from jyexport.storage import snapshot


class NativePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.folder=self.root/'new-native-copy';self.timeline=self.folder/'Timelines/episode'
        self.timeline.mkdir(parents=True)
        for name in ('draft_content.json','draft_content.json.bak','template-2.tmp'):
            (self.timeline/name).write_bytes(b'original encrypted data')
        self.backup=snapshot(self.folder,self.root/'backup')
    def tearDown(self):self.temp.cleanup()

    def test_original_or_preexisting_project_is_not_a_new_native_copy(self):
        before={'all_draft_store':[{'draft_id':'source','draft_fold_path':str(self.root/'source'),'draft_name':'Source'}]}
        with self.assertRaises(ValueError):verify_new_native_copy(before,before,'source','source')

    def test_binding_follows_editor_folder_rename_by_id(self):
        before={'all_draft_store':[{'draft_id':'source','draft_fold_path':str(self.root/'source'),'draft_name':'Source'}]}
        current={'all_draft_store':[{'draft_id':'new','draft_fold_path':str(self.folder),'draft_name':'Prepared V2'}]}
        binding=verify_new_native_copy(before,current,'source','new')
        current['all_draft_store'][0]['draft_fold_path']=str(self.root/'renamed-by-editor')
        self.assertEqual(resolve_copy(current,binding),self.root/'renamed-by-editor')

    def test_advanced_fields_are_kept_for_native_renderer(self):
        data={'id':'episode','new_version':'191.0.0','duration':1000000,
              'tracks':[{'id':'audio','type':'audio','segments':[{'id':'bgm','material_id':'music'},{'id':'voice','material_id':'voice','extra_material_refs':['separation']}]},
                        {'id':'text','type':'text','segments':[{'id':'sub','material_id':'sub'}]}],
              'materials':{'audios':[{'id':'music','type':'music'},{'id':'voice','type':'audio','matting':{'flag':1,'path':'original-mask'}}],
                           'vocal_separations':[{'id':'separation','type':'vocal_separation','choice':1,'production_path':'processed-voice'}],
                           'texts':[{'id':'sub','type':'subtitle','content':'Edited subtitles'}]}}
        before=copy.deepcopy(data)
        roles={'audio:bgm':'背景音乐','audio:voice':'人声','text:sub':'字幕'}
        result=variant_for_native(data,roles,'V2')
        self.assertEqual(result['materials'],data['materials'])
        self.assertEqual(result['tracks'][0]['segments'],[data['tracks'][0]['segments'][1]])
        self.assertEqual(result['new_version'],'191.0.0');self.assertEqual(data,before)

    def test_unproven_closed_state_does_not_write_any_file(self):
        with self.assertRaises(ValueError):commit_prepared_payload(self.folder,'episode',b'new encrypted data',self.backup,self.root/'journal.json',lambda:False)
        self.assertTrue(all(path.read_bytes()==b'original encrypted data' for path in self.timeline.iterdir()))

    def test_copy_change_since_backup_blocks_transaction(self):
        (self.timeline/'draft_content.json').write_bytes(b'changed by editor')
        with self.assertRaises(ValueError):commit_prepared_payload(self.folder,'episode',b'new',self.backup,self.root/'journal.json',lambda:True)
        self.assertEqual((self.timeline/'draft_content.json.bak').read_bytes(),b'original encrypted data')

    def test_success_records_all_three_files(self):
        result=commit_prepared_payload(self.folder,'episode',b'prepared encrypted data',self.backup,self.root/'journal.json',lambda:True)
        self.assertEqual(result['state'],'prepared');self.assertEqual(len(result['written']),3)
        self.assertTrue(all(path.read_bytes()==b'prepared encrypted data' for path in self.timeline.iterdir()))

    def test_partial_write_failure_rolls_back_only_known_copy_changes(self):
        import jyexport.native_prepare as module
        real=module.os.replace;calls=[]
        def failing(source,target):
            if Path(target).parent==self.timeline:
                calls.append(target)
                if len(calls)==2:raise OSError('simulated second content replacement failure')
            return real(source,target)
        with patch.object(module.os,'replace',side_effect=failing):
            with self.assertRaises(OSError):commit_prepared_payload(self.folder,'episode',b'new encrypted data',self.backup,self.root/'journal.json',lambda:True)
        result=json.loads((self.root/'journal.json').read_text('utf-8'))
        self.assertEqual(result['state'],'failed_rolled_back')
        self.assertEqual(len(result['written']),1);self.assertEqual(len(result['rollback']),1)
        for name in ('draft_content.json','draft_content.json.bak','template-2.tmp'):
            self.assertEqual((self.timeline/name).read_bytes(),b'original encrypted data')


if __name__=='__main__':unittest.main()
