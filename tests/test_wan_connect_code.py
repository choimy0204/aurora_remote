"""접속 정보(connect_code) · 자동 설정 · 접속 정보 붙여넣기 · 관리자 권한으로 다시 시작 시험"""

import socket
from unittest.mock import MagicMock

import pytest
from wan_helpers import patch_ui

from wan import connect_code as cc
from wan.config import DEFAULT_RELAY_KEY, WanConfig
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
    patch_ui(monkeypatch, "is_admin", lambda: admin)
    patch_ui(monkeypatch, "lan_addresses", lambda: list(lan))
    patch_ui(monkeypatch, "open_firewall", lambda port: firewall)
    ctl, ctx, ui = make_ctl(**cfg)
    ctx["client"] = MagicMock()
    ctl.client = ctx["client"]
    return ctl, ctx, ui


def test_auto_setup_uses_default_key_starts_relay_and_builds_code(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch)
    assert ctl.relay_auto_setup("") is True
    key = ctx["cfg"].get("relay_key")
    assert key == DEFAULT_RELAY_KEY                                       # 키를 따로 안 정했으면 기본 공용 키
    ctx["set_relay_host"].assert_called_once_with(True)
    urls, k = cc.decode(ui.last("set_relay_code")[0])
    assert urls == ["ws://192.168.0.140:9100"] and k == key
    assert ctx["cfg"].get("relay_url") == ""                              # 이 PC 는 중계 PC 라 접속할 안내 서버가 없음
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
    ctx["client"].start.assert_called_once()


def test_import_as_field_needs_no_code_and_defaults_name(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch)
    monkeypatch.setattr(cc, "pick_reachable", lambda urls, timeout=2.0: urls[0])
    monkeypatch.setattr(wui.socket, "gethostname", lambda: "PC-FIELD-7")
    ctx["cfg"].d.update(enabled=False, access_code="", name="")
    ctx["set_field_mode"] = MagicMock()
    code = cc.encode(["ws://h:9100"], "k")
    assert ctl.import_connection(code, as_field=True) is True              # 접속 코드·이름 없이도 됨
    assert ctx["cfg"].get("access_code") == "" and ctx["cfg"].get("name") == "PC-FIELD-7"
    ctx["set_field_mode"].assert_called_once_with(True)
    assert ctx["cfg"].get("relay_key") == "k"                              # 이 공용 키가 곧 접속 코드


def test_import_as_field_keeps_name_and_sets_code_when_given(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch)
    monkeypatch.setattr(cc, "pick_reachable", lambda urls, timeout=2.0: urls[0])
    ctx["cfg"].d.update(enabled=False, access_code="", name="")
    ctx["set_field_mode"] = MagicMock()
    assert ctl.import_connection(cc.encode(["ws://h:9100"], "k"), as_field=True, name="CCW 1", code="4321") is True
    assert ctx["cfg"].get("access_code") == "4321" and ctx["cfg"].get("name") == "CCW 1"


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
    patch_ui(monkeypatch, "lan_addresses", lambda: ["127.0.0.1"])
    patch_ui(monkeypatch, "open_firewall", lambda port: (True, "열었습니다"))
    relay_pc = host()
    relay_pc.cfg.update(relay_key="")                                     # 키 없이 시작 → 기본 공용 키가 들어가야 함
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
        assert other.cfg.get("relay_key") == relay_pc.cfg.get("relay_key") == DEFAULT_RELAY_KEY
    finally:
        other.shutdown()


# ── 더블클릭 연결 (접속 코드를 묻지 않음) ──
from wan.protocol import WanDenied


def conn_ctl(monkeypatch, **cfg):
    ctl, ctx, ui = with_ctx(monkeypatch, relay_key="공용키", **cfg)
    ctx["cfg"].d.setdefault("saved_codes", {})
    ctl.client.agents = [{"id": "a1", "name": "현장1", "online": True}]
    ctl.agent = lambda aid: {"id": aid, "name": "현장1"} if aid == "a1" else None
    ctx["open_remote"] = MagicMock()
    ctl.ctx = ctx
    return ctl, ctx, ui


