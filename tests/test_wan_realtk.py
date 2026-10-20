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


def test_auto_button_and_paste_dialog_with_real_widgets(app, monkeypatch):
    """진짜 위젯: 자동 설정 버튼이 있고, 접속 정보 붙여넣기 창이 입력값을 컨트롤러에 넘긴다"""
    import time
    from wan import connect_code
    tab = app._wan_tab
    assert hasattr(tab, "auto_btn") and hasattr(tab, "relay_code_var") and hasattr(tab, "admin_btn")
    got = []
    monkeypatch.setattr(tab.controller, "import_connection", lambda *a, **k: got.append(a) or True)
    monkeypatch.setattr(tab.controller, "find_on_lan", lambda: None)
    app._select_tab(4)
    tab._import_dialog()
    d = tab._import_win
    code = connect_code.encode(["ws://10.0.0.5:9100"], "k")
    d.code.set(code)
    d.role.set("field")
    d.name.set("CCW 1")
    d.ok()
    app.root.update()
    assert got == [(code, True, "CCW 1")]
    assert not d.win.winfo_exists()                                      # 성공하면 창이 닫힘
    tab.set_relay_code("AW1-test")
    assert tab.relay_code_var.get() == "AW1-test"
    tab.set_server_fields("ws://a:1", "kk")
    assert (tab.url_var.get(), tab.key_var.get()) == ("ws://a:1", "kk")
    from wan import ui as wan_ui
    monkeypatch.setattr(wan_ui, "is_admin", lambda: False)             # 주기 갱신이 관리자 여부를 다시 계산하므로 값을 고정
    tab.controller.refresh_relay()
    app.root.update()
    assert tab.admin_btn.winfo_ismapped()                               # 관리자가 아니면 '관리자로 다시 시작' 버튼이 보임
    monkeypatch.setattr(wan_ui, "is_admin", lambda: True)
    tab.controller.refresh_relay()
    app.root.update()
    assert not tab.admin_btn.winfo_ismapped()


def test_paste_dialog_field_inputs_appear_above_the_buttons(app, monkeypatch):
    """현장 PC 를 고르면 나오는 이름·접속 코드 칸이 확인/취소 버튼 위에 있어야 한다 (예전에는 버튼 아래로 밀렸음)"""
    tab = app._wan_tab
    monkeypatch.setattr(tab.controller, "find_on_lan", lambda: None)
    app._select_tab(4)
    tab._import_dialog()
    d = tab._import_win
    d.role.set("field")
    d.show_field()
    d.win.update()
    assert d.field.winfo_ismapped()
    assert d.field.winfo_y() < d.btns.winfo_y(), (d.field.winfo_y(), d.btns.winfo_y())
    d.role.set("user")
    d.show_field()
    d.win.update()
    assert not d.field.winfo_ismapped()
    d.win.destroy()


def test_paste_dialog_searches_lan_when_clipboard_is_empty_and_fills_result(app, monkeypatch):
    """복사해 둔 게 없으면 열자마자 같은 망에서 찾고, 찾은 접속 정보가 입력칸에 들어온다"""
    from wan import connect_code
    tab = app._wan_tab
    searched = []
    monkeypatch.setattr(tab.controller, "find_on_lan", lambda: searched.append(1))
    try:
        app.root.clipboard_clear()
    except Exception:
        pass
    app._select_tab(4)
    tab._import_dialog()
    d = tab._import_win
    assert searched == [1] and "찾는 중" in d.hint.cget("text")
    code = connect_code.encode(["ws://10.0.0.5:9100"], "k")
    tab.set_found_code(code)
    assert d.code.get() == code and "찾았습니다" in d.hint.cget("text")
    d.win.destroy()
    tab.set_found_code(code)                                              # 창이 닫힌 뒤 늦게 도착해도 오류 없음
