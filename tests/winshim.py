"""
Linux 에서 aurora_client.py 를 불러오기 위한 Windows 전용 부분 대체 도구 (시험용)
=================================================================================
winreg · pystray · tkinter · ctypes.windll 등을 가짜로 채워 넣는다. GUI 는 만들 수 없고
모듈 수준 코드와 CommandHandler(파일 API) · 클래스 정의가 오류 없이 불러와지는지 확인하는 용도다.
"""

import ctypes
import importlib
import sys
import types
from unittest import mock


def install():
    if "winreg" not in sys.modules:
        winreg = types.ModuleType("winreg")
        for name in ("HKEY_CURRENT_USER", "KEY_SET_VALUE", "KEY_READ", "REG_SZ"):
            setattr(winreg, name, 0)

        def _no(*a, **k):
            raise OSError("가짜 winreg")
        for name in ("OpenKey", "QueryValueEx", "SetValueEx", "CloseKey", "DeleteValue"):
            setattr(winreg, name, _no)
        sys.modules["winreg"] = winreg
    for mod in ("tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog", "tkinter.simpledialog",
                "tkinter.font", "pystray"):
        sys.modules.setdefault(mod, mock.MagicMock())
    # tkinter 의 클래스를 상속하는 코드(class _FileGrid(tk.Frame))가 있어 진짜 클래스가 필요함
    tk = sys.modules["tkinter"]
    for cls in ("Frame", "Tk", "Toplevel", "Canvas", "Label", "Entry", "Text", "Button"):
        setattr(tk, cls, type(cls, (), {"__init__": lambda self, *a, **k: None}))
    for name in ("WINFUNCTYPE",):
        if not hasattr(ctypes, name):
            setattr(ctypes, name, ctypes.CFUNCTYPE)
    for name in ("windll", "WinDLL", "oledll"):
        if not hasattr(ctypes, name):
            setattr(ctypes, name, mock.MagicMock())


def _fake_aurora_remote():
    """aurora_remote.py 는 Windows ctypes 구조체 크기에 의존해 Linux 에서 불러올 수 없음 → 필요한 이름만 가짜로"""
    import contextlib
    mod = types.ModuleType("aurora_remote")
    mod.RemoteViewer = type("RemoteViewer", (), {"QUALITY": {}})
    mod.RemoteHost = mock.MagicMock(name="RemoteHost")
    mod.low_priority = lambda level=-1: contextlib.nullcontext()
    return mod


def load_client():
    install()
    sys.modules.setdefault("aurora_remote", _fake_aurora_remote())
    sys.modules.pop("aurora_client", None)
    return importlib.import_module("aurora_client")
