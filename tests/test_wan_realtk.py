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
    (tmp_path / "wan").mkdir(exist_ok=True)
    (tmp_path / "wan" / "wan_config.json").write_text('{"wan_enabled": true}')      # 화면 시험은 WAN 을 켠 상태로 시작
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


def test_auto_button_and_connect_dialog_with_real_widgets(app, monkeypatch):
    """진짜 위젯: 자동 설정 버튼이 있고, 연결 추가 창이 입력값을 컨트롤러에 넘긴다"""
    from wan import connect_code
    tab = app._wan_tab
    assert hasattr(tab, "auto_btn") and hasattr(tab, "relay_code_var") and hasattr(tab, "admin_btn")
    got = []
    monkeypatch.setattr(tab.controller, "add_connection", lambda *a, **k: got.append(a) or True)
    monkeypatch.setattr(tab.controller, "find_on_lan", lambda: None)
    app._select_tab(4)
    tab._connect_dialog()
    d = tab._connect_win
    code = connect_code.encode(["ws://10.0.0.5:9100"], "k")
    d.text.set(code)
    d.field.set(True)
    d.ok()
    app.root.update()
    assert got == [(code, "", True)]
    assert not d.win.winfo_exists()                                      # 성공하면 창이 닫힘
    tab.set_relay_code("AW1-test")
    assert tab.relay_code_var.get() == "AW1-test"
    from wan import ui as wan_ui
    monkeypatch.setattr(wan_ui, "is_admin", lambda: False)             # 주기 갱신이 관리자 여부를 다시 계산하므로 값을 고정
    tab.controller.refresh_relay()
    app.root.update()
    tab._relay_expand(True)
    app.root.update()
    assert tab.admin_btn.winfo_ismapped()                               # 관리자가 아니면 '관리자로 다시 시작' 버튼이 보임
    monkeypatch.setattr(wan_ui, "is_admin", lambda: True)
    tab.controller.refresh_relay()
    app.root.update()
    assert not tab.admin_btn.winfo_ismapped()


def test_connect_dialog_shows_code_row_only_for_addresses(app, monkeypatch):
    """주소를 넣으면 접근 코드 칸(공용 키로 미리 채움)이 버튼 위에 나타나고, 접속 정보를 넣으면 숨는다"""
    from wan import connect_code
    tab = app._wan_tab
    app._wan.cfg.update(relay_key="공용키", relay_url="ws://h:1")
    monkeypatch.setattr(tab.controller, "find_on_lan", lambda: None)
    app._select_tab(4)
    tab._connect_dialog()
    d = tab._connect_win
    d.win.update()
    assert not d.code_row.winfo_ismapped() and not d.field_chk.winfo_ismapped()
    d.text.set("192.168.0.5")
    d.win.update()
    assert d.code_row.winfo_ismapped() and d.code.get() == "공용키"
    assert d.code_row.winfo_y() < d.btns.winfo_y(), (d.code_row.winfo_y(), d.btns.winfo_y())
    assert not d.field_chk.winfo_ismapped()
    d.text.set(connect_code.encode(["ws://10.0.0.5:9100"], "k"))
    d.win.update()
    assert not d.code_row.winfo_ismapped() and d.field_chk.winfo_ismapped()
    assert d.field_chk.winfo_y() < d.btns.winfo_y()
    d.text.set(connect_code.encode_direct(["ws://10.0.0.5:9102"], "c"))
    d.win.update()
    assert not d.code_row.winfo_ismapped() and not d.field_chk.winfo_ismapped()
    assert "현장 PC" in d.hint.cget("text")
    d.win.destroy()


def test_connect_dialog_opened_from_status_prefills_current_relay(app, monkeypatch):
    app._wan.cfg.update(relay_url="ws://h:7", relay_key="kk")
    tab = app._wan_tab
    app._select_tab(4)
    tab._connect_dialog(tab._current_relay())
    d = tab._connect_win
    d.win.update()
    assert d.text.get() == "ws://h:7" and d.code.get() == "kk" and d.code_row.winfo_ismapped()
    d.win.destroy()


def test_connect_dialog_searches_lan_when_no_relay_and_clipboard_is_empty(app, monkeypatch):
    """중계 PC 가 아직 없고 복사해 둔 게 없으면 열자마자 같은 망에서 찾고, 찾은 접속 정보가 입력칸에 들어온다"""
    from wan import connect_code
    tab = app._wan_tab
    app._wan.cfg.update(relay_url="")
    searched = []
    monkeypatch.setattr(tab.controller, "find_on_lan", lambda: searched.append(1))
    try:
        app.root.clipboard_clear()
    except Exception:
        pass
    app._select_tab(4)
    tab._connect_dialog()
    d = tab._connect_win
    assert searched == [1] and "찾는 중" in d.hint.cget("text")
    code = connect_code.encode(["ws://10.0.0.5:9100"], "k")
    tab.set_found_code(code)
    assert d.text.get() == code and "찾았습니다" in d.hint.cget("text")
    d.win.destroy()
    tab.set_found_code(code)                                              # 창이 닫힌 뒤 늦게 도착해도 오류 없음


