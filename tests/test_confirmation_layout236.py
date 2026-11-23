"""Own-app widget geometry: confirmation actions stay inside the client area."""
import copy
import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from jyexport.easy_ui import EasyApp


def descendants(parent):
    for child in parent.winfo_children():
        yield child
        yield from descendants(child)


class ConfirmationLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.path/'profile')});self.env.start()
        self.base=patch('jyexport.easy_ui.base_directory',return_value=self.path);self.base.start()
        self.root=tk.Tk();self.root.withdraw()
        self.scaling=float(self.root.tk.call('tk','scaling'))
        self.errors=[];self.root.report_callback_exception=lambda k,v,t:self.errors.append(str(v))
        original_toplevel=tk.Toplevel
        def invisible_toplevel(*args,**kwargs):
            window=original_toplevel(*args,**kwargs);window.withdraw()
            return window
        self.toplevel=patch('jyexport.easy_ui.tk.Toplevel',side_effect=invisible_toplevel);self.toplevel.start()
        self.app=None

    def tearDown(self):
        if self.app:self.app.windows.close_all()
        for token in self.root.tk.call('after','info'):self.root.after_cancel(token)
        self.root.tk.call('tk','scaling',self.scaling);self.root.destroy()
        self.toplevel.stop();self.base.stop();self.env.stop();self.temp.cleanup();self.assertEqual(self.errors,[])

    def init_app(self,scale=1.5):
        self.root.tk.call('tk','scaling',scale)
        self.app=EasyApp(self.root)
        for token in self.root.tk.call('after','info'):self.root.after_cancel(token)
        self.app.folder=self.path/'batch'
        return self.app

    def job(self,number=1,audio=True,long_name=False):
        rows=[{'key':f'text:{number}','kind':'text','is_container':False,'name':'Edited caption',
               'suggestion':'字幕','track':'text','start':0,'duration':1000000}]
        if audio:
            rows+=[{'key':f'audio:{number}:{i}','kind':'audio','is_container':False,'name':'fixture-'+str(i),
                'suggestion':'背景音乐' if i==0 else '待确认','track':'audio轨道'+str(i),'start':0,'duration':43000000-i,
                'bgm_reference_token':'shared-music' if i==0 else 'other-'+str(i)} for i in range(30)]
        return {'id':str(number),'draft':{'name':'长剧名用于检查换行和按钮可见性'*5 if long_name else '神经病本地','timeline_name':str(number)},
            'easy_analysis':{'rows':rows,'caption_keys':[f'text:{number}']}}

    def button(self,label):
        return next(w for w in descendants(self.app.dialog) if w.winfo_class()=='TButton' and w.cget('text')==label)

    def geometry(self,width,height):
        window=self.app.dialog
        # Allocate the actual dialog content frame a client-sized viewport.
        # This exercises Tk's real pack geometry while every top-level remains
        # withdrawn: no desktop window activation or OS input is necessary.
        viewport=next(w for w in window.winfo_children() if w.winfo_class()=='TFrame')
        viewport.pack_forget();viewport.place(x=0,y=0,width=width,height=height)
        window.update_idletasks()
        self.assertEqual(window.state(),'withdrawn')
        self.assertEqual((viewport.winfo_width(),viewport.winfo_height()),(width,height))
        return viewport

    def assert_inside(self,widget,window):
        x=widget.winfo_rootx()-window.winfo_rootx();y=widget.winfo_rooty()-window.winfo_rooty()
        self.assertGreaterEqual(x,0);self.assertGreaterEqual(y,0)
        self.assertGreater(widget.winfo_width(),1);self.assertGreater(widget.winfo_height(),1)
        self.assertEqual(widget.winfo_height(),widget.winfo_reqheight())
        self.assertLessEqual(x+widget.winfo_width(),window.winfo_width())
        self.assertLessEqual(y+widget.winfo_height(),window.winfo_height())

    def assert_actions(self,window):
        self.assert_inside(self.button('稍后继续'),window)
        self.assert_inside(self.button('确认并继续制作'),window)
        self.assertIsNone(self.root.grab_current())

    def test_continuous_second_dialog_with_bgm_memory_keeps_both_buttons_visible(self):
        app=self.init_app();first=self.job(1);second=self.job(28)
        app.session={'jobs':[first,second]};app.confirm_next()
        self.assert_actions(self.geometry(1000,620))
        def confirmed(folder,session,job,bgm,captions,none,**flags):
            job['easy_confirmed']=True
        app.bgm_memory.remember(first['easy_analysis']['rows'],{'audio:1:0'})
        with patch('jyexport.easy_ui.confirm',side_effect=confirmed):self.button('确认并继续制作').invoke()
        labels=[str(w.cget('text')) for w in descendants(app.dialog) if w.winfo_class()=='TLabel']
        self.assertTrue(any('已沿用本轮此前确认' in text for text in labels))
        self.assert_actions(self.geometry(1000,620))
        table=next(w for w in descendants(app.dialog) if w.winfo_class()=='Treeview')
        self.assertGreater(table.winfo_height(),40)
        # With an entirely withdrawn window Tk does not expose row fractions;
        # verify the real allocated list area and both scrollbar bindings.
        self.assertTrue(table.cget('yscrollcommand'))
        scroll=next(w for w in descendants(app.dialog) if w.winfo_class()=='TScrollbar')
        self.assertIn('yview',str(scroll.cget('command')))

    def test_minimum_window_long_title_and_memory_leave_actions_visible(self):
        app=self.init_app();job=self.job(28,long_name=True);app.session={'jobs':[job]}
        app.bgm_memory.remember(job['easy_analysis']['rows'],{'audio:28:0'});app.confirm_dialog(job)
        window=self.geometry(700,470);self.assert_actions(window)
        self.assert_inside(self.button('查看 / 调整字幕范围'),window)

    def test_no_audio_notice_keeps_actions_visible_in_small_window(self):
        app=self.init_app();job=self.job(audio=False,long_name=True);app.session={'jobs':[job]};app.confirm_dialog(job)
        self.assert_actions(self.geometry(700,470))

    def test_higher_tk_scaling_with_bgm_memory_keeps_actions_visible(self):
        app=self.init_app(2.0);job=self.job(28,long_name=True);app.session={'jobs':[job]}
        app.bgm_memory.remember(job['easy_analysis']['rows'],{'audio:28:0'});app.confirm_dialog(job)
        self.assert_actions(self.geometry(700,470))

    def test_footer_layout_does_not_change_bgm_or_prior_confirmations(self):
        app=self.init_app();first=self.job(1);first['easy_confirmed']=True;first['roles']={'prior':'背景音乐'}
        second=self.job(28);app.session={'jobs':[first,second]};prior=copy.deepcopy(first)
        app.bgm_memory.remember(first['easy_analysis']['rows'],{'audio:1:0'});app.confirm_next()
        self.assert_actions(self.geometry(700,470))
        tree=next(w for w in descendants(app.dialog) if w.winfo_class()=='Treeview')
        self.assertEqual(tree.set('0','check'),'☑');self.assertEqual(first,prior)
        self.button('稍后继续').invoke();self.assertEqual(first,prior);self.assertFalse(app.bgm_memory.tokens)


if __name__=='__main__':unittest.main()
