import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from jyexport.easy_ui import EasyApp
from jyexport.discovery import Draft


class EasyUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.path/'profile')});self.env.start()
        self.base=patch('jyexport.easy_ui.base_directory',return_value=self.path);self.base.start()
        self.root=tk.Tk();self.root.withdraw();self.app=EasyApp(self.root);self.root.update_idletasks()
    def tearDown(self):
        self.root.destroy();self.base.stop();self.env.stop();self.temp.cleanup()

    def draft(self):return Draft('case','test project',str(self.path),str(self.path/'draft_content.json'),'episode','18',True)

    def test_no_notebook_or_intermediate_file_fields_in_operator_interface(self):
        def descendants(parent):
            for widget in parent.winfo_children():yield widget;yield from descendants(widget)
        self.assertFalse(any(widget.winfo_class()=='TNotebook' for widget in descendants(self.root)))
        labels=[widget.cget('text') for widget in descendants(self.root) if widget.winfo_class()=='TButton']
        self.assertIn('开始制作交付包',labels)
        self.assertFalse(any('V2' in str(value) or 'V3' in str(value) for value in labels))
        self.assertIs(self.app.results,self.app.tree)

    def test_ready_machine_starts_selected_drafts_without_manual_backup_analyze_steps(self):
        app=self.app;draft=self.draft();app.drafts=[draft];app.selected={draft.key};app.environment_ready=True
        app.begin_selected=Mock();app.start()
        app.begin_selected.assert_called_once();self.assertEqual(app.pending_drafts,[draft])

    def test_first_start_checks_environment_and_preserves_selected_request(self):
        app=self.app;draft=self.draft();app.drafts=[draft];app.selected={draft.key};app.environment_ready=False;app.profile=None
        app.prepare_environment=Mock();app.begin_selected=Mock();app.start()
        app.prepare_environment.assert_called_once();app.begin_selected.assert_not_called()
        self.assertTrue(app.pending_start);self.assertEqual(app.pending_drafts,[draft])

    def test_completed_button_opens_delivery_instead_of_creating_another_batch(self):
        app=self.app;app.result_mode=True;app.session={'jobs':[{'id':'one','easy_delivery_folder':'ready'}]}
        app.open_delivery=Mock();app.resume=Mock();app.start()
        app.open_delivery.assert_called_once();app.resume.assert_not_called()

    def test_stop_control_is_available_while_regular_actions_are_disabled(self):
        app=self.app
        def descendants(parent):
            for widget in parent.winfo_children():yield widget;yield from descendants(widget)
        stop=next(widget for widget in descendants(self.root) if widget.winfo_class()=='TButton' and widget.cget('text')=='停止')
        for widget in app.buttons:widget.configure(state='disabled')
        self.assertNotIn(stop,app.buttons);self.assertNotEqual(str(stop.cget('state')),'disabled')
        stop.invoke();self.assertTrue(app.cancel.is_set())

    def test_short_screen_reduces_table_height_to_keep_actions_visible(self):
        with patch.object(self.root,'winfo_screenheight',return_value=720):self.app.fit_height()
        self.assertLessEqual(self.root.winfo_reqheight(),620)

    def test_analysis_failure_is_visible_on_the_exact_episode_row(self):
        app=self.app
        app.session={'jobs':[{'id':'failed','draft':{'name':'case','timeline_name':'24'},
                            'easy_analysis_error':'unsupported voice result'},
                           {'id':'pending','draft':{'name':'case','timeline_name':'25'}}]}
        app.show_results()
        self.assertIn('unsupported voice result',app.results.set('failed','state'))
        self.assertEqual(app.results.set('pending','state'),'待分析')

    def test_failed_episode_explains_its_reason_instead_of_opening_an_empty_folder(self):
        app=self.app;app.session={'jobs':[{'id':'bad','draft':{'name':'case','timeline_name':'24'},
            'easy_analysis_error':'unsupported file','easy_delivery_state':'分析未完成：unsupported file'}]}
        app.show_results()
        with patch('jyexport.easy_ui.messagebox.showinfo') as notice,patch('jyexport.easy_ui.os.startfile') as opened:
            app.open_delivery()
        opened.assert_not_called();self.assertIn('unsupported file',notice.call_args.args[1])

    def test_parallel_settings_preserve_simple_default_interface(self):
        app=self.app
        self.assertEqual(app.batch_jobs.get(),'4');self.assertEqual(app.native_limit.get(),'4')
        app.batch_jobs.set('1');app.native_limit.set('1');app.save_preferences()
        import json
        saved=json.loads((app.state_dir/'settings.json').read_text('utf-8'))
        self.assertEqual((saved['batch_jobs'],saved['native_limit']),(1,1))

    def test_fully_verified_resume_does_not_send_the_batch_through_renderer_again(self):
        app=self.app;app.environment_ready=True
        (self.path/'任务清单.json').write_text('{}','utf-8')
        app.folder=self.path;app.preferences['last_direct_batch']=str(self.path)
        job={'id':'one','draft':{'name':'case','timeline_name':'1'},'easy_confirmed':True,
             'easy_delivery_folder':'ready','easy_resume_verified':True}
        app.confirm_next=Mock();app.engine_adapter=Mock();app.dispatch=lambda operation,callback:callback(operation())
        app.notify_results=Mock()
        with patch('jyexport.easy_ui.read_batch',return_value=(self.path,{'jobs':[job]})),patch('jyexport.easy_ui.analyze'):
            app.resume()
        app.confirm_next.assert_not_called();self.assertIn('完整交付已复核',app.status.get())


if __name__=='__main__':unittest.main()
