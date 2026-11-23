from __future__ import annotations
import sys

def main():
    if len(sys.argv)==3 and sys.argv[1]=='--editor-update-check':
        # Explicit maintenance entry. Ordinary doctor/smoke commands stay read-only.
        from pathlib import Path
        from jyexport.deployment import configuration_directory
        from jyexport.discovery import user_data
        from jyexport.editor_updates import apply_saved_update_policy
        from jyexport.storage import atomic_json
        report=Path(sys.argv[2]).resolve()
        if report.exists():raise FileExistsError('结果报告已存在，拒绝覆盖：'+str(report))
        base=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
        state=configuration_directory(base)
        result=apply_saved_update_policy(user_data(),state/'剪映更新设置备份',state/'settings.json')
        report.parent.mkdir(parents=True,exist_ok=True)
        atomic_json(report,result)
        raise SystemExit(0 if result['success'] else 1)
    if len(sys.argv)>1 and sys.argv[1]=='--delivery-run':
        from jyexport.delivery_service import cli
        raise SystemExit(cli(sys.argv[2:]))
    if len(sys.argv)>1 and sys.argv[1]=='--advanced-ui':
        from jyexport.gui import run
        run()
        return
    if len(sys.argv)==3 and sys.argv[1]=='--operator-smoke-test':
        import tkinter as tk
        from pathlib import Path
        from jyexport.easy_ui import EasyApp
        from jyexport.storage import atomic_json
        root=tk.Tk();root.withdraw();app=EasyApp(root);root.update_idletasks()
        def children(parent):
            for widget in parent.winfo_children():
                yield widget
                yield from children(widget)
        controls=list(children(root))
        from jyexport import __version__
        from jyexport.editor_updates import disables_auto_update
        atomic_json(Path(sys.argv[2]),{'gui_initialized':True,'default_entry':'direct_delivery',
            'program_version':__version__,'batch_jobs':int(app.batch_jobs.get()),'native_limit':int(app.native_limit.get()),
            'automatic_environment_check':True,'manual_first_prepare_button':any(widget.winfo_class()=='TButton' and widget.cget('text')=='首次准备环境' for widget in controls),
            'state_directory':str(app.state_dir),'engine_storage':app.engine_storage.get(),
            'notebook_count':sum(widget.winfo_class()=='TNotebook' for widget in controls),
            'buttons':[widget.cget('text') for widget in controls if widget.winfo_class()=='TButton'],
            'single_list_for_selection_and_progress':app.results is app.tree,
            'default_archive_mode':'legacy' if app.legacy_layout.get() else 'series',
            'archive_layout_choice':app.layout_choice.cget('text'),
            'disable_jianying_auto_update':disables_auto_update(app.preferences),
            'editor_policy_checked_during_smoke':app.editor_policy_checked,
            'editor_beta_policy_fixed_off':True})
        root.destroy()
        return
    if len(sys.argv)>1 and sys.argv[1]=='--portable-test':
        from jyexport.portable_test import cli
        raise SystemExit(cli(sys.argv[2:]))
    if len(sys.argv)>1 and sys.argv[1]=='--engine':
        from jyexport.engine import cli
        raise SystemExit(cli(sys.argv[2:]))
    if len(sys.argv)>1 and sys.argv[1] in ('--inbox-once','--inbox-watch'):
        from jyexport.submission import cli
        raise SystemExit(cli(sys.argv[2:],watch=sys.argv[1]=='--inbox-watch'))
    if len(sys.argv) > 1 and sys.argv[1] == '--fast-delivery':
        from jyexport.fast import cli
        cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == '--workflow':
        from jyexport.workflow import cli
        cli(sys.argv[2:])
        return
    if len(sys.argv) == 5 and sys.argv[1] == '--codec-worker':
        from jyexport.codec import worker
        worker(sys.argv[2], sys.argv[3], sys.argv[4])
        return
    if len(sys.argv) == 4 and sys.argv[1] == '--diagnose':
        from jyexport.diagnostics import diagnose
        diagnose(sys.argv[2], sys.argv[3])
        return
    if len(sys.argv) == 3 and sys.argv[1] == '--gui-smoke-test':
        import tkinter as tk
        from pathlib import Path
        from jyexport.gui import App
        root = tk.Tk()
        root.withdraw()
        app = App(root)
        root.update_idletasks()
        destination = Path(sys.argv[2])
        if destination.suffix.lower() == '.json':
            from jyexport.storage import atomic_json
            atomic_json(destination, {'gui_initialized':True,'simple_mode_default':not app.simple_advanced.get(),'visible_tabs':[app.tabs.tab(tab,'text') for tab in app.tabs.tabs() if app.tabs.tab(tab,'state')!='hidden'],'state_directory':str(app.state_dir),'engine_storage':app.engine_storage.get(),'app_dir':app.app_dir.get(),'editor_component_exists':bool(app.app_dir.get()) and (Path(app.app_dir.get())/'videoeditor.dll').is_file(),'draft_roots':app.roots.get(),'cache':app.cache.get(),'presets':app.presets.get(),'fast_delivery_fields':list(app.fast_fields),'tabs':[app.tabs.tab(tab,'text') for tab in app.tabs.tabs()],'inbox_auto_started':app._inbox_running(),'inbox_confirmation_default':app.inbox_confirm.get(),'compatibility_test_available':callable(getattr(app,'engine_self_test',None)),'advanced_mode_default':app.engine_advanced.get(),'media_relocation_available':callable(getattr(app,'locate_media',None))})
        else:
            destination.write_text('GUI initialized successfully', 'utf-8')
        root.destroy()
        return
    from jyexport.easy_ui import launch
    launch()

if __name__ == '__main__':
    main()
