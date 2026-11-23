import copy,json,os,tempfile,time,tkinter as tk,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from jyexport.easy_ui import EasyApp


def descendants(parent):
    for child in parent.winfo_children():yield child;yield from descendants(child)


class Interaction221Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.path/'profile')});self.env.start()
        self.base=patch('jyexport.easy_ui.base_directory',return_value=self.path);self.base.start()
        self.root=tk.Tk();self.root.withdraw();self.app=EasyApp(self.root)
        for token in self.root.tk.call('after','info'):self.root.after_cancel(token)
        self.profile={'dll_sha256':'verified-dll','tool_sha256':'verified-helper'}
        self.app.media_directory=Mock(return_value=str(self.path))
        self.app.engine_adapter=Mock(return_value=Mock(verify=Mock(return_value=self.profile)))
        self.app.dispatch=lambda operation,callback=None:callback(operation()) if callback else operation()
        self.errors=[];self.root.report_callback_exception=lambda kind,value,trace:self.errors.append(str(value))
    def tearDown(self):
        self.root.destroy();self.base.stop();self.env.stop();self.temp.cleanup()
        self.assertEqual(self.errors,[])

    def test_prepared_startup_never_prepares_again_or_starts_an_export(self):
        app=self.app;app.preferences['direct_environment_check']=app.machine_key(self.profile)
        app.prepare_environment=Mock();app.self_test=Mock();app.begin_selected=Mock()
        app.check_environment(auto_prepare=True)
        self.assertTrue(app.environment_ready);app.prepare_environment.assert_not_called()
        app.self_test.assert_not_called();app.begin_selected.assert_not_called()
        self.assertEqual(app.environment_retry.winfo_manager(),'')

    def test_initial_open_chains_missing_engine_setup_and_check_without_start_click(self):
        app=self.app;installed=False;app.begin_selected=Mock()
        app.dispatch=EasyApp.dispatch.__get__(app,EasyApp)
        def verify():
            if not installed:raise ValueError('missing fixture runtime')
            return self.profile
        app.engine_adapter.return_value.verify.side_effect=verify
        def setup(instance):
            def work():
                nonlocal installed
                installed=True;return {'runtime_directory':str(self.path/'runtime')}
            def done(value):instance.engine_runtime.set(value['runtime_directory'])
            instance.dispatch(work,done)
        with patch('jyexport.easy_ui.current_drafts',return_value=([],[])),patch('jyexport.easy_ui.launch_setup',side_effect=setup) as install,patch('jyexport.portable_test.run_test',return_value={'success':True}) as selftest:
            app.initial();deadline=time.monotonic()+3
            while not app.environment_ready and time.monotonic()<deadline:app.pump();time.sleep(.01)
        self.assertTrue(app.environment_ready);install.assert_called_once();selftest.assert_called_once()
        app.begin_selected.assert_not_called()

    def test_missing_engine_automatically_prepares_once_and_refresh_does_not_loop(self):
        app=self.app;app.engine_adapter.return_value.verify.side_effect=ValueError('not installed')
        app.prepare_environment=Mock();app.begin_selected=Mock()
        app.check_environment(auto_prepare=True);app.check_environment(auto_prepare=False)
        app.prepare_environment.assert_called_once();app.begin_selected.assert_not_called()
        self.assertTrue(app.startup_checked);self.assertTrue(app.environment_auto_attempted)

    def test_existing_engine_without_success_marker_only_runs_first_check(self):
        app=self.app;app.prepare_environment=Mock();app.self_test=Mock()
        app.check_environment(auto_prepare=True)
        app.self_test.assert_called_once();app.prepare_environment.assert_not_called()

    def test_first_check_records_success_without_export_and_is_reused_on_restart(self):
        app=self.app;app.begin_selected=Mock()
        with patch('jyexport.portable_test.run_test',return_value={'success':True}):app.self_test()
        self.assertTrue(app.environment_ready);app.begin_selected.assert_not_called()
        values=json.loads((app.state_dir/'settings.json').read_text('utf-8'))
        self.assertEqual(values['direct_environment_check'],app.machine_key(self.profile))
        app.prepare_environment=Mock();app.self_test=Mock();app.preferences=values
        app.check_environment(auto_prepare=True)
        app.prepare_environment.assert_not_called();app.self_test.assert_not_called()

    def test_unsuccessful_check_exposes_retry_without_saving_success(self):
        app=self.app
        with patch('jyexport.portable_test.run_test',return_value={'success':False,'error':'test check failed'}),patch('jyexport.easy_ui.messagebox.showwarning'):
            app.self_test()
        self.assertFalse(app.environment_ready);self.assertNotIn('direct_environment_check',app.preferences)
        self.assertEqual(app.environment_retry.winfo_manager(),'pack')
        app.self_test=Mock();app.retry_environment();app.self_test.assert_called_once()

    def test_cannot_save_success_marker_does_not_leave_a_false_ready_state(self):
        app=self.app;app.dispatch=lambda operation,callback=None:app.messages.put(('done',operation(),callback))
        before=dict(app.preferences)
        with patch('jyexport.portable_test.run_test',return_value={'success':True}),patch.object(app,'save_preferences',side_effect=PermissionError('configuration blocked')),patch('jyexport.easy_ui.messagebox.showwarning') as warning:
            app.self_test();app.pump()
        self.assertFalse(app.environment_ready);self.assertEqual(app.preferences,before);warning.assert_called_once()

    def test_stop_between_setup_and_selftest_does_not_restart_or_mark_ready(self):
        app=self.app;follow=Mock();app.next_action=follow;app.environment_in_progress=True;app.busy=True
        app.stop();app.messages.put(('done',{'prepared':True},None))
        with patch('jyexport.easy_ui.messagebox.showwarning') as warning:app.pump()
        follow.assert_not_called();warning.assert_not_called();self.assertFalse(app.environment_ready)

    def test_ready_check_continues_only_an_explicit_pending_action(self):
        app=self.app;action=Mock();app.environment_action=action
        app.preferences['direct_environment_check']=app.machine_key(self.profile)
        app.check_environment(auto_prepare=True)
        action.assert_called_once();self.assertIsNone(app.environment_action)

    def test_completion_label_is_only_presentation_and_does_not_mark_reviewed(self):
        app=self.app
        app.session={'jobs':[{'id':'done','draft':{'name':'case','timeline_name':'18'},'want_srt':False,
            'easy_delivery_state':'完成，待声画审核（无字幕，未生成SRT）','easy_delivery_folder':'somewhere',
            'variants':{'V1':{'reviewed':False}},'srt':{'omitted':True,'reviewed':False}}]}
        before=copy.deepcopy(app.session);app.show_results()
        self.assertEqual(app.results.set('done','state'),'完成');self.assertEqual(app.session,before)
        self.assertIn('complete',app.results.item('done','tags'))

    def test_success_partial_failure_and_stop_have_distinct_honest_notifications(self):
        app=self.app;app.session={'jobs':[{'id':str(i)} for i in range(3)]};app.completion_dialog=Mock()
        app.notify_results({'completed':3,'failed':0,'pending':0});app.completion_dialog.assert_called_once_with(3,False)
        self.assertEqual(float(app.progress.cget('value')),100)
        app.completion_dialog.reset_mock()
        with patch('jyexport.easy_ui.messagebox.showwarning') as warning:app.notify_results({'completed':2,'failed':1,'pending':0})
        app.completion_dialog.assert_not_called();self.assertIn('需要处理 1',warning.call_args.args[1])
        app.cancel.set()
        with patch('jyexport.easy_ui.messagebox.showinfo') as notice:app.notify_results({'completed':0,'failed':1,'pending':2})
        self.assertIn('未执行 2',notice.call_args.args[1]);self.assertIn('需要处理 1',notice.call_args.args[1])

    def test_close_suppresses_completion_popups(self):
        app=self.app;app.closed_requested=True;app.completion_dialog=Mock()
        app.session={'jobs':[{}]};app.notify_results({'completed':1,'failed':0,'pending':0})
        app.completion_dialog.assert_not_called()

    def test_success_dialog_shows_omissions_and_opens_the_actual_folder(self):
        app=self.app;target=self.path/'交付'/'one';target.mkdir(parents=True)
        app.folder=self.path;app.session={'jobs':[{'want_srt':False,'easy_delivery_folder':str(target)}]}
        top=tk.Toplevel
        def hidden(*args,**kwargs):
            window=top(*args,**kwargs);window.withdraw();return window
        with patch('jyexport.easy_ui.tk.Toplevel',side_effect=hidden),patch.object(top,'grab_set'):
            app.completion_dialog(1)
        labels=[widget.cget('text') for widget in descendants(app.notice) if widget.winfo_class()=='TLabel']
        self.assertTrue(any('全部完成' in label for label in labels));self.assertTrue(any('未生成SRT' in label for label in labels))
        button=next(widget for widget in descendants(app.notice) if widget.winfo_class()=='TButton' and widget.cget('text')=='打开交付文件夹')
        with patch('jyexport.easy_ui.os.startfile') as opened:button.invoke()
        opened.assert_called_once_with(target)

    def test_manual_environment_button_is_gone_and_small_screen_keeps_actions(self):
        labels=[w.cget('text') for w in descendants(self.root) if w.winfo_class()=='TButton']
        self.assertNotIn('首次准备环境',labels)
        with patch.object(self.root,'winfo_screenheight',return_value=720):self.app.fit_height()
        self.assertLessEqual(self.root.winfo_reqheight(),620)

    def test_minimized_window_is_restored_for_completion_notice(self):
        app=self.app;app.folder=self.path;app.session={'jobs':[{'want_srt':True}]}
        top=tk.Toplevel
        def hidden(*args,**kwargs):
            window=top(*args,**kwargs);window.withdraw();return window
        with patch.object(self.root,'state',return_value='iconic'),patch.object(self.root,'deiconify') as restored,patch('jyexport.easy_ui.tk.Toplevel',side_effect=hidden),patch.object(top,'grab_set'):
            app.completion_dialog(1)
        restored.assert_called_once()


if __name__=='__main__':unittest.main()
