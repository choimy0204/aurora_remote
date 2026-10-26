"""WAN 탭을 엄격한 가짜 tkinter 로 만들어 본다 — pack/grid 혼용·없어진 위젯·잘못된 옵션 이름 같은 실수를 잡는다"""

from unittest.mock import MagicMock

import pytest

import strict_tk
from test_wan_ui import FakeCfg, FakeClient, FakeUsage


class StrictApp:
    """AuroraClientApp 의 꾸미기 도구 모양 (실제로는 위젯을 만들어 돌려줌)"""
    BG = PANEL = TEXT = TEXT_DIM = BLUE = GREEN = RED = YELLOW = BORDER = INPUT_BORDER = SELECT_BG = LOG_BG = "#123456"
    FONT = FONT_BOLD = FONT_SMALL = FONT_INPUT = FONT_HEAD = ("f", 9)
    FONT_MONO_SMALL = ("m", 8)

    def __init__(self, tk):
        self.tk = tk
        self.root = tk.Toplevel()
        self.buttons = {}

    def _card(self, parent):
        return self.tk.Frame(parent, bg=self.PANEL, highlightthickness=1)

    def _section_title(self, parent, text, bg=None):
        return self.tk.Label(parent, text=text, bg=bg or self.PANEL)

    def _field_label(self, parent, text, width=12):
        return self.tk.Label(parent, text=text, width=width)

    def _entry(self, parent, var, width, font=None):
        return self.tk.Entry(parent, textvariable=var, width=width)

    def _button(self, parent, text, kind, cmd, small=False):
        b = self.tk.Button(parent, text=text, command=cmd)
        self.buttons[text] = cmd
        return b

    def _tip(self, widget, text):
        return widget


@pytest.fixture
def strict(monkeypatch):
    return strict_tk.install(monkeypatch)


def build(strict, **ctx_over):
    import wan.ui as wui
    app = StrictApp(strict)
    parent = strict.Frame(app.root)
    ctx = {"cfg": FakeCfg(relay_url="ws://h:1", relay_key="k", enabled=True), "client": FakeClient(),
           "usage": FakeUsage(), "supervisor_state": lambda: {"state": "running", "pid": 9, "next_restart_in": None},
           "set_field_mode": MagicMock(), "open_remote": MagicMock(), "px": lambda v: v, "log": MagicMock()}
    ctx.update(ctx_over)
    return wui.build_tab(app, parent, ctx), app, parent


def test_strict_fake_really_rejects_mixed_geometry_managers(strict):
    parent = strict.Frame()
    strict.Label(parent).pack()
    with pytest.raises(strict_tk.TclError, match="geometry manager"):
        strict.Label(parent).grid()


def test_strict_fake_rejects_unknown_option(strict):
    with pytest.raises(strict_tk.TclError, match="unknown option"):
        strict.Label(strict.Frame(), txet="오타")


def test_tab_builds_without_geometry_errors(strict):
    tab, app, parent = build(strict)
    assert len(strict_tk.Widget.all_widgets) > 20
    assert {"+ 연결 추가", "원격 연결", "설정 보내기"} <= set(app.buttons)


def test_tab_builds_for_empty_config(strict):
    build(strict, cfg=FakeCfg(relay_url="", relay_key="", enabled=False),
          supervisor_state=lambda: {"state": "stopped", "pid": None, "next_restart_in": None})


def test_tab_buttons_do_not_crash_without_selection(strict):
    tab, app, _ = build(strict)
    app.buttons["원격 연결"]()
    app.buttons["설정 보내기"]()


def test_tab_log_and_stop(strict):
    tab, app, _ = build(strict)
    tab.log("테스트 메시지")
    tab.stop()


RELAY_CTX = dict(relay_available=True, relay_state=lambda: {"state": "running", "pid": 5},
                 set_relay_host=MagicMock(), restart_relay=MagicMock())


def test_tab_builds_with_relay_card_and_dialogs(strict):
    tab, app, _ = build(strict, **RELAY_CTX)
    assert {"자동 설정", "복사", "파일 저장", "방화벽 열기", "관리자로 다시 시작"} <= set(app.buttons)
    tab._relay_expand(False)
    tab._relay_expand(True)
    tab.set_admin(False)
    tab.set_admin(True)
    tab._connect_dialog()                                   # 연결 추가 창의 pack/before 가 엄격한 가짜에서도 통과
    tab._connect_win.text.set("192.168.0.5")
    tab._connect_win.text.set("AW1-abc")
    tab._connect_win.text.set("")
    tab._config_dialog()                                    # 선택이 없으면 안내만


def test_tab_field_card_toggles_without_errors(strict):
    tab, app, _ = build(strict, cfg=FakeCfg(relay_url="", relay_key="k", enabled=False))
    tab.enabled_var.set(True)
    tab._field_layout()
    tab.set_field_address("192.168.0.5:9102")
    tab.set_field_code("abc")
    tab.set_field_info("A1B2", "PC", "")
    tab.set_field_enabled(False)
    tab.set_rows([("a1", ("PC", "중계", "켜짐", "지금")), ("d:1.2.3.4:9102", ("현장", "주소", "꺼짐", "어제"))])
