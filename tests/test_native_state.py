import tempfile
import unittest
from pathlib import Path
from jyexport.native_state import NativeJournal


class NativeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jy_native_journal_')
        self.path=Path(self.temp.name)/'run.json'
        self.journal=NativeJournal.create(self.path,{'copy_id':'new-copy','job_id':'job'})
    def tearDown(self):self.temp.cleanup()
    def ready(self):
        for key in ('caption','bgm1','bgm2'):self.journal.original(key,True,'observed-native')
        self.journal.ready(['caption','bgm1','bgm2'])
    def test_partial_preflight_cannot_start_changes(self):
        self.journal.original('bgm1',True,'observed')
        with self.assertRaises(ValueError):self.journal.ready(['bgm1','bgm2'])
        with self.assertRaises(ValueError):self.journal.intent('bgm1',True,False,'menu')
    def test_process_loss_after_click_retains_unknown_intent(self):
        self.ready();self.journal.intent('bgm1',True,False,'停用菜单')
        recovered=NativeJournal.open(self.path)
        self.assertTrue(recovered.needs_recovery())
        with self.assertRaises(ValueError):recovered.restored()
        recovered.observe('bgm1',False,'原生显示启用片段')
        recovered.intent('bgm1',False,True,'恢复意图')
        recovered.observe('bgm1',True,'原生显示停用片段')
        recovered.restored();self.assertTrue(recovered.data['restored'])
    def test_failure_cannot_claim_restored_when_one_clip_changed(self):
        self.ready();self.journal.intent('bgm2',True,False,'native')
        self.journal.observe('bgm2',False,'native')
        self.journal.fail('timeout')
        self.assertEqual(NativeJournal.open(self.path).data['state'],'needs_recovery')
        self.assertFalse(self.journal.data['complete'])
    def test_initial_state_cannot_be_replaced_by_later_state(self):
        self.ready()
        with self.assertRaises(ValueError):self.journal.original('bgm1',False,'later')
    def test_complete_requires_both_real_exports_and_restore(self):
        self.ready();srt=self.path.parent/'s.srt';srt.write_text('1\n00:00:00,000 --> 00:00:00,400\ntest\n')
        self.journal.restored()
        with self.assertRaises(ValueError):self.journal.finish(srt)
        for key in ('V2','V3'):
            path=self.path.parent/(key+'.mp4');path.write_bytes(b'fixture'*200)
            self.journal.export(key,path,{'full_decode':True,'width':1080,'height':1920,'fps':'60','audio_streams':1},'native success')
        self.journal.finish(srt)
        self.assertTrue(NativeJournal.open(self.path).data['complete'])
    def test_repeated_intent_needs_new_observation(self):
        self.ready();self.journal.intent('caption',True,False,'native')
        with self.assertRaises(ValueError):self.journal.intent('caption',True,False,'native')
        self.journal.observe('caption',True,'first click did not act')
        self.journal.intent('caption',True,False,'fresh native menu')
    def test_preempt_before_any_edit_does_not_claim_restoration(self):
        self.journal.fail('USER_CONTROL; executed_steps=0')
        self.assertEqual(self.journal.data['state'],'preflight_failed')
        self.assertFalse(self.journal.data['restored'])
        self.assertFalse(self.journal.needs_recovery())


if __name__=='__main__':unittest.main()
