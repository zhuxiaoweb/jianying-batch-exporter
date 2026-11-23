"""Presentation-only palette and lightweight vector mark; no media dependencies."""
import tkinter as tk
from tkinter import ttk

COLORS={'background':'#F3F6FA','surface':'#FFFFFF','ink':'#1D354B','muted':'#64788B',
        'accent':'#147D71','accent_hover':'#0C665C','line':'#DCE5ED','soft':'#EAF4F2',
        'success':'#216B52','success_bg':'#EAF6EF','warning':'#94621C','warning_bg':'#FFF4DC',
        'error':'#AD4141','error_bg':'#FDEEEE','selected':'#D8EEE8'}


def apply_theme(root):
    c=COLORS;root.configure(background=c['background'])
    style=ttk.Style(root);style.theme_use('clam')
    style.configure('.',font=('Microsoft YaHei UI',10),background=c['surface'],foreground=c['ink'])
    style.configure('App.TFrame',background=c['background'])
    style.configure('App.TLabel',background=c['background'])
    style.configure('Muted.TLabel',foreground=c['muted'])
    style.configure('AppMuted.TLabel',background=c['background'],foreground=c['muted'])
    style.configure('Title.TLabel',background=c['background'],foreground=c['ink'],font=('Microsoft YaHei UI',22,'bold'))
    style.configure('TButton',padding=(12,7),background=c['surface'],bordercolor=c['line'],relief='flat')
    style.map('TButton',background=[('active','#EAF0F5'),('disabled','#EFF2F5')],
              foreground=[('disabled','#91A0AE')],bordercolor=[('focus',c['accent'])])
    style.configure('Primary.TButton',background=c['accent'],foreground='#FFFFFF',font=('Microsoft YaHei UI',12,'bold'),padding=(22,10),bordercolor=c['accent'])
    style.map('Primary.TButton',background=[('disabled','#A6C8C0'),('active',c['accent_hover'])],foreground=[('disabled','#F5FAF8'),('!disabled','#FFFFFF')])
    style.configure('Quiet.TButton',padding=(10,5),foreground=c['muted'])
    style.configure('TEntry',fieldbackground=c['surface'],bordercolor=c['line'],padding=(7,6))
    style.map('TEntry',bordercolor=[('focus',c['accent'])])
    style.configure('Treeview',background=c['surface'],fieldbackground=c['surface'],foreground=c['ink'],rowheight=31,
                    bordercolor=c['line'],lightcolor=c['line'],darkcolor=c['line'])
    style.configure('Treeview.Heading',background='#EAF0F6',foreground=c['ink'],font=('Microsoft YaHei UI',10,'bold'),padding=(8,8),relief='flat')
    style.map('Treeview',background=[('selected',c['selected'])],foreground=[('selected','#174E46')])
    style.map('Treeview.Heading',background=[('active','#DEE9F1')])
    style.configure('TProgressbar',background=c['accent'],troughcolor='#E2EBEF',bordercolor='#E2EBEF',lightcolor=c['accent'],darkcolor=c['accent'])
    for name,fg,bg in [('Checking','#386486','#EAF2FA'),('Preparing','#386486','#EAF2FA'),
                       ('Ready',c['success'],c['success_bg']),('Failed',c['warning'],c['warning_bg'])]:
        style.configure(name+'.TFrame',background=bg)
        style.configure(name+'.TLabel',background=bg,foreground=fg)
        style.configure(name+'Badge.TLabel',background=bg,foreground=fg,font=('Microsoft YaHei UI',10,'bold'))
    return c


def brand_mark(parent):
    canvas=tk.Canvas(parent,width=46,height=46,background=COLORS['background'],highlightthickness=0)
    canvas.create_rectangle(3,5,43,41,fill='#DCEFE8',outline='')
    canvas.create_rectangle(3,5,43,14,fill='#B7DCD0',outline='')
    for x in (10,23,36):canvas.create_line(x,5,x-4,14,fill=COLORS['background'],width=3)
    canvas.create_polygon(18,21,18,35,31,28,fill=COLORS['accent'],outline='')
    return canvas


def decorate_rows(tree):
    tree.tag_configure('even',background='#FFFFFF')
    tree.tag_configure('odd',background='#F6F9FC')
    tree.tag_configure('complete',background=COLORS['success_bg'],foreground=COLORS['success'])
    tree.tag_configure('problem',background=COLORS['error_bg'],foreground=COLORS['error'])
    tree.tag_configure('working',background='#EDF4FC',foreground='#345F86')


def result_state(job):
    """Display completion without changing review receipts or backend state."""
    if job.get('easy_analysis_error'):return '分析未完成：'+job['easy_analysis_error']
    state=job.get('easy_delivery_state') or ('已确认，待制作' if job.get('easy_confirmed') else
          '待确认长配乐' if job.get('easy_analysis') else '待分析')
    if state.startswith(('完成，待声画审核','完成，已审核')):return '完成'
    return state
