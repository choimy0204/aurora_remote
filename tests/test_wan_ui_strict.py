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
    assert {"적용 후 연결", "원격 연결", "설정 보내기"} <= set(app.buttons)


def test_tab_builds_for_empty_config(strict):
    build(strict, cfg=FakeCfg(relay_url="", relay_key="", enabled=False),
          supervisor_state=lambda: {"state": "stopped", "pid": None, "next_restart_in": None})


def test_tab_buttons_do_not_crash_without_selection(strict):
    tab, app, _ = build(strict)
    app.buttons["원격 연결"]()
    app.buttons["설정 보내기"]()
    app.buttons["적용 후 연결"]()


def test_tab_log_and_stop(strict):
    tab, app, _ = build(strict)
    tab.log("테스트 메시지")
    tab.stop()
