"""Editor settings policy joins the UI lifecycle without touching live profiles."""
import copy
import json
import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from jyexport.easy_ui import EasyApp,check_editor_update_policy
from jyexport.editor_updates import PREFERENCE,disables_auto_update,merge_preferences
from jyexport.gui import App


def descendants(parent):
    for widget in parent.winfo_children():
        yield widget
        yield from descendants(widget)


def verified(disabled=True):
    return {'success':True,'disable_auto_update':disabled,'beta':{'ok':True,'changed':False,'value':False},
            'auto_update':{'ok':True,'changed':False,'value':not disabled}}


def saved_policy(profile,backup,preferences):
    path=Path(preferences)
    return verified(disables_auto_update(json.loads(path.read_text('utf-8')) if path.exists() else {}))


class EditorUpdateUITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jy_update_ui_')
        self.path=Path(self.temp.name);self.base=self.path/'assistant';self.base.mkdir()
        self.patches=[patch.dict(os.environ,{'LOCALAPPDATA':str(self.path/'profile')}),
                      patch('jyexport.easy_ui.base_directory',return_value=self.base),
                      patch('jyexport.gui.sys_base',return_value=str(self.base)),
                      patch('jyexport.easy_ui.apply_saved_update_policy',side_effect=saved_policy)]
        values=[item.start() for item in self.patches];self.applied=values[-1]
        self.root=tk.Tk();self.root.withdraw();self.app=EasyApp(self.root)
        self.other_roots=[];self.errors=[]
        self.root.report_callback_exception=lambda kind,value,trace:self.errors.append(str(value))
        self.cancel_timers(self.root)
        self.top_class=tk.Toplevel
        def hidden(*args,**kwargs):
            window=self.top_class(*args,**kwargs);window.withdraw();return window
        self.hidden=patch('jyexport.easy_ui.tk.Toplevel',side_effect=hidden);self.hidden.start()

    def tearDown(self):
        self.app.windows.close_all();self.cancel_timers(self.root);self.root.destroy()
        for root,app in self.other_roots:
            app.codec.close();self.cancel_timers(root);root.destroy()
        self.hidden.stop()
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup();self.assertEqual(self.errors,[])

    @staticmethod
    def cancel_timers(root):
        for token in root.tk.call('after','info'):root.after_cancel(token)

    @staticmethod
    def synchronous(operation,callback=None,**kwargs):
        result=operation()
        if callback:callback(result)
        return True

    def button(self,window,label,kind='TButton'):
        return next(widget for widget in descendants(window) if widget.winfo_class()==kind and widget.cget('text')==label)

    def settings(self):
        self.app.settings();return self.app.windows.active()

    def saved(self):
        return json.loads((self.app.state_dir/'settings.json').read_text('utf-8'))

    def drain(self,app=None):
        app=app or self.app;deadline=time.monotonic()+3
        while (app.busy or not app.messages.empty()) and time.monotonic()<deadline:
            app.pump();time.sleep(.005)
        self.assertFalse(app.busy);self.assertTrue(app.messages.empty())

    def advanced(self):
        root=tk.Tk();root.withdraw();app=App(root);root.update_idletasks();self.cancel_timers(root)
        self.other_roots.append((root,app));return app

    def test_construct_and_idle_smoke_do_not_apply_editor_policy(self):
        self.root.update_idletasks();advanced=self.advanced();advanced.root.update_idletasks()
        self.applied.assert_not_called()
        self.assertFalse((self.app.state_dir/'剪映更新设置核验.json').exists())
        window=self.settings();self.root.update_idletasks()
        self.assertTrue(self.button(window,'关闭剪映专业版自动更新','TCheckbutton').instate(['selected']))
        self.applied.assert_not_called()

    def test_prepared_environment_still_checks_once_and_refresh_only_scans(self):
        app=self.app;profile={'dll_sha256':'known','tool_sha256':'known'}
        app.preferences['direct_environment_check']=app.machine_key(profile)
        app.media_directory=Mock();app.engine_adapter=Mock(return_value=Mock(verify=Mock(return_value=profile)))
        app.prepare_environment=Mock();app.self_test=Mock();app.dispatch=self.synchronous
        with patch('jyexport.easy_ui.current_drafts',return_value=([],[])) as scanned:
            app.initial();app.refresh()
        self.assertTrue(app.environment_ready);self.applied.assert_called_once();self.assertEqual(scanned.call_count,2)
        app.prepare_environment.assert_not_called();app.self_test.assert_not_called()
        self.assertEqual(app.editor_policy_status.get(),'自动更新：已关闭\n参与内测体验计划：已关闭')
        self.assertEqual(app.editor_policy_notice.winfo_manager(),'')

    def test_busy_or_blocked_startup_is_deferred_without_marking_complete(self):
        app=self.app;app.busy=True;app.initial()
        self.assertFalse(app.editor_policy_checked);self.assertIsNotNone(app.editor_policy_retry)
        app.busy=False;window=self.settings();app.initial()
        self.assertFalse(app.editor_policy_checked);self.applied.assert_not_called();window.destroy()
        app.dispatch=self.synchronous;app.check_environment=Mock()
        with patch('jyexport.easy_ui.current_drafts',return_value=([],[])):app.initial()
        self.applied.assert_called_once();self.assertTrue(app.editor_policy_checked)

    def test_rejected_dispatch_leaves_startup_check_pending(self):
        app=self.app;app.dispatch=Mock(return_value=False);app.initial()
        self.assertFalse(app.editor_policy_checked);self.assertIsNotNone(app.editor_policy_retry)
        self.applied.assert_not_called()

    def test_saved_false_on_disk_wins_over_stale_startup_snapshot(self):
        app=self.app;app.preferences[PREFERENCE]=True
        merge_preferences(app.state_dir/'settings.json',{},explicit_disable=False)
        app.dispatch=self.synchronous;app.check_environment=Mock()
        with patch('jyexport.easy_ui.current_drafts',return_value=([],[])):app.initial()
        self.assertFalse(app.editor_policy_result['disable_auto_update'])
        self.assertIn('已开启（按已保存选择）',app.editor_policy_status.get())
        self.assertFalse(app.editor_policy_result['beta']['value'])

    def test_closing_uncommitted_checkbox_then_normal_save_does_not_allow_updates(self):
        window=self.settings();choice=self.button(window,'关闭剪映专业版自动更新','TCheckbutton')
        choice.invoke();self.assertFalse(choice.instate(['selected']));window.destroy()
        self.app.save_preferences();self.assertTrue(self.saved()[PREFERENCE]);self.applied.assert_not_called()

    def test_explicit_uncheck_and_save_applies_immediately_once_in_background(self):
        self.app.environment_ready=True;window=self.settings()
        self.button(window,'关闭剪映专业版自动更新','TCheckbutton').invoke()
        self.button(window,'保存').invoke();self.drain()
        self.assertFalse(self.saved()[PREFERENCE]);self.applied.assert_called_once()
        self.assertFalse(self.app.editor_policy_result['disable_auto_update'])
        self.assertFalse(self.app.windows.blocked())
        self.assertIn('参与内测体验计划：已关闭',self.app.editor_policy_status.get())

    def test_settings_retry_uses_saved_preference_not_an_unsaved_checkbox(self):
        merge_preferences(self.app.state_dir/'settings.json',{},explicit_disable=False)
        window=self.settings();choice=self.button(window,'关闭剪映专业版自动更新','TCheckbutton')
        self.assertFalse(choice.instate(['selected']));choice.invoke()
        self.button(window,'重新核验').invoke();self.drain()
        self.assertFalse(self.app.editor_policy_result['disable_auto_update'])
        self.assertFalse(self.saved()[PREFERENCE]);self.assertTrue(window.winfo_exists())
        self.assertFalse(self.button(window,'保存').instate(['disabled']))

    def test_cannot_save_preference_keeps_dialog_and_never_enables_updates(self):
        window=self.settings();self.button(window,'关闭剪映专业版自动更新','TCheckbutton').invoke()
        with patch.object(self.app,'save_preferences',side_effect=PermissionError('save blocked')),patch('jyexport.easy_ui.messagebox.showerror') as error:
            self.button(window,'保存').invoke()
        error.assert_called_once();self.assertTrue(window.winfo_exists());self.applied.assert_not_called()

    def test_partial_failure_is_visible_per_item_without_blocking_scan_or_popup(self):
        result=verified();result.update(success=False)
        result['auto_update']={'ok':False,'error':'globalSetting locked','value':False}
        self.applied.side_effect=None;self.applied.return_value=result
        app=self.app;app.dispatch=self.synchronous;app.check_environment=Mock()
        with patch('jyexport.easy_ui.current_drafts',return_value=([],[])) as scan,patch('jyexport.easy_ui.messagebox.showerror') as error,patch('jyexport.easy_ui.messagebox.showwarning') as warning:
            app.initial()
        scan.assert_called_once();app.check_environment.assert_called_once();error.assert_not_called();warning.assert_not_called()
        self.assertIn('自动更新：未能核验；globalSetting locked',app.editor_policy_status.get())
        self.assertIn('参与内测体验计划：已关闭',app.editor_policy_status.get())
        self.assertEqual(app.editor_policy_notice.winfo_manager(),'pack')
        self.assertFalse(json.loads((app.state_dir/'剪映更新设置核验.json').read_text('utf-8'))['success'])

    def test_unexpected_policy_failure_is_structured_and_scan_continues(self):
        self.applied.side_effect=RuntimeError('unexpected fixture error')
        app=self.app;app.dispatch=self.synchronous;app.check_environment=Mock()
        with patch('jyexport.easy_ui.current_drafts',return_value=([],[])) as scan:app.initial()
        scan.assert_called_once();self.assertFalse(app.editor_policy_result['success'])
        self.assertIn('unexpected fixture error',app.editor_policy_status.get())

    def test_failed_scan_does_not_hide_policy_result_or_repeat_its_application(self):
        app=self.app;app.check_environment=Mock()
        with patch('jyexport.easy_ui.current_drafts',side_effect=ValueError('draft scan blocked')),patch('jyexport.easy_ui.messagebox.showerror'):
            app.initial();self.drain();app.initial();self.drain()
        self.applied.assert_called_once();self.assertIsNotNone(app.editor_policy_result)
        self.assertIn('自动更新：已关闭',app.editor_policy_status.get());app.check_environment.assert_not_called()

    def test_report_write_failure_does_not_claim_a_saved_record_or_stop_scan(self):
        app=self.app;app.dispatch=self.synchronous;app.check_environment=Mock()
        with patch('jyexport.easy_ui.atomic_json',side_effect=PermissionError('record blocked')),patch('jyexport.easy_ui.current_drafts',return_value=([],[])) as scan:
            app.initial()
        scan.assert_called_once();self.assertIn('核验记录未能保存：record blocked',app.editor_policy_status.get())
        self.assertEqual(app.editor_policy_notice.winfo_manager(),'pack')

    def test_ordinary_and_advanced_saves_preserve_another_windows_explicit_false(self):
        app=self.app;app.preferences[PREFERENCE]=True
        merge_preferences(app.state_dir/'settings.json',{'unrelated':'keep'},explicit_disable=False)
        for _ in range(2):app.save_preferences()
        self.assertFalse(self.saved()[PREFERENCE]);self.assertEqual(self.saved()['unrelated'],'keep')
        advanced=self.advanced();advanced.preferences[PREFERENCE]=True;advanced.save_preferences()
        self.assertFalse(self.saved()[PREFERENCE]);self.assertEqual(self.saved()['unrelated'],'keep')
        self.applied.assert_not_called()

    def test_advanced_startup_checks_before_scan_and_only_once(self):
        app=self.advanced();app.dispatch=self.synchronous;app.scan=Mock()
        self.applied.assert_not_called();app.initial();app.initial()
        self.applied.assert_called_once();self.assertEqual(app.scan.call_count,2)
        self.assertTrue(app.editor_policy_result['success'])

    def test_disk_work_is_off_ui_thread_and_result_is_delivered_on_ui_thread(self):
        ui_thread=threading.get_ident();threads=[];presented=[]
        def apply(*args,**kwargs):threads.append(threading.get_ident());return saved_policy(*args,**kwargs)
        self.applied.side_effect=apply
        original=self.app.show_editor_update_result
        def show(result):presented.append(threading.get_ident());original(result)
        self.app.show_editor_update_result=show;self.app.check_environment=Mock()
        with patch('jyexport.easy_ui.current_drafts',return_value=([],[])):
            self.app.initial();self.drain()
        self.assertEqual(len(threads),1);self.assertNotEqual(threads[0],ui_thread);self.assertEqual(presented,[ui_thread])

    def test_helper_writes_report_but_never_mutates_result_given_by_backend(self):
        original=verified(False);self.applied.side_effect=None;self.applied.return_value=original;before=copy.deepcopy(original)
        result=check_editor_update_policy(self.app.state_dir)
        self.assertEqual(original,before);self.assertFalse(result['disable_auto_update'])
        self.assertEqual(json.loads((self.app.state_dir/'剪映更新设置核验.json').read_text('utf-8')),result)

    def test_untouched_old_false_dialog_cannot_overwrite_a_new_saved_true(self):
        app=self.app;app.environment_ready=True
        merge_preferences(app.state_dir/'settings.json',{},explicit_disable=False)
        window=self.settings();self.assertFalse(self.button(window,'关闭剪映专业版自动更新','TCheckbutton').instate(['selected']))
        merge_preferences(app.state_dir/'settings.json',{},explicit_disable=True)
        app.output.set(str(self.path/'changed output'))
        self.button(window,'保存').invoke();self.drain()
        self.assertTrue(self.saved()[PREFERENCE]);self.assertTrue(app.editor_policy_result['disable_auto_update'])
        self.assertEqual(self.saved()['output'],str(self.path/'changed output'))

    def test_delayed_worker_reads_latest_saved_policy_instead_of_scheduled_choice(self):
        app=self.app;pending=[]
        merge_preferences(app.state_dir/'settings.json',{},explicit_disable=False)
        app.dispatch=lambda operation,callback=None:pending.append((operation,callback)) or True
        app.retry_editor_updates()
        merge_preferences(app.state_dir/'settings.json',{},explicit_disable=True)
        operation,callback=pending.pop();callback(operation())
        self.assertTrue(app.editor_policy_result['disable_auto_update'])
        self.assertEqual(self.applied.call_args.args[2],app.state_dir/'settings.json')
        self.assertEqual(self.applied.call_args.kwargs,{})

    def test_batch_save_failure_keeps_task_and_pump_schedules_again(self):
        app=self.app;session={'jobs':[],'keep':'batch'};app.show_results=Mock()
        for error in (PermissionError('settings lock held'),ValueError('damaged settings JSON')):
            with self.subTest(error=str(error)),patch.object(app,'save_preferences',side_effect=error):
                app.messages.put(('batch',self.path/'batch',session));app.messages.put(('log','later message'))
                app.pump()
            self.assertTrue(self.root.tk.call('after','info'))
            # The UI pump yields after its 12 ms budget and schedules the rest.
            self.drain(app)
            self.assertIs(app.session,session);self.assertEqual(app.folder,self.path/'batch')
            self.assertTrue(app.messages.empty());self.assertTrue(self.root.tk.call('after','info'))
            self.assertIn('设置未能保存',app.status.get())

    def test_corrupt_settings_batch_and_close_keep_file_task_and_window(self):
        app=self.app;path=app.state_dir/'settings.json';path.write_text('{broken',encoding='utf-8')
        session={'jobs':[]};app.show_results=Mock();app.messages.put(('batch',self.path/'batch',session));app.pump()
        self.assertEqual(path.read_text('utf-8'),'{broken');self.assertIs(app.session,session)
        app.close();self.assertTrue(self.root.winfo_exists());self.assertFalse(app.closed_requested)
        self.assertIn('窗口已保留',app.status.get())
        app.closed_requested=True;app.messages.put(('done',None,None));app.pump()
        self.assertTrue(self.root.winfo_exists());self.assertFalse(app.closed_requested)
        self.assertTrue(self.root.tk.call('after','info'));self.assertEqual(path.read_text('utf-8'),'{broken')

    def test_close_lock_failure_does_not_exit_or_rewrite_saved_false(self):
        app=self.app;merge_preferences(app.state_dir/'settings.json',{},explicit_disable=False)
        with patch.object(app,'save_preferences',side_effect=PermissionError('settings lock held')):app.close()
        self.assertTrue(self.root.winfo_exists());self.assertFalse(app.closed_requested);self.assertFalse(self.saved()[PREFERENCE])

    def test_advanced_scan_callback_survives_corrupt_preferences_and_keeps_pumping(self):
        app=self.advanced();app.roots.set(str(self.path/'drafts'));app.session={'jobs':[],'keep':True}
        path=app.state_dir/'settings.json';path.write_text('{broken',encoding='utf-8')
        with patch('jyexport.gui.scan',return_value=([],[])):
            app.messages.put(('done',None,lambda _:app.scan()));app.pump();self.drain(app)
        self.assertEqual(app.session,{'jobs':[],'keep':True});self.assertEqual(path.read_text('utf-8'),'{broken')
        self.assertIn('设置未能保存',app.status.get());self.assertTrue(app.root.tk.call('after','info'))

    def test_advanced_callback_error_and_failed_close_do_not_stop_pump_or_fake_exit(self):
        app=self.advanced();callback=Mock(side_effect=ValueError('preferences conflict'))
        app.messages.put(('done',None,callback));app.messages.put(('log','following callback'))
        app.pump();callback.assert_called_once();self.assertTrue(app.messages.empty())
        self.assertTrue(app.root.tk.call('after','info'))
        with patch.object(app,'save_preferences',side_effect=PermissionError('settings lock held')),patch.object(app.codec,'close') as close:
            app._close_requested=True;app.pump()
            self.assertTrue(app.root.winfo_exists());self.assertFalse(app._close_requested);close.assert_not_called()
        self.assertIn('设置未能保存',app.status.get());self.assertTrue(app.root.tk.call('after','info'))


if __name__=='__main__':unittest.main()
