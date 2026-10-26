"""
엄격한 가짜 tkinter — 실제 Tk 가 오류를 내는 실수를 흉내 낸다 (Linux 에는 tkinter 가 없어 MagicMock 으로는 못 잡는 것들)
  - 같은 부모 안에서 pack 과 grid 를 섞어 쓰면 TclError
  - 이미 없어진(destroy) 위젯을 만지면 TclError
  - 알 수 없는 옵션 이름을 넘기면 TclError (오타 검출)
"""

import sys
import types


class TclError(Exception):
    pass


KNOWN = {
    "bg", "fg", "font", "text", "textvariable", "variable", "command", "width", "height", "padx", "pady", "anchor",
    "justify", "relief", "bd", "borderwidth", "highlightthickness", "highlightbackground", "highlightcolor",
    "state", "wraplength", "image", "cursor", "show", "values", "orient", "selectmode", "columns", "style",
    "activebackground", "activeforeground", "selectcolor", "takefocus", "yscrollcommand", "xscrollcommand",
    "insertbackground", "wrap", "disabledforeground", "readonlybackground", "exportselection", "maximum", "value",
    "length", "mode", "offvalue", "onvalue", "selectbackground", "selectforeground", "padding", "height", "name",
    "background", "foreground", "activestyle", "tearoff", "spacing1", "spacing3", "insertwidth", "fieldbackground",
}


class Variable:
    def __init__(self, master=None, value=None, **kw):
        self._v = value if value is not None else self._default
        self._traces = []

    _default = ""

    def get(self):
        return self._v

    def set(self, v):
        self._v = v
        for cb in self._traces:
            cb()

    def trace_add(self, mode, cb):
        self._traces.append(lambda: cb())
        return "trace"


class StringVar(Variable):
    _default = ""


class BooleanVar(Variable):
    _default = False


class IntVar(Variable):
    _default = 0


class Widget:
    all_widgets = []

    def __init__(self, master=None, **kw):
        self._check(kw)
        self.master, self.kw, self.children = master, dict(kw), []
        self._child_mgr = None
        self._destroyed = False
        self._mgr = None
        self.items = []
        if master is not None:
            master._alive()
            master.children.append(self)
        Widget.all_widgets.append(self)

    @staticmethod
    def _check(kw):
        bad = set(kw) - KNOWN
        if bad:
            raise TclError(f'unknown option "-{sorted(bad)[0]}"')

    def _alive(self):
        if self._destroyed:
            raise TclError("invalid command name (destroyed widget)")

    def _manage(self, kind):
        self._alive()
        m = self.master
        if m is not None:
            if m._child_mgr not in (None, kind):
                raise TclError(f"cannot use geometry manager {kind} inside {type(m).__name__} "
                               f"which already has slaves managed by {m._child_mgr}")
            m._child_mgr = kind
        self._mgr = kind

    def pack(self, **kw):
        self._manage("pack")

    def grid(self, **kw):
        self._manage("grid")

    def place(self, **kw):
        self._manage("place")

    def pack_configure(self, **kw):
        self._manage("pack")

    def pack_forget(self):
        self._mgr = None

    grid_forget = grid_remove = place_forget = pack_forget

    def configure(self, cnf=None, **kw):
        self._alive()
        self._check(kw)
        self.kw.update(kw)

    config = configure

    def cget(self, key):
        return self.kw.get(key)

    def __getitem__(self, key):
        return self.kw.get(key)

    def __setitem__(self, key, value):
        self.kw[key] = value

    def destroy(self):
        self._destroyed = True
        for c in self.children:
            c.destroy()
        if self.master is not None and self in self.master.children:
            self.master.children.remove(self)

    def winfo_children(self):
        return list(self.children)

    def winfo_toplevel(self):
        w = self
        while w.master is not None:
            w = w.master
        return w

    def winfo_manager(self):
        return self._mgr or ""

    def winfo_width(self):
        return 600

    winfo_height = winfo_reqwidth = winfo_reqheight = winfo_width

    def after(self, ms, fn=None, *a):
        return "after#1"

    def bind(self, *a, **k):
        pass

    def insert(self, *a, **k):
        self.items.append(a)
        return f"I{len(self.items)}"

    def delete(self, *a, **k):
        self.items.clear()

    def selection(self, *a):
        return ()

    def get_children(self, *a):
        return [f"I{i + 1}" for i in range(len(self.items))]

    def __getattr__(self, name):          # focus_set, see, yview, heading, column, selection ... 처럼 모양만 필요한 것
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


class Toplevel(Widget):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self._mgr = "toplevel"


Frame = Label = Entry = Button = Checkbutton = Radiobutton = Text = Canvas = Scrollbar = Listbox = Widget


class Menu(Widget):
    def add_command(self, **kw):
        self._check({k: v for k, v in kw.items() if k not in ("label", "command")})

    def tk_popup(self, *a):
        pass

    def grab_release(self):
        pass


class Style:
    def configure(self, *a, **k):
        pass

    map = configure
    theme_use = configure


def build_modules():
    tk = types.ModuleType("tkinter")
    for n, cls in dict(Frame=Frame, Label=Label, Entry=Entry, Button=Button, Checkbutton=Checkbutton,
                       Radiobutton=Radiobutton, Text=Text, Canvas=Canvas, Scrollbar=Scrollbar, Listbox=Listbox,
                       Toplevel=Toplevel, Menu=Menu, StringVar=StringVar, BooleanVar=BooleanVar, IntVar=IntVar,
                       TclError=TclError).items():
        setattr(tk, n, cls)
    ttk = types.ModuleType("tkinter.ttk")
    for n, cls in dict(Frame=Frame, Label=Label, Entry=Entry, Button=Button, Checkbutton=Checkbutton,
                       Scrollbar=Scrollbar, Treeview=Widget, Combobox=Widget, Style=Style,
                       Progressbar=Widget).items():
        setattr(ttk, n, cls)
    mb = types.ModuleType("tkinter.messagebox")
    for n in ("showinfo", "showerror", "showwarning", "askyesno"):
        setattr(mb, n, lambda *a, **k: True)
    sd = types.ModuleType("tkinter.simpledialog")
    sd.askstring = lambda *a, **k: None
    fd = types.ModuleType("tkinter.filedialog")
    fd.askopenfilename = fd.asksaveasfilename = lambda *a, **k: ""
    tk.ttk, tk.messagebox, tk.simpledialog, tk.filedialog = ttk, mb, sd, fd
    return {"tkinter": tk, "tkinter.ttk": ttk, "tkinter.messagebox": mb, "tkinter.simpledialog": sd,
            "tkinter.filedialog": fd}


def install(monkeypatch):
    mods = build_modules()
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    Widget.all_widgets.clear()
    return mods["tkinter"]
