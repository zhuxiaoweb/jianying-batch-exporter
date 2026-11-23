"""A short guided workflow over the existing verified batch actions."""
from pathlib import Path
import os
import tkinter as tk
from tkinter import ttk,messagebox


def install(app,frame,paths,draft,engine,fast,inbox,options,legacy_taskbar,manual_archive,engine_actions,compose):
    home=ttk.Frame(app.tabs,padding=12)
    app.tabs.insert(0,home,text='开始使用')
    for page,title in ((draft,'1 选择草稿'),(app.class_tab,'2 确认内容'),(app.task_tab,'3 导出与审核'),(app.help_tab,'帮助与日志')):
        app.tabs.tab(page,text=title)
    app.tabs.insert(app.task_tab,app.class_tab)
    app.simple_home=home;app.simple_draft_tab=draft
    app.simple_advanced=tk.BooleanVar(value=False)
    app.simple_hidden_tabs=(engine,fast,inbox)
    app.simple_hidden_frames=(options,legacy_taskbar,manual_archive,engine_actions,compose)
    app.simple_paths=paths
    saved={str(widget):widget.pack_info() for widget in (paths,*app.simple_hidden_frames)}
    toolbar=ttk.Frame(frame)
    toolbar.pack(fill='x',before=app.tabs,pady=(0,7))

    def choose_job():
        if not app.session:
            messagebox.showinfo('先备份','先在“1 选择草稿”勾选集数，点击“创建导出任务（先备份）”。');app.tabs.select(draft);return False
        selected=app.task_tree.selection()
        if not selected and len(app.session['jobs'])==1:
            first=next(iter(app.task_rows),None)
            if first:app.task_tree.selection_set(first)
        elif not selected:
            messagebox.showinfo('选择当前集','请在“3 导出与审核”选择要处理的集。同一集任意一条版本行都可以。');app.tabs.select(app.task_tab);return False
        return bool(app.task_tree.selection())

    def analyze():
        if choose_job():app.analyze(True)
    def export():
        if choose_job():app.engine_export()
    def show_guide():
        base=app.engine_component.parent
        path=base/'新手操作指南.html'
        if not path.is_file():
            messagebox.showinfo('指南位置','请打开解压目录中的“新手操作指南.html”。');return
        os.startfile(path)

    def toggle():
        advanced=app.simple_advanced.get()
        if advanced:
            paths.pack(**{key:value for key,value in saved[str(paths)].items() if key!='in'},before=toolbar)
        else:paths.pack_forget()
        for widget in app.simple_hidden_frames:
            if advanced:widget.pack(**{key:value for key,value in saved[str(widget)].items() if key!='in'})
            else:widget.pack_forget()
        for page in app.simple_hidden_tabs:
            if advanced:app.tabs.add(page)
            else:app.tabs.hide(page)
        if not advanced:
            for value in app.options.values():value.set(True)
            app.want_srt.set(True)
        app.tabs.select(home)
    app.simple_toggle=toggle
    app.simple_analyze=analyze;app.simple_export=export
    ttk.Checkbutton(toolbar,text='显示高级工具与路径设置',variable=app.simple_advanced,command=toggle).pack(side='right')
    ttk.Button(toolbar,text='打开详细操作指南',command=show_guide).pack(side='right',padx=8)
    ttk.Label(toolbar,text='日常只用编号1–3的页面；第一次先在这里准备环境。',foreground='#466080').pack(side='left')
    body=app.scroll_panel(home)
    ttk.Label(body,text='先准备一次，以后按顺序完成每集交付',font=('Microsoft YaHei UI',18,'bold')).pack(anchor='w',pady=(0,8))
    ttk.Label(body,text='固定输出：四版1080P / MP4 / 60fps / 9:16视频，以及最终SRT字幕。\n原草稿先备份；配乐和字幕需要确认；导出完成后仍须声画审核。',justify='left',foreground='#466080').pack(anchor='w',pady=(0,12))

    def card(title,description):
        box=ttk.LabelFrame(body,text=title,padding=10);box.pack(fill='x',pady=(0,9))
        ttk.Label(box,text=description,wraplength=940,justify='left').pack(anchor='w',pady=(0,7))
        return box
    first=card('A  首次准备环境（每台电脑一次）','C盘空间不足时，请选有空间的数据盘。建议预留6GB；旧版已下载的完整安装包会自动复用。')
    locations=ttk.Frame(first);locations.pack(fill='x')
    ttk.Label(locations,text='引擎存放位置').pack(side='left')
    ttk.Entry(locations,textvariable=app.engine_storage,state='readonly').pack(side='left',fill='x',expand=True,padx=8)
    app.button(locations,'选择位置',app.choose_engine_storage).pack(side='left')
    actions=ttk.Frame(first);actions.pack(fill='x',pady=7)
    for text,command in [('准备引擎',app.engine_setup),('检查引擎',app.engine_doctor),('运行自测',app.engine_self_test)]:
        app.button(actions,text,command).pack(side='left',padx=(0,8))
    ttk.Label(first,textvariable=app.engine_state,wraplength=940,foreground='#466080').pack(anchor='w')
    second=card('B  选择集数并备份','进入“1 选择草稿”，勾选需要交付的集，再点击“创建导出任务（先备份）”。第一次建议只选一集。')
    output=ttk.Frame(second);output.pack(fill='x')
    ttk.Label(output,text='成片和备份存放位置').pack(side='left')
    ttk.Entry(output,textvariable=app.output,state='readonly').pack(side='left',fill='x',expand=True,padx=8)
    app.button(output,'选择目录',app.choose_output).pack(side='left')
    actions=ttk.Frame(second);actions.pack(fill='x',pady=(7,0))
    app.button(actions,'去选集数',lambda:app.tabs.select(draft)).pack(side='left')
    app.button(actions,'继续已有任务',app.open_session).pack(side='left',padx=8)
    third=card('C  确认长配乐和字幕','分析后进入“2 确认内容”：长BGM标为背景音乐；短音乐、人声、环境音、音效保留；台词标为字幕，标题单独保留。最后点击“保存分类”。')
    ttk.Checkbutton(third,text='本集含复合片段或处理后声音：启用额外兼容检查（首次须原生对照）',variable=app.engine_advanced).pack(anchor='w',pady=(0,7))
    app.button(third,'分析当前集',analyze).pack(anchor='w')
    fourth=card('D  导出并审核','分类保存后开始导出。任务显示“规格通过”仍需听看检查。以后可打开已有任务继续，不必重复备份。')
    actions=ttk.Frame(fourth);actions.pack(fill='x')
    app.button(actions,'导出当前集',export).pack(side='left')
    app.button(actions,'查看导出与审核',lambda:app.tabs.select(app.task_tab)).pack(side='left',padx=8)
    app.button(actions,'打开当前集文件夹',app.open_job_folder).pack(side='left')
    # The main queue now has only the direct background workflow by default.
    queuebar=ttk.Frame(app.task_tab)
    queuebar.pack(fill='x',before=app.task_tree.master,pady=(0,7))
    for text,command in [('分析当前集',analyze),('导出当前集',export),('打开当前集文件夹',app.open_job_folder),('标记声画审核通过',app.approve)]:
        app.button(queuebar,text,command).pack(side='left',padx=(0,8))
    # Keep the already selected episode when a batch has several version rows.
    def select_first(event=None):
        if app.session and len(app.session['jobs'])==1 and not app.task_tree.selection():
            first=next(iter(app.task_rows),None)
            if first:app.task_tree.selection_set(first)
    app.tabs.bind('<<NotebookTabChanged>>',select_first,add='+')
    toggle()
