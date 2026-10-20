"""
Linux 에서 aurora_client.py · aurora_remote.py 를 불러오기 위한 Windows 전용 부분 대체 도구 (시험용)
==================================================================================
- winreg · pystray · ctypes.windll 등은 가짜로 채운다.
- tkinter 가 진짜로 설치되어 있으면 그대로 쓴다 (화면은 xvfb-run 으로). 없으면 가짜.
- aurora_remote.py 는 Windows 구조체 크기(c_ulong=4바이트)에 의존하므로, 불러오는 동안만 크기를 맞춘다.
  load_client(real_remote=True) 면 진짜 RemoteViewer 를 쓴다 (시험에서 Windows 호출은 MagicMock 으로 흡수).
"""

import contextlib
import ctypes
import ctypes.wintypes as wt
import importlib
import os
import sys
import types
from unittest import mock

IS_WINDOWS = os.name == "nt"      # 진짜 Windows(또는 Wine 위의 Windows 용 Python)에서는 아무것도 가짜로 바꾸지 않는다


def real_tk_available() -> bool:
    if IS_WINDOWS:
        return True
    try:
        import tkinter  # noqa: F401
        return not isinstance(sys.modules.get("tkinter"), mock.MagicMock)
    except Exception:
        return False


def install():
    if IS_WINDOWS:
        return
    if "winreg" not in sys.modules:
        winreg = types.ModuleType("winreg")
        for name in ("HKEY_CURRENT_USER", "KEY_SET_VALUE", "KEY_READ", "REG_SZ"):
            setattr(winreg, name, 0)

        def _no(*a, **k):
            raise FileNotFoundError("가짜 winreg — 항목 없음")
        for name in ("OpenKey", "QueryValueEx", "SetValueEx", "CloseKey", "DeleteValue"):
            setattr(winreg, name, _no)
        sys.modules["winreg"] = winreg
    sys.modules.setdefault("pystray", mock.MagicMock())
    if not real_tk_available():
        for mod in ("tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog", "tkinter.simpledialog",
                    "tkinter.font"):
            sys.modules[mod] = mock.MagicMock()
        tk = sys.modules["tkinter"]   # tkinter 를 상속하는 코드(class _FileGrid(tk.Frame))가 있어 진짜 클래스가 필요
        for cls in ("Frame", "Tk", "Toplevel", "Canvas", "Label", "Entry", "Text", "Button"):
            setattr(tk, cls, type(cls, (), {"__init__": lambda self, *a, **k: None}))
    if not hasattr(ctypes, "WINFUNCTYPE"):
        ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
    for name in ("windll", "WinDLL", "oledll"):
        if not hasattr(ctypes, name):
            setattr(ctypes, name, mock.MagicMock())


@contextlib.contextmanager
def windows_ctypes_sizes():
    """Linux 에서 c_ulong 은 8바이트 — Windows 처럼 4바이트로 맞춰 구조체 크기를 같게 (불러오는 동안만)"""
    if IS_WINDOWS:
        yield
        return
    saved = {n: getattr(ctypes, n) for n in ("c_ulong", "c_long")}
    saved_wt = {n: getattr(wt, n) for n in ("DWORD", "ULONG", "LONG")}
    ctypes.c_ulong, ctypes.c_long = ctypes.c_uint32, ctypes.c_int32
    wt.DWORD, wt.ULONG, wt.LONG = ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32
    try:
        yield
    finally:
        for n, v in saved.items():
            setattr(ctypes, n, v)
        for n, v in saved_wt.items():
            setattr(wt, n, v)


def _fake_aurora_remote():
    """진짜 aurora_remote 를 쓰지 않는 시험용 — 필요한 이름만 가짜로"""
    mod = types.ModuleType("aurora_remote")
    mod.RemoteViewer = type("RemoteViewer", (), {"QUALITY": {}})
    mod.RemoteHost = mock.MagicMock(name="RemoteHost")
    mod.low_priority = lambda level=-1: contextlib.nullcontext()
    return mod


def load_remote():
    """진짜 aurora_remote.py (RemoteViewer · RemoteHost 포함)"""
    install()
    if "aurora_remote" in sys.modules and not getattr(sys.modules["aurora_remote"], "_IS_FAKE", False) \
            and hasattr(sys.modules["aurora_remote"], "_grab"):
        return sys.modules["aurora_remote"]
    sys.modules.pop("aurora_remote", None)
    with windows_ctypes_sizes():
        return importlib.import_module("aurora_remote")


def load_client(real_remote: bool = False):
    install()
    if real_remote or IS_WINDOWS:
        load_remote()
    else:
        fake = _fake_aurora_remote()
        fake._IS_FAKE = True
        sys.modules["aurora_remote"] = fake
    sys.modules.pop("aurora_client", None)
    return importlib.import_module("aurora_client")


def make_root():
    """시험용 Tk 루트. 화면이 없는 Linux 에서만 건너뛰고, 그 밖에서 실패하면 진짜 오류로 드러낸다"""
    import time
    import pytest
    import tkinter
    try:
        for attempt in range(4):      # Wine 의 Tcl 은 이미 쓴 프로세스에서 init.tcl 읽기가 간헐적으로 실패함 — 잠깐 뒤 재시도
            try:
                return tkinter.Tk()
            except tkinter.TclError:
                if not IS_WINDOWS or attempt == 3:
                    raise
                time.sleep(1.0)
    except tkinter.TclError as e:
        if not IS_WINDOWS and not os.environ.get("DISPLAY"):
            pytest.skip("화면(DISPLAY)이 없음 — xvfb-run 으로 실행")
        raise RuntimeError(f"Tk 루트를 만들 수 없음: {e}") from e


def sandbox(monkeypatch, tmp_path):
    """시험이 사용자 폴더·작업 폴더를 어지럽히지 않게 격리.
    Linux: aurora_client 를 불러올 때 'D:\\RemoteConfig' 라는 이름의 폴더가 현재 폴더에 생기므로 임시 폴더로 이동.
    Windows/Wine: D: 드라이브가 있으므로 이동하지 않음 (Wine 의 Tcl 은 chdir 직후 Tk 를 만들면 간헐적으로 실패함)"""
    if not IS_WINDOWS:
        monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))      # Windows 는 ~ 를 USERPROFILE 로 해석


def destroy_root(root):
    """Tk 루트를 없애기 전에 메인 스레드에서 가비지를 먼저 해제.
    닫힌 뷰어에 남은 PhotoImage 가 루트가 사라진 뒤 다른 스레드의 GC 에서 해제되면 Tcl 이 죽을 수 있다 (Wine 에서 확인됨).
    실제 앱은 루트를 앱이 끝날 때까지 유지하므로 시험 정리에서만 필요한 처리"""
    import gc
    for _ in range(2):
        gc.collect()
        try:
            root.update()
        except Exception:
            break
    try:
        root.destroy()
    except Exception:
        pass
