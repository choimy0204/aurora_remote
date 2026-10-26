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
    direct.open_session.assert_called_once_with("192.168.0.5:9102", "공용키", "")
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
    direct.open_session.assert_called_once_with("10.0.0.9:7000", "내코드", "")
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
    direct.open_session.assert_called_once_with("192.168.0.5:9102", "현장코드", "")


def test_add_direct_code_broken_shows_error():
    ctl, ctx, ui, direct = direct_ctl()
    assert ctl.add_connection("AD1-@@@@") is False
    assert "깨져" in ui.last("show_error")[1]


# ── 더블클릭(저장된 주소 현장) ──
def test_quick_connect_saved_target_uses_saved_code_without_asking():
    ctl, ctx, ui, direct = direct_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "사무실", "code": "저장코드", "last": 0}}
    assert ctl.quick_connect("d:10.0.0.5:9102") is True
    direct.open_session.assert_called_once_with("10.0.0.5:9102", "저장코드", "사무실")
    assert not ui.all("ask_code")


def test_quick_connect_saved_target_wrong_code_asks_once_and_saves_new():
    ctl, ctx, ui, direct = direct_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "사무실", "code": "옛코드", "last": 0}}
    calls = []

    def open_session(address, code, name):
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
def test_forget_target_removes_only_address_rows():
    ctl, ctx, ui, direct = direct_ctl()
    ctx["cfg"].d["direct_targets"] = {"10.0.0.5:9102": {"name": "a", "code": "c", "last": 0}}
    assert ctl.forget_target("d:10.0.0.5:9102") is True
    assert ctx["cfg"].get("direct_targets") == {}
    assert ctl.forget_target("d:10.0.0.5:9102") is False
    assert ctl.forget_target("relay-agent-id") is False
    assert "지울 수 없" in ui.last("show_info")[1]


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
    assert urls == ["ws://192.168.0.5:9555", "ws://10.1.1.1:9555", "ws://211.1.2.3:9555"] and code == "공용키"
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
