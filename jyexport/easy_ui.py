"""Operator UI: choose episodes, confirm BGM, receive a complete delivery folder."""
from __future__ import annotations
import os
import platform
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk,filedialog,messagebox
from . import __version__

from .advanced import AdvancedAdapter
from .delivery_service import prepare,analyze,confirm,run,final_folder
from .deployment import configuration_directory,recommend_storage,launch_setup
from .discovery import load_json,roots_and_index,scan,resource_dirs,resolve_app_dir,user_data
from .engine import component_root
from .storage import atomic_json,Cancelled
from .workflow import read_batch
from .delivery_contract import missing_features,NO_SUBTITLES,NO_AUDIO_TRACKS
from .execution import performance_limits
from .ui_theme import apply_theme,brand_mark,decorate_rows,result_state,COLORS
from .ui_windows import DialogWindows
from .bgm_memory import RoundBgmMemory,confirmation_counts
from .editor_updates import disables_auto_update,merge_preferences,apply_saved_update_policy


def check_editor_update_policy(state_dir):
    """Run only inside an initiated UI worker; keep failures separate from export."""
    state_dir=Path(state_dir)
    try:
        result=apply_saved_update_policy(user_data(),state_dir/'剪映更新设置备份',state_dir/'settings.json')
    except Exception as exc:
        result={'success':False,'beta':{'ok':False,'error':str(exc)},
                'auto_update':{'ok':False,'error':str(exc)}}
    result=dict(result)
    try:atomic_json(state_dir/'剪映更新设置核验.json',result)
    except (OSError,ValueError) as exc:result['report_error']=str(exc)
    return result


def editor_update_summary(result):
    if not result:return '自动更新与内测体验计划：等待本次启动核验。'
    lines=[]
    observation=result.get('disk_observation')
    for key,label,success in [('auto_update','自动更新','已关闭' if result.get('disable_auto_update',True) else '已开启（按已保存选择）'),
                              ('beta','参与内测体验计划','已关闭')]:
        item=result.get(key,{})
        filename='globalSetting' if key=='auto_update' else 'updateInfo'
        observed=(observation or {}).get('files',{}).get(filename,{})
        if item.get('ok') and observed.get('ok') and type(item.get('value')) is bool and observed.get('value') is not item['value']:
            state='磁盘值已变化，请重新核验'
        else:state=success if item.get('ok') else '未能核验；'+str(item.get('error') or '请重试')
        lines.append(label+'：'+state)
    if observation:
        if observation.get('matches_policy') is False:
            lines.append('磁盘设置在执行后发生变化，请重新核验。')
        if observation.get('ok'):
            versions=[item['version'] for item in observation.get('version_reminders',[]) if item.get('last_time_show',{}).get('present')]
            if versions:lines.append('新版提醒：发现 '+', '.join(versions)+' 的提示记录，弹窗效果待原生核验。')
            else:lines.append('新版提醒：当前配置未发现带版本号的提示时间记录。')
        else:lines.append('升级提醒诊断未完成，详情见本次核验记录。')
        lines.append('核验范围为自动下载/安装设置；新版提醒由剪映独立处理。')
        process=observation.get('editor_process',{})
        if process.get('running') is True:
            lines.append('检测到剪映正在运行，请退出并重新打开剪映后核验更新界面。')
        if observation.get('sampled_at'):lines.append('采样时间：'+observation['sampled_at'])
    if result.get('report_error'):lines.append('核验记录未能保存：'+str(result['report_error']))
    return '\n'.join(lines)


def current_drafts(roots,base):
    _,index=roots_and_index(base)
    return scan(roots,index)


def refresh_selection(chosen,roots,base):
    drafts,warnings=current_drafts(roots,base)
    lookup={draft.key:draft for draft in drafts}
    absent=[draft.name+' / '+draft.timeline_name for draft in chosen if draft.key not in lookup]
    if absent:raise ValueError('选中的草稿已移动、删除或尚未保存：'+'；'.join(absent[:5])+'。请保存剪映草稿后刷新列表，其他选择已保留。')
    return [lookup[draft.key] for draft in chosen]


def base_directory():
    import sys
    return Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]


