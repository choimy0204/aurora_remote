"""'이 PC 에서 안내 서버 운영' 시험 — 런타임(진짜 별도 프로세스) · 화면 컨트롤러 · 방화벽/주소 도우미"""

import os
import socket
import sys
import urllib.request
from unittest.mock import MagicMock

import pytest

import wan.runtime as rt
from wan import netutil
from wan import ui as wui
from wan.client import WanClient
from wan.config import RELAY_EXIT_PORT_BUSY, WanConfig
from wan_helpers import eventually

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "비밀-key-123"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def health(port: int) -> bool:
    """앱이 띄우는 안내 서버는 암호화(https)가 기본 — 자체 서명 인증서라 검사는 생략하고 응답만 본다"""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    try:
        with urllib.request.urlopen(f"https://127.0.0.1:{port}/health", timeout=1, context=ctx) as r:
            return r.read().strip() == b"ok"
    except Exception:
        return False


@pytest.fixture
def host(tmp_path, monkeypatch):
    """진짜 안내 서버 프로세스를 띄우는 WanRuntime (프로그램 대신 python -m wan.relay_run)"""
    monkeypatch.setenv("AURORA_WAN_DIR", str(tmp_path))          # 자식 프로세스의 로그·alive 파일 위치
    monkeypatch.setenv("PYTHONPATH", ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    made = []

    def _make(**cfg):
        r = rt.WanRuntime("2.16.0", lambda: [sys.executable, "-c", "import time; time.sleep(60)"], None, str(tmp_path),
                          lambda m: None,
                          relay_command=lambda: [sys.executable, "-m", "wan.relay_run", "--parent-pid", str(os.getpid()),
                                                 "--discover-port", str(free_port())])
        r.cfg.update(relay_port=free_port(), relay_key=KEY, **cfg)
        made.append(r)
        return r
    yield _make
    for r in made:
        r.shutdown()


def test_relay_runs_as_separate_process_and_stops(host):
    r = host()
    r.set_relay_host(True)
    port = r.cfg.get("relay_port")
    eventually(lambda: health(port), 15, msg="안내 서버가 떠야 함")
    st = r.relay_state()
    assert st["state"] == "running" and st["pid"] not in (None, os.getpid())
    assert KEY not in " ".join(r.relay_supervisor.cmd)                 # 키는 명령줄에 나오면 안 됨
    assert r.cfg.get("relay_host") is True
    r.stop_relay()
    eventually(lambda: not health(port), 10, msg="끄면 포트가 닫혀야 함")
    assert r.relay_state()["state"] == "stopped"
    assert r.cfg.get("relay_host") is True                             # stop_relay 는 설정을 바꾸지 않음


def test_clients_can_use_the_hosted_relay_with_key_only(host, tmp_path):
    r = host()
    r.set_relay_host(True)
    port = r.cfg.get("relay_port")
    eventually(lambda: health(port), 15)
    fp = r.relay_fp()                                                   # 접속 정보에 들어갈 인증서 지문
    assert len(fp) == 64
    good = WanConfig(str(tmp_path / "u1"))
    good.update(relay_url=f"wss://127.0.0.1:{port}", relay_key=KEY, relay_fp=fp, stun="")
    bad = WanConfig(str(tmp_path / "u2"))
    bad.update(relay_url=f"wss://127.0.0.1:{port}", relay_key="틀린키", relay_fp=fp, stun="")
    c1 = WanClient(good, None, "2.16.0", log=lambda m: None)
    c2 = WanClient(bad, None, "2.16.0", log=lambda m: None)
    c1.start(); c2.start()
    try:
        eventually(lambda: c1.state == "연결됨", 15, msg=f"올바른 키로 연결: {c1.state}")
        eventually(lambda: c2.state != "연결 중", 15)
        assert c2.state != "연결됨"                                      # 틀린 키는 거부
    finally:
        c1.stop(); c2.stop()


def test_start_launches_relay_when_saved_as_on(host):
    r = host(relay_host=True)
    r.start()
    eventually(lambda: health(r.cfg.get("relay_port")), 15)


def test_restart_relay_gives_new_process_and_only_when_running(host):
    r = host()
    r.restart_relay()                                                   # 꺼져 있으면 아무 일도 없음
    assert r.relay_supervisor is None
    r.set_relay_host(True)
    port = r.cfg.get("relay_port")
    eventually(lambda: health(port), 15)
    pid = r.relay_state()["pid"]
    r.restart_relay()
    eventually(lambda: health(port) and r.relay_state()["pid"] not in (None, pid), 15)


def test_port_in_use_is_reported_with_exit_code(host):
    r = host()
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen()
        r.cfg.update(relay_port=blocker.getsockname()[1])
        r.set_relay_host(True)
        st = eventually(lambda: (lambda s: s if s["last_exit"] is not None else None)(r.relay_state()), 15,
                        msg="포트가 막혀 있으면 프로세스가 끝나야 함")
        assert st["last_exit"] == RELAY_EXIT_PORT_BUSY
        assert wui.WanTabController({"cfg": r.cfg, "client": MagicMock(), "usage": None,
                                     "relay_state": r.relay_state}, lambda f: f(), ui=wui.NullUI(),
                                    spawn=lambda f: f()).relay_status()[1] in ("bad", "warn", "ok")


def test_unavailable_without_relay_command(tmp_path):
    r = rt.WanRuntime("2.16.0", lambda: [], None, str(tmp_path), lambda m: None)
    assert r.tab_context(lambda *a: None)["relay_available"] is False
    r.start()                                                           # relay_host 가 꺼져 있으면 아무 일 없음
    with pytest.raises(RuntimeError):
        r.set_relay_host(True)
    r.shutdown()


# ─────────────────────────────────────────
# 화면 컨트롤러 (tkinter 없음)
# ─────────────────────────────────────────
class Cfg:
    def __init__(self, **kw):
        self.d = {"relay_host": False, "relay_port": 9100, "relay_key": "", "relay_url": "", "agent_id": "a1b2c3d4e5f6"}
        self.d.update(kw)

    def get(self, k, default=None):
        return self.d.get(k, default)

    def update(self, **kw):
        self.d.update(kw)

    def ensure_id(self):
        return self.d["agent_id"]


class Rec(wui.NullUI):
    def __init__(self):
        self.calls = []

    def __getattribute__(self, name):
        if name.startswith(("set_", "show_")):
            return lambda *a: object.__getattribute__(self, "calls").append((name,) + a)
        return object.__getattribute__(self, name)

    def last(self, name):
        return [c for c in self.calls if c[0] == name][-1][1:]


def make_ctl(state=None, **cfg):
    ctx = {"cfg": Cfg(**cfg), "client": MagicMock(), "usage": MagicMock(), "relay_available": True,
           "relay_state": lambda: state or {"state": "stopped"}, "set_relay_host": MagicMock(),
           "restart_relay": MagicMock(), "log": MagicMock()}
    ui = Rec()
    return wui.WanTabController(ctx, lambda f: f(), ui=ui, spawn=lambda f: f()), ctx, ui


def test_validate_port():
    assert wui.validate_port(" 9100 ") == (9100, None)
    assert wui.validate_port("abc")[1] and wui.validate_port("80")[1] and wui.validate_port("70000")[1]


def test_relay_status_texts():
    ctl, *_ = make_ctl({"state": "running", "pid": 77})
    assert ctl.relay_status() == ("운영 중 (PID 77)", "ok")
    ctl, *_ = make_ctl({"state": "stopped"})
    assert ctl.relay_status() == ("꺼짐", "off")
    ctl, *_ = make_ctl({"state": "waiting", "next_restart_in": 4.2, "restarts": 2, "last_exit": 1})
    assert ctl.relay_status() == ("5초 뒤 재시작 · 재시작 2회", "warn")
    ctl, *_ = make_ctl({"state": "waiting", "next_restart_in": 4.2, "last_exit": RELAY_EXIT_PORT_BUSY}, relay_port=9100)
    text, level = ctl.relay_status()
    assert level == "bad" and "9100" in text and "다른 프로그램" in text


def test_toggle_on_saves_port_and_starts_off_ui_thread():
    ctl, ctx, ui = make_ctl(relay_key="k")
    assert ctl.relay_toggled(True, "9200") is True
    assert ctx["cfg"].get("relay_port") == 9200
    ctx["set_relay_host"].assert_called_once_with(True)
    assert ui.last("set_relay_status")


def test_toggle_on_with_bad_port_is_rejected_and_unchecked():
    ctl, ctx, ui = make_ctl()
    assert ctl.relay_toggled(True, "abc") is False
    ctx["set_relay_host"].assert_not_called()
    assert ui.last("set_relay_enabled") == (False,) and ui.last("show_error")


def test_toggle_failure_reverts_checkbox_and_config():
    ctl, ctx, ui = make_ctl()
    ctx["set_relay_host"].side_effect = RuntimeError("boom")
    ctl.relay_toggled(True, "9100")
    assert ctx["cfg"].get("relay_host") is False
    assert ui.last("set_relay_enabled") == (False,) and "boom" in ui.last("show_error")[1]


def test_toggle_off_needs_no_valid_port():
    ctl, ctx, _ = make_ctl(relay_host=True)
    assert ctl.relay_toggled(False, "not a port") is True
    ctx["set_relay_host"].assert_called_once_with(False)


def test_info_warns_when_key_missing_and_shows_addresses(monkeypatch):
    monkeypatch.setattr(wui, "lan_addresses", lambda: ["192.168.0.140"])
    ctl, *_ = make_ctl(relay_port=9100)
    port, hint, warn = ctl.relay_info()
    assert port == "9100" and "ws://192.168.0.140:9100" in hint and "포트포워딩" in hint and "공용 키" in warn
    ctl, *_ = make_ctl(relay_key="k")
    assert ctl.relay_info()[2] == ""


def test_apply_server_restarts_hosted_relay_only_when_hosting():
    ctl, ctx, _ = make_ctl(relay_host=True)
    ctl.apply_server("ws://h:9100", "newkey")
    ctx["restart_relay"].assert_called_once()
    ctl, ctx, _ = make_ctl(relay_host=False)
    ctl.apply_server("ws://h:9100", "newkey")
    ctx["restart_relay"].assert_not_called()


def test_old_ctx_without_relay_keys_still_works():
    ctx = {"cfg": Cfg(), "client": MagicMock(), "usage": MagicMock()}
    ctl = wui.WanTabController(ctx, lambda f: f(), ui=Rec(), spawn=lambda f: f())
    assert ctl.relay_available() is False
    ctl.refresh_relay()                                                 # 아무 일도 하지 않음


def test_open_firewall_reports_result(monkeypatch):
    ctl, _, ui = make_ctl(relay_port=9300)
    monkeypatch.setattr(wui, "open_firewall", lambda port: (False, f"실패 {port}\n명령"))
    ctl.open_firewall()
    assert ui.last("show_error") == ("방화벽", "실패 9300\n명령")
    monkeypatch.setattr(wui, "open_firewall", lambda port: (True, f"열었습니다 {port}"))
    ctl.open_firewall()
    assert ui.last("show_info")[1] == "열었습니다 9300"


# ─────────────────────────────────────────
# 도우미
# ─────────────────────────────────────────
def test_firewall_command_and_non_windows():
    cmd = netutil.firewall_command(9100)
    assert "localport=9100" in cmd and "protocol=TCP" in cmd and "dir=in" in cmd
    if os.name != "nt":
        assert netutil.open_firewall(9100)[0] is False


def test_lan_addresses_are_plain_ipv4_and_exclude_loopback():
    for ip in netutil.lan_addresses():
        assert ip.count(".") == 3 and not ip.startswith(("127.", "169.254."))


def test_relay_pc_can_save_key_without_address():
    """중계 PC 는 주소 없이 공용 키만 저장할 수 있어야 한다 (예전에는 '주소를 입력하세요' 오류가 났음)"""
    ctl, ctx, ui = make_ctl(relay_host=True)
    assert ctl.apply_server("", " newkey ") is True
    assert ctx["cfg"].get("relay_key") == "newkey" and ctx["cfg"].get("relay_url") == ""
    ctx["client"].stop.assert_called_once()
    ctx["client"].start.assert_not_called()
    ctx["restart_relay"].assert_called_once()                           # 운영 중이면 새 키로 다시 시작
    assert not [c for c in ui.calls if c[0] == "show_error"]


def test_empty_address_is_still_an_error_when_not_a_relay_pc():
    ctl, ctx, ui = make_ctl()
    ctx["relay_available"] = False
    assert ctl.apply_server("", "k") is False
    assert [c for c in ui.calls if c[0] == "show_error"]
