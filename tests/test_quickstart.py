import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from jyexport.gui import App


class QuickstartTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.environment=patch.dict(os.environ,{'LOCALAPPDATA':str(self.path/'profile')});self.environment.start()
        self.base=patch('jyexport.gui.sys_base',return_value=str(self.path));self.base.start()
        self.root=tk.Tk();self.root.withdraw();self.app=App(self.root);self.root.update_idletasks()
    def tearDown(self):
        self.root.destroy();self.base.stop();self.environment.stop();self.temp.cleanup()

    def test_simple_mode_is_default_and_legacy_controls_are_hidden(self):
        app=self.app
        self.assertEqual(app.tabs.select(),str(app.simple_home))
        self.assertFalse(app.simple_advanced.get())
        for page in app.simple_hidden_tabs:self.assertEqual(app.tabs.tab(page,'state'),'hidden')
        self.assertEqual(app.simple_paths.winfo_manager(),'')
        for widget in app.simple_hidden_frames:self.assertEqual(widget.winfo_manager(),'')
        self.assertTrue(all(variable.get() for variable in app.options.values()))
        self.assertTrue(app.want_srt.get())
        self.assertIn('1 选择草稿',[app.tabs.tab(tab,'text') for tab in app.tabs.tabs()])
        visible=[app.tabs.tab(tab,'text') for tab in app.tabs.tabs() if app.tabs.tab(tab,'state')!='hidden']
        self.assertEqual(visible,['开始使用','1 选择草稿','2 确认内容','3 导出与审核','帮助与日志'])

    def test_advanced_controls_can_be_shown_and_hidden_without_losing_task(self):
        app=self.app;sentinel={'jobs':[]};app.session=sentinel
        app.simple_advanced.set(True);app.simple_toggle()
        for page in app.simple_hidden_tabs:self.assertEqual(app.tabs.tab(page,'state'),'normal')
        self.assertEqual(app.simple_paths.winfo_manager(),'pack')
        app.simple_advanced.set(False);app.simple_toggle()
        self.assertIs(app.session,sentinel)
        self.assertEqual(app.simple_paths.winfo_manager(),'')

    def test_single_episode_can_be_analyzed_without_selecting_one_of_five_rows(self):
        app=self.app;job={'id':'case'};app.session={'jobs':[job]}
        app.task_rows={}
        for key in ('V1','V2','V3','V4','SRT'):
            iid=app.task_tree.insert('','end',values=('demo','18',key,'pending','pending'));app.task_rows[iid]=(job,key)
        app.analyze=Mock();app.simple_analyze()
        app.analyze.assert_called_once_with(True)
        self.assertEqual(len(app.task_tree.selection()),1)

    def test_multiple_episodes_without_selection_request_specific_episode(self):
        app=self.app;app.session={'jobs':[{'id':'one'},{'id':'two'}]};app.analyze=Mock()
        with patch('jyexport.quickstart.messagebox.showinfo') as message:app.simple_analyze()
        message.assert_called_once();app.analyze.assert_not_called()
        self.assertEqual(app.tabs.select(),str(app.task_tab))

    def test_no_session_routes_back_to_draft_selection(self):
        app=self.app;app.engine_export=Mock()
        with patch('jyexport.quickstart.messagebox.showinfo') as message:app.simple_export()
        message.assert_called_once();app.engine_export.assert_not_called()
        self.assertEqual(app.tabs.select(),str(app.simple_draft_tab))


if __name__=='__main__':unittest.main()
