"""Window recovery, explicit confirmation, and progress-queue responsiveness."""
import os,tempfile,time,tkinter as tk,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from jyexport.easy_ui import EasyApp
from jyexport.ui_windows import DialogWindows


def widgets(parent):
    for child in parent.winfo_children():yield child;yield from widgets(child)


class WindowRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.path/'profile')});self.env.start()
        self.base=patch('jyexport.easy_ui.base_directory',return_value=self.path);self.base.start()
        self.root=tk.Tk();self.root.withdraw();self.app=EasyApp(self.root)
        self.cancel_timers();self.errors=[]
        self.root.report_callback_exception=lambda k,v,t:self.errors.append(str(v))
        self.job={'id':'case','draft':{'name':'test only','timeline_name':'18'},'easy_analysis':{
            'rows':[{'key':'voice:s','kind':'audio','is_container':False,'name':'voice',
                'suggestion':'待确认','track':'VOICE','start':0,'duration':2000000},
                {'key':'caption:s','kind':'text','is_container':False,'name':'Edited caption',
                'suggestion':'字幕','track':'text','start':0,'duration':1000000}],
            'caption_keys':['caption:s']}}
        self.app.session={'jobs':[self.job]};self.app.folder=self.path/'batch'
    def cancel_timers(self):
        for token in self.root.tk.call('after','info'):self.root.after_cancel(token)
    def tearDown(self):
        self.app.windows.close_all();self.cancel_timers();self.root.destroy()
        self.base.stop();self.env.stop();self.temp.cleanup();self.assertEqual(self.errors,[])
    def button(self,parent,label):
        return next(w for w in widgets(parent) if w.winfo_class()=='TButton' and w.cget('text')==label)

    def test_pending_confirmation_has_no_grab_main_can_activate_and_cannot_start_another_batch(self):
        self.app.confirm_dialog(self.job)
        self.assertIsNone(self.root.grab_current());self.assertTrue(self.app.windows.blocked())
        self.assertIn('disabled',self.app.start_button.state())
        recover=self.button(self.root,'显示当前窗口');self.assertNotIn('disabled',recover.state())
        work=Mock();self.app.dispatch(work);work.assert_not_called()
        mode=self.app.result_mode;self.app.show_drafts();self.assertEqual(self.app.result_mode,mode)
        self.button(self.app.dialog,'稍后继续').invoke()
        self.assertFalse(self.app.windows.blocked());self.assertIsNone(self.root.grab_current())
        self.assertNotIn('disabled',self.app.start_button.state())

    def test_caption_child_blocks_only_parent_actions_and_returns_without_a_grab(self):
        self.app.confirm_dialog(self.job);parent=self.app.dialog
        accept=self.button(parent,'确认并继续制作')
        self.button(parent,'查看 / 调整字幕范围').invoke();child=self.app.windows.active()
        self.assertIsNot(child,parent);self.assertIn('disabled',accept.state())
        self.assertIsNone(self.root.grab_current())
        with patch('jyexport.easy_ui.confirm') as confirm:accept.invoke();confirm.assert_not_called()
        self.button(child,'保存范围').invoke()
        self.assertIs(self.app.windows.active(),parent);self.assertNotIn('disabled',accept.state())
        self.assertIsNone(self.root.grab_current())

    def test_stopping_pending_decision_retains_task_and_does_not_start_a_renderer(self):
        self.app.confirm_dialog(self.job);self.app.begin_render=Mock()
        self.app.stop()
        self.assertTrue(self.app.cancel.is_set());self.assertFalse(self.app.windows.blocked())
        self.assertIsNone(self.app.dialog);self.assertIs(self.app.session['jobs'][0],self.job)
        self.app.begin_render.assert_not_called();self.assertIn('已保留',self.app.status.get())

    def test_completion_notice_does_not_lock_main_window_or_request_topmost(self):
        self.app.completion_dialog(1)
        self.assertFalse(self.app.windows.blocked());self.assertIsNone(self.root.grab_current())
        self.assertNotIn('disabled',self.app.start_button.state())
        self.assertFalse(bool(self.app.notice.attributes('-topmost')))
        self.button(self.app.notice,'知道了').invoke();self.assertEqual(self.app.windows.items,[])

    def test_many_progress_updates_yield_to_window_events_and_do_not_lose_completion(self):
        app=self.app;app.busy=True;app.started=time.monotonic();app.show_results=Mock()
        for _ in range(1000):app.messages.put(('job',))
        done=Mock();app.messages.put(('done',{'completed':1},done))
        app.pump();remaining=app.messages.qsize()
        self.assertGreater(remaining,0);self.assertLess(remaining,1001)
        self.assertLessEqual(app.show_results.call_count,1);done.assert_not_called()
        heartbeat=[];self.root.after_idle(lambda:heartbeat.append(True));self.root.update_idletasks()
        self.assertEqual(heartbeat,[True]);self.assertTrue(app.busy)
        for _ in range(40):
            if not app.messages.qsize():break
            app.pump()
        self.assertFalse(app.messages.qsize());done.assert_called_once_with({'completed':1})
        self.assertFalse(app.busy)


class RecoveryPolicyTests(unittest.TestCase):
    def manager(self):
        root=Mock();root.state.return_value='normal'
        root.focus_displayof.return_value=None
        manager=DialogWindows(root);return root,manager

    def test_minimized_main_and_current_dialog_are_restored_by_explicit_recovery(self):
        root,m=self.manager();window=Mock();window.winfo_exists.return_value=True
        m.items=[{'window':window,'blocking':True}];root.state.return_value='iconic'
        m.recover();root.deiconify.assert_called_once();window.deiconify.assert_called_once()
        window.lift.assert_called_once();window.focus_force.assert_called_once()
        window.attributes.assert_not_called()

    def test_queued_activation_does_not_steal_focus_after_user_switches_away(self):
        root,m=self.manager();window=Mock();window.winfo_exists.return_value=True
        m.items=[{'window':window,'blocking':True}]
        source=Mock();source.winfo_toplevel.return_value=root
        root.after_idle.side_effect=lambda cb:cb
        m._activated(SimpleNamespace(widget=source));callback=m.pending
        callback();window.focus_force.assert_not_called();window.lift.assert_not_called()

    def test_user_activation_of_main_recovers_the_deepest_pending_dialog(self):
        root,m=self.manager();parent=Mock();child=Mock()
        parent.winfo_exists.return_value=child.winfo_exists.return_value=True
        m.items=[{'window':parent,'blocking':True},{'window':child,'blocking':True}]
        focus=Mock();focus.winfo_toplevel.return_value=root;root.focus_displayof.return_value=focus
        root.after_idle.side_effect=lambda cb:cb
        m._activated(SimpleNamespace(widget=focus));m.pending()
        child.lift.assert_called_once();child.focus_force.assert_called_once();parent.lift.assert_not_called()

    def test_destroy_cancels_delayed_reactivation(self):
        root,m=self.manager();m.pending='pending-token'
        m._destroyed(SimpleNamespace(widget=root))
        root.after_cancel.assert_called_once_with('pending-token');self.assertTrue(m.closed)


if __name__=='__main__':unittest.main()