def test_double_click_uses_shared_key_without_asking(monkeypatch):
    ctl, ctx, ui = conn_ctl(monkeypatch)
    ctl.client.open_session = MagicMock(return_value=MagicMock(mode="direct"))
    assert ctl.quick_connect("a1") is True
    ctl.client.open_session.assert_called_once_with("a1", "공용키", "현장1")
    assert not [c for c in ui.calls if c[0] in ("ask_code", "show_error")]
    ctx["open_remote"].assert_called_once()
    assert ctx["cfg"].get("saved_codes") == {}                              # 공용 키는 따로 저장하지 않음


def test_wrong_key_asks_once_then_remembers_typed_code(monkeypatch):
    ctl, ctx, ui = conn_ctl(monkeypatch)
    calls = []

    def open_session(aid, code, name):
        calls.append(code)
        if code != "내코드":
            raise WanDenied("접속 코드가 맞지 않습니다")
        return MagicMock(mode="relay")
    ctl.client.open_session = open_session
    asked = []
    ui.ask_code = lambda aid, label, why, cb: (asked.append((label, why)), cb("내코드"))
    ctl.quick_connect("a1")
    assert calls == ["공용키", "내코드"] and len(asked) == 1 and "따로" in asked[0][1]
    assert ctx["cfg"].get("saved_codes") == {"a1": "내코드"}
    calls.clear(); asked.clear()
    ctl._codes.clear()
    ctl.quick_connect("a1")                                                 # 다음 더블클릭은 묻지 않고 기억한 코드로
    assert calls == ["내코드"] and not asked


def test_saved_code_that_stops_working_is_forgotten_and_asked_again(monkeypatch):
    ctl, ctx, ui = conn_ctl(monkeypatch)
    ctx["cfg"].d["saved_codes"] = {"a1": "옛코드"}
    ctl.client.open_session = MagicMock(side_effect=WanDenied("접속 코드가 맞지 않습니다"))
    asked = []
    ui.ask_code = lambda aid, label, why, cb: (asked.append(1), cb(None))   # 사용자가 취소
    ctl.quick_connect("a1")
    assert ctx["cfg"].get("saved_codes") == {} and asked == [1]


def test_locked_or_offline_is_an_error_not_a_prompt(monkeypatch):
    ctl, ctx, ui = conn_ctl(monkeypatch)
    ctl.client.open_session = MagicMock(side_effect=WanDenied("접속 코드를 여러 번 틀려 잠시 막혔습니다"))
    ui.ask_code = MagicMock()
    ctl.quick_connect("a1")
    ui.ask_code.assert_not_called()
    assert ui.last("show_error")


def test_no_key_and_no_saved_code_asks_directly(monkeypatch):
    ctl, ctx, ui = conn_ctl(monkeypatch)
    ctx["cfg"].d["relay_key"] = ""
    asked = []
    ui.ask_code = lambda aid, label, why, cb: asked.append(label)
    ctl.quick_connect("a1")
    assert asked == ["현장1"]


def test_field_info_text_for_key_based_access(monkeypatch):
    ctl, ctx, _ = conn_ctl(monkeypatch)
    assert ctl.field_info()[2] == ""                                       # 공용 키로 접속 가능 — 경고 없음
    assert ctl.field_code() == "공용키"
    ctx["cfg"].d["access_code"] = "x"
    assert ctl.field_info()[2] == "" and ctl.field_code() == "x"
    ctx["cfg"].d.update(access_code="", relay_key="")
    assert "거부" in ctl.field_info()[2]


