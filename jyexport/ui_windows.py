"""Recoverable Tk dialogs without a mouse grab or a disabled main window.

Only application widgets are disabled while an explicit decision is pending;
Windows can still activate/minimize the application through its main window.
Never use a permanent topmost flag, OS hooks, or periodic focus stealing.
"""
from __future__ import annotations
import tkinter as tk


class DialogWindows:
    def __init__(self,root,changed=lambda:None):
        self.root=root;self.changed=changed;self.items=[];self.pending=None;self.restoring=False;self.closed=False
        root.bind('<FocusIn>',self._activated,add='+')
        root.bind('<Map>',self._activated,add='+')
        root.bind('<Destroy>',self._destroyed,add='+')

    def _destroyed(self,event):
        if event.widget is not self.root:return
        self.closed=True
        if self.pending is not None:
            try:self.root.after_cancel(self.pending)
            except tk.TclError:pass
            self.pending=None
        self.items.clear()

    def active(self):
        return next((item['window'] for item in reversed(self.items)
                     if item['blocking'] and self._exists(item['window'])),None)

    @staticmethod
    def _exists(window):
        try:return bool(window.winfo_exists())
        except tk.TclError:return False

    def blocked(self):return self.active() is not None

    @staticmethod
    def _widgets(parent):
        for child in parent.winfo_children():
            if child.winfo_toplevel() is child:continue
            yield child
            yield from DialogWindows._widgets(child)

    def register(self,window,parent=None,blocking=True):
        parent=parent or self.root
        # Keep the native owner relationship for Windows/Tk lifecycle handling,
        # but do not take a Tk input grab or disable the native main window.
        # Main-window activation raises the current decision window below.
        window.transient(parent)
        states=[]
        if blocking and parent is not self.root:
            for widget in self._widgets(parent):
                try:
                    previous=tuple(widget.state());widget.state(['disabled'])
                    states.append((widget,previous))
                except (AttributeError,tk.TclError):pass
        item={'window':window,'parent':parent,'blocking':blocking,'states':states}
        self.items.append(item)
        def destroyed(event):
            if event.widget is not window:return
            self._remove(item)
        window.bind('<Destroy>',destroyed,add='+')
        self.changed()
        self._present(window,explicit=False)
        return window

    def _remove(self,item):
        if item not in self.items:return
        window=item['window']
        for child in list(reversed(self.items)):
            if child['parent'] is window and self._exists(child['window']):
                child['window'].destroy()
        self.items.remove(item)
        for widget,previous in item['states']:
            try:
                if widget.winfo_exists():widget.state(['!disabled',*previous])
            except tk.TclError:pass
        self.changed()

    def current(self,window):
        active=self.active()
        return active is None or active is window

    def _present(self,window,explicit):
        if self.restoring or not self._exists(window):return
        self.restoring=True
        try:
            # Hidden root is also used by automated widget tests/smoke checks.
            if self.root.state()=='withdrawn' and not explicit:return
            if self.root.state()=='iconic':self.root.deiconify()
            if window.state()=='withdrawn' and not explicit:return
            if explicit or window.state()=='iconic':window.deiconify()
            window.lift()
            if explicit:window.focus_force()
            else:window.focus_set()
        except tk.TclError:pass
        finally:self.restoring=False

    def recover(self):
        active=self.active()
        if active is None:
            active=next((item['window'] for item in reversed(self.items) if self._exists(item['window'])),self.root)
        self._present(active,explicit=True)

    def _activated(self,event):
        if self.closed or self.restoring or self.pending is not None:return
        try:
            if event.widget.winfo_toplevel() is not self.root or not self.blocked():return
        except tk.TclError:return
        def later():
            self.pending=None
            if self.closed:return
            try:
                focused=self.root.focus_displayof()
                # A queued Map/FocusIn must not steal focus after the user
                # has already switched to a different application.
                if focused is None or focused.winfo_toplevel() is not self.root:return
                active=self.active()
                if active is not None:self._present(active,explicit=True)
            except tk.TclError:pass
        self.pending=self.root.after_idle(later)

    def close_all(self):
        if self.pending is not None:
            try:self.root.after_cancel(self.pending)
            except tk.TclError:pass
            self.pending=None
        for item in list(reversed(self.items)):
            if self._exists(item['window']):item['window'].destroy()