def test_connect_dialog_does_not_search_when_relay_already_set(app, monkeypatch):
    tab = app._wan_tab
    app._wan.cfg.update(relay_url="ws://h:1")
    searched = []
    monkeypatch.setattr(tab.controller, "find_on_lan", lambda: searched.append(1))
    app._select_tab(4)
    tab._connect_dialog()
    tab._connect_win.win.destroy()
    assert searched == []


def test_field_card_shows_address_and_code_only_when_enabled(app, monkeypatch):
    tab = app._wan_tab
    app._select_tab(4)
    tab.set_field_enabled(False)
    app.root.update()
    assert not tab.field_box.winfo_ismapped()
    tab.set_field_enabled(True)
    tab.set_field_address("192.168.0.5:9102")
    tab.set_field_code("my-code")
    tab.set_field_info("A1B2-C3D4-E5F6", "사무실", "")
    app.root.update()
    assert tab.field_box.winfo_ismapped()
    assert tab.address_lbl.cget("text") == "192.168.0.5:9102" and tab.code_var.get() == "my-code"
    assert "사무실" in tab.id_lbl.cget("text") and "A1B2-C3D4-E5F6" in tab.id_lbl.cget("text")


def test_editing_access_code_on_screen_saves_it(app, monkeypatch):
    tab = app._wan_tab
    app._wan.cfg.update(relay_key="공용키", access_code="")
    app._select_tab(4)
    tab.code_var.set("새코드")
    tab._apply_code()
    assert app._wan.cfg.get("access_code") == "새코드"
    tab.code_var.set("공용키")                                             # 공용 키와 같으면 '따로 정한 코드 없음'
    tab._apply_code()
    assert app._wan.cfg.get("access_code") == ""


def test_relay_card_is_collapsed_until_opened(app, monkeypatch):
    tab = app._wan_tab
    app._wan.cfg.update(relay_host=False)
    tab._relay_expand(False)
    app._select_tab(4)
    app.root.update()
    assert not tab.relay_more.winfo_ismapped() and "▸" in tab.relay_toggle_lbl.cget("text")
    tab.relay_toggle_lbl.event_generate("<Button-1>")
    app.root.update()
    assert tab.relay_more.winfo_ismapped() and "▾" in tab.relay_toggle_lbl.cget("text")


def test_rows_menu_and_delete_key_for_saved_address_targets(app, monkeypatch):
    tab = app._wan_tab
    app._wan.cfg.update(direct_targets={"10.0.0.5:9102": {"name": "사무실", "code": "c", "last": 0}})
    app._select_tab(4)
    tab.controller.refresh_agents()
    app.root.update()
    assert tab.tree.exists("d:10.0.0.5:9102")
    tab.tree.selection_set("d:10.0.0.5:9102")
    tab._forget()
    app.root.update()
    assert not tab.tree.exists("d:10.0.0.5:9102") and app._wan.cfg.get("direct_targets") == {}


def test_save_path_and_copy_helpers(app, monkeypatch, tmp_path):
    tab = app._wan_tab
    monkeypatch.setattr(tab.fd, "asksaveasfilename", lambda **k: str(tmp_path / "x.conn"))
    got = []
    tab.ask_save_path(str(tmp_path), "aurora_wan.conn", got.append)
    assert got == [str(tmp_path / "x.conn")]
    monkeypatch.setattr(tab.fd, "asksaveasfilename", lambda **k: "")
    tab.ask_save_path(str(tmp_path), "aurora_wan.conn", got.append)
    assert got[-1] is None                                                # 취소
    tab.copy_text("hello", "복사됨")
    assert app.root.clipboard_get() == "hello"


def test_wan_switch_off_hides_everything_and_on_shows_it_again(app):
    tab = app._wan_tab
    app._select_tab(4)
    app.root.update()
    assert tab.body.winfo_ismapped() and tab.wan_var.get() is True
    tab.set_wan_enabled(False)
    app.root.update()
    assert not tab.body.winfo_ismapped() and tab.wan_off_lbl.winfo_ismapped() and tab.wan_var.get() is False
    tab.set_wan_enabled(True)
    app.root.update()
    assert tab.body.winfo_ismapped() and not tab.wan_off_lbl.winfo_ismapped()


def test_switch_click_reaches_runtime_and_stops_background_work(app, monkeypatch):
    import time
    tab = app._wan_tab
    calls = []
    monkeypatch.setattr(app._wan, "set_wan_enabled", lambda on: calls.append(on))
    tab.ctx["set_wan_enabled"] = app._wan.set_wan_enabled
    app._select_tab(4)
    tab.wan_var.set(False)
    tab._wan_toggled()
    for _ in range(20):
        app.root.update()
        time.sleep(0.02)
    assert calls == [False]