class EasyApp:
    def __init__(self,root):
        self.root=root;self.base=base_directory()
        root.title('剪映交付助手 '+__version__+' - 直接制作交付包')
        root.geometry(f'{min(1100,root.winfo_screenwidth()-60)}x{min(780,root.winfo_screenheight()-100)}+25+25')
        root.minsize(min(800,root.winfo_screenwidth()-60),min(560,root.winfo_screenheight()-100));root.protocol('WM_DELETE_WINDOW',self.close)
        self.state_dir=configuration_directory(self.base)
        self.preferences=load_json(self.state_dir/'settings.json',{})
        self.engine_component=component_root(self.base)
        runtime=load_json(self.state_dir/'engine_runtime.local.json',{})
        resources=resource_dirs();roots,self.index=roots_and_index(self.base)
        self.engine_runtime=tk.StringVar(value=self.preferences.get('engine_runtime') or runtime.get('runtime_directory',''))
        self.engine_storage=tk.StringVar(value=str(recommend_storage(self.base,self.state_dir,self.preferences.get('engine_storage'))))
        self.output=tk.StringVar(value=self.preferences.get('output') or str(Path(self.engine_storage.get()).parent/'交付归档'))
        self.app_dir=tk.StringVar(value=resolve_app_dir(self.preferences.get('app_dir'),self.base))
        self.roots=tk.StringVar(value=self.preferences.get('roots') or ';'.join(roots))
        self.cache=tk.StringVar(value=self.preferences.get('cache') or resources['cache'])
        self.filter=tk.StringVar();self.engine_state=tk.StringVar(value='正在读取本地草稿…')
        self.status=tk.StringVar(value='选中集数后点“开始制作交付包”。')
        self.messages=queue.Queue();self.cancel=threading.Event();self.busy=False;self.buttons=[]
        self.drafts=[];self.selected=set();self.tree_rows={};self.folder=None;self.session=None;self.result_mode=False
        self.dialog=None;self.pending_start=False;self.pending_drafts=[];self.next_action=None;self.environment_ready=False;self.profile=None
        self.started=0;self.phase='';self.closed_requested=False
        self.startup_checked=False;self.environment_auto_attempted=False;self.environment_in_progress=False
        self.environment_action=None;self.notice=None
        self.preference_save_error=None
        self.editor_policy_checked=False;self.editor_policy_retry=None;self.editor_policy_result=None
        self.editor_policy_status=tk.StringVar(value=editor_update_summary(None))
        self.editor_policy_notice_text=tk.StringVar()
        self.bgm_memory=RoundBgmMemory()
        self.legacy_layout=tk.BooleanVar(value=False)
        self.windows=DialogWindows(root,self.update_actions)
        jobs_limit,render_limit=performance_limits(self.preferences)
        self.batch_jobs=tk.StringVar(value=str(jobs_limit))
        self.native_limit=tk.StringVar(value=str(render_limit))
        self.layout();self.fit_height();root.after(80,self.initial);root.after(100,self.pump)

    def fit_height(self):
        target=min(780,self.root.winfo_screenheight()-100)
        self.root.update_idletasks()
        while self.root.winfo_reqheight()>target and int(self.tree.cget('height'))>2:
            self.tree.configure(height=int(self.tree.cget('height'))-1)
            self.root.update_idletasks()

    def button(self,parent,text,command,**kwargs):
        result=ttk.Button(parent,text=text,command=command,**kwargs);self.buttons.append(result);return result

    def layout(self):
        apply_theme(self.root)
        body=ttk.Frame(self.root,padding=18,style='App.TFrame');body.pack(fill='both',expand=True)
        head=ttk.Frame(body,style='App.TFrame');head.pack(fill='x')
        brand_mark(head).pack(side='left',padx=(0,10))
        ttk.Label(head,text='剪映交付助手',style='Title.TLabel').pack(side='left')
        self.button(head,'设置',self.settings,style='Quiet.TButton').pack(side='right')
        ttk.Button(head,text='显示当前窗口',command=self.windows.recover,style='Quiet.TButton').pack(side='right',padx=(0,8))
        ttk.Label(body,text='选草稿  →  确认配乐  →  领取交付包',style='AppMuted.TLabel').pack(anchor='w',pady=(5,10))
        self.environment_panel=ttk.Frame(body,padding=(12,8),style='Checking.TFrame');self.environment_panel.pack(fill='x',pady=(0,10))
        self.environment_badge=ttk.Label(self.environment_panel,text='● 检查中',style='CheckingBadge.TLabel');self.environment_badge.pack(side='left',padx=(0,12))
        self.environment_label=ttk.Label(self.environment_panel,textvariable=self.engine_state,wraplength=650,style='Checking.TLabel')
        self.environment_label.pack(side='left',fill='x',expand=True)
        self.environment_retry=ttk.Button(self.environment_panel,text='重试',command=self.retry_environment,style='Quiet.TButton')
        self.buttons.append(self.environment_retry)
        self.editor_policy_notice=ttk.Label(body,textvariable=self.editor_policy_notice_text,wraplength=960,style='AppMuted.TLabel')
        search=ttk.Frame(body,style='App.TFrame');search.pack(fill='x',pady=(0,7))
        ttk.Label(search,text='搜索剧名 / 集号',style='App.TLabel').pack(side='left')
        ttk.Entry(search,textvariable=self.filter).pack(side='left',fill='x',expand=True,padx=8)
        self.button(search,'重新选集',self.refresh).pack(side='left')
        self.button(search,'没找到草稿？',self.choose_root).pack(side='left',padx=(6,0))
        self.filter.trace_add('write',lambda *args:self.show_drafts())
        listing=ttk.Frame(body);listing.pack(fill='both',expand=True)
        self.tree=ttk.Treeview(listing,columns=('check','project','episode','duration'),show='headings',selectmode='extended',height=7)
        decorate_rows(self.tree)
        for key,label,width in [('check','选择',55),('project','剧名',420),('episode','集 / 时间线',170),('duration','时长',100)]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width,minwidth=45,stretch=key!='check')
        scrollbar=ttk.Scrollbar(listing,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y');self.tree.pack(side='left',fill='both',expand=True)
        self.tree.bind('<Button-1>',self.toggle);self.tree.bind('<space>',self.toggle_selected)
        ttk.Label(body,text='点击左侧方框选集，制作前会自动备份原草稿。',style='AppMuted.TLabel').pack(anchor='w',pady=(5,9))
        output=ttk.Frame(body,style='App.TFrame');output.pack(fill='x',pady=(0,10))
        ttk.Label(output,text='交付保存到',style='App.TLabel').pack(side='left')
        ttk.Entry(output,textvariable=self.output,state='readonly').pack(side='left',fill='x',expand=True,padx=8)
        self.button(output,'选择位置',self.choose_output).pack(side='left')
        self.layout_choice=ttk.Checkbutton(body,text='新批次沿用原结构（每集一个文件夹）；不勾选时按剧名、版本汇总',variable=self.legacy_layout)
        self.layout_choice.pack(anchor='w',pady=(0,8));self.buttons.append(self.layout_choice)
        primary=ttk.Frame(body,style='App.TFrame');primary.pack(fill='x')
        self.start_button=self.button(primary,'开始制作交付包',self.start,style='Primary.TButton');self.start_button.pack(side='left')
        self.button(primary,'继续上次任务',self.resume).pack(side='left',padx=9)
        self.button(primary,'打开交付文件夹',self.open_delivery).pack(side='right')
        self.progress=ttk.Progressbar(body,mode='determinate',maximum=100,value=0);self.progress.pack(fill='x',pady=(9,5))
        ttk.Label(body,textvariable=self.status,wraplength=960,style='App.TLabel').pack(anchor='w')
        self.results=self.tree
        self.tree.bind('<Double-1>',lambda event:self.open_delivery() if self.result_mode else None)
        bottom=ttk.Frame(body,style='App.TFrame');bottom.pack(fill='x',pady=(5,0))
        ttk.Button(bottom,text='停止',command=self.stop,style='Quiet.TButton').pack(side='right')
        ttk.Label(bottom,text='原稿自动备份  ·  1080P / 60帧 / 9:16',style='AppMuted.TLabel').pack(side='left')
        ttk.Label(bottom,text=__version__,style='AppMuted.TLabel').pack(side='left',padx=14)

    def engine_adapter(self):
        return AdvancedAdapter(self.engine_component/'bin',self.engine_runtime.get() or self.state_dir/'not_prepared',self.cache.get())

    def save_preferences(self,explicit_disable=None):
        values={**self.preferences,'settings_version':2,'engine_runtime':self.engine_runtime.get(),'engine_storage':self.engine_storage.get(),
                'output':self.output.get(),'roots':self.roots.get(),'app_dir':self.app_dir.get(),'cache':self.cache.get(),
                'batch_jobs':int(self.batch_jobs.get()),'native_limit':int(self.native_limit.get()),'performance_settings_version':2}
        if self.folder:values['last_direct_batch']=str(self.folder)
        self.preferences=merge_preferences(self.state_dir/'settings.json',values,explicit_disable=explicit_disable)
        self.preference_save_error=None

    def try_save_preferences(self,closing=False):
        try:self.save_preferences();return True
        except (OSError,ValueError) as exc:
            self.preference_save_error=str(exc)
            if closing:self.closed_requested=False
            self.status.set('设置未能保存：'+self.short_text(exc)+('。窗口已保留，处理后可再次关闭。' if closing else '。任务及已完成文件已保留。'))
            return False

    def show_editor_update_result(self,result):
        self.editor_policy_result=result
        if self.closed_requested:return
        self.editor_policy_status.set(editor_update_summary(result))
        observation=result.get('disk_observation',{})
        if result.get('success') and not result.get('report_error') and observation.get('matches_policy') is not False:self.editor_policy_notice.pack_forget()
        else:
            states=['自动更新'+('已核验' if result.get('auto_update',{}).get('ok') else '未能核验'),
                    '内测体验计划'+('已关闭' if result.get('beta',{}).get('ok') else '未能核验')]
            self.editor_policy_notice_text.set('；'.join(states)+'。请在“设置”查看详情或重新核验。')
            self.editor_policy_notice.pack(fill='x',pady=(0,8),after=self.environment_panel);self.fit_height()

    def retry_editor_updates(self,window=None,finished=None):
        if self.closed_requested or self.busy:return False
        def done(result):
            self.show_editor_update_result(result)
            if finished and not self.closed_requested:finished()
        operation=lambda:check_editor_update_policy(self.state_dir)
        if window is None:accepted=self.dispatch(operation,done)
        else:accepted=self.dispatch(operation,done,allow_window=window)
        if accepted is not False:self.editor_policy_checked=True
        return accepted is not False

    def log(self,value):self.messages.put(('log',str(value)))

    def update_actions(self):
        try:
            if not self.root.winfo_exists() or (hasattr(self,'start_button') and not self.start_button.winfo_exists()):return
        except tk.TclError:return
        state='disabled' if self.busy or self.windows.blocked() else 'normal'
        for button in self.buttons:
            try:button.configure(state=state)
            except tk.TclError:pass

    @staticmethod
    def short_text(value,limit=125):
        text=' '.join(str(value).split())
        return text if len(text)<=limit else text[:limit-1]+'…'

    def environment_view(self,kind,text):
        self.environment_panel.configure(style=kind+'.TFrame')
        self.environment_label.configure(style=kind+'.TLabel')
        labels={'Checking':'● 检查中','Preparing':'● 准备中','Ready':'● 已就绪','Failed':'● 需处理'}
        self.environment_badge.configure(text=labels[kind],style=kind+'Badge.TLabel')
        self.engine_state.set(self.short_text(text,140))
        if kind=='Failed':self.environment_retry.pack(side='right',padx=(8,0))
        else:self.environment_retry.pack_forget()

    def environment_failed(self,reason,show_dialog=True):
        self.environment_ready=False;self.environment_in_progress=False
        self.next_action=None;self.pending_start=False;self.environment_action=None
        self.environment_view('Failed','准备尚未完成。可重试，或在“设置”检查存放位置。')
        self.status.set('准备未完成：'+self.short_text(reason))
        if show_dialog and not self.closed_requested:messagebox.showwarning('环境准备未完成',str(reason)+'\n\n任务和原有文件已保留。调整设置后可点“重试”。',parent=self.root)

    def retry_environment(self):
        if self.busy:return
        self.environment_auto_attempted=False
        self.check_environment(auto_prepare=True)

    def continue_after_environment(self):
        if self.closed_requested:return
        if self.pending_start:self.begin_selected()
        elif self.environment_action:
            action=self.environment_action;self.environment_action=None;action()

    def stop(self):
        self.bgm_memory.reset()
        self.cancel.set()
        if not self.busy and self.windows.blocked():
            self.windows.close_all();self.dialog=None
            self.pending_start=False;self.environment_action=None
            self.status.set('已停止等待确认，任务和此前确认的分类已保留，可稍后继续。')
        if self.environment_in_progress or not self.startup_checked:
            self.environment_auto_attempted=True;self.pending_start=False;self.environment_action=None

    def dispatch(self,operation,callback=None,allow_window=None):
        if self.busy or (self.windows.blocked() and (allow_window is None or not self.windows.current(allow_window))):return False
        if self.notice and self.notice.winfo_exists():self.notice.destroy();self.notice=None
        self.busy=True;self.cancel.clear();self.started=time.monotonic()
        for button in self.buttons:button.configure(state='disabled')
        self.progress.configure(mode='indeterminate');self.progress.start(12)
        def worker():
            try:self.messages.put(('done',operation(),callback))
            except Exception as exc:self.messages.put(('error',str(exc)))
        threading.Thread(target=worker,daemon=True).start()
        return True

    def pump(self):
        dirty=False;processed=0;until=time.monotonic()+.012
        try:
            while processed<50 and time.monotonic()<until:
                message=self.messages.get_nowait();kind=message[0]
                processed+=1
                if kind=='log':self.phase=self.short_text(message[1]);self.status.set(self.phase)
                elif kind=='engine_progress':self.engine_state.set(self.short_text(message[1],140))
                elif kind=='batch':
                    self.folder,self.session=message[1:];self.try_save_preferences();self.show_results()
                elif kind=='job':dirty=True
                elif kind in ('done','error'):
                    if dirty:self.show_results();dirty=False
                    self.busy=False;self.progress.stop();self.progress.configure(mode='determinate',value=0)
                    self.update_actions()
                    if kind=='error':
                        self.next_action=None;self.pending_start=False
                        if self.session:self.show_results()
                        if self.environment_in_progress:self.environment_failed(message[1],not self.cancel.is_set())
                        else:
                            self.status.set('需要处理：'+self.short_text(message[1]))
                            if not self.closed_requested:messagebox.showerror('未完成交付',message[1],parent=self.root)
                    else:
                        follow=self.next_action;self.next_action=None
                        try:
                            if message[2]:message[2](message[1])
                            if follow and not self.closed_requested:
                                if self.cancel.is_set():self.environment_failed('已停止准备，点击“重试”可以继续。',False)
                                else:follow()
                        except (OSError,ValueError) as exc:
                            if self.environment_in_progress:self.environment_failed(str(exc))
                            elif not self.closed_requested:messagebox.showerror('需要处理',str(exc),parent=self.root)
        except queue.Empty:pass
        if dirty:self.show_results()
        if self.busy and self.phase:self.status.set(self.phase+'  ·  已用'+str(int(time.monotonic()-self.started))+'秒')
        if self.closed_requested and not self.busy:
            if self.try_save_preferences(closing=True):self.root.destroy();return
        if self.preference_save_error:
            self.status.set('设置未能保存：'+self.short_text(self.preference_save_error)+'。任务已保留；处理后可重试保存或再次关闭窗口。')
        self.root.after(10 if not self.messages.empty() else 100,self.pump)

    def initial(self):
        if self.editor_policy_retry is not None:
            try:self.root.after_cancel(self.editor_policy_retry)
            except tk.TclError:pass
            self.editor_policy_retry=None
        if self.closed_requested:return
        if self.busy or self.windows.blocked():
            self.editor_policy_retry=self.root.after(100,self.initial)
            return
        if not self.editor_policy_checked:
            def checked(result):
                self.show_editor_update_result(result)
                if not self.closed_requested:self.initial()
            self.editor_policy_checked=True
            if self.dispatch(lambda:check_editor_update_policy(self.state_dir),checked) is False:
                self.editor_policy_checked=False;self.editor_policy_retry=self.root.after(100,self.initial)
            return
        def done(result):
            self.drafts,warnings=result
            if self.closed_requested:return
            self.selected.intersection_update(draft.key for draft in self.drafts);self.show_drafts()
            self.status.set('已找到'+str(len(self.drafts))+'条时间线。'+('；'.join(warnings[:2]) if warnings else '请选择要交付的集。'))
            self.check_environment(auto_prepare=not self.startup_checked)
        roots=[value.strip() for value in self.roots.get().split(';') if value.strip()]
        self.dispatch(lambda:current_drafts(roots,self.base),done)

    def check_environment(self,auto_prepare=False):
        if self.busy:return
        self.environment_in_progress=True;self.environment_view('Checking','正在检查本机运行环境…')
        try:
            self.media_directory();adapter=self.engine_adapter()
        except (OSError,ValueError) as exc:
            self.startup_checked=True;self.environment_auto_attempted=True;self.environment_failed(str(exc));return
        def check():
            try:return adapter.verify()
            except (OSError,ValueError):return None
        def checked(profile):
            self.startup_checked=True;self.profile=profile
            self.environment_ready=bool(profile and self.preferences.get('direct_environment_check')==self.machine_key(profile))
            if self.environment_ready:
                self.environment_in_progress=False;self.environment_view('Ready','可以选择草稿，直接开始制作。')
                self.continue_after_environment();return
            self.environment_in_progress=False
            if auto_prepare and not self.environment_auto_attempted and not self.closed_requested:
                self.environment_auto_attempted=True
                if profile:self.self_test()
                else:self.prepare_environment()
            else:self.environment_view('Failed','首次准备尚未完成，可点“重试”继续。')
        self.dispatch(check,checked)

    def refresh(self):
        if not self.busy:self.initial()

    def show_drafts(self):
        if self.windows.blocked() or (self.busy and self.result_mode):return
        self.result_mode=False
        self.start_button.configure(text='开始制作交付包')
        self.tree.configure(columns=('check','project','episode','duration'))
        for key,label,width in [('check','选择',55),('project','剧名',420),('episode','集 / 时间线',170),('duration','时长',100)]:
            self.tree.heading(key,text=label);self.tree.column(key,width=width,minwidth=45,stretch=key!='check')
        self.tree.delete(*self.tree.get_children());self.tree_rows={}
        keyword=self.filter.get().strip().casefold()
        for number,draft in enumerate(self.drafts):
            if keyword and keyword not in (draft.name+' '+draft.timeline_name).casefold():continue
            duration=f'{draft.duration/1e6:.1f}秒' if draft.duration else '打开后读取'
            iid=self.tree.insert('','end',values=('☑' if draft.key in self.selected else '□',draft.name,draft.timeline_name,duration),tags=('odd' if number%2 else 'even',));self.tree_rows[iid]=draft

    def toggle(self,event):
        if self.busy or self.result_mode:return
        if self.tree.identify_column(event.x)=='#1':
            iid=self.tree.identify_row(event.y)
            if iid in self.tree_rows:self.selected.symmetric_difference_update({self.tree_rows[iid].key});self.show_drafts();return 'break'

    def toggle_selected(self,event=None):
        if not self.busy and not self.result_mode:
            for iid in self.tree.selection():
                if iid in self.tree_rows:self.selected.symmetric_difference_update({self.tree_rows[iid].key})
            self.show_drafts()

    def choose_root(self):
        selected=filedialog.askdirectory(title='选择剪映本地草稿库或某部剧的草稿文件夹',parent=self.root)
        if selected:self.roots.set(selected);self.save_preferences();self.refresh()

    def choose_output(self):
        path=filedialog.askdirectory(title='选择交付保存位置（需存放草稿备份和成片）',parent=self.root)
        if path:self.output.set(path);self.save_preferences()

    def media_directory(self):
        value=resolve_app_dir(self.app_dir.get(),self.base)
        if not value or not all((Path(value)/name).is_file() for name in ('ffmpeg.exe','ffprobe.exe')):
            raise ValueError('没有找到视频核验组件。请在“设置”选择已安装的剪映目录，然后重试。')
        return value

    def machine_key(self,profile):
        return '|'.join([platform.node(),platform.platform(),profile.get('dll_sha256',''),profile.get('tool_sha256','')])

    def prepare_environment(self):
        if self.busy:return
        self.environment_in_progress=True;self.environment_view('Preparing','首次使用，正在自动准备运行环境；完成后会记住。')
        try:
            self.media_directory()
            self.next_action=self.self_test
            self.phase='准备环境，完成后自动自测'
            launch_setup(self)
        except (OSError,ValueError) as exc:self.environment_failed(str(exc))

    def self_test(self):
        from .portable_test import run_test
        self.environment_in_progress=True;self.environment_view('Preparing','正在完成首次检查，请稍等；成功后无需重复准备。')
        try:adapter=self.engine_adapter();app=self.media_directory()
        except (OSError,ValueError) as exc:self.environment_failed(str(exc));return
        self.phase='首次自测，结束后自动继续'
        output=Path(self.output.get())/'环境自测'
        def done(report):
            if not report['success']:
                self.environment_failed(report.get('error','检查未通过，请查看环境自测报告。'),not self.cancel.is_set());return
            if self.closed_requested or self.cancel.is_set():
                self.environment_failed('已停止首次检查，可以稍后重试。',False);return
            profile=adapter.verify();previous=dict(self.preferences)
            try:
                self.preferences['direct_environment_check']=self.machine_key(profile);self.save_preferences()
            except (OSError,ValueError):self.preferences=previous;raise
            self.profile=profile;self.environment_ready=True;self.environment_in_progress=False
            self.environment_view('Ready','运行环境已准备好，以后打开即可使用。')
            self.status.set('准备完成，请选择需要制作的草稿。')
            self.continue_after_environment()
        self.dispatch(lambda:run_test(adapter,app,output,self.cancel,self.log),done)

    def start(self):
        if self.busy:return
        if self.result_mode and self.session:
            if all(job.get('easy_delivery_folder') for job in self.session['jobs']):self.open_delivery();return
            self.resume();return
        chosen=[draft for draft in self.drafts if draft.key in self.selected]
        if not chosen:messagebox.showinfo('先选集数','点击左侧方框选中需要交付的集。',parent=self.root);return
        self.pending_drafts=chosen;self.pending_start=True
        if not self.environment_ready:
            if self.profile:self.self_test()
            else:self.prepare_environment()
            return
        self.begin_selected()

    def begin_selected(self):
        self.bgm_memory.reset()
        self.pending_start=False
        adapter=self.engine_adapter();output=self.output.get();chosen=list(self.pending_drafts)
        archive_mode='legacy' if self.legacy_layout.get() else 'series'
        self.phase='完整备份草稿并分析配乐与字幕'
        def created(folder,session):self.messages.put(('batch',folder,session))
        def done(result):self.folder,self.session=result;self.save_preferences();self.show_results();self.confirm_next()
        roots=[value.strip() for value in self.roots.get().split(';') if value.strip()]
        def operation():
            latest=refresh_selection(chosen,roots,self.base)
            return prepare(latest,output,adapter,self.cancel,self.log,created,archive_mode=archive_mode)
        self.dispatch(operation,done)

    def resume(self):
        if self.busy:return
        self.bgm_memory.reset()
        path=str(self.folder) if self.folder else self.preferences.get('last_direct_batch')
        if not path or not (Path(path)/'任务清单.json').is_file():
            path=filedialog.askopenfilename(title='选择需要继续的任务清单.json',filetypes=[('任务清单','*.json')],parent=self.root)
        if not path:return
        try:self.folder,self.session=read_batch(path)
        except (OSError,ValueError) as exc:messagebox.showerror('无法继续',str(exc),parent=self.root);return
        if not self.environment_ready:
            self.environment_action=self.resume;self.retry_environment();return
        self.phase='核对原有备份和已保存的分类'
        def done(_):
            self.save_preferences();self.show_results()
            if all(job.get('easy_resume_verified') for job in self.session['jobs']):
                self.status.set('已有'+str(len(self.session['jobs']))+'集完整交付已复核；点击“打开交付文件夹”取成片。')
                if self.session.get('csv_notice'):self.status.set(self.status.get()+' '+self.session['csv_notice'])
                self.notify_results({'completed':len(self.session['jobs']),'failed':0,'pending':0},resumed=True)
                return
            self.confirm_next()
        self.dispatch(lambda:analyze(self.folder,self.session,self.engine_adapter(),self.cancel,self.log),done)

    def confirm_next(self):
        pending=[job for job in self.session['jobs'] if not job.get('easy_confirmed') and not job.get('easy_analysis_error')]
        if not pending:self.begin_render();return
        self.confirm_dialog(pending[0])

    def confirm_dialog(self,job):
        rows=job['easy_analysis']['rows'];audio=[row for row in rows if row['kind']=='audio' and not row['is_container']]
        text_rows=[row for row in rows if row['kind']=='text' and not row['is_container']]
        captions=set(job['easy_analysis']['caption_keys']);bgm=self.bgm_memory.suggest(rows)
        window=tk.Toplevel(self.root);self.dialog=window;window.title('确认本集长配乐和字幕')
        window.geometry(f'{min(1000,self.root.winfo_screenwidth()-70)}x{min(620,self.root.winfo_screenheight()-100)}');window.minsize(700,470)
        body=ttk.Frame(window,padding=15);body.pack(fill='both',expand=True)
        # Reserve decisions and actions before allocating the elastic list.
        # Later episodes can add a BGM-memory notice without clipping buttons.
        controls=ttk.Frame(body);controls.pack(side='bottom',fill='x')
        foot=ttk.Frame(controls);foot.pack(side='bottom',fill='x',pady=(12,0))
        ttk.Label(body,text='第'+job['draft']['timeline_name']+'集 · '+job['draft']['name'],font=('Microsoft YaHei UI',15,'bold'),wraplength=min(930,self.root.winfo_screenwidth()-110)).pack(anchor='w')
        ttk.Label(body,text='只勾选无配乐版需要去掉的长BGM。其余人声、环境音、音效和短音乐全部保留。',wraplength=930).pack(anchor='w',pady=(7,9))
        listing=ttk.Frame(body);listing.pack(fill='both',expand=True)
        tree=ttk.Treeview(listing,columns=('check','name','track','start','duration'),show='headings',height=9)
        for key,label,width in [('check','去掉BGM',80),('name','素材名称',350),('track','所在轨道 / 复合',270),('start','起点(秒)',80),('duration','时长(秒)',90)]:
            tree.heading(key,text=label);tree.column(key,width=width,minwidth=45,stretch=key not in ('check','start','duration'))
        scroll=ttk.Scrollbar(listing,orient='vertical',command=tree.yview);tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');tree.pack(side='left',fill='both',expand=True)
        summary=tk.StringVar();none=tk.BooleanVar(value=not audio);mapped={}
        for i,row in enumerate(sorted(audio,key=lambda row:(row['suggestion']!='背景音乐',-row['duration'],row['start']))):
            mapped[str(i)]=row
            tree.insert('','end',iid=str(i),values=('☑' if row['key'] in bgm else '□',row['name'],row['track'],f'{row["start"]/1e6:.1f}',f'{row["duration"]/1e6:.1f}'))
        def refresh():summary.set(f'已勾选{len(bgm)}段长配乐；最终字幕{len(captions)}条，其他文字{len(text_rows)-len(captions)}条保留为标题。')
        def toggle(event):
            if not self.windows.current(window):return
            if tree.identify_column(event.x)!='#1':return
            iid=tree.identify_row(event.y)
            if iid in mapped:
                key=mapped[iid]['key'];bgm.symmetric_difference_update({key});tree.set(iid,'check','☑' if key in bgm else '□')
                if bgm:none.set(False)
                refresh();return 'break'
        tree.bind('<Button-1>',toggle)
        ttk.Checkbutton(controls,text='本集没有需要移除的独立长配乐',variable=none).pack(anchor='w',pady=(8,4))
        ttk.Label(controls,textvariable=summary,foreground='#365a78').pack(anchor='w',pady=(0,7))
        if bgm:ttk.Label(controls,text=f'已沿用本轮此前确认的相同引用：{len(bgm)} 段，可在本集取消勾选。',foreground=COLORS['success']).pack(anchor='w',pady=(0,5))
        if not audio:ttk.Label(controls,text=NO_AUDIO_TRACKS,foreground='#8b531a',wraplength=900).pack(anchor='w')
        def adjust_text():
            if not self.windows.current(window):return
            dialog=tk.Toplevel(window);dialog.title('字幕范围');dialog.geometry('850x500')
            ttk.Label(dialog,text='勾选台词字幕；片名、标题、角标不勾选。',padding=12).pack(anchor='w')
            listing=ttk.Frame(dialog);listing.pack(fill='both',expand=True,padx=12)
            table=ttk.Treeview(listing,columns=('check','text','track'),show='headings',height=12)
            for key,label,width in [('check','作为字幕',90),('text','最终文字',500),('track','所在轨道',220)]:table.heading(key,text=label);table.column(key,width=width)
            scroll=ttk.Scrollbar(listing,orient='vertical',command=table.yview);table.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right',fill='y');table.pack(side='left',fill='both',expand=True)
            for i,row in enumerate(text_rows):table.insert('','end',iid=str(i),values=('☑' if row['key'] in captions else '□',row['name'],row['track']))
            def toggled(event):
                if table.identify_column(event.x)!='#1':return
                iid=table.identify_row(event.y)
                if iid:
                    key=text_rows[int(iid)]['key'];captions.symmetric_difference_update({key});table.set(iid,'check','☑' if key in captions else '□');refresh();return 'break'
            table.bind('<Button-1>',toggled)
            def close():dialog.destroy();refresh()
            ttk.Button(dialog,text='保存范围',command=close).pack(anchor='e',padx=12,pady=10);dialog.protocol('WM_DELETE_WINDOW',close)
            self.windows.register(dialog,window)
        ttk.Button(controls,text='查看 / 调整字幕范围',command=adjust_text).pack(anchor='w')
        ttk.Label(controls,text=confirmation_counts(self.session['jobs']),wraplength=920,foreground=COLORS['muted']).pack(anchor='w',pady=(9,0))
        def later():self.bgm_memory.reset();window.destroy();self.dialog=None;self.status.set('任务已保存，点击“继续上次任务”可接着制作。')
        def accepted():
            if not self.windows.current(window):return
            missing=missing_features(job['easy_analysis'],captions)
            notes=[]
            if missing['no_subtitles']:notes.append('未识别出可导出字幕。'+NO_SUBTITLES)
            if missing['no_audio_tracks']:notes.append(NO_AUDIO_TRACKS+' 若源视频也无声音，成片将为静音。')
            if notes and not messagebox.askyesno('本集缺少轨道，是否继续？',
                    '第'+job['draft']['timeline_name']+'集 · '+job['draft']['name']+'\n\n'+'\n\n'.join(notes)+'\n\n是否按现状继续导出？选择“否”可返回检查。',
                    parent=window,default=messagebox.NO):return
            flags={}
            if missing['no_subtitles']:flags['no_captions_confirmed']=True
            if missing['no_audio_tracks']:flags['no_audio_confirmed']=True
            try:confirm(self.folder,self.session,job,bgm,captions,none.get(),**flags)
            except (OSError,ValueError) as exc:messagebox.showerror('请完成确认',str(exc),parent=window);return
            self.bgm_memory.remember(rows,bgm)
            window.destroy();self.dialog=None;self.confirm_next()
        ttk.Button(foot,text='稍后继续',command=later).pack(side='left')
        ttk.Button(foot,text='确认并继续制作',command=accepted,style='Primary.TButton').pack(side='right')
        window.protocol('WM_DELETE_WINDOW',later);refresh();self.windows.register(window)

    def begin_render(self):
        self.bgm_memory.reset()
        self.phase='正在生成四版视频、可用字幕和最终交付文件夹'
        adapter=self.engine_adapter()
        try:app=self.media_directory()
        except ValueError as exc:messagebox.showerror('无法开始',str(exc),parent=self.root);return
        def done(result):
            self.show_results()
            ending='点“打开交付文件夹”取成片。' if result['completed'] else '任务已保留，可继续；双击未完成的集查看原因。'
            self.status.set(f'完成{result["completed"]}集，需处理{result["failed"]}集，未执行{result["pending"]}集。'+ending)
            peak=result.get('resources',{}).get('native',{}).get('peak_render',0)
            if peak:self.status.set(self.status.get()+f' 实际同时渲染峰值{peak}份，制作耗时{result["elapsed_seconds"]:.1f}秒。')
            if result.get('resources',{}).get('downgraded'):self.status.set(self.status.get()+' 本批次渲染上限已自动降为'+str(result['resources']['native']['limit'])+'。')
            if self.session.get('csv_notice'):self.status.set(self.status.get()+' '+self.session['csv_notice'])
            self.notify_results(result)
        self.dispatch(lambda:run(self.folder,self.session,adapter,app,self.cancel,self.log,lambda job:self.messages.put(('job',)),
            max_jobs=int(self.batch_jobs.get()),native_limit=int(self.native_limit.get())),done)

    def show_results(self):
        self.result_mode=True
        self.start_button.configure(text='打开交付文件夹' if self.session and all(job.get('easy_delivery_folder') for job in self.session['jobs']) else '继续制作交付包')
        self.results.configure(columns=('project','episode','state'))
        for key,label,width in [('project','本次任务',380),('episode','集',90),('state','进度',450)]:
            self.results.heading(key,text=label);self.results.column(key,width=width,stretch=True)
        self.results.delete(*self.results.get_children())
        if not self.session:return
        for job in self.session['jobs']:
            state=result_state(job)
            tag='complete' if state=='完成' else ('problem' if job.get('easy_analysis_error') or state.startswith(('需要处理：','分析未完成：','导出失败')) else 'working')
            self.results.insert('','end',iid=job['id'],values=(job['draft']['name'],job['draft']['timeline_name'],state),tags=(tag,))
        if self.session['jobs']:self.results.selection_set(self.session['jobs'][0]['id'])

    def notify_results(self,result,resumed=False):
        if self.closed_requested:return
        completed,failed,pending=(int(result.get(key,0)) for key in ('completed','failed','pending'))
        total=len(self.session['jobs']) if self.session else 0
        self.progress.configure(mode='determinate',value=100*completed/total if total else 0)
        if total and completed==total and not failed and not pending:
            self.completion_dialog(completed,resumed)
        elif self.cancel.is_set() and pending:
            messagebox.showinfo('已停止制作',f'完成 {completed} 集，需要处理 {failed} 集，未执行 {pending} 集。\n已完成文件和任务进度已保留，点击“继续上次任务”即可继续。',parent=self.root)
        else:
            resource_count=sum((job.get('easy_failure') or {}).get('kind')=='resources' for job in (self.session or {}).get('jobs',[]))
            note=(f'\n\n其中 {resource_count} 集因内存或显存余量不足暂未完成。请保存并正常关闭多余应用，再点击“继续上次任务”。' if resource_count else '')
            messagebox.showwarning('本次制作已结束',f'完成 {completed} 集，需要处理 {failed} 集，未执行 {pending} 集。\n\n已完成文件和任务进度已保留；双击未完成的集可查看原因。'+note,parent=self.root)

    def completion_dialog(self,count,resumed=False):
        if self.notice and self.notice.winfo_exists():self.notice.destroy()
        window=tk.Toplevel(self.root);self.notice=window;window.title('交付包已完成')
        window.geometry('560x310');window.resizable(False,False)
        body=ttk.Frame(window,padding=24);body.pack(fill='both',expand=True)
        ttk.Label(body,text='✓  全部完成',font=('Microsoft YaHei UI',21,'bold'),foreground=COLORS['success']).pack(anchor='w')
        text=f'{count} 集交付包已准备好，可以打开文件夹查看。'
        if resumed:text=f'{count} 集已有交付已核对完成，可以直接取用。'
        ttk.Label(body,text=text,wraplength=510).pack(anchor='w',pady=(10,8))
        omitted=sum(not job.get('want_srt',True) for job in self.session['jobs'])
        if omitted:ttk.Label(body,text=f'其中 {omitted} 集按此前确认未生成SRT，详情见缺项说明。',foreground=COLORS['warning'],wraplength=510).pack(anchor='w',pady=(0,6))
        ttk.Label(body,text='交付位置',style='Muted.TLabel').pack(anchor='w')
        ttk.Label(body,text=str(Path(self.folder)/'交付'),wraplength=510,foreground=COLORS['muted']).pack(anchor='w',pady=(3,12))
        foot=ttk.Frame(body);foot.pack(side='bottom',fill='x')
        def close():window.destroy();self.notice=None
        ttk.Button(foot,text='知道了',command=close).pack(side='left')
        ttk.Button(foot,text='打开交付文件夹',command=self.open_completed_folder,style='Primary.TButton').pack(side='right')
        window.protocol('WM_DELETE_WINDOW',close);window.update_idletasks()
        height=min(self.root.winfo_screenheight()-90,max(310,window.winfo_reqheight()))
        left=max(10,min(self.root.winfo_screenwidth()-570,self.root.winfo_rootx()+(self.root.winfo_width()-560)//2))
        top=max(10,min(self.root.winfo_screenheight()-height-55,self.root.winfo_rooty()+(self.root.winfo_height()-height)//2))
        window.geometry(f'560x{height}+{left}+{top}');self.windows.register(window,blocking=False)

    def open_completed_folder(self):
        if not self.session:return
        jobs=[job for job in self.session['jobs'] if job.get('easy_delivery_folder')]
        path=Path(jobs[0]['easy_delivery_folder']) if len(jobs)==1 else Path(self.folder)/'交付'
        try:
            if not path.is_dir():raise FileNotFoundError('交付文件夹已移动或不存在，请在任务列表中重新打开。')
            os.startfile(path)
        except (OSError,ValueError) as exc:messagebox.showerror('暂时无法打开',str(exc),parent=self.notice or self.root)

    def open_delivery(self):
        if not self.session:messagebox.showinfo('还没有任务','先选择集数制作交付包。',parent=self.root);return
        if not self.result_mode:self.show_results();return
        selected=self.results.selection()
        if len(selected)!=1:messagebox.showinfo('选择一集','在下方本次任务列表选中一集，再打开交付文件夹。',parent=self.root);return
        job=next(job for job in self.session['jobs'] if job['id']==selected[0])
        reason=job.get('easy_delivery_state','')
        if not job.get('easy_delivery_folder') and (job.get('easy_analysis_error') or reason.startswith(('需要处理：','分析未完成：','已停止'))):
            detail=(job.get('easy_failure') or {}).get('message') or reason or job['easy_analysis_error']
            messagebox.showinfo('第'+job['draft']['timeline_name']+'集尚未完成',detail,parent=self.root)
            return
        try:directory=final_folder(job)
        except (OSError,ValueError) as exc:messagebox.showinfo('交付尚未完成',str(exc),parent=self.root);return
        os.startfile(directory)

    def settings(self):
        window=tk.Toplevel(self.root);window.title('设置（每台电脑通常只需一次）');window.geometry('850x'+str(min(600,self.root.winfo_screenheight()-100)))
        foot=ttk.Frame(window,padding=(15,4,15,10));foot.pack(side='bottom',fill='x')
        body=ttk.Frame(window,padding=15);body.pack(fill='both',expand=True);body.columnconfigure(1,weight=1)
        fields=[('环境文件存放位置',self.engine_storage),('剪映安装位置',self.app_dir),('草稿库',self.roots),('媒体缓存',self.cache)]
        for i,(label,var) in enumerate(fields):
            ttk.Label(body,text=label).grid(row=i,column=0,sticky='w',pady=8)
            ttk.Entry(body,textvariable=var).grid(row=i,column=1,sticky='ew',padx=8)
            def browse(variable=var,index=i):
                path=filedialog.askdirectory(parent=window)
                if path:variable.set(str(Path(path)/'HeadlessRuntime') if index==0 else resolve_app_dir(path,self.base) or path if index==1 else path)
            ttk.Button(body,text='浏览',command=browse).grid(row=i,column=2)
        limits=ttk.Frame(body);limits.grid(row=4,column=0,columnspan=3,sticky='ew',pady=(12,3))
        ttk.Label(limits,text='同时处理集数').pack(side='left')
        ttk.Combobox(limits,textvariable=self.batch_jobs,values=tuple(map(str,range(1,9))),state='readonly',width=5).pack(side='left',padx=(8,22))
        ttk.Label(limits,text='同时渲染上限').pack(side='left')
        ttk.Combobox(limits,textvariable=self.native_limit,values=tuple(map(str,range(1,9))),state='readonly',width=5).pack(side='left',padx=8)
        ttk.Label(body,text='默认4集 / 4个渲染进程，可选1至8。资源不足时自动排队或降低并发；已完成的文件会保留。\n运行环境首次自动准备，成功后无需重复；4档及以上最多两集同时完整核验。',wraplength=780).grid(row=5,column=0,columnspan=3,sticky='w',pady=10)
        policy=ttk.LabelFrame(body,text='剪映更新设置',padding=8);policy.grid(row=6,column=0,columnspan=3,sticky='ew')
        policy.columnconfigure(0,weight=1)
        disabled=tk.BooleanVar(window,value=disables_auto_update(load_json(self.state_dir/'settings.json',self.preferences)))
        user_touched=False
        def touched():
            nonlocal user_touched
            user_touched=True
        choice=ttk.Checkbutton(policy,text='关闭剪映专业版自动更新',variable=disabled,command=touched);choice.grid(row=0,column=0,sticky='w')
        ttk.Label(policy,text='每次启动核验自动下载/安装设置；取消勾选并保存才重新开启。\n“参与内测体验计划”始终关闭。新版提醒由剪映独立处理，提醒记录与采样时间见下方。',wraplength=760).grid(row=1,column=0,columnspan=2,sticky='w',pady=4)
        ttk.Label(policy,textvariable=self.editor_policy_status,wraplength=720).grid(row=2,column=0,columnspan=2,sticky='w')
        def restore():
            if window.winfo_exists():
                for control in (choice,retry,save):control.state(['!disabled'])
        def retried():
            if self.busy:return
            for control in (choice,retry,save):control.state(['disabled'])
            if not self.retry_editor_updates(window,restore):restore()
        retry=ttk.Button(policy,text='重新核验',command=retried);retry.grid(row=0,column=1,sticky='e')
        def saved():
            if self.busy:return
            try:self.save_preferences(explicit_disable=bool(disabled.get()) if user_touched else None)
            except (OSError,ValueError) as exc:messagebox.showerror('设置未保存',str(exc),parent=window);return
            window.destroy()
            self.retry_editor_updates(finished=lambda:self.retry_environment() if not self.environment_ready and not self.cancel.is_set() else None)
        save=ttk.Button(foot,text='保存',command=saved);save.pack(side='right')
        self.windows.register(window)

    def close(self):
        self.bgm_memory.reset()
        self.windows.close_all()
        self.notice=None;self.dialog=None
        if self.busy:self.cancel.set();self.closed_requested=True;self.status.set('正在安全停止，任务和已完成文件会保留。')
        elif self.try_save_preferences(closing=True):self.root.destroy()


def launch():
    root=tk.Tk()
    try:EasyApp(root)
    except (OSError,ValueError) as exc:
        root.withdraw();messagebox.showerror('无法启动',str(exc),parent=root);root.destroy();return
    root.mainloop()
