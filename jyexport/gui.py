from __future__ import annotations
import json
import math
import os
import queue
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from .codec import Codec
from .discovery import app_dirs, load_json, roots_and_index, scan, resolve_app_dir, resource_dirs
from .draft import ROLES, VARIANTS, inspect
from .media import archive_srt, archive_video
from .storage import atomic_json, create_session, event, prepare_job, register_prepared, save_session, sha, validate_session
from .engine import EngineAdapter, classify_standard_job, component_root, ensure_timeline_identity, persist_manual_roles, render_job
from .editor_updates import merge_preferences

HELP = '''从“开始使用”按顺序完成交付

A 首次准备环境：确认数据盘位置，点击“准备引擎”→“检查引擎”→“运行自测”。每台电脑只需准备一次。
B 进入“1 选择草稿”，勾选集数，点击“创建导出任务（先备份）”。普通模式固定生成四版视频和SRT。
C 在“3 导出与审核”选中一集的任意版本行，点击“分析当前集”。分析完成会进入“2 确认内容”。
   将所有长背景音乐标为“背景音乐”；短音乐、人声、环境音和音效保留；台词标为“字幕”，片名、标题和角标标为“标题/其他文字”。完成后“保存分类”。
D 返回“3 导出与审核”，点击“导出当前集”。完成后打开文件夹，听看确认四版开关及SRT，再标记审核通过。

遇到复合片段或处理后声音提示，在开始使用页启用额外兼容检查，再重新分析。首次必须和剪映原生成片对照。
无配乐版只移除已确认的长BGM，不移除短音乐、音效、人声和环境音；无法分离已混入素材的配乐或已烧录的字幕。
原草稿先完整备份，已有结果不覆盖。重开程序后用“继续已有任务”选择任务清单.json；保持原批次目录位置。

“规格通过”不等于声画审核通过；不同电脑、未知更高草稿结构、全部效果均未通用验收。
旧版读取、注册草稿副本、两母版合成、公司收件箱等在“显示高级工具与路径设置”中，日常后台导出不需要操作这些按钮。

详细图文式步骤、版本表和故障处理请打开解压目录中的“新手操作指南.html”。'''

