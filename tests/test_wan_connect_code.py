"""접속 정보(connect_code) · 자동 설정 · 접속 정보 붙여넣기 · 관리자 권한으로 다시 시작 시험"""

import socket
from unittest.mock import MagicMock

import pytest

from wan import connect_code as cc
from wan import ui as wui
from test_wan_relay_host import Rec, host, make_ctl  # noqa: F401  (host 는 pytest 도구)


# ── 접속 정보 글자 ──
def test_roundtrip_with_korean_key_and_several_urls():
    code = cc.encode(["ws://203.0.113.5:9100", "ws://192.168.0.140:9100"], "키-abc_123")
    assert code.startswith("AW1-") and " " not in code
    assert cc.decode(code) == (["ws://203.0.113.5:9100", "ws://192.168.0.140:9100"], "키-abc_123")


def test_decode_tolerates_whitespace_newlines_and_surrounding_text():
    code = cc.encode(["ws://h:9100"], "k")
    messy = f"접속 정보:\n  {code[:10]}\n{code[10:]}  \n"
    assert cc.decode(messy) == (["ws://h:9100"], "k")


@pytest.mark.parametrize("bad", ["", "hello", "AW1-", "AW1-!!!!", "AW1-" + "A" * 10,
                                 cc.PREFIX + "e30", cc.encode(["http://x"], "k")])
def test_decode_rejects_garbage_with_message(bad):
    with pytest.raises(ValueError) as e:
        cc.decode(bad)
    assert "접속 정보" in str(e.value)


def test_make_url_variants():
    assert cc.make_url("203.0.113.5", 9100) == "ws://203.0.113.5:9100"
    assert cc.make_url("relay.example.com:8000", 9100) == "ws://relay.example.com:8000"
    assert cc.make_url("wss://relay.example.com", 9100) == "wss://relay.example.com"


def test_new_key_is_long_and_unique():
    a, b = cc.new_key(), cc.new_key()
    assert a != b and len(a) >= 16


def test_pick_reachable_prefers_first_that_connects():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        good = f"ws://127.0.0.1:{srv.getsockname()[1]}"
        with socket.socket() as tmp:
            tmp.bind(("127.0.0.1", 0))
            dead = f"ws://127.0.0.1:{tmp.getsockname()[1]}"     # 닫힌 포트
        assert cc.pick_reachable([dead, good], timeout=0.5) == good
        assert cc.pick_reachable([dead], timeout=0.5) == dead   # 하나도 안 닿으면 첫 번째


# ── 자동 설정 ──
def with_ctx(monkeypatch, admin=True, lan=("192.168.0.140",), firewall=(True, "열었습니다 9100"), **cfg):
    monkeypatch.setattr(wui, "is_admin", lambda: admin)
    monkeypatch.setattr(wui, "lan_addresses", lambda: list(lan))
    monkeypatch.setattr(wui, "open_firewall", lambda port: firewall)
    ctl, ctx, ui = make_ctl(**cfg)
    ctx["client"] = MagicMock()
    ctl.client = ctx["client"]
    return ctl, ctx, ui


def test_auto_setup_generates_key_starts_relay_and_builds_code(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch)
    assert ctl.relay_auto_setup("") is True
    key = ctx["cfg"].get("relay_key")
    assert len(key) >= 16
    ctx["set_relay_host"].assert_called_once_with(True)
    urls, k = cc.decode(ui.last("set_relay_code")[0])
    assert urls == ["ws://192.168.0.140:9100"] and k == key
    assert ui.last("set_server_fields") == ("", key)
    assert ui.last("set_relay_enabled") == (True,)
    ctx["client"].stop.assert_called_once()                              # 다른 안내 서버에 붙어 있었다면 끊음


def test_auto_setup_keeps_existing_key_and_puts_external_address_first(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch, relay_key="내가정한키", relay_port=9200)
    ctl.relay_auto_setup(" 203.0.113.5 ")
    urls, k = cc.decode(ui.last("set_relay_code")[0])
    assert k == "내가정한키" and urls == ["ws://203.0.113.5:9200", "ws://192.168.0.140:9200"]


def test_auto_setup_start_failure_reports_and_reverts(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch)
    ctx["set_relay_host"].side_effect = RuntimeError("boom")
    ctl.relay_auto_setup("")
    assert ctx["cfg"].get("relay_host") is False
    assert "boom" in ui.last("show_error")[1]


def test_auto_setup_unavailable_without_relay_support(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch)
    ctx["relay_available"] = False
    assert ctl.relay_auto_setup("") is False and ui.last("show_error")


# ── 관리자 권한 ──
def test_not_admin_asks_and_relaunches_with_autosetup_flag(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch, admin=False)
    ctx["relaunch_admin"] = MagicMock(return_value=(True, "관리자 권한으로 다시 시작합니다"))
    answers = []
    ui.ask_yes_no = lambda t, m, cb: (answers.append(m), cb(True))          # 사용자가 '예'
    assert ctl.relay_auto_setup("") is True
    ctx["relaunch_admin"].assert_called_once_with(["--wan-autosetup"])
    ctx["set_relay_host"].assert_not_called()                              # 다시 시작할 것이므로 지금은 시작하지 않음
    assert answers and "관리자" in answers[0]


