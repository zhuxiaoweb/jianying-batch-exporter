import copy,os,tempfile,tkinter as tk,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from jyexport.easy_ui import EasyApp

class ArchiveChoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.path/'profile')});self.env.start()
        self.base=patch('jyexport.easy_ui.base_directory',return_value=self.path);self.base.start()
        self.root=tk.Tk();self.root.withdraw();self.app=EasyApp(self.root)
        for token in self.root.tk.call('after','info'):self.root.after_cancel(token)
    def tearDown(self):
        self.app.windows.close_all();self.root.destroy();self.base.stop();self.env.stop();self.temp.cleanup()
    def test_unchecked_is_grouped_and_checked_is_legacy_for_new_tasks(self):
        app=self.app;self.assertFalse(app.legacy_layout.get());self.assertIn('新批次',app.layout_choice.cget('text'))
        app.engine_adapter=Mock();app.pending_drafts=[];app.dispatch=lambda operation,callback=None:operation()
        with patch('jyexport.easy_ui.refresh_selection',return_value=[]),patch('jyexport.easy_ui.prepare') as prepare:
            app.begin_selected();self.assertEqual(prepare.call_args.kwargs['archive_mode'],'series')
            app.legacy_layout.set(True);app.begin_selected();self.assertEqual(prepare.call_args.kwargs['archive_mode'],'legacy')
    def test_old_task_resume_never_inserts_a_new_layout_from_current_checkbox(self):
        app=self.app;old={'jobs':[{'id':'old','directory':str(self.path/'交付/episode')}]};before=copy.deepcopy(old)
        app.environment_ready=True;app.dispatch=Mock();app.legacy_layout.set(False)
        with patch('jyexport.easy_ui.filedialog.askopenfilename',return_value='任务清单.json'),patch('jyexport.easy_ui.read_batch',return_value=(self.path,old)):
            app.resume()
        self.assertEqual(app.session,before)

if __name__=='__main__':unittest.main()