class App:
    def __init__(self, root):
        self.root = root
        from . import __version__
        self.root.title(f'剪映交付助手 {__version__} · 后台导出验证 / 草稿备份 / 审核归档')
        screen_width,screen_height=self.root.winfo_screenwidth(),self.root.winfo_screenheight()
        width=min(1200,max(800,screen_width-80));height=min(880,max(560,screen_height-120))
        self.root.geometry(f'{width}x{height}+20+20')
        self.root.minsize(min(980,width),min(640,height))
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.messages = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.inbox_stop=threading.Event()
        self.inbox_thread=None
        self.inbox_rows={}
        self.inbox_tree_rows={}
        self.inbox_run_description=''
        self._close_requested=False
        self.editor_policy_checked=False
        self.preference_save_error=None
        self.drafts = []
        self.selected = set()
        self.rows = {}
        self.task_rows = {}
        self.session = None
        self.session_folder = None
        self.current_job = None
        self.classification = []
        self.role_map = {}
        self.buttons = []
        from .deployment import configuration_directory,recommend_storage
        self.state_dir = configuration_directory(sys_base())
        self.preferences = load_json(self.state_dir/'settings.json',{})
        hint = Path(sys_base())
        versions = app_dirs(hint)
        roots, self.index = roots_and_index(hint)
        self.app_dir = tk.StringVar(value=resolve_app_dir(self.preferences.get('app_dir'), hint))
        saved_roots=self.preferences.get('roots')
        if self.preferences.get('settings_version',1)<2:
            # Version 1 incorrectly treated an EXE's Desktop as a draft library.
            roots.extend(p.strip() for p in (saved_roots or '').split(';') if p.strip() and (Path(p.strip())/'draft_content.json').is_file())
            saved_roots=';'.join(dict.fromkeys(roots))
        self.roots = tk.StringVar(value=saved_roots or ';'.join(roots))
        self.output = tk.StringVar(value=self.preferences.get('output') or str(Path.home()/'Videos/剪映审核归档'))
        self.inbox_root=tk.StringVar(value=self.preferences.get('inbox_root') or str(Path.home()/'Videos/剪映交付收件箱'))
        self.inbox_audit_seconds=tk.StringVar(value=str(self.preferences.get('inbox_audit_seconds',3600)))
        resources = resource_dirs()
        self.cache = tk.StringVar(value=self.preferences.get('cache') or resources['cache'])
        self.presets = tk.StringVar(value=self.preferences.get('presets') or resources['presets'])
        self.engine_component = component_root(sys_base())
        runtime_config = load_json(self.state_dir/'engine_runtime.local.json',{}) or load_json(self.engine_component / 'runtime.local.json', {})
        self.engine_runtime = tk.StringVar(value=self.preferences.get('engine_runtime') or runtime_config.get('runtime_directory',''))
        self.engine_storage = tk.StringVar(value=str(recommend_storage(sys_base(),self.state_dir,self.preferences.get('engine_storage'))))
        if not self.preferences.get('output'):self.output.set(str(Path(self.engine_storage.get()).parent/'交付归档'))
        if not self.preferences.get('inbox_root'):self.inbox_root.set(str(Path(self.engine_storage.get()).parent/'交付收件箱'))
        self.engine_parallel = tk.IntVar(value=self.preferences.get('engine_parallel',2))
        self.engine_hardware = tk.BooleanVar(value=self.preferences.get('engine_hardware',True))
        self.engine_standard_confirm = tk.BooleanVar(value=False)
        self.engine_advanced = tk.BooleanVar(value=False)
        self.filter = tk.StringVar()
        self.status = tk.StringVar(value='只读扫描 · 先备份后处理 · 视频由剪映原生渲染')
        self.codec = Codec(self.app_dir.get() or None)
        self._layout(versions)
        self.root.after(100,self.pump)
        self.root.after(200,self.initial)

    def button(self,parent,label,command):
        button = ttk.Button(parent,text=label,command=command)
        self.buttons.append(button)
        return button

    def _layout(self,versions):
        style=ttk.Style()
        style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',10))
        style.configure('Treeview',rowheight=30,background='#ffffff',fieldbackground='#ffffff')
        style.configure('Treeview.Heading',font=('Microsoft YaHei UI',10,'bold'),background='#e8eef7')
        style.map('Treeview',background=[('selected','#d5e7ff')],foreground=[('selected','#14233c')])
        style.configure('Accent.TButton',background='#2368b5',foreground='white',padding=7)
        frame=ttk.Frame(self.root,padding=16);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='剪映交付助手',font=('Microsoft YaHei UI',21,'bold')).pack(anchor='w')
        ttk.Label(frame,text='四种成片 + SRT  ·  1080 × 1920 / MP4 / 60 fps  ·  本地备份与审核记录',foreground='#466080').pack(anchor='w',pady=(4,12))
        paths=ttk.Frame(frame);paths.pack(fill='x')
        for index,(label,var,command) in enumerate([('剪映版本',self.app_dir,self.choose_app),('草稿库',self.roots,self.choose_root),('媒体缓存',self.cache,self.choose_cache),('预设位置',self.presets,self.choose_presets),('输出目录',self.output,self.choose_output)]):
            ttk.Label(paths,text=label,width=9).grid(row=index,column=0,sticky='w',pady=3)
            if index==0:
                ttk.Combobox(paths,textvariable=var,values=versions).grid(row=index,column=1,sticky='ew',padx=8,pady=3)
            else:
                ttk.Entry(paths,textvariable=var).grid(row=index,column=1,sticky='ew',padx=8,pady=3)
            self.button(paths,'浏览',command).grid(row=index,column=2,pady=3)
        paths.columnconfigure(1,weight=1)
        ttk.Label(frame,text='按“开始使用”的顺序操作；原稿先备份，成片完成后仍须声画审核。',foreground='#9a5a18').pack(anchor='w',pady=(8,10))
        self.tabs=ttk.Notebook(frame);self.tabs.pack(fill='both',expand=True)
        draft_tab=ttk.Frame(self.tabs,padding=10);task_tab=ttk.Frame(self.tabs,padding=10);class_tab=ttk.Frame(self.tabs,padding=10);help_tab=ttk.Frame(self.tabs,padding=10)
        fast_tab=ttk.Frame(self.tabs,padding=18);inbox_tab=ttk.Frame(self.tabs,padding=12);engine_tab=ttk.Frame(self.tabs,padding=14)
        for tab,title in [(draft_tab,'本地草稿与时间线'),(engine_tab,'后台导出（验证版）'),(fast_tab,'快速交付：两母版→四版'),(inbox_tab,'公司收件箱'),(task_tab,'导出任务 / 审核归档'),(class_tab,'片段分类'),(help_tab,'使用说明 / 日志')]:self.tabs.add(tab,text=title)
        self.task_tab,self.class_tab,self.help_tab=task_tab,class_tab,help_tab
        self._fast_layout(fast_tab)
        self._inbox_layout(inbox_tab)
        self._engine_layout(self.scroll_panel(engine_tab))
        bar=ttk.Frame(draft_tab);bar.pack(fill='x',pady=(0,8))
        self.button(bar,'扫描草稿',self.scan).pack(side='left')
        self.button(bar,'全选筛选结果',self.select_all).pack(side='left',padx=6)
        self.button(bar,'取消全部',self.select_none).pack(side='left')
        ttk.Label(bar,text='搜索').pack(side='left',padx=(18,5))
        ttk.Entry(bar,textvariable=self.filter,width=25).pack(side='left')
        self.filter.trace_add('write',lambda *args:self.show_drafts())
        self.selection_label=ttk.Label(bar,text='');self.selection_label.pack(side='right')
        self.draft_tree=self.tree(draft_tab,('check','name','timeline','format','duration'),('选择','草稿','集 / 时间线','读取方式','时长'),(55,320,150,150,90))
        self.draft_tree.bind('<Button-1>',self.toggle_click)
        self.draft_tree.bind('<space>',self.toggle_selected)
        options=ttk.Frame(draft_tab);options.pack(fill='x',pady=10)
        self.options={}
        for key,value in VARIANTS.items():
            self.options[key]=tk.BooleanVar(value=True)
            ttk.Checkbutton(options,text=key+' '+value['label'],variable=self.options[key]).pack(side='left',padx=(0,12))
        self.want_srt=tk.BooleanVar(value=True);ttk.Checkbutton(options,text='SRT字幕',variable=self.want_srt).pack(side='left')
        action=ttk.Frame(draft_tab);action.pack(fill='x')
        self.button(action,'创建导出任务（先备份）',self.create).pack(side='left')
        self.button(action,'打开已有批次',self.open_session).pack(side='left',padx=8)
        ttk.Label(action,text='勾选框可多选；一个草稿中的每集时间线独立管理',foreground='#61738a').pack(side='right')
        taskbar=ttk.Frame(task_tab);taskbar.pack(fill='x',pady=(0,8))
        for text,func in [('生成AI原生工作单',self.native_work_order),('旧版读取（备用）',self.analyze),('生成草稿副本 / SRT',self.prepare),('注册已生成副本',self.register),('打开任务目录',self.open_job_folder)]:self.button(taskbar,text,func).pack(side='left',padx=(0,6))
        self.task_tree=self.tree(task_tab,('draft','timeline','variant','label','state'),('草稿','集','版本','内容','状态'),(265,85,65,230,230))
        foot=ttk.Frame(task_tab);foot.pack(fill='x',pady=(10,6))
        self.reviewed=tk.BooleanVar()
        self.task_tree.bind('<<TreeviewSelect>>', lambda event:self.reviewed.set(False))
        ttk.Checkbutton(foot,text='已人工核对：集数、背景音乐、字幕、人声、环境音、音效均符合该版本',variable=self.reviewed).pack(anchor='w')
        archivebar=ttk.Frame(task_tab);archivebar.pack(fill='x')
        for text,func in [('选择MP4，核验并归档',self.archive),('归档剪映导出的SRT',self.import_srt),('标记声画审核通过',self.approve),('复制建议成片文件名',self.copy_name)]:self.button(archivebar,text,func).pack(side='left',padx=(0,6))
        compose=self.button(task_tab,'从已登记V2/V3补齐V1/V4（无需再渲染）',self.fast_complete);compose.pack(anchor='w',pady=(6,0))
        engine_actions=ttk.Frame(task_tab);engine_actions.pack(fill='x',pady=(5,0))
        self.button(engine_actions,'用后台引擎分析所选集',lambda:self.analyze(True)).pack(side='left')
        self.button(engine_actions,'后台导出所选集的四版 + SRT',self.engine_export).pack(side='left',padx=7)
        ttk.Label(task_tab,text='机器核验规格和完整解码；内容审核由人确认。完整解码可能耗时数分钟。',foreground='#61738a').pack(anchor='w',pady=(8,0))
        self.class_label=ttk.Label(class_tab,text='先在任务队列选择某一集，再点击“分析选中集”。');self.class_label.pack(anchor='w',pady=(0,8))
        self.class_tree=self.tree(class_tab,('track','name','start','suggest','role'),('轨道','素材 / 文字','起点(s)','建议（待确认）','确认分类'),(140,420,75,120,130))
        classbar=ttk.Frame(class_tab);classbar.pack(fill='x',pady=8)
        self.role=tk.StringVar(value='背景音乐')
        ttk.Combobox(classbar,textvariable=self.role,values=ROLES[1:],state='readonly',width=15).pack(side='left')
        self.button(classbar,'应用到选中片段',self.set_role).pack(side='left',padx=6)
        self.button(classbar,'逐项确认全部建议',self.accept_suggestions).pack(side='left')
        self.button(classbar,'其余待确认音频保留',self.keep_pending_audio).pack(side='left',padx=6)
        self.button(classbar,'保存分类',self.save_roles).pack(side='right')
        self.button(classbar,'重新定位本集素材',self.locate_media).pack(side='right',padx=7)
        ttk.Label(class_tab,text='先标全长BGM，再保留其余声音。复合容器保留，配乐和字幕在内部分类；同一复合的全部引用共享内部分类。',foreground='#61738a').pack(anchor='w')
        self.help_text=tk.Text(help_tab,wrap='word',font=('Microsoft YaHei UI',10),height=14,relief='flat',padx=8,pady=8)
        self.help_text.insert('1.0',HELP);self.help_text.configure(state='disabled');self.help_text.pack(fill='both',expand=True)
        ttk.Label(help_tab,text='运行日志').pack(anchor='w',pady=(10,3))
        self.log_text=tk.Text(help_tab,wrap='word',font=('Consolas',9),height=6,relief='flat');self.log_text.pack(fill='x')
        from .quickstart import install
        install(self,frame,paths,draft_tab,engine_tab,fast_tab,inbox_tab,options,taskbar,archivebar,engine_actions,compose)
        bottom=ttk.Frame(frame);bottom.pack(fill='x',pady=(10,0))
        ttk.Label(bottom,textvariable=self.status).pack(side='left')
        ttk.Button(bottom,text='停止后台 / 取消备份',command=self.cancel.set).pack(side='right')

    def scroll_panel(self,parent):
        canvas=tk.Canvas(parent,highlightthickness=0,background=self.root.cget('background'))
        bar=ttk.Scrollbar(parent,orient='vertical',command=canvas.yview)
        bar.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
        canvas.configure(yscrollcommand=bar.set)
        body=ttk.Frame(canvas);item=canvas.create_window((0,0),window=body,anchor='nw')
        body.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
        def resize(event):
            canvas.itemconfigure(item,width=event.width)
            for child in body.winfo_children():
                if isinstance(child,ttk.Label) and int(child.cget('wraplength') or 0)>0:
                    child.configure(wraplength=max(420,event.width-20))
        canvas.bind('<Configure>',resize)
        def wheel(event):
            if canvas.winfo_ismapped() and str(event.widget).startswith(str(parent)) and event.widget.winfo_class() not in ('TCombobox','TSpinbox','Treeview','Listbox'):
                canvas.yview_scroll(-1 if event.delta>0 else 1,'units')
        canvas.bind_all('<MouseWheel>',wheel,add='+')
        return body

    def _engine_layout(self,parent):
        ttk.Label(parent,text='备份与分类后，后台生成四版和SRT',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w')
        ttk.Label(parent,text='无需控制剪映界面。先在本地草稿页勾选集并创建备份，再分析片段、确认所有长BGM和字幕，最后开始后台导出。\n旧草稿支持按片段分类；混排音效和短音乐可选“保留原声音”，不能整轨移除。',wraplength=1040,justify='left').pack(anchor='w',pady=(8,12))
        fields=ttk.Frame(parent);fields.pack(fill='x')
        ttk.Label(fields,text='独立引擎目录',width=17).grid(row=0,column=0,sticky='w',pady=5)
        ttk.Entry(fields,textvariable=self.engine_runtime).grid(row=0,column=1,sticky='ew',padx=8)
        self.button(fields,'浏览并绑定',self.choose_engine_runtime).grid(row=0,column=2)
        ttk.Label(fields,text='同时渲染母版数',width=17).grid(row=1,column=0,sticky='w',pady=5)
        ttk.Combobox(fields,textvariable=self.engine_parallel,values=(1,2),state='readonly',width=8).grid(row=1,column=1,sticky='w',padx=8)
        ttk.Checkbutton(fields,text='硬件编码（取消后使用实验性软件编码，当前码率较低，需复核画质）',variable=self.engine_hardware).grid(row=2,column=1,columnspan=2,sticky='w',padx=8,pady=4)
        ttk.Checkbutton(fields,text='复合片段与处理声音（实验，首次需剪映原生对照）',variable=self.engine_advanced).grid(row=3,column=1,columnspan=2,sticky='w',padx=8,pady=4)
        ttk.Label(fields,text='引擎存放位置',width=17).grid(row=4,column=0,sticky='w',pady=5)
        ttk.Entry(fields,textvariable=self.engine_storage).grid(row=4,column=1,sticky='ew',padx=8)
        self.button(fields,'选择存放位置',self.choose_engine_storage).grid(row=4,column=2)
        fields.columnconfigure(1,weight=1)
        actions=ttk.Frame(parent);actions.pack(fill='x',pady=(12,8))
        for index,(text,command) in enumerate([
                ('检查引擎',self.engine_doctor),('首次准备独立引擎',self.engine_setup),('离线安装包准备',self.engine_setup_offline),
                ('分析任务队列所选集',lambda:self.analyze(True)),('导出任务队列所选集',self.engine_export),
                ('导出本批次已分类的集',lambda:self.engine_export(True))]):
            self.button(actions,text,command).grid(row=index//3,column=index%3,sticky='w',padx=(0,7),pady=4)
        ttk.Checkbutton(parent,text='本批次已按公司规范命名：长BGM独立轨，短音乐归音效，台词字幕与标题分开',variable=self.engine_standard_confirm).pack(anchor='w',pady=(7,4))
        self.button(parent,'按公司规范分类本批次（旧稿不符合时停止该集）',self.engine_classify_standard).pack(anchor='w',pady=(0,5))
        self.button(parent,'本机兼容自测（生成测试素材，不读取草稿）',self.engine_self_test).pack(anchor='w',pady=(0,5))
        self.engine_state=tk.StringVar(value='引擎尚未检查。当前为内部验证功能，成片仍须声画审核。')
        ttk.Label(parent,textvariable=self.engine_state,wraplength=1020,foreground='#466080').pack(anchor='w',pady=(6,12))
        ttk.Label(parent,text='运行时：固定11.6.0.14552独立引擎。真实成片已实测草稿结构164及191，未逐版验证137–189全部结构。\n191须通过有效字段保留检查，版本号保持191。实验模式保留已验证的最终处理声音及音频复合；抠像和其他未验证处理走原生流程。\n不会覆盖现有剪映安装。首次部署见组件中的Setup-HeadlessRuntime.ps1；结果仍需声画审核。\n\n采用两次引擎渲染与媒体流复用，统一1080×1920、MP4、60fps。已完成且哈希一致的母版可恢复复用。\n当前后台仅支持四版与SRT全部勾选。字幕富文本保持原样，缺失字体的替代效果须首次验收。\n公司命名示例：BGM 1、人声 1、环境音 1、音效 1、短音乐 1、标题。未命名音轨或BGM轨混入音效会停止自动分类。',wraplength=1020,justify='left',foreground='#61738a').pack(anchor='w')

    def choose_engine_runtime(self):
        path=filedialog.askdirectory(title='选择独立11.6运行时目录（含videoeditor.dll）')
        if path:
            self.engine_runtime.set(path)
            self.save_preferences()

    def choose_engine_storage(self):
        path=filedialog.askdirectory(title='选择有空间的数据盘文件夹（引擎约3GB，建议预留6GB）')
        if path:
            self.engine_storage.set(str(Path(path)/'JianyingExportAssistantData/HeadlessRuntime'))
            self.save_preferences()

    def engine_adapter(self):
        if not self.engine_runtime.get().strip():
            raise ValueError('请先准备并绑定独立后台引擎目录')
        adapter_type=EngineAdapter
        if self.engine_advanced.get():
            from .advanced import AdvancedAdapter
            adapter_type=AdvancedAdapter
        return adapter_type(self.engine_component/'bin',self.engine_runtime.get(),self.cache.get())

    def engine_doctor(self):
        try:adapter=self.engine_adapter()
        except ValueError as exc:
            messagebox.showinfo('绑定后台引擎',str(exc));return
        self.dispatch(adapter.verify,lambda result:self.engine_state.set('已通过精确版本和文件校验：'+result['engine_version']+'。可分析已备份任务。'))

    def engine_self_test(self):
        try:adapter=self.engine_adapter()
        except ValueError as exc:
            messagebox.showinfo('先准备引擎',str(exc));return
        from .portable_test import run_test
        app_dir=resolve_app_dir(self.app_dir.get(),sys_base())
        output=Path(self.output.get())/'兼容自测结果'
        def done(result):
            self.engine_state.set(('自动自测通过，仍须真实草稿验收。' if result['success'] else '自动自测未通过：'+result.get('error','查看报告'))+' 结果目录：'+result['directory'])
            messagebox.showinfo('兼容自测结果',self.engine_state.get())
        self.dispatch(lambda:run_test(adapter,app_dir,output,self.cancel,self.log),done)

    def engine_setup_offline(self):
        installer=filedialog.askopenfilename(title='选择官方11.6.0.14552安装包（只解压，不运行安装程序）',filetypes=[('安装包','*.exe')])
        if installer:self.engine_setup(installer)

    def engine_setup(self,installer=None):
        from .deployment import launch_setup
        try:launch_setup(self,installer)
        except (OSError,ValueError) as exc:messagebox.showerror('引擎准备',str(exc))

    def engine_classify_standard(self):
        if not self.session:
            messagebox.showinfo('先备份','请先创建或打开备份批次。');return
        if not self.engine_standard_confirm.get():
            messagebox.showinfo('确认分类规范','请先确认本批次按公司轨道规范制作；旧稿可使用逐片段分类。');return
        try:adapter=self.engine_adapter()
        except ValueError as exc:
            messagebox.showinfo('后台引擎',str(exc));return
        def work():
            results=[]
            for job in self.session['jobs']:
                if self.cancel.is_set():break
                try:
                    results.append({**classify_standard_job(self.session_folder,self.session,job,adapter,True),'ok':True})
                except Exception as exc:
                    self.log(f'{job["draft"]["name"]}/{job["draft"]["timeline_name"]}：{exc}')
                    results.append({'job':job['id'],'ok':False,'error':str(exc)})
            return results
        def done(results):
            self.show_tasks()
            count=sum(row['ok'] for row in results)
            self.engine_state.set(f'{count}集已按公司规范分类；{len(results)-count}集需逐片段确认。详细原因见运行日志。')
        self.dispatch(work,done)

    def engine_export(self,all_jobs=False):
        if not self.session:
            messagebox.showinfo('先备份','请先勾选草稿并创建备份任务。');return
        if all_jobs:
            jobs=list(self.session['jobs'])
        else:
            chosen={self.task_rows[iid][0]['id'] for iid in self.task_tree.selection() if iid in self.task_rows}
            jobs=[job for job in self.session['jobs'] if job['id'] in chosen]
        if not jobs:
            messagebox.showinfo('选择集','请在任务队列选中需要后台导出的集。');return
        try:
            adapter=self.engine_adapter();parallel=int(self.engine_parallel.get());hardware=self.engine_hardware.get()
        except ValueError as exc:
            messagebox.showinfo('后台引擎',str(exc));return
        app_dir=resolve_app_dir(self.app_dir.get(),sys_base())
        self.save_preferences()
        def work():
            results=[]
            for job in jobs:
                if self.cancel.is_set():break
                self.log(f'后台处理：{job["draft"]["name"]} / {job["draft"]["timeline_name"]}')
                try:
                    result=render_job(self.session_folder,self.session,job,adapter,app_dir,parallel,self.cancel,self.log,hard_encode=hardware)
                    results.append({'job':job['id'],'ok':True,'result':result})
                except Exception as exc:
                    job['status']='后台待处理：'+str(exc)
                    event(self.session,'后台导出未完成',job=job['id'],error=str(exc))
                    save_session(self.session_folder,self.session)
                    self.log(str(exc));results.append({'job':job['id'],'ok':False,'error':str(exc)})
            return results
        def done(results):
            self.show_tasks()
            count=sum(row['ok'] for row in results)
            text=f'完成{count}集，需处理{len(results)-count}集；剩余{len(jobs)-len(results)}集未执行。成片仍待声画审核。'
            self.engine_state.set(text)
            self.status.set(text)
            if any(not row['ok'] for row in results):
                self.log('\n'.join(row['error'] for row in results if not row['ok']))
        self.dispatch(work,done)

    def _fast_layout(self, parent):
        ttk.Label(parent,text='剪映只导出两次，程序补齐四版',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w')
        ttk.Label(parent,text='先在备份后的草稿副本导出 V3（有BGM、无字幕），再导出 V2（无BGM、有字幕）及SRT。\n两次都选1080P / MP4 / 60fps / 9:16；期间只切换配乐和字幕，保持全部剪辑时序不变。',wraplength=960,justify='left').pack(anchor='w',pady=(10,14))
        fields=ttk.Frame(parent);fields.pack(fill='x')
        self.fast_fields={key:tk.StringVar() for key in ('identity','V2','V3','SRT')}
        self.fast_confirm=tk.BooleanVar(value=False)
        for index,(key,label) in enumerate([('identity','剧名 / 集号'),('V2','V2 无BGM有字幕'),('V3','V3 有BGM无字幕'),('SRT','原生SRT')]):
            ttk.Label(fields,text=label,width=21).grid(row=index,column=0,sticky='w',pady=7)
            ttk.Entry(fields,textvariable=self.fast_fields[key]).grid(row=index,column=1,sticky='ew',padx=8,pady=7)
            if key!='identity':
                self.button(fields,'选择文件',lambda k=key:self.fast_choose(k)).grid(row=index,column=2,pady=7)
                self.fast_fields[key].trace_add('write',lambda *args:self.fast_confirm.set(False))
        fields.columnconfigure(1,weight=1)
        ttk.Checkbutton(parent,text='已确认本组（或CSV中每组）两母版来自同集，仅切换配乐/字幕，画面与声音时序完全相同',variable=self.fast_confirm).pack(anchor='w',pady=(14,10))
        actions=ttk.Frame(parent);actions.pack(fill='x')
        self.button(actions,'生成四版 + SRT',self.fast_start).pack(side='left')
        self.button(actions,'从CSV批量生成',self.fast_batch).pack(side='left',padx=8)
        self.button(actions,'保存CSV模板',self.fast_template).pack(side='left')
        ttk.Label(parent,text='程序逐包检查时间戳和媒体指纹，直接复用原生画面/声音，保留字幕样式和混音；随后完整解码并生成审核CSV。\n输出使用上方“输出目录”，每集新建独立目录，不覆盖母版。此阶段无需AI、联网或控制剪映。\n两母版若不同步会停止并提示重新导出；最终仍需人工声画审核。',wraplength=950,justify='left',foreground='#466080').pack(anchor='w',pady=(18,0))

    def _inbox_layout(self,parent):
        ttk.Label(parent,text='提交单集目录，后台自动完成交付',font=('Microsoft YaHei UI',16,'bold')).pack(anchor='w')
        ttk.Label(parent,text='每集提供V2.mp4、V3.mp4、字幕.srt；也识别唯一的V2/V2.mp4、V2/V2.srt等同名原生子目录。\n选择目录并确认来源后提交，无需制作CSV或编辑JSON。收件箱内直接提交；其他位置会核验复制，原文件保留。',justify='left',wraplength=1020).pack(anchor='w',pady=(6,9))
        fields=ttk.Frame(parent);fields.pack(fill='x')
        ttk.Label(fields,text='收件箱根目录',width=18).grid(row=0,column=0,sticky='w',pady=4)
        self.inbox_root_entry=ttk.Entry(fields,textvariable=self.inbox_root)
        self.inbox_root_entry.grid(row=0,column=1,sticky='ew',padx=7)
        self.inbox_browse_button=self.button(fields,'浏览',self.choose_inbox_root)
        self.inbox_browse_button.grid(row=0,column=2)
        ttk.Label(fields,text='交付输出（上方设置）',width=18).grid(row=1,column=0,sticky='w',pady=4)
        ttk.Entry(fields,textvariable=self.output,state='readonly').grid(row=1,column=1,sticky='ew',padx=7)
        ttk.Label(fields,text='完成项审计间隔(秒)',width=18).grid(row=2,column=0,sticky='w',pady=4)
        audit=ttk.Frame(fields);audit.grid(row=2,column=1,columnspan=2,sticky='ew',padx=7)
        self.inbox_audit_entry=ttk.Spinbox(audit,from_=0,to=86400,increment=300,width=10,textvariable=self.inbox_audit_seconds)
        self.inbox_audit_entry.pack(side='left')
        ttk.Label(audit,text='  0=每轮全验；窗口内仅查元数据，隐藏内容变化到期复核').pack(side='left')
        self.inbox_folder=tk.StringVar();self.inbox_identity=tk.StringVar();self.inbox_confirm=tk.BooleanVar(value=False)
        ttk.Label(fields,text='本集已导出目录',width=18).grid(row=3,column=0,sticky='w',pady=4)
        ttk.Entry(fields,textvariable=self.inbox_folder).grid(row=3,column=1,sticky='ew',padx=7)
        self.button(fields,'选择本集',self.choose_inbox_folder).grid(row=3,column=2)
        ttk.Label(fields,text='剧名 / 集号',width=18).grid(row=4,column=0,sticky='w',pady=4)
        ttk.Entry(fields,textvariable=self.inbox_identity).grid(row=4,column=1,sticky='ew',padx=7)
        fields.columnconfigure(1,weight=1)
        for variable in (self.inbox_folder,self.inbox_identity,self.inbox_root):
            variable.trace_add('write',lambda *args:self.inbox_confirm.set(False))
        ttk.Checkbutton(parent,text='已确认本集V2/V3来自同一时间线，仅切换配乐/字幕；此确认不代表成片已人工审核',variable=self.inbox_confirm).pack(anchor='w',pady=(8,5))
        actions=ttk.Frame(parent);actions.pack(fill='x')
        self.button(actions,'提交本集',self.submit_inbox_folder).pack(side='left')
        self.inbox_start_button=ttk.Button(actions,text='开始自动处理',command=lambda:self.start_inbox(True))
        self.inbox_start_button.pack(side='left',padx=7)
        self.inbox_once_button=ttk.Button(actions,text='强制全量复核一轮',command=lambda:self.start_inbox(False,True))
        self.inbox_once_button.pack(side='left')
        self.inbox_stop_button=ttk.Button(actions,text='停止（完成当前集）',command=self.stop_inbox,state='disabled')
        self.inbox_stop_button.pack(side='left',padx=7)
        self.button(actions,'打开选中回执',self.open_inbox_receipt).pack(side='right')
        self.inbox_state=tk.StringVar(value='自动处理未启动；每次重开程序均保持关闭。')
        self.inbox_counts=tk.StringVar(value='已见记录：完成0 / 待处理0 / 失败0')
        ttk.Label(parent,textvariable=self.inbox_state,wraplength=1050,foreground='#466080').pack(anchor='w',pady=(8,2))
        ttk.Label(parent,textvariable=self.inbox_counts).pack(anchor='w',pady=(0,5))
        self.inbox_tree=self.tree(parent,('identity','state','detail','receipt'),
            ('项目/任务','状态','说明','回执 / 清单路径'),(210,135,310,350))

    def _inbox_running(self):
        return self.inbox_thread is not None and self.inbox_thread.is_alive()

    def _refresh_inbox_controls(self):
        running=self._inbox_running()
        self.inbox_start_button.configure(state='disabled' if running or self.busy else 'normal')
        self.inbox_once_button.configure(state='disabled' if running or self.busy else 'normal')
        self.inbox_stop_button.configure(state='normal' if running and not self.inbox_stop.is_set() else 'disabled')
        self.inbox_root_entry.configure(state='disabled' if running or self.busy else 'normal')
        self.inbox_audit_entry.configure(state='disabled' if running or self.busy else 'normal')
        self.inbox_browse_button.configure(state='disabled' if running or self.busy else 'normal')

    def choose_inbox_root(self):
        if self._inbox_running():return
        path=filedialog.askdirectory(title='选择独立于交付输出的收件箱根目录')
        if path:self.inbox_root.set(path);self.save_preferences()

    def choose_inbox_folder(self):
        path=filedialog.askdirectory(title='选择本集已经完成的原生导出目录')
        if path:
            self.inbox_folder.set(path)
            self.inbox_identity.set(Path(path).name)

    def submit_inbox_folder(self):
        from .submission import submit_folder
        if not self.inbox_confirm.get():
            messagebox.showinfo('确认来源','请明确勾选：本集两母版来自同一时间线，期间仅切换配乐和字幕。');return
        folder=self.inbox_folder.get().strip();inbox=self.inbox_root.get().strip()
        identity=self.inbox_identity.get().strip();output=self.output.get().strip()
        if not all((folder,inbox,identity,output)):
            messagebox.showinfo('补齐提交信息','请选择收件箱、单集目录、剧名/集号和交付输出。');return
        def done(result):
            self.inbox_confirm.set(False)
            self.status.set('本集已提交，处理状态以收件箱记录为准：'+result['folder'])
            self.log('提交清单：'+result['manifest'])
            self.update_inbox_row({'id':result['id'],'task':Path(result['folder']).name,'identity':result['identity'],
                'state':'waiting','receipt':result['manifest'],
                'error':'已提交过，未重复创建任务' if result['already_submitted'] else '等待稳定扫描；来源文件保留'})
        self.dispatch(lambda:submit_folder(folder,inbox,identity,confirmed_pair=True,output_root=output,log=self.log),done)

    def start_inbox(self,watch=True,force_audit=False):
        if self._inbox_running() or self.busy:return
        from .inbox import InboxWorker
        from .submission import prepare_roots,process_once
        inbox=self.inbox_root.get().strip();output=self.output.get().strip()
        if not inbox or not output:
            messagebox.showinfo('配置收件箱','请设置收件箱和交付输出目录。');return
        app_dir=resolve_app_dir(self.app_dir.get(),sys_base())
        if not app_dir or not all((Path(app_dir)/name).is_file() for name in ('ffmpeg.exe','ffprobe.exe')):
            messagebox.showinfo('选择剪映版本','请先选择能提供FFmpeg和FFprobe的剪映版本目录。');return
        try:
            audit_seconds=float(self.inbox_audit_seconds.get())
            if not math.isfinite(audit_seconds) or audit_seconds<0:raise ValueError('审计间隔需为有限非负秒数，0表示每轮严格复核')
            inbox,output=prepare_roots(inbox,output,create=True)
            worker=InboxWorker(inbox,output,app_dir,log=self.log,
                on_result=lambda row:self.messages.put(('inbox_row',row)),completed_audit_interval=audit_seconds)
        except Exception as exc:
            messagebox.showerror('收件箱配置无效',str(exc));return
        self.save_preferences();self.inbox_stop.clear()
        mode='每轮严格哈希复核' if force_audit or audit_seconds==0 else f'完成项{audit_seconds:g}秒审计窗口，期间仅查元数据'
        self.inbox_run_description=('自动处理' if watch else '强制单轮处理')+'运行中（'+mode+'）：'+str(inbox)+' → '+str(output)
        self.inbox_state.set(self.inbox_run_description)
        def observed(result):self.messages.put(('inbox_scan',result))
        def work():
            try:
                if watch:worker.run(self.inbox_stop,poll_seconds=2,on_scan=observed,force_audit=force_audit)
                else:process_once(worker,stop_event=self.inbox_stop,on_scan=observed,force_audit=force_audit)
            except Exception as exc:self.messages.put(('inbox_error',str(exc)))
            finally:self.messages.put(('inbox_stopped',None))
        self.inbox_thread=threading.Thread(target=work,daemon=False)
        self.inbox_thread.start();self._refresh_inbox_controls()

    def stop_inbox(self):
        self.inbox_stop.set()
        self.inbox_state.set('正在停止：当前集处理完成后退出，不强杀正在解码的进程。')
        self._refresh_inbox_controls()

    def update_inbox_row(self,row):
        from .submission import DONE,PENDING
        key='task:'+row['task'] if row.get('task') else row.get('id','unknown')
        old=self.inbox_rows.get(key,{})
        if (old.get('id') and old.get('id')==row.get('id') and old.get('state') not in DONE|PENDING
                and row.get('state') in ('waiting','settling')):
            row={**row,'state':old['state'],'error':old.get('error',''),
                 'receipt':old.get('receipt') or row.get('receipt')}
        self.inbox_rows[key]={**old,**row}
        if 'error' not in row:self.inbox_rows[key]['error']=''
        self.inbox_rows[key]['hash_verified_this_scan']=row.get('hash_verified_this_scan',False)
        self.inbox_rows[key]['metadata_checked_this_scan']=row.get('metadata_checked_this_scan',False)
        self.inbox_rows[key]['present_this_scan']=row.get('present_this_scan',True)
        value=self.inbox_rows[key]
        names={'waiting':'待处理','settling':'等待文件稳定','waiting_files':'等待文件','processing':'处理中',
            'completed':'完成·待声画审核','already_completed':'完成·本轮全验','recovered':'完成·恢复登记',
            'completed_cached':'完成·缓存(本轮未全验)',
            'failed':'失败','interrupted':'中断·需处理','source_changed':'来源变化·需处理',
            'output_changed':'结果变化·需处理','invalid_task':'清单无效','duplicate_id':'ID冲突','state_corrupt':'回执损坏'}
        state=value.get('state','waiting')
        detail=value.get('error','');label=names.get(state,state)
        if state=='completed_cached':detail='最近全验 '+str(value.get('last_verified'))+'；到期 '+str(value.get('audit_due'))+'；本轮仅元数据'
        elif value.get('hash_verified_this_scan'):detail='本轮完整哈希复核通过：'+str(value.get('last_verified'))
        if state in DONE and not value.get('present_this_scan',True):
            label='完成·历史记录';detail='本轮未观察到任务；最近全验 '+str(value.get('last_verified'))
        display=(value.get('identity') or value.get('task') or key,label,detail,
                 value.get('receipt') or value.get('diagnostic') or value.get('directory',''))
        if key in self.inbox_tree_rows:self.inbox_tree.item(self.inbox_tree_rows[key],values=display)
        else:self.inbox_tree_rows[key]=self.inbox_tree.insert('','end',values=display)
        counts={'done':0,'pending':0,'failed':0,'cached':0}
        for item in self.inbox_rows.values():
            category='done' if item.get('state') in DONE else ('pending' if item.get('state') in PENDING else 'failed')
            counts[category]+=1
            if item.get('state')=='completed_cached' and item.get('present_this_scan',True):counts['cached']+=1
        self.inbox_counts.set(f'已见记录：完成{counts["done"]}（缓存{counts["cached"]}） / 待处理{counts["pending"]} / 失败{counts["failed"]}')

    def open_inbox_receipt(self):
        selected=self.inbox_tree.selection()
        if not selected:return
        path=self.inbox_tree.item(selected[0],'values')[3]
        if path and Path(path).exists():os.startfile(path)
        else:messagebox.showinfo('回执尚未生成','等待任务扫描后生成；当前尚无可打开文件。')

    def fast_choose(self, key):
        types=[('SRT字幕','*.srt')] if key=='SRT' else [('MP4视频','*.mp4')]
        path=filedialog.askopenfilename(title='选择 '+key+' 原生母版',filetypes=types)
        if path:self.fast_fields[key].set(path)

    def fast_start(self):
        from .fast import compose_bundle
        if not self.fast_confirm.get():
            messagebox.showinfo('确认母版对应关系','请确认两母版来自同集，且只切换配乐和字幕。此确认不等于成片声画审核通过。');return
        values={key:value.get().strip() for key,value in self.fast_fields.items()}
        if not all(values.values()) or not self.output.get():
            messagebox.showinfo('补齐文件','请填写剧名/集号，选择V2、V3和SRT，并设置输出目录。');return
        app_dir=resolve_app_dir(self.app_dir.get(),sys_base())
        output=self.output.get()
        def done(result):
            self.status.set('四版视频与SRT完成，待声画审核：'+result['directory'])
            self.log('交付目录：'+result['directory'])
            messagebox.showinfo('快速交付完成','四版视频和SRT已核验，待声画审核。\n\n'+result['directory'])
        self.dispatch(lambda:compose_bundle(values['V2'],values['V3'],values['SRT'],output,app_dir,values['identity'],True,self.log),done)

    def fast_batch(self):
        from .fast import compose_csv
        if not self.fast_confirm.get():
            messagebox.showinfo('确认清单','请先确认CSV内各组母版来自同集且仅切换配乐和字幕。');return
        path=filedialog.askopenfilename(title='选择五列交付CSV',filetypes=[('CSV清单','*.csv')])
        if not path:return
        app_dir=resolve_app_dir(self.app_dir.get(),sys_base());output=self.output.get()
        self.dispatch(lambda:compose_csv(path,output,app_dir,True,self.log),
            lambda result:messagebox.showinfo('批量处理结束',f'完成 {result["completed"]} 集，失败 {result["failed"]} 集。\n所有成片仍待声画审核。\n\n{result["report"]}'))

    def fast_template(self):
        path=filedialog.asksaveasfilename(title='保存CSV模板',defaultextension='.csv',filetypes=[('CSV清单','*.csv')])
        if path:
            Path(path).write_text('剧名,集号,V2,V3,SRT\n',encoding='utf-8-sig')
            self.status.set('CSV模板已保存；一行一集，文件路径可相对CSV所在目录填写')

    def fast_complete(self):
        job,key=self.selected_task()
        if not job:return
        from .workflow import complete_fast
        app_dir=resolve_app_dir(self.app_dir.get(),sys_base())
        self.dispatch(lambda:complete_fast(self.session_folder,self.session,job,app_dir,self.log),lambda result:self.show_tasks())

    def tree(self,parent,columns,headings,widths):
        frame=ttk.Frame(parent);frame.pack(fill='both',expand=True)
        tree=ttk.Treeview(frame,columns=columns,show='headings',selectmode='extended',height=4)
        for column,heading,width in zip(columns,headings,widths):
            tree.heading(column,text=heading)
            tree.column(column,width=width,minwidth=40,stretch=True)
        scroll=ttk.Scrollbar(frame,orient='vertical',command=tree.yview);tree.configure(yscrollcommand=scroll.set)
        tree.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        return tree

    def log(self,message):self.messages.put(('log',str(message)))

    def dispatch(self,func,callback=None):
        if self.busy:return
        self.busy=True;self.cancel.clear()
        for button in self.buttons:button.configure(state='disabled')
        self.status.set('正在处理，请稍候…')
        def work():
            try:self.messages.put(('done',func(),callback))
            except Exception as exc:self.messages.put(('error',str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def pump(self):
        try:
            while True:
                message=self.messages.get_nowait()
                if message[0]=='log':
                    self.log_text.insert('end',f'{datetime.now():%H:%M:%S}  {message[1]}\n');self.log_text.see('end')
                elif message[0]=='engine_progress':self.engine_state.set(message[1])
                elif message[0]=='inbox_row':self.update_inbox_row(message[1])
                elif message[0]=='inbox_scan':
                    present={'task:'+row['task'] if row.get('task') else row.get('id','unknown') for row in message[1].get('results',[])}
                    for key,old in list(self.inbox_rows.items()):
                        if key not in present:self.update_inbox_row({**old,'present_this_scan':False,'hash_verified_this_scan':False,'metadata_checked_this_scan':False})
                    for row in message[1].get('results',[]):self.update_inbox_row(row)
                    if not self.inbox_stop.is_set() and self._inbox_running():
                        self.inbox_state.set('另一处理进程持有当前收件箱，本窗口等待。' if message[1].get('busy') else self.inbox_run_description)
                elif message[0]=='inbox_error':
                    self.inbox_state.set('自动处理已停止：'+message[1]);self.log(message[1])
                elif message[0]=='inbox_stopped':
                    if not self.inbox_state.get().startswith('自动处理已停止：'):
                        self.inbox_state.set('处理线程已结束；再次启动需点击开始，重启不会自动监听。')
                else:
                    self.busy=False
                    for button in self.buttons:button.configure(state='normal')
                    if message[0]=='error':
                        self.status.set(message[1]);self.log(message[1])
                        if not self._close_requested:messagebox.showerror('操作未完成',message[1],parent=self.root)
                        if self.session:self.show_tasks()
                    else:
                        self.status.set('操作完成')
                        if message[2] and not self._close_requested:
                            try:message[2](message[1])
                            except (OSError,ValueError) as exc:
                                self.status.set('操作未完成，已有任务已保留：'+str(exc));self.log(str(exc))
        except queue.Empty:pass
        if self._close_requested and not self.busy and not self._inbox_running():
            if self._finish_close() is not False:return
        if self.preference_save_error:self.status.set('设置未能保存：'+self.preference_save_error+'。任务已保留；处理后可重试保存或再次关闭窗口。')
        self._refresh_inbox_controls()
        self.root.after(100,self.pump)

    def save_preferences(self):
        self.preferences=merge_preferences(self.state_dir/'settings.json',{'settings_version':2,'app_dir':self.app_dir.get(),'roots':self.roots.get(),'output':self.output.get(),'cache':self.cache.get(),'presets':self.presets.get(),'inbox_root':self.inbox_root.get(),'inbox_audit_seconds':self.inbox_audit_seconds.get(),'engine_runtime':self.engine_runtime.get(),'engine_storage':self.engine_storage.get(),'engine_parallel':self.engine_parallel.get(),'engine_hardware':self.engine_hardware.get()})
        self.preference_save_error=None

    def try_save_preferences(self,closing=False):
        try:self.save_preferences();return True
        except (OSError,ValueError) as exc:
            self.preference_save_error=str(exc)
            if closing:self._close_requested=False
            self.status.set('设置未能保存：'+str(exc)+('。窗口已保留，处理后可再次关闭。' if closing else '。任务已保留。'))
            self.log(self.status.get());return False

    def initial(self):
        if self._close_requested:return
        if self.busy:self.root.after(100,self.initial);return
        if self.editor_policy_checked:self.scan();return
        self.editor_policy_checked=True
        from .easy_ui import check_editor_update_policy,editor_update_summary
        def done(result):
            self.editor_policy_result=result;self.log(editor_update_summary(result))
            if not self._close_requested:self.scan()
        self.dispatch(lambda:check_editor_update_policy(self.state_dir),done)

    def choose_app(self):
        path=filedialog.askdirectory(title='选择包含 videoeditor.dll 和 ffprobe.exe 的剪映版本目录')
        if path:self.app_dir.set(resolve_app_dir(path) or path);self.save_preferences()

    def choose_cache(self):
        path=filedialog.askdirectory(title='选择剪映媒体缓存目录（不会当成草稿内容）')
        if path:self.cache.set(path);self.save_preferences()

    def choose_presets(self):
        path=filedialog.askdirectory(title='选择剪映Presets预设目录')
        if path:self.presets.set(path);self.save_preferences()

    def choose_root(self):
        path=filedialog.askdirectory(title='选择本地草稿库（可在输入框以分号分隔多个库）')
        if path:self.roots.set(path);self.save_preferences()

    def choose_output(self):
        path=filedialog.askdirectory(title='选择备份和交付输出目录')
        if path:self.output.set(path);self.save_preferences()

    def scan(self):
        roots=[p.strip() for p in self.roots.get().split(';') if p.strip()]
        if not roots:return
        self.try_save_preferences()
        def done(result):
            self.drafts,warnings=result
            self.selected.intersection_update(x.key for x in self.drafts)
            self.show_drafts()
            for warning in warnings:self.log(warning)
            count=len({x.folder for x in self.drafts})
            self.status.set(f'发现 {count} 个本地草稿 / {len(self.drafts)} 条时间线；{len(warnings)} 条提示')
        self.dispatch(lambda:scan(roots,self.index),done)

    def show_drafts(self):
        self.draft_tree.delete(*self.draft_tree.get_children())
        self.rows={}
        term=self.filter.get().strip().casefold()
        for draft in self.drafts:
            if term and term not in (draft.name+' '+draft.timeline_name).casefold():continue
            iid=self.draft_tree.insert('','end',values=('☑' if draft.key in self.selected else '☐',draft.name,draft.timeline_name,'加密·可备份' if draft.encrypted else '可读取',f'{draft.duration/1e6:.2f}' if draft.duration else '待读取核对'))
            self.rows[iid]=draft
        self.selection_label.configure(text=f'已勾选 {len(self.selected)} 集')

    def toggle_click(self,event):
        if self.busy:return
        if self.draft_tree.identify_column(event.x)=='#1':
            iid=self.draft_tree.identify_row(event.y)
            if iid in self.rows:
                key=self.rows[iid].key
                self.selected.symmetric_difference_update({key});self.show_drafts()
                return 'break'

    def toggle_selected(self,event=None):
        if self.busy:return
        for iid in self.draft_tree.selection():self.selected.symmetric_difference_update({self.rows[iid].key})
        self.show_drafts()

    def select_all(self):self.selected.update(x.key for x in self.rows.values());self.show_drafts()
    def select_none(self):self.selected.clear();self.show_drafts()

    def create(self):
        chosen=[x for x in self.drafts if x.key in self.selected]
        variants=[key for key,var in self.options.items() if var.get()]
        want_srt=self.want_srt.get();output=self.output.get()
        if not output or not chosen:
            messagebox.showinfo('选择草稿','请勾选时间线并选择输出目录。');return
        def done(result):
            self.session_folder,self.session=result;self.current_job=None
            self.engine_standard_confirm.set(False)
            self.show_tasks();self.tabs.select(self.task_tab)
            self.status.set(f'备份完成，已创建 {len(self.session["jobs"])} 集任务。可使用后台引擎分析与导出。')
            self.log(f'任务批次：{self.session_folder}')
        self.dispatch(lambda:create_session(chosen,output,variants,want_srt,self.cancel,self.log),done)

    def open_session(self):
        path=filedialog.askopenfilename(title='选择任务清单.json',filetypes=[('任务清单','*.json')])
        if not path:return
        data=load_json(path)
        if not isinstance(data,dict) or data.get('schema')!=1 or not isinstance(data.get('jobs'),list):
            messagebox.showerror('无法读取','不是受支持的任务批次');return
        try:
            validate_session(Path(path).parent,data)
        except (ValueError, KeyError, TypeError) as exc:
            messagebox.showerror('任务路径无效',str(exc));return
        self.session_folder=Path(path).parent;self.session=data;self.current_job=None
        self.engine_standard_confirm.set(False)
        self.show_tasks();self.tabs.select(self.task_tab)

    def show_tasks(self):
        self.task_tree.delete(*self.task_tree.get_children());self.task_rows={}
        if not self.session:return
        for job in self.session['jobs']:
            for key,item in job['variants'].items():
                iid=self.task_tree.insert('','end',values=(job['draft']['name'],job['draft']['timeline_name'],key,item['label'],item['status']))
                self.task_rows[iid]=(job,key)
            if job['want_srt']:
                iid=self.task_tree.insert('','end',values=(job['draft']['name'],job['draft']['timeline_name'],'SRT','字幕',(job.get('srt') or {}).get('status','待导出')))
                self.task_rows[iid]=(job,'SRT')

    def selected_task(self):
        selection=self.task_tree.selection()
        if not selection or selection[0] not in self.task_rows:
            messagebox.showinfo('选择任务','请先选择一条导出任务');return None,None
        return self.task_rows[selection[0]]

    def new_codec(self):
        resolved=resolve_app_dir(self.app_dir.get(),sys_base())
        self.app_dir.set(resolved)
        self.save_preferences()
        self.codec.close();self.codec=Codec(resolved or None)
        return self.codec

    def analyze(self,background=False):
        job,key=self.selected_task()
        if not job:return
        try:codec=self.engine_adapter() if background else self.new_codec()
        except ValueError as exc:
            messagebox.showinfo('后台引擎',str(exc));return
        if background and job.get('classification_mode')=='scoped_v1' and not getattr(codec,'advanced',False):
            from .advanced import AdvancedAdapter
            self.engine_advanced.set(True)
            codec=AdvancedAdapter(codec.tool_directory,codec.runtime_directory,codec.cache_directory)
        def done(result):
            rows,fingerprint,mode=result
            self.current_analysis_mode=mode
            self.current_job=job;self.classification=rows;self.role_map=dict(job.get('roles',{}));self.class_sha=fingerprint
            self.class_label.configure(text=f'{job["draft"]["name"]} / 第 {job["draft"]["timeline_name"]} 集 · {len(rows)} 个音频/文字片段')
            self.show_classes();self.tabs.select(self.class_tab)
        def work():
            fingerprint=sha(job['backup_content'])
            data=codec.read(job['backup_content'])
            if sha(job['backup_content'])!=fingerprint:
                raise ValueError('分析过程中备份发生变化，请重新分析')
            ensure_timeline_identity(job,data)
            return getattr(codec,'inspect_tracks',inspect)(data),fingerprint,'scoped_v1' if getattr(codec,'advanced',False) else 'flat_v1'
        self.dispatch(work,done)

    def show_classes(self):
        selected_keys=set(self.class_tree.selection())
        self.class_tree.delete(*self.class_tree.get_children())
        for i,row in enumerate(self.classification):
            iid=str(i);self.class_tree.insert('','end',iid=iid,values=(row['track'],row['name'],f'{row["start"]/1e6:.3f}',row['suggestion'],self.role_map.get(row['key'],'待确认')))
            if iid in selected_keys:self.class_tree.selection_add(iid)

    def set_role(self):
        if self.busy:return
        for iid in self.class_tree.selection():
            row=self.classification[int(iid)]
            allowed=('字幕','标题/其他文字') if row['kind']=='text' else ('人声','环境音','音效','背景音乐','保留原声音')
            if self.role.get() not in allowed:
                messagebox.showerror('类别不匹配','音频只能选择声音类别，文字只能选择字幕或标题。');return
        for iid in self.class_tree.selection():self.role_map[self.classification[int(iid)]['key']]=self.role.get()
        self.show_classes()

    def accept_suggestions(self):
        if self.busy:return
        for row in self.classification:
            if row['suggestion']!='待确认':self.role_map[row['key']]=row['suggestion']
        self.show_classes()

    def keep_pending_audio(self):
        if self.busy:return
        for row in self.classification:
            if row['kind']=='audio' and self.role_map.get(row['key'],'待确认')=='待确认':
                self.role_map[row['key']]='保留原声音'
        self.show_classes()
        self.status.set('未分类音频已标为保留；请确认所有长BGM已单独标出后保存分类')

    def save_roles(self):
        if not self.current_job:return
        try:persist_manual_roles(self.session_folder,self.session,self.current_job,dict(self.role_map),self.class_sha,getattr(self,'current_analysis_mode','flat_v1'))
        except ValueError as exc:
            messagebox.showerror('分类未保存',str(exc));return
        self.status.set('分类已保存；待确认片段会阻止自动生成副本')

    def locate_media(self):
        job=self.current_job
        if not job:
            messagebox.showinfo('先分析','请先分析需要定位素材的集。');return
        try:adapter=self.engine_adapter()
        except ValueError as exc:
            messagebox.showinfo('后台引擎',str(exc));return
        from .advanced import media_rows,save_media_override
        from .native_prepare import read_native_document
        def work():
            adapter.verify()
            data,fingerprint=read_native_document(adapter,job['backup_content'])
            return media_rows(data,job,adapter.cache_directory),fingerprint
        def done(result):
            rows,fingerprint=result
            window=tk.Toplevel(self.root);window.title('定位本集素材');window.geometry('1080x530')
            ttk.Label(window,text='选择条目后重新定位。请核对素材身份；只保存本任务的路径，不改原草稿。').pack(anchor='w',padx=10,pady=8)
            tree=self.tree(window,('scope','name','path','state'),('所属片段','素材','文件路径','状态'),(130,200,580,120))
            for i,row in enumerate(rows):
                tree.insert('','end',iid=str(i),values=(row['scope_name'],row['name'],row['resolved_path'],
                    '已定位' if row['relocated'] else '存在' if row['exists'] else '缺失'))
            def pick():
                selected=tree.selection()
                if len(selected)!=1:
                    messagebox.showinfo('选择素材','请选择一个明确的素材条目。',parent=window);return
                row=rows[int(selected[0])]
                path=filedialog.askopenfilename(title='重新定位：'+row['name'],parent=window)
                if not path:return
                def saved(_):
                    if window.winfo_exists():tree.set(selected[0],'path',path);tree.set(selected[0],'state','已定位')
                    self.status.set('素材定位已保存；导出时仍会核验文件和内容范围')
                self.dispatch(lambda:save_media_override(self.session_folder,self.session,job,row['scope'],row['material_id'],row['field'],row['original_path'],path,fingerprint),saved)
            ttk.Button(window,text='为选中条目选择正确文件',command=pick).pack(anchor='w',padx=10,pady=10)
        self.dispatch(work,done)

    def prepare(self):
        job,key=self.selected_task()
        if not job:return
        codec=self.new_codec()
        self.dispatch(lambda:prepare_job(self.session_folder,self.session,job,codec,self.log),lambda result:self.show_tasks())

    def register(self):
        if not self.session:return
        self.dispatch(lambda:register_prepared(self.session_folder,self.session),lambda count:messagebox.showinfo('副本已注册',f'已添加 {count} 个草稿副本。重新打开剪映后检查画面和音轨，再渲染成片。'))

    def native_work_order(self):
        if not self.session:
            messagebox.showinfo('创建任务','请先勾选草稿并创建备份任务，或打开已有批次。');return
        from .workflow import enable_native
        config={'app_dir':resolve_app_dir(self.app_dir.get(),sys_base()),'draft_roots':[p.strip() for p in self.roots.get().split(';') if p.strip()],'cache':self.cache.get(),'presets':self.presets.get()}
        def done(result):
            text=f'使用 Windows-MCP 和 $jianying-delivery 继续这个批次：{self.session_folder}。在剪映原生副本中导出四版视频和SRT，完成后核验归档。'
            self.root.clipboard_clear();self.root.clipboard_append(text)
            messagebox.showinfo('AI工作单已生成','已备份草稿列表并生成AI原生导出工作单。\n\n已复制skill调用语句，粘贴到支持电脑控制的Codex聊天继续。此按钮本身不会自动操作剪映。')
        self.dispatch(lambda:enable_native(self.session_folder,self.session,config),done)

    def open_job_folder(self):
        job,key=self.selected_task()
        path=Path(job['directory']) if job else self.session_folder
        if path and Path(path).is_dir():os.startfile(str(path))

    def archive(self):
        job,key=self.selected_task()
        if not job:return
        if key=='SRT':self.import_srt();return
        source=filedialog.askopenfilename(title=f'选择 {job["draft"]["name"]} / {job["draft"]["timeline_name"]} / {key} 的原生导出成片',filetypes=[('MP4视频','*.mp4')])
        if not source:return
        reviewed=self.reviewed.get();app_dir=resolve_app_dir(self.app_dir.get(),sys_base())
        self.app_dir.set(app_dir);self.save_preferences()
        self.dispatch(lambda:archive_video(self.session_folder,self.session,job,key,source,app_dir,reviewed),lambda result:self.show_tasks())

    def import_srt(self):
        job,key=self.selected_task()
        if not job:return
        source=filedialog.askopenfilename(title='选择剪映导出的SRT',filetypes=[('SRT字幕','*.srt')])
        if source:self.dispatch(lambda:archive_srt(self.session_folder,self.session,job,source),lambda result:self.show_tasks())

    def approve(self):
        job,key=self.selected_task()
        if not job:return
        if not self.reviewed.get():
            messagebox.showinfo('人工审核','请先检查内容并勾选“已人工核对”。');return
        item=job.get('srt') if key=='SRT' else job['variants'][key]
        if not item or not item.get('file'):
            messagebox.showerror('尚未归档','请先归档对应文件');return
        def work():
            if sha(item['file'])!=item['sha256']:raise ValueError('归档文件已被修改，请重新核验')
            item['status']='已审核';item['reviewed']=True
            event(self.session,'人工声画审核通过',job=job['id'],variant=key)
            save_session(self.session_folder,self.session)
        self.dispatch(work,lambda result:self.show_tasks())

    def copy_name(self):
        job,key=self.selected_task()
        if not job:return
        value='字幕.srt' if key=='SRT' else key+'_'+job['variants'][key]['label']+'.mp4'
        self.root.clipboard_clear();self.root.clipboard_append(value);self.status.set('已复制：'+value)

    def close(self):
        self._close_requested=True
        if self._inbox_running():self.stop_inbox()
        if self.busy:
            self.cancel.set();self.status.set('正在结束当前操作，请等待完成后再关闭。');return
        if self._inbox_running():
            self.status.set('等待当前集处理结束后关闭，不会强杀正在解码的进程。');return
        self._finish_close()

    def _finish_close(self):
        if not self.try_save_preferences(closing=True):return False
        self.codec.close();self.root.destroy();return True

def sys_base():
    import sys
    return str(Path(sys.executable).parent) if getattr(sys,'frozen',False) else str(Path(__file__).resolve().parents[1])

def run():
    root=tk.Tk()
    App(root)
    root.mainloop()
