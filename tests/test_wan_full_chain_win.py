"""끝에서 끝까지 — 가짜 현장 화면 대신 **진짜 RemoteHost**(Windows API 로 화면 캡처 · SendInput 입력 주입)를 WAN 경로 뒤에 둔다
실제 앱 → 진짜 RemoteViewer → (직접 연결 aiortc) → 현장 에이전트 → 진짜 CommandHandler → 진짜 RemoteHost
Windows(또는 Wine 위의 Windows 용 Python) 전용.  AURORA_HOST_TESTS=1 (캡처), AURORA_HOST_INPUT=1 (입력 주입 — 실제 마우스·키보드를 움직임)"""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt" or not os.environ.get("AURORA_HOST_TESTS"),
                                reason="Windows(또는 Wine) 전용, AURORA_HOST_TESTS=1 로 켬")

from test_wan_full_chain import make_chain, run  # noqa: E402


@pytest.fixture
def real_chain(tmp_path, monkeypatch):
    yield from make_chain(tmp_path, monkeypatch, real_host=True)


def open_viewer(env):
    import tkinter
    root, app, client, agent = env["root"], env["app"], env["client"], env["agent"]
    target = tkinter.Toplevel(root)                       # 원격으로 조작할 "현장 프로그램" 흉내 — 입력칸이 있는 별도 창
    target.geometry("520x160+700+40")
    entry = tkinter.Entry(target, font=("Arial", 24))
    entry.pack(fill="x", padx=20, pady=40)
    root.update()
    session = client.open_session(agent["id"], "1234", agent["name"])
    assert session.mode == "direct"
    app._wan_open_remote(session, agent)
    viewer = app._wan_views[agent["id"]]
    viewer.win.geometry("760x520+0+0")
    return viewer, session, target, entry


def test_real_host_screen_reaches_the_viewer_over_wan(real_chain):
    env = real_chain
    viewer, session, target, entry = open_viewer(env)
    run(env["root"], 5.0)
    assert viewer._error == "", viewer._error
    assert viewer._base is not None and viewer._base.size[0] > 100
    import PIL.ImageStat as stat
    # 진짜 화면이 왔는지 — 단색이 아님 (창·입력칸이 있는 화면)
    assert max(stat.Stat(viewer._base.convert("L")).stddev) > 5, "받은 화면이 단색임"
    assert session.rx > 5000
    viewer.close()


@pytest.mark.skipif(not os.environ.get("AURORA_HOST_INPUT"), reason="AURORA_HOST_INPUT=1 로 켬 (실제 입력을 주입함)")
def test_keys_sent_from_the_viewer_type_into_a_real_window(real_chain):
    env = real_chain
    root = env["root"]
    viewer, session, target, entry = open_viewer(env)
    run(root, 3.0)
    target.lift()
    entry.focus_force()
    root.update()
    time.sleep(0.3)
    viewer._ctl.set(True)
    viewer._ctl_changed()
    for vk in (0x41, 0x42, 0x43):                         # A B C — 보는 쪽에서 누르면 WAN 을 거쳐 현장의 SendInput 으로
        viewer._push(["k", vk, 1])
        viewer._push(["k", vk, 0])
    run(root, 3.0)
    assert entry.get().lower() == "abc", repr(entry.get())
    viewer.close()


@pytest.mark.skipif(not os.environ.get("AURORA_HOST_INPUT"), reason="AURORA_HOST_INPUT=1 로 켬 (실제 입력을 주입함)")
def test_mouse_moved_in_the_viewer_moves_the_real_pointer(real_chain):
    import ctypes
    from ctypes import wintypes
    env = real_chain
    root = env["root"]
    viewer, session, target, entry = open_viewer(env)
    run(root, 3.0)
    viewer._ctl.set(True)
    viewer._ctl_changed()
    viewer._push(["m", 16384, 49152])                      # 가로 25%, 세로 75%
    run(root, 2.0)
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    sw, sh = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    assert abs(pt.x - sw * 0.25) <= 4 and abs(pt.y - sh * 0.75) <= 4, (pt.x, pt.y, sw, sh)
    viewer.close()
