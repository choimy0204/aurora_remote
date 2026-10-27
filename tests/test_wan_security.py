"""보안 장치 — 기본 키 차단 · 출처별 잠금 · 인터넷용 자동 설정"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import wan.ui as wui
from wan import connect_code as cc
from wan import netutil
from wan.agent import CODE_FAILS, WanAgent
from wan.config import DEFAULT_RELAY_KEY
from wan.server import KEY_FAILS, KEY_LOCK, RelayServer
from wan_helpers import FakeCfg, patch_ui
from test_wan_ui import make_ctl, FakeCfg as UiCfg


@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("211.234.12.34", True),
    ("192.168.0.5", False), ("10.1.2.3", False), ("172.16.0.9", False), ("127.0.0.1", False),
    ("169.254.1.1", False), ("100.64.0.5", False), ("", False), ("not-an-ip", False), ("::1", False)])
def test_is_public_ip(ip, public):
    assert netutil.is_public_ip(ip) is public


# ── 중계 서버 ──
class FakeWS:
    def __init__(self, ip, hello):
        self.remote_address = (ip, 5555)
        self._hello, self.sent, self.closed = json.dumps(hello), [], None

    async def recv(self):
        return self._hello

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def close(self, code=1000, reason=""):
        self.closed = code


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def relay(tmp_path, key, now=None):
    clock = [1000.0]
    srv = RelayServer(str(tmp_path), key, None, now=lambda: clock[0])
    return srv, clock


def connect(srv, ip, key):
    ws = FakeWS(ip, {"t": "hello", "role": "unknown", "key": key})
    run(srv.handle(ws))
    return ws


@pytest.mark.parametrize("key", ["", DEFAULT_RELAY_KEY])
def test_relay_refuses_default_or_empty_key_from_internet(tmp_path, key):
    srv, _ = relay(tmp_path, key)
    ws = connect(srv, "211.234.12.34", key)
    assert ws.sent == [{"t": "error", "reason": "default_key"}] and ws.closed == 4001


@pytest.mark.parametrize("ip", ["192.168.0.5", "127.0.0.1", "10.0.0.2"])
def test_relay_still_accepts_default_key_inside_the_network(tmp_path, ip):
    srv, _ = relay(tmp_path, DEFAULT_RELAY_KEY)
    ws = connect(srv, ip, DEFAULT_RELAY_KEY)
    assert not [m for m in ws.sent if m.get("reason") in ("default_key", "key")]     # 키 단계는 통과 (역할이 이상해 그 뒤에서 닫힘)


def test_relay_accepts_strong_key_from_internet(tmp_path):
    srv, _ = relay(tmp_path, "아주-긴-임의의-키-9f3a")
    ws = connect(srv, "211.234.12.34", "아주-긴-임의의-키-9f3a")
    assert not [m for m in ws.sent if m.get("t") == "error"]


def test_relay_locks_a_source_after_repeated_wrong_keys_but_not_others(tmp_path):
    srv, clock = relay(tmp_path, "진짜키")
    for _ in range(KEY_FAILS):
        assert connect(srv, "1.2.3.4", "틀림").sent[-1]["reason"] == "key"
    ws = connect(srv, "1.2.3.4", "진짜키")                                         # 맞는 키도 한동안 거부
    assert ws.sent == [{"t": "error", "reason": "locked"}] and ws.closed == 4003
    assert not [m for m in connect(srv, "5.6.7.8", "진짜키").sent if m.get("t") == "error"]   # 다른 곳은 영향 없음
    clock[0] += KEY_LOCK + 1
    assert not [m for m in connect(srv, "1.2.3.4", "진짜키").sent if m.get("t") == "error"]   # 시간이 지나면 풀림


# ── 현장 에이전트 ──
def agent(**cfg):
    return WanAgent(FakeCfg(**cfg), 1, lambda: "", "2.16.0", lambda m: None)


def test_default_code_is_refused_from_the_internet_but_not_from_lan():
    a = agent(access_code="", relay_key=DEFAULT_RELAY_KEY)
    assert a.check_code(DEFAULT_RELAY_KEY, "211.234.12.34") == "weak_code"
    assert a.check_code(DEFAULT_RELAY_KEY, "192.168.0.7") is None
    assert a.check_code(DEFAULT_RELAY_KEY) is None                               # 출처를 모르는 중계 경유는 중계 서버가 막는다
    b = agent(access_code=DEFAULT_RELAY_KEY, relay_key="")
    assert b.check_code(DEFAULT_RELAY_KEY, "8.8.8.8") == "weak_code"


def test_explicit_code_works_from_the_internet():
    a = agent(access_code="나만-아는-코드-71", relay_key=DEFAULT_RELAY_KEY)
    assert a.check_code("나만-아는-코드-71", "211.234.12.34") is None
    assert a.check_code("틀림", "211.234.12.34") == "bad_code"


def test_lockout_is_per_source_so_an_attacker_cannot_lock_out_the_owner():
    a = agent(access_code="real-code-1", relay_key="")
    for _ in range(CODE_FAILS):
        assert a.check_code("x", "8.8.8.8") == "bad_code"
    assert a.check_code("real-code-1", "8.8.8.8") == "locked"                     # 공격자는 잠김
    assert a.check_code("real-code-1", "192.168.0.7") is None                     # 내 PC 는 그대로 접속
    a._fails.clear()
    assert a.check_code("real-code-1", "8.8.8.8") is None


def test_relay_path_lockout_is_still_global():
    a = agent(access_code="c1", relay_key="")
    for _ in range(CODE_FAILS):
        a.check_code("x")
    assert a.check_code("c1") == "locked"


# ── 자동 설정 ──
def setup_ctl(monkeypatch, **cfg):
    patch_ui(monkeypatch, "is_admin", lambda: True)
    patch_ui(monkeypatch, "lan_addresses", lambda: ["192.168.0.140"])
    patch_ui(monkeypatch, "open_firewall", lambda port: (True, "열었습니다"))
    ctl, ctx, ui = make_ctl(cfg=UiCfg(relay_port=9100, **cfg), relay_available=True,
                            set_relay_host=MagicMock(), restart_relay=MagicMock(), relay_state=lambda: {"state": "stopped"})
    ctx["client"] = MagicMock()
    ctl.client = ctx["client"]
    return ctl, ctx, ui


def test_auto_setup_for_internet_replaces_default_key_with_random_one(monkeypatch):
    ctl, ctx, ui = setup_ctl(monkeypatch, relay_key=DEFAULT_RELAY_KEY)
    ctl.relay_auto_setup("203.0.113.5")
    key = ctx["cfg"].d["relay_key"]
    assert key != DEFAULT_RELAY_KEY and len(key) >= 16
    ctx["restart_relay"].assert_called_once()                                    # 이미 돌던 서버도 새 키로
    assert cc.decode(ui.last("set_relay_code")[0])[1] == key


def test_auto_setup_on_lan_keeps_default_key(monkeypatch):
    ctl, ctx, ui = setup_ctl(monkeypatch, relay_key="")
    ctl.relay_auto_setup("")
    assert ctx["cfg"].d["relay_key"] == DEFAULT_RELAY_KEY                      # 같은 망 전용이면 기본 키 그대로 (편의)


def test_auto_setup_keeps_user_chosen_key_even_for_internet(monkeypatch):
    ctl, ctx, ui = setup_ctl(monkeypatch, relay_key="내가-정한-긴-키-123")
    ctl.relay_auto_setup("203.0.113.5")
    assert ctx["cfg"].d["relay_key"] == "내가-정한-긴-키-123"
    ctx["restart_relay"].assert_not_called()


def test_setting_external_address_restarts_running_relay_and_updates_warning(monkeypatch):
    ctl, ctx, ui = setup_ctl(monkeypatch, relay_key=DEFAULT_RELAY_KEY, relay_host=True)
    ctl.set_relay_external("203.0.113.5")
    ctx["restart_relay"].assert_called_once()
    assert "거부" in ui.last("set_relay_info")[2]                                 # 기본 키 경고
    ctx["restart_relay"].reset_mock()
    ctl.set_relay_external("203.0.113.5")                                         # 같은 값이면 다시 시작하지 않음
    ctx["restart_relay"].assert_not_called()


def test_field_card_warns_about_default_code_only_when_internet_address_is_known():
    ctl, ctx, ui = make_ctl(cfg=UiCfg(relay_key=DEFAULT_RELAY_KEY))
    assert ctl.field_info()[2] == ""                                              # 같은 망에서만 쓰면 잔소리하지 않음
    ctl._public = "211.234.12.34"
    assert "접근 코드를 바꾸세요" in ctl.field_info()[2]
    ctx["cfg"].d["access_code"] = "내-코드-9981"
    assert ctl.field_info()[2] == ""


# ── 공용 키를 바꿔도 현장 PC 는 자동으로 새 키를 받는다 ──
from wan.server import OLD_KEY_DAYS, remember_old_key


def relay_with_agent(tmp_path, secret="agent-비밀-값", old="옛-키-1111", new="새-키-2222", aid="a1b2c3d4e5f6"):
    clock = [1000.0]
    srv = RelayServer(str(tmp_path), new, None, now=lambda: clock[0])
    srv.store[aid] = {"name": "현장", "config": {}, "rev": 0, "applied_rev": 0, "last_seen": 0, "ver": "", "secret": secret}
    remember_old_key(str(tmp_path), old, now=clock[0])
    srv.old_keys = srv._load_old_keys()
    return srv, clock


def agent_hello(key, secret="agent-비밀-값", aid="a1b2c3d4e5f6"):
    return {"t": "hello", "role": "agent", "key": key, "id": aid, "secret": secret, "name": "현장", "ver": "2", "rev": 0}


class AgentWS(FakeWS):
    """접속 직후 끊기는 현장 PC 연결 (받을 메시지가 더 없음)"""
    def __init__(self, hello, ip="192.168.0.9"):
        super().__init__(ip, hello)

    async def __aiter__(self):
        return
        yield


def _agent_connect(srv, hello, ip="192.168.0.9"):
    ws = AgentWS(hello, ip)
    run(srv.handle(ws))
    return ws


def welcome_of(ws):
    return next((m for m in ws.sent if m.get("t") == "welcome"), None)


def test_field_pc_with_old_key_and_its_secret_gets_the_new_key(tmp_path):
    srv, _ = relay_with_agent(tmp_path)
    ws = _agent_connect(srv, agent_hello("옛-키-1111"))
    assert welcome_of(ws)["rekey"] == "새-키-2222"


def test_current_key_gets_no_rekey_message(tmp_path):
    srv, _ = relay_with_agent(tmp_path)
    assert "rekey" not in welcome_of(_agent_connect(srv, agent_hello("새-키-2222")))


def test_old_key_without_the_right_secret_is_refused(tmp_path):
    srv, _ = relay_with_agent(tmp_path)
    for secret in ("", "남의-비밀", None):
        hello = agent_hello("옛-키-1111", secret=secret)
        ws = _agent_connect(srv, hello)
        assert welcome_of(ws) is None and ws.sent[-1] == {"t": "error", "reason": "key"}


def test_old_key_cannot_be_used_by_users_or_unknown_ids(tmp_path):
    srv, _ = relay_with_agent(tmp_path)
    user = {"t": "hello", "role": "user", "key": "옛-키-1111"}
    assert connect(srv, "192.168.0.9", "옛-키-1111").sent[-1]["reason"] == "key"
    ws = FakeWS("192.168.0.9", user)
    run(srv.handle(ws))
    assert ws.sent[-1] == {"t": "error", "reason": "key"}
    ws2 = _agent_connect(srv, agent_hello("옛-키-1111", aid="ffffffffffff"))     # 처음 보는 현장 ID 는 안 됨
    assert welcome_of(ws2) is None


def test_old_key_expires(tmp_path):
    srv, clock = relay_with_agent(tmp_path)
    clock[0] += (OLD_KEY_DAYS + 1) * 86400
    assert welcome_of(_agent_connect(srv, agent_hello("옛-키-1111"))) is None


def test_agent_that_never_registered_a_secret_cannot_use_old_key(tmp_path):
    srv, _ = relay_with_agent(tmp_path, secret="")
    assert welcome_of(_agent_connect(srv, agent_hello("옛-키-1111", secret="아무거나"))) is None


def test_secret_is_remembered_only_at_first_registration(tmp_path):
    srv = RelayServer(str(tmp_path), "k", None)
    _agent_connect(srv, agent_hello("k", secret="첫-비밀"))
    assert srv.store["a1b2c3d4e5f6"]["secret"] == "첫-비밀"
    _agent_connect(srv, agent_hello("k", secret="바꾼-비밀"))                    # 키만 아는 사람이 비밀 값을 덮어쓰지 못함
    assert srv.store["a1b2c3d4e5f6"]["secret"] == "첫-비밀"


def test_old_keys_file_keeps_recent_only(tmp_path):
    for i in range(12):
        remember_old_key(str(tmp_path), f"key-{i}", now=1000.0 + i)
    srv = RelayServer(str(tmp_path), "now", None, now=lambda: 2000.0)
    keys = [i["key"] for i in srv.old_keys]
    assert len(keys) == 8 and keys[-1] == "key-11"
    remember_old_key(str(tmp_path), "", now=1.0)                                  # 빈 키는 기록하지 않음