def test_not_admin_user_says_no_continues_without_restart(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch, admin=False, firewall=(False, "방화벽 실패\n명령"))
    ctx["relaunch_admin"] = MagicMock()
    ui.ask_yes_no = lambda t, m, cb: cb(False)
    ctl.relay_auto_setup("")
    ctx["relaunch_admin"].assert_not_called()
    ctx["set_relay_host"].assert_called_once_with(True)                    # 방화벽은 직접 열게 하고 나머지는 진행


def test_relaunch_failure_is_shown_and_returns_false(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch, admin=False)
    ctx["relaunch_admin"] = MagicMock(return_value=(False, "UAC 취소"))
    assert ctl.relaunch_admin(["--x"]) is False
    assert ui.last("show_error") == ("관리자 권한", "UAC 취소")


def test_admin_user_is_not_asked(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch, admin=True)
    ctx["relaunch_admin"] = MagicMock()
    ui.ask_yes_no = MagicMock()
    ctl.relay_auto_setup("")
    ui.ask_yes_no.assert_not_called()
    ctx["relaunch_admin"].assert_not_called()


# ── 접속 정보 붙여넣기 ──
def test_import_as_user_sets_address_key_and_connects(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch)
    ctx["client"].start = MagicMock()
    monkeypatch.setattr(cc, "pick_reachable", lambda urls, timeout=2.0: urls[1])
    code = cc.encode(["ws://203.0.113.5:9100", "ws://192.168.0.140:9100"], "k1")
    assert ctl.import_connection(code) is True
    assert ctx["cfg"].get("relay_url") == "ws://192.168.0.140:9100" and ctx["cfg"].get("relay_key") == "k1"
    assert ui.last("set_server_fields") == ("ws://192.168.0.140:9100", "k1")
    ctx["client"].start.assert_called_once()


def test_import_as_field_requires_code_then_turns_on_field_mode(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch)
    monkeypatch.setattr(cc, "pick_reachable", lambda urls, timeout=2.0: urls[0])
    ctx["cfg"].d.update(enabled=False, access_code="", name="")
    ctx["set_field_mode"] = MagicMock()
    code = cc.encode(["ws://h:9100"], "k")
    assert ctl.import_connection(code, as_field=True, name="CCW 1", code="") is False
    assert "접속 코드" in ui.last("show_error")[1]
    assert ctl.import_connection(code, as_field=True, name="CCW 1", code="4321") is True
    assert ctx["cfg"].get("access_code") == "4321" and ctx["cfg"].get("name") == "CCW 1"
    ctx["set_field_mode"].assert_called_once_with(True)


def test_import_garbage_shows_error_and_changes_nothing(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch, relay_url="ws://keep:1")
    assert ctl.import_connection("엉터리") is False
    assert ctx["cfg"].get("relay_url") == "ws://keep:1" and ui.last("show_error")


# ── 도우미 ──
def test_is_admin_true_off_windows_and_relaunch_unsupported():
    from wan import netutil
    import os
    if os.name != "nt":
        assert netutil.is_admin() is True
        assert netutil.relaunch_as_admin("x", [])[0] is False


# ── 끝에서 끝까지: 중계 PC 가 자동 설정 → 다른 PC 가 접속 정보를 붙여넣어 실제로 연결 ──
def test_auto_setup_then_paste_connects_for_real(host, tmp_path, monkeypatch):
    from test_wan_relay_host import health
    from wan_helpers import eventually
    monkeypatch.setattr(wui, "lan_addresses", lambda: ["127.0.0.1"])
    monkeypatch.setattr(wui, "open_firewall", lambda port: (True, "열었습니다"))
    relay_pc = host()
    relay_pc.cfg.update(relay_key="")                                     # 키 없이 시작 → 자동으로 만들어져야 함
    ctl1 = wui.WanTabController(relay_pc.tab_context(lambda *a: None, relaunch_admin=None), lambda f: f(),
                                ui=Rec(), spawn=lambda f: f())
    ui1 = ctl1.ui
    ctl1.relay_auto_setup("")
    eventually(lambda: health(relay_pc.cfg.get("relay_port")), 15, msg="자동 설정으로 안내 서버가 떠야 함")
    code = ui1.last("set_relay_code")[0]
    assert code.startswith("AW1-")

    import wan.runtime as rt
    other = rt.WanRuntime("2.16.0", lambda: ["x"], None, str(tmp_path / "other"), lambda m: None)
    try:
        ui2 = Rec()
        ctl2 = wui.WanTabController(other.tab_context(lambda *a: None), lambda f: f(), ui=ui2, spawn=lambda f: f())
        assert ctl2.import_connection(code) is True
        eventually(lambda: other.client.state == "연결됨", 15, msg=f"붙여넣기만으로 연결되어야 함: {other.client.state}")
        assert other.cfg.get("relay_key") == relay_pc.cfg.get("relay_key") != ""
    finally:
        other.shutdown()