def test_new_install_starts_with_wan_off(tmp_path, monkeypatch):
    import winshim
    winshim.sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("AURORA_WAN_DIR", str(tmp_path / "fresh"))
    root = winshim.make_root()
    mod = winshim.load_client(real_remote=True)
    monkeypatch.setattr(mod.AuroraClientApp, "_start_threads", lambda self: None)
    mod._init_scale(root)
    mod.AuroraClientApp.setup_styles()
    application = mod.AuroraClientApp(root)
    try:
        application._select_tab(4)
        root.update()
        assert application._wan.wan_on() is False and not application._wan_tab.body.winfo_ismapped()
        assert application._wan_tab.wan_var.get() is False
    finally:
        application._wan_shutdown()
        winshim.destroy_root(root)


def test_relay_card_has_refresh_and_git_controls(app, monkeypatch):
    tab = app._wan_tab
    for name in ("git_var", "git_entry", "git_status_lbl", "relay_code_var"):
        assert hasattr(tab, name), name
    app._select_tab(4)
    tab._relay_expand(True)
    tab.set_relay_git("https://git.corp/a.git", "Git 연결됨", "ok")
    app.root.update()
    assert tab.git_var.get() == "https://git.corp/a.git" and not hasattr(tab, "rotate_var")      # 자동 갱신 체크는 없다
    assert tab.git_status_lbl.cget("text") == "Git 연결됨"
    calls = []
    monkeypatch.setattr(tab.controller, "relay_refresh_code", lambda: calls.append("refresh"))
    monkeypatch.setattr(tab.controller, "publish_git", lambda s: calls.append(("publish", s)))
    from wan import ui as wan_ui
    buttons = {w.cget("text"): w for w in _all_children(tab.relay_more) if w.winfo_class() == "Button"}
    assert {"새로고침", "올리기", "복사", "파일 저장"} <= set(buttons)
    buttons["새로고침"].invoke()
    buttons["올리기"].invoke()
    tab.git_entry.event_generate("<Return>")
    app.root.update()
    assert "refresh" in calls and ("publish", "https://git.corp/a.git") in calls


def test_connect_dialog_offers_git_off_button_only_when_git_is_set(app, monkeypatch):
    tab = app._wan_tab
    app._wan.cfg.update(relay_url="ws://h:1", git_source="")
    app._select_tab(4)
    tab._connect_dialog()
    d = tab._connect_win
    assert "Git 연동 끄기" not in [w.cget("text") for w in _all_children(d.btns) if w.winfo_class() == "Button"]
    d.win.destroy()
    app._wan.cfg.update(git_source="https://git.corp/a.git")
    tab._connect_dialog()
    d = tab._connect_win
    assert "Git 연동 끄기" in [w.cget("text") for w in _all_children(d.btns) if w.winfo_class() == "Button"]
    d.text.set("https://git.corp/b.git")
    d.win.update()
    assert "Git" in d.hint.cget("text")
    d.win.destroy()


def test_off_screen_shows_only_code_box_and_start_runs_full_setup(tmp_path, monkeypatch):
    import time
    import winshim
    from wan import connect_code
    winshim.sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("AURORA_WAN_DIR", str(tmp_path / "fresh"))
    root = winshim.make_root()
    mod = winshim.load_client(real_remote=True)
    monkeypatch.setattr(mod.AuroraClientApp, "_start_threads", lambda self: None)
    mod._init_scale(root)
    mod.AuroraClientApp.setup_styles()
    application = mod.AuroraClientApp(root)
    try:
        tab = application._wan_tab
        application._select_tab(4)
        root.update()
        assert tab.setup_box.winfo_ismapped() and not tab.body.winfo_ismapped()      # 꺼져 있으면 코드 입력칸만
        assert tab.setup_field_var.get() is True
        started = []
        monkeypatch.setattr(application._wan, "set_wan_enabled", lambda on: started.append(on))
        tab.ctx["set_wan_enabled"] = application._wan.set_wan_enabled
        monkeypatch.setattr(tab.controller, "add_connection", lambda *a, **k: started.append(("add", a)) or False)
        code = connect_code.encode(["wss://10.0.0.1:9100"], "k")
        tab.setup_var.set(code)
        tab._setup_start()
        end = time.time() + 5
        while len(started) < 2 and time.time() < end:                # 작업 스레드가 Tk 호출을 하려면 화면 스레드가 돌아야 함
            root.update()
            time.sleep(0.02)
        assert started[0] is True and started[1] == ("add", (code, "", True))
        tab.set_wan_enabled(True)
        root.update()
        assert tab.body.winfo_ismapped() and not tab.setup_box.winfo_ismapped()      # 켜지면 보통 화면으로
    finally:
        application._wan_shutdown()
        winshim.destroy_root(root)
