"""진짜 tkinter(Xvfb 가상 화면)로 실제 AuroraClientApp 을 띄워 WAN 탭을 확인
실행:  xvfb-run -a python -m pytest tests/test_wan_realtk.py
tkinter 나 화면이 없으면 건너뜀. Windows 전용 호출은 tests/winshim.py 가 흡수한다."""

import os
import sys
import threading

import pytest

tkinter = pytest.importorskip("tkinter")


@pytest.fixture
def app(tmp_path, monkeypatch):
    import winshim
    winshim.sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("AURORA_WAN_DIR", str(tmp_path / "wan"))
    import winshim
    root = winshim.make_root()
    mod = winshim.load_client(real_remote=True)
    monkeypatch.setattr(mod.AuroraClientApp, "_start_threads", lambda self: None)   # 네트워크 스레드는 띄우지 않음
    mod._init_scale(root)
    mod.AuroraClientApp.setup_styles()
    application = mod.AuroraClientApp(root)
    application._mod = mod
    root.update()
    yield application
    try:
        application._wan_shutdown()
    except Exception:
        pass
    winshim.destroy_root(root)


def test_real_app_builds_with_five_tabs_and_wan_tab(app):
    assert len(app._tab_frames) == 5
    assert getattr(app, "_wan", None) is not None, "WAN 탭을 만들지 못해 대체 안내 문구가 표시됨"
    assert getattr(app, "_wan_tab", None) is not None


def test_lan_tabs_still_build_and_switch(app):
    for i in range(5):
        app._select_tab(i)
        app.root.update()
    assert app._tab_frames[4].winfo_ismapped()


def test_wan_tab_fits_inside_the_window(app):
    app._select_tab(4)
    app.root.update_idletasks()
    app.root.update()
    frame = app._tab_frames[4]
    root = app.root
    bottom = max(w.winfo_rooty() + w.winfo_height() for w in _all_children(frame) if w.winfo_ismapped())
    assert bottom <= root.winfo_rooty() + root.winfo_height() + 2, (bottom, root.winfo_rooty() + root.winfo_height())
    right = max(w.winfo_rootx() + w.winfo_width() for w in _all_children(frame) if w.winfo_ismapped())
    assert right <= root.winfo_rootx() + root.winfo_width() + 2


def test_wan_tab_does_not_break_when_server_is_unset(app):
    app._select_tab(4)
    app.root.update()
    assert app._wan.client.state in ("끊김", "주소 없음", "연결 중")      # 시작 전이라 아직 아무 것도 연결 안 함
    assert app._wan.supervisor is None


def test_wan_tab_shows_agents_from_client(app):
    app._select_tab(4)
    client = app._wan.client
    client.agents = [{"id": "a1b2c3d4e5f6", "name": "CCW-1", "online": True, "last_seen": 0, "ver": "2.16.0",
                      "rev": 1, "applied_rev": 1}]
    client.state = "연결됨"
    client.on_change("agents")
    for _ in range(5):
        app.root.update()
    tree = _find(app._tab_frames[4], "Treeview")
    assert tree is not None
    rows = [tree.item(i, "values") for i in tree.get_children()]
    assert rows and rows[0][0] == "CCW-1"


def test_real_tab_screenshot(app, tmp_path):
    """눈으로 볼 수 있게 화면을 저장 (실패해도 시험은 통과 — 기록용)"""
    app._select_tab(4)
    app.root.update()
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab() if os.name == "nt" else ImageGrab.grab(xdisplay=os.environ.get("DISPLAY"))
        out = os.environ.get("WAN_SHOT", str(tmp_path / "wan_tab.png"))
        img.save(out)
        assert os.path.getsize(out) > 1000
    except Exception as e:
        pytest.skip(f"스크린샷 불가: {e}")


def _all_children(w):
    for c in w.winfo_children():
        yield c
        yield from _all_children(c)


def _find(w, cls_name):
    for c in _all_children(w):
        if c.winfo_class() == cls_name:
            return c
    return None


def test_relay_card_exists_and_toggle_reaches_runtime(app, monkeypatch):
    """중계 PC 용 '안내 서버 운영' 칸 — 진짜 위젯으로 체크하면 런타임이 불리고, 잘못된 포트는 막힘"""
    tab = app._wan_tab
    assert hasattr(tab, "relay_var"), "안내 서버 운영 칸이 만들어지지 않음"
    calls = []
    monkeypatch.setattr(app._wan, "set_relay_host", lambda on: calls.append(on))
    tab.ctx["set_relay_host"] = app._wan.set_relay_host
    monkeypatch.setattr(tab.mb, "showerror", lambda *a, **k: calls.append("error"))
    app._select_tab(4)
    tab.relay_port_var.set("abc")
    tab.relay_var.set(True)
    tab._relay_toggled()
    for _ in range(5):
        app.root.update()
        import time; time.sleep(0.05)
    assert calls == ["error"] and tab.relay_var.get() is False          # 포트가 잘못되면 체크가 풀리고 런타임은 안 불림
    tab.relay_port_var.set("9333")
    tab.relay_var.set(True)
    tab._relay_toggled()
    deadline = __import__("time").time() + 3
    while not calls[1:] and __import__("time").time() < deadline:
        app.root.update()
    assert calls[1:] == [True] and app._wan.cfg.get("relay_port") == 9333