# ── 같은 망 자동 찾기 ──
def test_discovery_roundtrip_on_loopback():
    import threading
    from wan import discover
    with socket.socket(socket.SOCK_DGRAM and socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    stop = threading.Event()
    code = cc.encode(["ws://192.168.0.5:9100"], "키")
    t = threading.Thread(target=discover.serve, args=(lambda: code, stop, port), daemon=True)
    t.start()
    try:
        import time
        time.sleep(0.3)
        got = discover.find_relay(timeout=3, port=port, targets=[("127.0.0.1", port)])
        assert got == code and cc.decode(got) == (["ws://192.168.0.5:9100"], "키")
        assert discover.find_relay(timeout=0.5, port=port, targets=[("127.0.0.1", port + 1)]) is None
    finally:
        stop.set()
        t.join(3)


def test_discovery_serve_port_in_use_does_not_crash():
    import threading
    from wan import discover
    msgs = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
        blocker.bind(("0.0.0.0", 0))
        port = blocker.getsockname()[1]
        # SO_REUSEADDR 로 열리는 환경이면 조용히 같이 열리고, 아니면 로그만 남기고 끝난다 — 어느 쪽이든 예외가 새지 않아야 함
        stop = threading.Event()
        stop.set()
        discover.serve(lambda: "x", stop, port, msgs.append)


def test_find_on_lan_fills_dialog_or_reports(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch)
    from wan import discover
    monkeypatch.setattr(discover, "find_relay", lambda *a, **k: "AW1-abc")
    ctl.find_on_lan()
    assert ui.last("set_found_code") == ("AW1-abc",)
    monkeypatch.setattr(discover, "find_relay", lambda *a, **k: None)
    ui.calls.clear()
    ctl.find_on_lan()
    assert ui.last("set_found_code") == ("",)                              # 못 찾았다고 창에 알림


def test_firewall_commands_include_discovery_udp():
    from wan import netutil
    cmds = netutil.firewall_commands(9100)
    assert "protocol=TCP" in cmds[0] and "localport=9100" in cmds[0]
    assert "protocol=UDP" in cmds[1] and "localport=9101" in cmds[1]


def test_agent_accepts_shared_key_only_when_no_access_code():
    from wan.agent import WanAgent
    class C:
        def __init__(self, **d): self.d = d
        def get(self, k, default=None): return self.d.get(k, default)
    mk = lambda **d: WanAgent(C(**d), 1, lambda: "", "1", lambda m: None)
    a = mk(access_code="", relay_key="공용키")
    assert a.check_code("공용키") is None
    assert mk(access_code="", relay_key="공용키").check_code("틀림") == "bad_code"
    assert mk(access_code="4321", relay_key="공용키").check_code("공용키") == "bad_code"   # 코드를 정했으면 키로는 안 열림
    assert mk(access_code="4321", relay_key="공용키").check_code("4321") is None
    assert mk(access_code="", relay_key="").check_code("") == "no_code"                  # 둘 다 없으면 모두 거부


# ── 기본 공용 키 ──
def test_fresh_config_starts_with_default_key(tmp_path):
    assert WanConfig(str(tmp_path / "new")).get("relay_key") == DEFAULT_RELAY_KEY


def test_existing_empty_key_is_not_overwritten(tmp_path):
    cfg = WanConfig(str(tmp_path / "old"))
    cfg.update(relay_key="")
    assert WanConfig(str(tmp_path / "old")).get("relay_key") == ""         # 이미 저장된 설정은 그대로


def test_user_chosen_key_is_kept_by_auto_setup(monkeypatch):
    ctl, ctx, ui = with_ctx(monkeypatch, relay_key="내키")
    ctl.relay_auto_setup("")
    assert ctx["cfg"].get("relay_key") == "내키"


def test_default_key_warns_only_when_exposed_to_internet(monkeypatch):
    ctl, ctx, _ = with_ctx(monkeypatch, relay_key=DEFAULT_RELAY_KEY)
    assert ctl.relay_info()[2] == ""                                       # 같은 망만 쓰면 경고 없음
    ctx["cfg"].d["relay_external"] = "203.0.113.5"
    assert "기본 공용 키" in ctl.relay_info()[2]                             # 외부 주소를 쓰면 경고
    ctx["cfg"].d["relay_key"] = "내가 바꾼 키"
    assert ctl.relay_info()[2] == ""
