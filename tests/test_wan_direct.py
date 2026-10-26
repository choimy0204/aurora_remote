"""주소로 바로 접속 (안내 서버 없이) — 실제 에이전트 · websockets · DirectClient"""

import hashlib
import time

import pytest

from wan import protocol as P
from wan import agent as agent_mod
from wan.config import WanConfig
from wan.direct import DirectClient
from wan.usage import UsageStore
from wan_helpers import AgentRunner, FakeLocal, eventually, free_port, pattern


class Site:
    """안내 서버 없이 주소 접속만 받는 현장 + 사용자 쪽 DirectClient"""

    def __init__(self, tmp, code="1234", key=""):
        self.tmp = str(tmp)
        self.local = FakeLocal()
        self.port = free_port()
        self.agent = AgentRunner(self.tmp + "/field", "", self.local.port, code=code, key=key, direct_port=self.port)
        eventually(lambda: self.agent.agent.direct_state == "listening", msg="주소 접속 대기")
        self.cfg = WanConfig(self.tmp + "/user")
        self.usage = UsageStore(self.tmp + "/user/wan_usage.json")
        self.client = DirectClient(self.cfg, self.usage, "2.16.0", log=lambda m: None)
        self.address = f"127.0.0.1:{self.port}"

    def close(self):
        for fn in (self.client.stop, self.agent.stop, self.local.stop):
            try:
                fn()
            except Exception:
                pass


@pytest.fixture
def site(tmp_path):
    s = Site(tmp_path)
    yield s
    s.close()


def test_connects_with_address_and_code_without_relay(site):
    s = site.client.open_session(site.address, "1234")
    try:
        assert s.mode == "direct" and s.state == "연결됨"
        assert s.request("GET", "/info").json()["hostname"] == "FAKE-PC"
        assert s.name == "현장1"                                    # 현장이 알려 준 이름
        frame = s.viewer_request("/remote/frame", {"size": 120000, "q": 60}, (3, 8))
        assert frame.content == pattern(120000)
        assert site.usage.day_total() > 0                            # 데이터 사용량 집계에도 들어감
    finally:
        s.close()


def test_big_file_roundtrip_over_address(site):
    s = site.client.open_session(site.address, "1234")
    try:
        size = 3 * 1024 * 1024
        with s.request("GET", "/fs/file", {"path": f"size:{size}"}, stream=True, timeout=(3, 30)) as r:
            assert hashlib.sha256(b"".join(r.iter_content(1 << 20))).hexdigest() == hashlib.sha256(pattern(size)).hexdigest()
        data = pattern(1_500_000)
        up = s.request("PUT", "/fs/upload", {"path": "x.bin"}, data=data, timeout=(3, 30))
        assert up.json()["size"] == len(data)
    finally:
        s.close()


def test_wrong_code_is_denied(site):
    with pytest.raises(P.WanDenied, match="맞지 않"):
        site.client.open_session(site.address, "nope")


def test_lockout_after_repeated_failures(site):
    for _ in range(agent_mod.CODE_FAILS):
        with pytest.raises(P.WanDenied):
            site.client.open_session(site.address, "bad")
    with pytest.raises(P.WanDenied, match="막혔"):
        site.client.open_session(site.address, "1234")


def test_blocked_routes_stay_blocked_over_address(site):
    s = site.client.open_session(site.address, "1234")
    try:
        r = s.request("GET", "/command")
        assert r.status_code in (403, 404)
        assert not any(h[1] == "/command" for h in site.local.hits)
    finally:
        s.close()


def test_shared_key_is_code_when_no_access_code(tmp_path):
    s = Site(tmp_path, code="", key="shared-1")
    try:
        s.client.open_session(s.address, "shared-1").close()
        with pytest.raises(P.WanDenied, match="맞지 않"):
            s.client.open_session(s.address, "wrong")
    finally:
        s.close()


def test_no_code_at_all_denies(tmp_path):
    s = Site(tmp_path, code="", key="")
    try:
        with pytest.raises(P.WanDenied, match="코드"):
            s.client.open_session(s.address, "")
    finally:
        s.close()


def test_code_changed_on_screen_is_applied_without_restart(site):
    """화면(다른 프로세스)이 파일에 접근 코드를 바꿔 쓰면 에이전트가 다음 접속부터 따라간다"""
    other = WanConfig(site.tmp + "/field")
    time.sleep(0.05)
    other.update(access_code="new-code")
    s = site.client.open_session(site.address, "new-code")
    s.close()
    with pytest.raises(P.WanDenied):
        site.client.open_session(site.address, "1234")


def test_nothing_listening_gives_clear_error(site):
    with pytest.raises(P.WanError, match="거부|닿지"):
        site.client.open_session(f"127.0.0.1:{free_port()}", "1234")


def test_port_change_rebinds_listener(site):
    new_port = free_port()
    other = WanConfig(site.tmp + "/field")
    other.update(direct_port=new_port)
    eventually(lambda: site.agent.agent.direct_state == "listening" and _opens(site, new_port), timeout=8,
               msg="새 포트로 다시 열림")
    with pytest.raises(P.WanError):
        site.client.open_session(site.address, "1234")


def _opens(site, port):
    try:
        site.client.open_session(f"127.0.0.1:{port}", "1234").close()
        return True
    except P.WanError:
        return False


def test_listener_off_closes_port(site):
    WanConfig(site.tmp + "/field").update(direct_listen=False)
    eventually(lambda: site.agent.agent.direct_state == "off", timeout=8, msg="수신 끔")
    with pytest.raises(P.WanError):
        site.client.open_session(site.address, "1234")


def test_busy_port_reports_and_does_not_crash(tmp_path):
    import socket
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    local = FakeLocal()
    ag = AgentRunner(str(tmp_path / "f"), "", local.port, code="1", direct_port=port)
    try:
        eventually(lambda: ag.agent.direct_state == "busy", msg="포트 사용 중 표시")
        assert any("열지 못했습니다" in m for m in ag.logs)
    finally:
        ag.stop()
        local.stop()
        blocker.close()


def test_session_dies_with_agent_and_reconnects_when_back(tmp_path):
    s = Site(tmp_path)
    try:
        sess = s.client.open_session(s.address, "1234")
        s.agent.stop()
        eventually(lambda: sess.mux is None or not sess.mux.alive, timeout=15, msg="현장이 꺼지면 세션 끊김")
        s.agent = AgentRunner(s.tmp + "/field", "", s.local.port, code="1234", direct_port=s.port)
        eventually(lambda: sess.mux is not None and sess.mux.alive, timeout=20, msg="현장이 다시 켜지면 자동 재연결")
        assert sess.request("GET", "/info").status_code == 200
        sess.close()
    finally:
        s.close()


def test_agent_without_relay_url_is_quiet(site):
    time.sleep(0.5)
    assert not any("안내 서버 주소가 설정되지 않았습니다" in m for m in site.agent.logs)
