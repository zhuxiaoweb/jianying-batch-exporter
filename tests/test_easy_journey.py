"""Exercise Tk button/binding callbacks without desktop input or visible windows."""
import os
import re
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from jyexport.easy_ui import EasyApp
from jyexport.delivery_service import selection_roles
from jyexport.discovery import Draft


def descendants(parent):
    for widget in parent.winfo_children():
        yield widget
        yield from descendants(widget)


class EasyJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.path/'profile')});self.env.start()
        self.base=patch('jyexport.easy_ui.base_directory',return_value=self.path);self.base.start()
        self.root=tk.Tk();self.root.withdraw();self.app=EasyApp(self.root)
        for token in self.root.tk.call('after','info'):self.root.after_cancel(token)
        self.errors=[];self.root.report_callback_exception=lambda kind,value,trace:self.errors.append((kind,value))
        self.top_class=tk.Toplevel
        self.grab=patch.object(self.top_class,'grab_set',lambda window:None);self.grab.start()
        def hidden_top(*args,**kwargs):
            window=self.top_class(*args,**kwargs);window.withdraw();return window
        self.top=patch('jyexport.easy_ui.tk.Toplevel',side_effect=hidden_top);self.top.start()
        self.rows=[
            {'key':'a:music','kind':'audio','is_container':False,'name':'approved long music','suggestion':'背景音乐','track':'BGM 1','start':0,'duration':10000000},
            {'key':'a:voice','kind':'audio','is_container':False,'name':'voice','suggestion':'待确认','track':'VOICE 1','start':0,'duration':10000000},
            {'key':'t:caption','kind':'text','is_container':False,'name':'Final edited caption','suggestion':'字幕','track':'caption','start':0,'duration':1000000},
            {'key':'t:manual','kind':'text','is_container':False,'name':'Hand edited caption','suggestion':'标题/其他文字','track':'manual text','start':1000000,'duration':1000000}]
        self.job={'id':'case','draft':{'name':'test project','timeline_name':'18'},
                  'easy_analysis':{'rows':self.rows,'caption_keys':['t:caption']}}
        self.app.session={'jobs':[self.job]};self.app.folder=self.path/'batch'
    def tearDown(self):
        self.root.destroy();self.top.stop();self.grab.stop();self.base.stop();self.env.stop();self.temp.cleanup()
        self.assertEqual(self.errors,[],'Tk swallowed a callback exception')

    def button(self,parent,label,kind='TButton'):
        return next(widget for widget in descendants(parent) if widget.winfo_class()==kind and widget.cget('text')==label)

    def press_row_checkbox(self,tree,iid):
        # Call the real registered Tcl/Python binding, supplying a Tk Event.
        # No OS click, key, window capture or user input hook is involved.
        script=tree.bind('<Button-1>');command=re.search(r'\[([^\s]+)',script).group(1)
        values=[1,1,0,0,0,0,0,0,10,25,'',0,'',0,str(tree),4,10,25,0]
        with patch.object(tree,'identify_column',return_value='#1'),patch.object(tree,'identify_row',return_value=iid):
            tree.tk.call(command,*values)

    def test_real_checkbox_and_accept_button_submit_only_selected_bgm(self):
        self.app.confirm_next=Mock();self.app.confirm_dialog(self.job)
        window=self.app.dialog
        tree=next(widget for widget in descendants(window) if widget.winfo_class()=='Treeview')
        iid=next(iid for iid in tree.get_children() if tree.set(iid,'name')=='approved long music')
        self.press_row_checkbox(tree,iid)
        self.assertEqual(tree.set(iid,'check'),'☑')
        with patch('jyexport.easy_ui.confirm') as confirmed:
            self.button(window,'确认并继续制作').invoke()
        args=confirmed.call_args.args
        self.assertEqual(args[3],{'a:music'});self.assertEqual(args[4],{'t:caption'});self.assertFalse(args[5])
        self.app.confirm_next.assert_called_once();self.assertIsNone(self.app.dialog)
        self.assertTrue(all(button.winfo_exists() for button in self.app.buttons))

    def test_no_bgm_requires_explicit_choice_and_does_not_close_form_early(self):
        self.app.confirm_next=Mock();self.app.confirm_dialog(self.job);window=self.app.dialog
        def validate(folder,session,job,bgm,captions,none):return selection_roles(self.rows,bgm,captions,none)
        with patch('jyexport.easy_ui.confirm',side_effect=validate),patch('jyexport.easy_ui.messagebox.showerror') as error:
            self.button(window,'确认并继续制作').invoke()
            error.assert_called_once();self.assertIs(self.app.dialog,window);self.app.confirm_next.assert_not_called()
            self.button(window,'本集没有需要移除的独立长配乐','TCheckbutton').invoke()
            self.button(window,'确认并继续制作').invoke()
        self.app.confirm_next.assert_called_once();self.assertIsNone(self.app.dialog)

    def test_optional_manual_caption_adjustment_reaches_final_confirmation(self):
        self.app.confirm_next=Mock();self.app.confirm_dialog(self.job);window=self.app.dialog
        self.button(window,'查看 / 调整字幕范围').invoke()
        child=next(widget for widget in window.winfo_children() if isinstance(widget,self.top_class))
        table=next(widget for widget in descendants(child) if widget.winfo_class()=='Treeview')
        iid=next(iid for iid in table.get_children() if table.set(iid,'text')=='Hand edited caption')
        self.assertEqual(table.set(iid,'check'),'□');self.press_row_checkbox(table,iid)
        self.button(child,'保存范围').invoke()
        self.button(window,'本集没有需要移除的独立长配乐','TCheckbutton').invoke()
        with patch('jyexport.easy_ui.confirm') as confirmed:self.button(window,'确认并继续制作').invoke()
        self.assertEqual(confirmed.call_args.args[4],{'t:caption','t:manual'})

    def test_later_keeps_batch_and_never_starts_rendering(self):
        self.app.confirm_next=Mock();self.app.confirm_dialog(self.job);window=self.app.dialog
        with patch('jyexport.easy_ui.confirm') as confirmed:self.button(window,'稍后继续').invoke()
        confirmed.assert_not_called();self.app.confirm_next.assert_not_called()
        self.assertIsNone(self.app.dialog);self.assertEqual(self.app.folder,self.path/'batch')

    def test_missing_subtitles_and_audio_prompt_once_and_no_returns_to_form(self):
        self.job['easy_analysis'].update(rows=[],caption_keys=[],exportable_caption_keys=[])
        self.app.confirm_next=Mock();self.app.confirm_dialog(self.job);window=self.app.dialog
        with patch('jyexport.easy_ui.messagebox.askyesno',return_value=False) as asked,patch('jyexport.easy_ui.confirm') as confirmed:
            self.button(window,'确认并继续制作').invoke()
            asked.assert_called_once();confirmed.assert_not_called();self.app.confirm_next.assert_not_called()
            self.assertIn('视频自带原声',asked.call_args.args[1]);self.assertIn('不生成SRT',asked.call_args.args[1])
            self.assertIs(self.app.dialog,window)
        with patch('jyexport.easy_ui.messagebox.askyesno',return_value=True),patch('jyexport.easy_ui.confirm') as confirmed:
            self.button(window,'确认并继续制作').invoke()
        self.assertEqual(confirmed.call_args.kwargs,{'no_captions_confirmed':True,'no_audio_confirmed':True})
        self.assertTrue(confirmed.call_args.args[5]);self.app.confirm_next.assert_called_once()

    def test_refresh_keeps_selected_episodes_and_loads_new_index(self):
        draft=Draft('one','new saved draft',str(self.path),str(self.path/'draft_content.json'),'','1',False)
        self.app.selected={'one'};self.app.engine_adapter=Mock(return_value=Mock(verify=Mock(return_value={})))
        self.app.dispatch=lambda operation,callback=None:callback(operation()) if callback else operation()
        self.app.check_environment=Mock()
        with patch('jyexport.easy_ui.current_drafts',return_value=([draft],[])) as scan:
            self.app.refresh()
        self.assertEqual(self.app.selected,{'one'});self.assertEqual(self.app.drafts,[draft]);scan.assert_called_once()

    def test_deleted_selection_stops_before_creating_backup_and_keeps_selection(self):
        draft=Draft('one','removed draft',str(self.path),str(self.path/'draft_content.json'),'','1',False)
        self.app.selected={'one'};self.app.pending_drafts=[draft];self.app.engine_adapter=Mock()
        def dispatch(operation,callback):
            with self.assertRaisesRegex(ValueError,'尚未保存'):operation()
        self.app.dispatch=dispatch
        with patch('jyexport.easy_ui.current_drafts',return_value=([],[])),patch('jyexport.easy_ui.prepare') as prepare:
            self.app.begin_selected()
        prepare.assert_not_called();self.assertEqual(self.app.selected,{'one'})

    def test_first_start_automatically_continues_after_setup_and_selftest(self):
        app=self.app;app.session=None;app.folder=None
        draft=Draft('draft','test',str(self.path),str(self.path/'draft_content.json'),'episode','18',True)
        app.drafts=[draft];app.selected={draft.key};app.environment_ready=False;app.profile=None
        app.media_directory=Mock(return_value=str(self.path));app.engine_adapter=Mock(return_value=Mock(verify=Mock(return_value={'dll_sha256':'dll','tool_sha256':'tool'})))
        app.begin_selected=Mock()
        def setup(instance):
            def done(prepared):instance.engine_runtime.set(prepared['runtime_directory'])
            instance.dispatch(lambda:{'runtime_directory':str(self.path/'runtime')},done)
        with patch('jyexport.easy_ui.launch_setup',side_effect=setup) as installer,patch('jyexport.portable_test.run_test',return_value={'success':True}) as selftest:
            app.start()
            deadline=time.monotonic()+3
            while not app.begin_selected.called and time.monotonic()<deadline:
                app.pump();time.sleep(.01)
        installer.assert_called_once();selftest.assert_called_once();app.begin_selected.assert_called_once()
        self.assertTrue(app.environment_ready);self.assertEqual(app.pending_drafts,[draft])


if __name__=='__main__':unittest.main()
