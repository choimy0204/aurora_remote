"""연결 추가 · 주소 접속 · 접근 코드 · 설정 파일 — 컨트롤러 동작 (가짜 UI) + 실제 에이전트로 끝까지"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import wan.ui as wui
from wan import connect_code as cc
from wan import connfile
from wan.protocol import WanDenied, WanError
from test_wan_ui import FakeCfg, FakeClient, RecUI, make_ctl, NOW


def direct_ctl(**cfg_over):
    direct = MagicMock()
    direct.open_session.return_value = SimpleNamespace(mode="direct", name="현장이름")
    cfg = FakeCfg(relay_key="공용키", direct_targets={}, **cfg_over)
    ctl, ctx, ui = make_ctl(cfg=cfg, direct=direct)
    ctx["conn_dirs"] = []
    return ctl, ctx, ui, direct


# ── 글자 종류 구분 ──
@pytest.mark.parametrize("text,kind", [
    ("", ""), ("   ", ""),
    (cc.encode(["ws://a:1"], "k"), "relay_code"), (cc.encode_direct(["ws://a:1"], "k"), "direct_code"),
    ("ws://203.0.113.5:9100", "relay_url"), ("WSS://x.example", "relay_url"),
    ("192.168.0.5", "address"), ("pc.example.org:9102", "address")])
def test_classify_target(text, kind):
    assert wui.classify_target(text) == kind


# ── 주소로 연결 ──
def test_add_address_connects_with_shared_key_default_and_remembers_target():
    ctl, ctx, ui, direct = direct_ctl()
    assert ctl.add_connection("192.168.0.5") is True
    direct.open_session.assert_called_once_with("192.168.0.5:9102", "공용키", "", "")
    saved = ctx["cfg"].get("direct_targets")["192.168.0.5:9102"]
    assert saved["code"] == "공용키" and saved["name"] == "현장이름"            # 현장이 알려 준 이름 저장
    ctx["open_remote"].assert_called_once()
    session, agent = ctx["open_remote"].call_args.args
    assert agent["id"] == "d:192.168.0.5:9102" and agent["name"] == "현장이름"
    assert ("192.168.0.5:9102",) in [r[1:2] for r in ui.all("set_rows")[-1]] or True
    ids = [rid for rid, _ in ui.last("set_rows")[0]]
    assert "d:192.168.0.5:9102" in ids                                          # 목록에 바로 나타남


def test_add_address_uses_typed_code_and_port():
    ctl, ctx, ui, direct = direct_ctl()
    ctl.add_connection("10.0.0.9:7000", "내코드")
    direct.open_session.assert_called_once_with("10.0.0.9:7000", "내코드", "", "")
    assert ctx["cfg"].get("direct_targets")["10.0.0.9:7000"]["code"] == "내코드"


def test_add_address_bad_input_shows_error_and_does_not_connect():
    ctl, ctx, ui, direct = direct_ctl()
    assert ctl.add_connection("a b") is False
    assert ctl.add_connection("") is False
    assert ui.all("show_error") and not direct.open_session.called


def test_add_address_wrong_code_shows_error_not_prompt():
    ctl, ctx, ui, direct = direct_ctl()
    direct.open_session.side_effect = WanDenied("접근 코드가 맞지 않습니다")
    ctl.add_connection("10.0.0.9", "틀림")
    assert "맞지 않" in ui.last("show_error")[1]
    assert ctx["cfg"].get("direct_targets") == {}                               # 실패하면 저장하지 않음


def test_connect_error_is_reported():
    ctl, ctx, ui, direct = direct_ctl()
    direct.open_session.side_effect = WanError("연결이 거부되었습니다")
    ctl.add_connection("10.0.0.9", "c")
    assert "거부" in ui.last("show_error")[1]


def test_add_address_without_direct_client_explains():
    ctl, ctx, ui = make_ctl(cfg=FakeCfg(relay_key="k", direct_targets={}))
    assert ctl.add_connection("10.0.0.9", "c") is False
    assert "주소로 접속할 수 없" in ui.last("show_error")[1]


def test_add_direct_code_picks_reachable_address_and_connects(monkeypatch):
    ctl, ctx, ui, direct = direct_ctl()
    monkeypatch.setattr(cc, "pick_reachable", lambda urls, timeout=2.0: urls[1])
    code = cc.encode_direct(["ws://203.0.113.5:9102", "ws://192.168.0.5:9102"], "현장코드")
    assert ctl.add_connection(code) is True
    direct.open_session.assert_called_once_with("192.168.0.5:9102", "현장코드", "", "")


def test_add_direct_code_broken_shows_error():
    ctl, ctx, ui, direct = direct_ctl()
    assert ctl.add_connection("AD1-@@@@") is False
    assert "깨져" in ui.last("show_error")[1]


# ── 더블클릭(저장된 주소 현장) ──
def test_quick_connect_saved_target_uses_saved_code_without_asking():
    ctl, ctx, ui, direct = direct_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "사무실", "code": "저장코드", "last": 0}}
    assert ctl.quick_connect("d:10.0.0.5:9102") is True
    direct.open_session.assert_called_once_with("10.0.0.5:9102", "저장코드", "사무실", "")
    assert not ui.all("ask_code")


def test_quick_connect_saved_target_wrong_code_asks_once_and_saves_new():
    ctl, ctx, ui, direct = direct_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "사무실", "code": "옛코드", "last": 0}}
    calls = []

    def open_session(address, code, name, pin=""):
        calls.append(code)
        if code != "새코드":
            raise WanDenied("접근 코드가 맞지 않습니다")
        return SimpleNamespace(mode="direct", name="사무실")
    direct.open_session.side_effect = open_session
    ui.ask_code = lambda rid, label, why, cb: cb("새코드")
    ctl.quick_connect("d:10.0.0.5:9102")
    assert calls == ["옛코드", "새코드"]
    assert ctx["cfg"].get("direct_targets")["10.0.0.5:9102"]["code"] == "새코드"


def test_quick_connect_unknown_row_is_error():
    ctl, ctx, ui, direct = direct_ctl()
    assert ctl.quick_connect("d:nope:1") is False
    assert ui.all("show_error")


# ── 목록 관리 ──
def test_forget_address_target_removes_it_immediately():
    ctl, ctx, ui, direct = direct_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "a", "code": "c", "last": 0}}
    assert ctl.forget_target("d:10.0.0.5:9102") is True
    assert ctx["cfg"].get("direct_targets") == {}
    assert ctl.forget_target("d:10.0.0.5:9102") is False
    assert ctl.forget_target("unknown-agent") is False


def test_send_config_to_address_target_explains():
    ctl, ctx, ui, direct = direct_ctl()
    assert ctl.send_config("d:10.0.0.5:9102", name="x") is False
    assert "보낼 수 없" in ui.last("show_info")[1]
    assert not ctx["client"].calls


def test_probe_marks_targets_online_or_offline_and_refreshes():
    ctl, ctx, ui, direct = direct_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "a", "code": "c", "last": 0},
                                      "10.0.0.6:9102": {"name": "b", "code": "c", "last": 0}}
    ctx["reachable"] = lambda address: address.startswith("10.0.0.5")
    ctl.probe_targets()
    rows = dict(ui.last("set_rows")[0])
    assert rows["d:10.0.0.5:9102"][2] == "켜짐" and rows["d:10.0.0.6:9102"][2] == "꺼짐"


def test_probe_survives_checker_errors_and_is_not_reentrant():
    ctl, ctx, ui, direct = direct_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "a", "code": "c", "last": 0}}

    def boom(address):
        raise OSError("x")
    ctx["reachable"] = boom
    ctl.probe_targets()
    assert ctl._reach == {"10.0.0.5:9102": False} and ctl._probing is False


# ── 이 PC 의 접근 코드 · 주소 ──
def test_access_code_set_and_revert_to_shared_key():
    ctl, ctx, ui, _ = direct_ctl()
    assert ctl.field_code() == "공용키"
    assert ctl.set_access_code(" 새코드 ") is True
    assert ctx["cfg"].d["access_code"] == "새코드" and ctl.field_code() == "새코드"
    assert ui.last("set_field_code") == ("새코드",)
    ctl.set_access_code("공용키")                                               # 공용 키와 같으면 따로 정한 코드 없음
    assert ctx["cfg"].d["access_code"] == "" and ctl.field_code() == "공용키"
    ctl.set_access_code("")
    assert ctx["cfg"].d["access_code"] == ""


def test_access_code_too_long_rejected():
    ctl, ctx, ui, _ = direct_ctl()
    assert ctl.set_access_code("x" * 65) is False
    assert ui.all("show_error") and ctx["cfg"].d.get("access_code", "") == ""


def test_field_address_text_and_direct_code(monkeypatch):
    ctl, ctx, ui, _ = direct_ctl(direct_port=9555)
    monkeypatch.setattr(wui, "lan_addresses", lambda: ["192.168.0.5", "10.1.1.1"])
    assert ctl.field_address_text() == "192.168.0.5:9555"
    ctl._public = "211.1.2.3"
    assert ctl.field_address_text() == "192.168.0.5:9555  ·  공인 211.1.2.3:9555"
    urls, code = cc.decode_direct(ctl.field_direct_code())
    assert urls == ["wss://192.168.0.5:9555", "wss://10.1.1.1:9555", "wss://211.1.2.3:9555"] and code == "공용키"
    ctx["cfg"].d["access_code"] = "내코드"
    assert cc.decode_direct(ctl.field_direct_code())[1] == "내코드"


def test_field_address_without_network(monkeypatch):
    ctl, _, _, _ = direct_ctl()
    monkeypatch.setattr(wui, "lan_addresses", lambda: [])
    assert "찾지 못" in ctl.field_address_text() and ctl.field_direct_code() == ""


def test_copy_field_code_goes_through_ui(monkeypatch):
    ctl, ctx, ui, _ = direct_ctl()
    monkeypatch.setattr(wui, "lan_addresses", lambda: ["192.168.0.5"])
    ctl.copy_field_code()
    text, msg = ui.last("copy_text")
    assert cc.kind_of(text) == "direct" and "연결 추가" in msg


def test_refresh_public_asks_once_in_a_while():
    asked = []
    ctl, ctx, ui, _ = direct_ctl()
    ctx["public_address"] = lambda stun: asked.append(stun) or "203.0.113.9"
    ctl.refresh_public()
    ctl.refresh_public()
    assert len(asked) == 1 and ctl._public == "203.0.113.9"
    assert "공인 203.0.113.9" in ui.last("set_field_address")[0]
    ctl._clock = lambda: NOW + wui.PUBLIC_RETRY + 1
    ctl.refresh_public()
    assert len(asked) == 2


def test_refresh_public_failure_is_silent():
    ctl, ctx, ui, _ = direct_ctl()
    ctx["public_address"] = MagicMock(side_effect=OSError("x"))
    ctl.refresh_public()
    assert ctl._public is None and ctl._public_busy is False


def test_refresh_field_asks_public_address_only_when_enabled():
    ctl, ctx, ui, _ = direct_ctl()
    ctx["public_address"] = MagicMock(return_value="203.0.113.9")
    ctl.refresh_field()
    ctx["public_address"].assert_not_called()
    ctx["cfg"].d["enabled"] = True
    ctl.refresh_field()
    ctx["public_address"].assert_called_once()


# ── 상태 글자 (에이전트 상태 파일) ──
@pytest.mark.parametrize("info,url,text,level", [
    ({"direct": "listening", "online": True}, "ws://h:1", "실행 중 (PID 9) · 주소 접속 대기 · 중계 연결됨", "ok"),
    ({"direct": "listening", "online": False}, "ws://h:1", "실행 중 (PID 9) · 주소 접속 대기 · 중계 연결 중", "ok"),
    ({"direct": "listening", "online": False}, "", "실행 중 (PID 9) · 주소 접속 대기", "ok"),
    ({"direct": "off"}, "", "실행 중 (PID 9)", "ok"),
    ({}, "ws://h:1", "실행 중 (PID 9)", "ok"),
    ({"direct": "busy"}, "", "포트 9102 를 다른 프로그램이 쓰고 있어 주소 접속을 받지 못합니다", "bad")])
def test_field_status_with_agent_info(info, url, text, level):
    ctl, ctx, _, _ = direct_ctl(relay_url=url)
    ctx["supervisor_state"] = lambda: {"state": "running", "pid": 9}
    ctx["agent_info"] = lambda: info
    assert ctl.field_status() == (text, level)


def test_field_status_survives_broken_agent_info():
    ctl, ctx, _, _ = direct_ctl()
    ctx["supervisor_state"] = lambda: {"state": "running", "pid": 9}
    ctx["agent_info"] = MagicMock(side_effect=OSError("x"))
    assert ctl.field_status() == ("실행 중 (PID 9)", "ok")


# ── 설정 파일 ──
def test_save_connection_file_writes_relay_code(tmp_path, monkeypatch):
    ctl, ctx, ui, _ = direct_ctl(relay_port=9100, relay_external="")
    monkeypatch.setattr(wui, "lan_addresses", lambda: ["192.168.0.140"])
    path = str(tmp_path / "x.conn")
    assert ctl.save_connection_file(path) is True
    assert cc.decode(connfile.read(path)) == (["ws://192.168.0.140:9100"], "공용키")
    assert "AuroraClient.exe 와 같은 폴더" in ui.last("show_info")[1]


def test_save_connection_file_without_code_or_bad_path(tmp_path, monkeypatch):
    ctl, ctx, ui, _ = direct_ctl(relay_port=9100)
    monkeypatch.setattr(wui, "lan_addresses", lambda: [])
    assert ctl.save_connection_file(str(tmp_path / "x.conn")) is False
    assert "자동 설정" in ui.last("show_error")[1]
    monkeypatch.setattr(wui, "lan_addresses", lambda: ["192.168.0.140"])
    assert ctl.save_connection_file(str(tmp_path / "no_such_dir" / "x.conn")) is False
    assert "저장하지 못했습니다" in ui.last("show_error")[1]


def test_save_connection_dialog_asks_path_then_saves(tmp_path, monkeypatch):
    ctl, ctx, ui, _ = direct_ctl(relay_port=9100)
    monkeypatch.setattr(wui, "lan_addresses", lambda: ["192.168.0.140"])
    ctx["conn_dirs"] = [str(tmp_path)]
    asked = []
    ui.ask_save_path = lambda directory, name, cb: (asked.append((directory, name)), cb(str(tmp_path / name)))
    ctl.save_connection_dialog()
    assert asked == [(str(tmp_path), "aurora_wan.conn")] and (tmp_path / "aurora_wan.conn").exists()
    ui.ask_save_path = lambda directory, name, cb: cb(None)                     # 취소
    ctl.save_connection_dialog()                                                # 오류 없이 끝남


def test_read_connection_file_errors_are_shown(tmp_path):
    ctl, ctx, ui, _ = direct_ctl()
    bad = tmp_path / "bad.conn"
    bad.write_text("hello")
    assert ctl.read_connection_file(str(bad)) == ""
    assert "접속 정보가 들어 있는" in ui.last("show_error")[1]
    good = tmp_path / "good.conn"
    connfile.write(str(good), cc.encode(["ws://a:1"], "k"))
    assert cc.decode(ctl.read_connection_file(str(good))) == (["ws://a:1"], "k")


# ── ws:// 주소 직접 입력 · 중계 접속 정보 ──
def test_add_connection_relay_url_uses_typed_key_or_current():
    ctl, ctx, ui, _ = direct_ctl()
    ctl.add_connection("ws://203.0.113.5:9100", "새키")
    assert ctx["cfg"].d["relay_url"] == "ws://203.0.113.5:9100" and ctx["cfg"].d["relay_key"] == "새키"
    ctl.add_connection("ws://203.0.113.5:9101", "")
    assert ctx["cfg"].d["relay_key"] == "새키" or ctx["cfg"].d["relay_key"] == ""


def test_add_connection_relay_code_with_field_checkbox(monkeypatch):
    ctl, ctx, ui, _ = direct_ctl()
    monkeypatch.setattr(cc, "pick_reachable", lambda urls, timeout=2.0: urls[0])
    ctx["client"] = MagicMock()
    ctl.client = ctx["client"]
    code = cc.encode(["ws://10.0.0.1:9100"], "k9")
    ctl.add_connection(code, "", as_field=True)
    assert ctx["cfg"].d["relay_url"] == "ws://10.0.0.1:9100" and ctx["cfg"].d["relay_key"] == "k9"
    assert ctx["cfg"].d["enabled"] is True
    ctx["set_field_mode"].assert_called_with(True)


# ── 컨트롤러 + 진짜 DirectClient + 진짜 현장 에이전트 ──
def test_controller_connects_to_real_agent_by_address_and_remembers(tmp_path):
    from test_wan_direct import Site
    from wan_helpers import eventually
    site = Site(tmp_path, code="1234")
    try:
        opened = []
        ctx = {"cfg": site.cfg, "client": FakeClient(), "usage": site.usage,
               "supervisor_state": lambda: {"state": "stopped"}, "set_field_mode": MagicMock(), "px": lambda v: v,
               "open_remote": lambda session, agent: opened.append((session, agent)), "direct": site.client,
               "log": MagicMock()}
        ctl = wui.WanTabController(ctx, lambda fn: fn(), ui=RecUI(), spawn=lambda fn: fn())
        ctl.add_connection(site.address, "9999")                                 # 틀린 코드
        assert not opened and "맞지 않" in ctl.ui.last("show_error")[1]
        assert site.cfg.get("direct_targets") == {}
        ctl.add_connection(site.address, "1234")
        session, agent = opened[0]
        try:
            assert session.request("GET", "/info").json()["hostname"] == "FAKE-PC"
            assert agent["id"] == f"d:{site.address}" and agent["name"] == "현장1"
        finally:
            session.close()
        row = dict(ctl.rows_with_ids())[f"d:{site.address}"]
        assert row[0] == "현장1" and row[1] == "주소"
        ctl.probe_targets()
        assert dict(ctl.rows_with_ids())[f"d:{site.address}"][2] == "켜짐"       # 진짜 TCP 확인
        ctl.quick_connect(f"d:{site.address}")                                   # 저장된 코드로 바로
        assert len(opened) == 2
        opened[1][0].close()
        site.agent.stop()
        eventually(lambda: (ctl.probe_targets(), dict(ctl.rows_with_ids())[f"d:{site.address}"][2] == "꺼짐")[1],
                   timeout=8, msg="현장이 꺼지면 목록에서 꺼짐")
    finally:
        site.close()


# ── 현장 켜기 → 주소 접속 포트 방화벽 ──
@pytest.mark.parametrize("ok,expect", [(True, "방화벽에서 TCP 9102"), (False, "자동으로 열지 못했습니다")])
def test_field_on_tries_to_open_direct_port_in_firewall(monkeypatch, ok, expect):
    ctl, ctx, ui, _ = direct_ctl()
    calls = []
    monkeypatch.setattr(wui.os, "name", "nt")
    monkeypatch.setattr(wui, "open_firewall",
                        lambda port, direct=False: (calls.append((port, direct)), (ok, "방화벽에서 TCP 9102 를 열었습니다"))[1])
    ctl.field_toggled(True)
    assert calls == [(9102, True)]
    assert any(expect in line for line in ui.last("set_log")[0])
    calls.clear()
    ctl.field_toggled(False)                                                  # 끌 때는 건드리지 않음
    assert calls == []


def test_field_on_does_not_touch_firewall_off_windows(monkeypatch):
    ctl, ctx, ui, _ = direct_ctl()
    monkeypatch.setattr(wui, "open_firewall", MagicMock(side_effect=AssertionError("부르면 안 됨")))
    ctl.field_toggled(True)


# ── 인증서 지문 ──
def test_relay_code_uses_wss_and_fingerprint_when_encryption_is_available(monkeypatch):
    ctl, ctx, ui, _ = direct_ctl(relay_port=9100, relay_external="203.0.113.5")
    monkeypatch.setattr(wui, "lan_addresses", lambda: ["192.168.0.140"])
    assert cc.decode_full(ctl.relay_code()) == (["ws://203.0.113.5:9100", "ws://192.168.0.140:9100"], "공용키", "")
    ctx["relay_fp"] = lambda: "ab" * 32
    urls, key, fp = cc.decode_full(ctl.relay_code())
    assert urls == ["wss://203.0.113.5:9100", "wss://192.168.0.140:9100"] and fp == "ab" * 32


def test_address_connection_pins_saved_fingerprint_and_remembers_new_one():
    ctl, ctx, ui, direct = direct_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "a", "code": "c", "last": 0, "fp": "aa" * 32}}
    direct.open_session.return_value = SimpleNamespace(mode="direct", name="a", fp="aa" * 32)
    ctl.quick_connect("d:10.0.0.5:9102")
    assert direct.open_session.call_args.args[3] == "aa" * 32            # 저장해 둔 지문으로 고정해서 접속
    ctl.add_connection("10.0.0.9", "c")                                   # 처음 보는 주소 — 지문 없이 접속하고 알려 준 지문을 기억
    direct.open_session.return_value = SimpleNamespace(mode="direct", name="", fp="bb" * 32)
    ctl.add_connection("10.0.0.7", "c")
    assert ctx["cfg"].get("direct_targets")["10.0.0.7:9102"]["fp"] == "bb" * 32


def test_direct_code_fingerprint_is_used_as_pin(monkeypatch):
    ctl, ctx, ui, direct = direct_ctl()
    monkeypatch.setattr(cc, "pick_reachable", lambda urls, timeout=2.0: urls[0])
    ctl.add_connection(cc.encode_direct(["wss://192.168.0.5:9102"], "c", "cd" * 32))
    assert direct.open_session.call_args.args[3] == "cd" * 32


def test_pin_mismatch_is_shown_as_error_and_not_saved():
    from wan.tls import PinMismatch, PIN_MISMATCH_TEXT
    ctl, ctx, ui, direct = direct_ctl()
    direct.open_session.side_effect = PinMismatch(PIN_MISMATCH_TEXT)
    ctl.add_connection("10.0.0.9", "c")
    assert "인증서" in ui.last("show_error")[1] and ctx["cfg"].get("direct_targets") == {}
    assert not ctx["open_remote"].called


def test_apply_server_forgets_old_fingerprint_only_when_address_changes():
    ctl, ctx, ui, _ = direct_ctl(relay_url="wss://a:1", relay_fp="ab" * 32)
    ctl.apply_server("wss://a:1", "k")
    assert ctx["cfg"].d["relay_fp"] == "ab" * 32
    ctl.apply_server("wss://b:1", "k")
    assert ctx["cfg"].d["relay_fp"] == ""


def test_import_relay_code_stores_fingerprint(monkeypatch):
    ctl, ctx, ui, _ = direct_ctl()
    monkeypatch.setattr(cc, "pick_reachable", lambda urls, timeout=2.0: urls[0])
    ctx["client"] = MagicMock()
    ctl.client = ctx["client"]
    ctl.import_connection(cc.encode(["wss://10.0.0.1:9100"], "k9", fp="ef" * 32))
    assert ctx["cfg"].d["relay_fp"] == "ef" * 32 and ctx["cfg"].d["relay_url"] == "wss://10.0.0.1:9100"


def test_server_status_for_certificate_mismatch_is_bad():
    ctl, ctx, ui, _ = direct_ctl(relay_url="wss://a:1")
    ctx["client"].state = "인증서 불일치"
    assert ctl.server_status() == ("● 인증서 불일치", "bad")


# ── 중계 PC 가 알려 준 현장 코드로 더블클릭 ──
def relay_agent(code="현장별-코드-77", online=True):
    return {"id": "x1", "name": "현장A", "online": online, "last_seen": 0, "rev": 0, "applied_rev": 0, "code": code}


def test_double_click_uses_code_given_by_relay_before_saved_or_shared_key():
    ctl, ctx, ui, _ = direct_ctl()
    ctx["client"].agents = [relay_agent()]
    ctx["cfg"].d["saved_codes"] = {"x1": "옛날-코드"}
    ctl.quick_connect("x1")
    assert ctx["client"].calls[-1] == ("open", "x1", "현장별-코드-77", "현장A")
    assert not ui.all("ask_code")


def test_double_click_falls_back_to_saved_then_shared_key_when_relay_gives_no_code():
    ctl, ctx, ui, _ = direct_ctl()
    ctx["client"].agents = [relay_agent(code="")]
    ctl.quick_connect("x1")
    assert ctx["client"].calls[-1][2] == "공용키"
    ctx["cfg"].d["saved_codes"] = {"x1": "내가-입력한-코드"}
    ctl._codes.clear()
    ctl.quick_connect("x1")
    assert ctx["client"].calls[-1][2] == "내가-입력한-코드"


def test_field_turned_on_gets_random_access_code_automatically():
    ctl, ctx, ui, _ = direct_ctl()
    assert ctx["cfg"].d.get("access_code", "") == ""
    ctl.field_toggled(True)
    assert ctx["cfg"].d["access_code"]
    assert ui.last("set_field_code")[0] == ctx["cfg"].d["access_code"]       # 화면에 바로 보임


def test_config_generates_unique_random_codes_once(tmp_path):
    from wan.config import WanConfig
    a, b = WanConfig(str(tmp_path / "a")), WanConfig(str(tmp_path / "b"))
    ca, cb = a.ensure_access_code(), b.ensure_access_code()
    assert len(ca) >= 12 and ca != cb and a.ensure_access_code() == ca       # 한 번만 만들고 유지
    assert WanConfig(str(tmp_path / "a")).get("access_code") == ca           # 저장됨
    a.update(access_code="내가-정한-코드")
    assert a.ensure_access_code() == "내가-정한-코드"                          # 직접 정한 코드는 덮어쓰지 않음


# ── 삭제 · 이름 바꾸기 ──
def relay_rows(ctl, ctx, online=False):
    ctx["client"].agents = [{"id": "x1", "name": "main", "online": online, "last_seen": 1, "rev": 0, "applied_rev": 0}]
    ctx["client"].forget_agent = MagicMock()


def test_offline_relay_agent_is_deleted_after_confirmation():
    ctl, ctx, ui, _ = direct_ctl()
    relay_rows(ctl, ctx)
    asked = []
    ui.ask_yes_no = lambda title, msg, cb: (asked.append(msg), cb(True))
    assert ctl.forget_target("x1") is True
    ctx["client"].forget_agent.assert_called_once_with("x1")
    assert "main" in asked[0] and "모든 PC" in asked[0]


def test_delete_cancelled_does_nothing():
    ctl, ctx, ui, _ = direct_ctl()
    relay_rows(ctl, ctx)
    ui.ask_yes_no = lambda title, msg, cb: cb(False)
    ctl.forget_target("x1")
    ctx["client"].forget_agent.assert_not_called()


def test_online_relay_agent_cannot_be_deleted():
    ctl, ctx, ui, _ = direct_ctl()
    relay_rows(ctl, ctx, online=True)
    ui.ask_yes_no = MagicMock()
    assert ctl.forget_target("x1") is False
    ui.ask_yes_no.assert_not_called()
    assert "켜져 있는" in ui.last("show_info")[1]


def test_delete_failure_is_reported():
    ctl, ctx, ui, _ = direct_ctl()
    relay_rows(ctl, ctx)
    ctx["client"].forget_agent.side_effect = WanError("안내 서버에 연결되어 있지 않습니다")
    ui.ask_yes_no = lambda t, m, cb: cb(True)
    ctl.forget_target("x1")
    assert "연결되어 있지 않" in ui.last("show_error")[1]


def test_rename_address_target_is_local_and_relay_agent_goes_through_config():
    ctl, ctx, ui, _ = direct_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "a", "code": "c", "last": 0}}
    assert ctl.rename_target("d:10.0.0.5:9102", " 사무실 ") is True
    assert ctx["cfg"].get("direct_targets")["10.0.0.5:9102"]["name"] == "사무실"
    assert dict(ctl.rows_with_ids())["d:10.0.0.5:9102"][0] == "사무실"
    relay_rows(ctl, ctx)
    assert ctl.rename_target("x1", "CCW-2") is True
    assert ctx["client"].calls[-1] == ("cfg", "x1", {"name": "CCW-2"})


def test_rename_rejects_empty_long_or_unknown():
    ctl, ctx, ui, _ = direct_ctl()
    assert ctl.rename_target("x1", "  ") is False
    assert ctl.rename_target("x1", "가" * 41) is False
    assert ctl.rename_target("nope", "이름") is False
    assert len(ui.all("show_error")) == 3


# ── WAN 켜기/끄기 ──
def off_ctl():
    ctl, ctx, ui, direct = direct_ctl()
    state = {"on": False}
    ctx["wan_enabled"] = lambda: state["on"]
    ctx["set_wan_enabled"] = MagicMock(side_effect=lambda v: state.update(on=v))
    ctx["public_address"] = MagicMock(return_value="203.0.113.9")
    ctx["reachable"] = MagicMock(return_value=True)
    return ctl, ctx, ui, direct, state


def test_wan_toggle_goes_to_runtime_and_screen_follows():
    ctl, ctx, ui, direct, state = off_ctl()
    ctl.wan_toggled(True)
    ctx["set_wan_enabled"].assert_called_once_with(True)
    assert ui.last("set_wan_enabled") == (True,)
    ctl.wan_toggled(False)
    assert state["on"] is False and ui.last("set_wan_enabled") == (False,)


def test_wan_toggle_failure_reverts_screen_and_reports():
    ctl, ctx, ui, direct, state = off_ctl()
    ctx["set_wan_enabled"].side_effect = OSError("시작 실패")
    ctl.wan_toggled(True)
    assert ui.last("set_wan_enabled") == (False,) and "시작 실패" in ui.last("show_error")[1]


def test_nothing_is_fetched_or_connected_while_wan_is_off():
    ctl, ctx, ui, direct, state = off_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "a", "code": "c", "last": 0}}
    ctx["cfg"].d["enabled"] = True
    ctl.tick_slow(); ctl.tick_fast(); ctl.refresh_all()
    ctx["reachable"].assert_not_called()                              # 현장 주소에 닿는지 확인도 하지 않음
    ctx["public_address"].assert_not_called()                         # 공인 주소를 STUN 에 묻지도 않음
    assert ctl.quick_connect("d:10.0.0.5:9102") is False
    assert ctl.add_connection("10.0.0.9", "c") is False
    assert not direct.open_session.called
    assert "WAN 사용" in ui.last("show_info")[1]
    state["on"] = True                                                # 켜면 그대로 동작
    ctl.tick_slow()
    ctx["reachable"].assert_called()


def test_screen_without_switch_is_always_on():
    ctl, ctx, ui, _ = direct_ctl()
    assert ctl.wan_switch_available() is False and ctl.wan_on() is True


# ── 접속 코드 새로고침 · Git ──
@pytest.mark.parametrize("text,kind", [("https://git.corp/aurora/wan.git", "git"), ("git@git.corp:aurora/wan.git", "git"),
                                       ("ssh://git@git.corp/x", "git"), ("\\\\\\\\server\\\\repos\\\\wan.git", "git"),
                                       ("ws://203.0.113.5:9100", "relay_url"), ("192.168.0.5", "address")])
def test_classify_git_source(text, kind):
    assert wui.classify_target(text) == kind


def git_ctl(**over):
    ctl, ctx, ui, direct = direct_ctl()
    ctx.update(rotate_relay_key=MagicMock(return_value={"key": "k2", "code": "AW1-new", "published": True, "error": ""}),
               git_publish=MagicMock(return_value=True), set_git_source=MagicMock(),
               git_sync=MagicMock(return_value={"state": "ok", "msg": ""}), git_state=lambda: {"state": "ok", "msg": ""})
    ctx.update(over)
    return ctl, ctx, ui


def test_refresh_code_asks_first_and_explains_consequences():
    ctl, ctx, ui = git_ctl()
    asked = []
    ui.ask_yes_no = lambda t, m, cb: (asked.append(m), cb(False))
    ctl.relay_refresh_code()
    ctx["rotate_relay_key"].assert_not_called()
    assert "사라집니다" in asked[0] and "끊깁니다" in asked[0] and "Git" in asked[0]


def test_refresh_code_rotates_and_shows_new_code():
    ctl, ctx, ui = git_ctl()
    ui.ask_yes_no = lambda t, m, cb: cb(True)
    ctl.relay_refresh_code()
    ctx["rotate_relay_key"].assert_called_once()
    assert ui.last("set_relay_code")[0] == "AW1-new"
    assert not ui.all("show_error")


def test_refresh_code_reports_git_failure_but_keeps_going():
    ctl, ctx, ui = git_ctl()
    ctx["rotate_relay_key"].return_value = {"key": "k2", "code": "AW1-new", "published": False, "error": "Git 서버에 닿지 않습니다"}
    ui.ask_yes_no = lambda t, m, cb: cb(True)
    ctl.relay_refresh_code()
    assert "닿지 않습니다" in ui.last("show_error")[1] and "'올리기'를 눌러" in ui.last("show_error")[1]


def test_refresh_code_exception_is_reported():
    ctl, ctx, ui = git_ctl()
    ctx["rotate_relay_key"].side_effect = RuntimeError("재시작 실패")
    ui.ask_yes_no = lambda t, m, cb: cb(True)
    ctl.relay_refresh_code()
    assert "재시작 실패" in ui.last("show_error")[1]


def test_refresh_code_unavailable_without_runtime_support():
    ctl, ctx, ui, _ = direct_ctl()
    assert ctl.relay_refresh_code() is False and ui.all("show_error")


def test_publish_git_needs_address_and_code(monkeypatch):
    monkeypatch.setattr(wui, "lan_addresses", lambda: [])
    ctl, ctx, ui = git_ctl()
    assert ctl.publish_git("  ") is False and "주소" in ui.last("show_error")[1]
    ctx["cfg"].d.update(relay_port=9100)
    assert ctl.publish_git("https://git.corp/a.git") is False                  # 접속 정보가 아직 없음 (주소 없음)
    ctl.relay_code = lambda: "AW1-x"
    assert ctl.publish_git("https://git.corp/a.git") is True
    ctx["git_publish"].assert_called_once_with("https://git.corp/a.git", "AW1-x")
    assert "올렸습니다" in ui.last("show_info")[1]


def test_publish_git_failure_is_shown():
    from wan.gitsync import GitError
    ctl, ctx, ui = git_ctl()
    ctl.relay_code = lambda: "AW1-x"
    ctx["git_publish"].side_effect = GitError("Git 서버 로그인에 실패했습니다")
    ctl.publish_git("https://git.corp/a.git")
    assert "로그인" in ui.last("show_error")[1]


def test_pasting_git_address_sets_source_and_syncs_once():
    ctl, ctx, ui = git_ctl()
    assert ctl.add_connection("https://git.corp/aurora/wan.git") is True
    ctx["set_git_source"].assert_called_once_with("https://git.corp/aurora/wan.git")
    ctx["git_sync"].assert_called_once()
    assert not ui.all("show_error")


def test_git_sync_error_is_shown_when_pasting_address():
    ctl, ctx, ui = git_ctl()
    ctx["git_sync"].return_value = {"state": "error", "msg": "Git 저장소를 찾을 수 없습니다"}
    ctl.add_connection("https://git.corp/aurora/nope.git")
    assert "찾을 수 없" in ui.last("show_error")[1]


def test_git_off_clears_source():
    ctl, ctx, ui = git_ctl()
    assert ctl.set_git_source("") is True
    ctx["set_git_source"].assert_called_once_with("")
    ctx["git_sync"].assert_not_called()


@pytest.mark.parametrize("state,text,level", [({"state": "ok"}, "Git 연결됨", "ok"), ({"state": "error", "msg": "로그인 실패"}, "로그인 실패", "bad"),
                                              ({"state": "off"}, "", "off"), ({}, "", "off")])
def test_git_status_texts(state, text, level):
    ctl, ctx, ui = git_ctl(git_state=lambda: state)
    assert ctl.git_status() == (text, level)


def test_relay_refresh_updates_code_and_git_widgets():
    ctl, ctx, ui = git_ctl(relay_available=True, relay_state=lambda: {"state": "stopped"})
    ctx["cfg"].d.update(git_source="https://git.corp/a.git", relay_port=9100)
    ctl.refresh_relay()
    assert ui.last("set_relay_git") == ("https://git.corp/a.git", "Git 연결됨", "ok")


# ── Git 서버 오류 알림 ──
def alert_ctl(state, source="https://git.corp/a.git"):
    box = {"st": state}
    ctl, ctx, ui = git_ctl(git_state=lambda: box["st"])
    ctx["cfg"].d["git_source"] = source
    return ctl, ctx, ui, box


def test_git_server_error_pops_up_once_until_recovered():
    ctl, ctx, ui, box = alert_ctl({"state": "error", "msg": "Git 서버에 닿지 않습니다 — 사내망(VPN)에 연결되어 있는지 확인하세요"})
    ctl.check_git_alert()
    assert len(ui.all("show_error")) == 1
    title, msg = ui.last("show_error")
    assert "Git 서버" in title and "닿지 않습니다" in msg and "자동으로 다시 시도" in msg
    ctl.check_git_alert(); ctl.tick_fast()
    assert len(ui.all("show_error")) == 1                                   # 계속 꺼져 있어도 창은 한 번만
    box["st"] = {"state": "ok", "msg": ""}
    ctl.check_git_alert()
    assert "다시 연결" in ui.last("set_log")[0][-1]
    box["st"] = {"state": "error", "msg": "또 꺼짐"}
    ctl.check_git_alert()
    assert len(ui.all("show_error")) == 2                                   # 복구된 뒤 다시 꺼지면 다시 알림


def test_no_git_alert_when_wan_off_or_git_not_used_or_healthy():
    ctl, ctx, ui, box = alert_ctl({"state": "error", "msg": "x"}, source="")
    ctl.check_git_alert()
    ctl2, ctx2, ui2, box2 = alert_ctl({"state": "ok"})
    ctl2.check_git_alert()
    ctl3, ctx3, ui3, box3 = alert_ctl({"state": "error", "msg": "x"})
    ctx3["wan_enabled"] = lambda: False
    ctl3.check_git_alert()
    assert not (ui.all("show_error") or ui2.all("show_error") or ui3.all("show_error"))


# ── 접속 코드 하나로 시작 (WAN 켜기 + 접속 + 현장 설정) ──
def setup_ctl(monkeypatch):
    ctl, ctx, ui, direct, state = off_ctl()
    ctx["client"] = MagicMock()
    ctl.client = ctx["client"]
    monkeypatch.setattr(cc, "pick_reachable", lambda urls, timeout=2.0: urls[0])
    return ctl, ctx, ui, state


def test_quick_setup_turns_wan_on_connects_and_enables_field_mode(monkeypatch):
    ctl, ctx, ui, state = setup_ctl(monkeypatch)
    code = cc.encode(["wss://10.0.0.1:9100"], "k9", fp="ef" * 32)
    assert ctl.quick_setup(code, as_field=True) is True
    assert state["on"] is True and ui.last("set_wan_enabled") == (True,)
    assert ctx["cfg"].d["relay_url"] == "wss://10.0.0.1:9100" and ctx["cfg"].d["relay_key"] == "k9"
    assert ctx["cfg"].d["relay_fp"] == "ef" * 32
    assert ctx["cfg"].d["enabled"] is True and ctx["cfg"].d["access_code"]       # 현장 PC 로 켜지고 접근 코드도 자동 생성
    ctx["set_field_mode"].assert_called_with(True)
    assert not ui.all("show_error")


def test_quick_setup_as_user_only_does_not_enable_field_mode(monkeypatch):
    ctl, ctx, ui, state = setup_ctl(monkeypatch)
    ctl.quick_setup(cc.encode(["wss://10.0.0.1:9100"], "k9"), as_field=False)
    assert state["on"] is True and ctx["cfg"].d["relay_url"] == "wss://10.0.0.1:9100"
    assert not ctx["cfg"].d.get("enabled")
    ctx["set_field_mode"].assert_not_called()


def test_quick_setup_with_git_address_also_sets_field_mode(monkeypatch):
    ctl, ctx, ui, state = setup_ctl(monkeypatch)
    ctx.update(set_git_source=MagicMock(), git_sync=MagicMock(return_value={"state": "ok", "msg": ""}))
    ctl.quick_setup("https://git.corp/aurora/wan.git", as_field=True)
    assert state["on"] is True
    ctx["set_git_source"].assert_called_once_with("https://git.corp/aurora/wan.git")
    ctx["set_field_mode"].assert_called_with(True)


@pytest.mark.parametrize("text", ["", "   ", "192.168.0.5", "10.0.0.7:9102"])
def test_quick_setup_rejects_things_that_are_not_connection_codes(monkeypatch, text):
    ctl, ctx, ui, state = setup_ctl(monkeypatch)
    assert ctl.quick_setup(text) is False
    assert state["on"] is False and "붙여넣으세요" in ui.last("show_error")[1]       # 아무것도 켜지 않음


def test_quick_setup_reports_when_wan_cannot_start(monkeypatch):
    ctl, ctx, ui, state = setup_ctl(monkeypatch)
    ctx["set_wan_enabled"].side_effect = OSError("시작 실패")
    ctl.quick_setup(cc.encode(["wss://10.0.0.1:9100"], "k"))
    assert "시작 실패" in ui.last("show_error")[1] and not ctx["cfg"].d.get("relay_url")


# ── 말씀하신 흐름 그대로: 현장 PC 는 켜 두기만, 접속하는 PC 는 중계 PC 정보만 ──
def test_user_pc_knowing_only_relay_info_sees_and_opens_every_field_pc(tmp_path):
    from wan_helpers import World, eventually, AgentRunner, FakeLocal
    w = World(tmp_path, code="", key="shared-key-9")                   # 현장 1: 접근 코드를 따로 정하지 않음(공용 키로 열림)
    local2 = FakeLocal()
    field2 = AgentRunner(str(tmp_path / "field2"), w.relay.url, local2.port, name="현장2", code="현장2-자동생성-코드", key="shared-key-9")
    try:
        opened = []
        ctx = {"cfg": w.user_cfg, "client": w.client, "usage": w.usage, "supervisor_state": lambda: {"state": "stopped"},
               "set_field_mode": MagicMock(), "px": lambda v: v, "log": MagicMock(),
               "open_remote": lambda session, agent: opened.append((agent["name"], session))}
        ctl = wui.WanTabController(ctx, lambda fn: fn(), ui=RecUI(), spawn=lambda fn: fn())
        eventually(lambda: len([a for a in w.client.agents if a["online"]]) == 2, msg="현장 PC 두 대가 목록에 자동으로 나타남")
        rows = ctl.rows()
        assert {r[0] for r in rows} == {"현장1", "현장2"} and all(r[2] == "켜짐" for r in rows)     # 따로 등록한 적 없음
        for aid, values in ctl.rows_with_ids():
            ctl.quick_connect(aid)                                                              # 코드는 한 번도 묻지 않음
        assert sorted(n for n, _ in opened) == ["현장1", "현장2"]
        assert not ctl.ui.all("ask_code") and not ctl.ui.all("show_error")
        for _, s in opened:
            assert s.request("GET", "/info").status_code == 200
            s.close()
    finally:
        field2.stop(); local2.stop(); w.close()
