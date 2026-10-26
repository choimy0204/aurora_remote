"""암호화(TLS) · 인증서 지문 고정 — 중계 서버 · 주소 접속 · 업데이트 · 접속 정보"""

import asyncio
import os
import socket
import threading

import pytest

from wan import connect_code as cc
from wan import protocol as P
from wan import tls
from wan import update as U
from wan.client import WanClient
from wan.config import WanConfig
from wan.direct import DirectClient
from wan.server import RelayServer, run_server
from wan_helpers import AgentRunner, FakeLocal, LoopThread, eventually, free_port


# ── 인증서 · 지문 ──
def test_cert_is_created_once_and_fingerprint_is_stable(tmp_path):
    p = tls.ensure_cert(str(tmp_path), "relay")
    assert tls.ensure_cert(str(tmp_path), "relay") == p
    fp = tls.cert_fingerprint(p)
    assert len(fp) == 64 and fp == tls.cert_fingerprint(p)
    assert tls.cert_fingerprint(str(tmp_path / "none.pem")) == ""
    other = tls.cert_fingerprint(tls.ensure_cert(str(tmp_path), "direct"))
    assert other != fp
    if os.name != "nt":
        assert oct(os.stat(p).st_mode & 0o777) == "0o600"             # 개인 키가 든 파일은 본인만


def test_two_processes_creating_cert_at_once_end_up_with_one(tmp_path):
    out = []
    ts = [threading.Thread(target=lambda: out.append(tls.cert_fingerprint(tls.ensure_cert(str(tmp_path), "x"))))
          for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(set(out)) == 1 and out[0]
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_fingerprint_comparison_ignores_case():
    assert tls.same_fingerprint("ABCD", "abcd") and not tls.same_fingerprint("abcd", "abce")
    assert not tls.same_fingerprint("", "abcd")


# ── 접속 정보에 지문 ──
def test_connection_codes_carry_fingerprint():
    code = cc.encode(["wss://a:1"], "k", fp="AB" * 32)
    assert cc.decode_full(code) == (["wss://a:1"], "k", "ab" * 32) and cc.decode(code) == (["wss://a:1"], "k")
    assert cc.decode_full(cc.encode(["ws://a:1"], "k"))[2] == ""
    d = cc.encode_direct(["wss://a:2"], "c", "cd" * 32)
    assert cc.decode_full(d, cc.PREFIX_DIRECT) == (["wss://a:2"], "c", "cd" * 32)


def test_make_url_scheme():
    assert cc.make_url("1.2.3.4", 9100, "wss") == "wss://1.2.3.4:9100"
    assert cc.make_url("1.2.3.4:7", 9100, "wss") == "wss://1.2.3.4:7"
    assert cc.make_url("ws://x:1", 9100, "wss") == "ws://x:1"


def test_apply_relay_code_stores_fingerprint_and_remember_direct(tmp_path):
    cfg = WanConfig(str(tmp_path))
    port = free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen()
        cc.apply_relay_code(cfg, cc.encode([f"wss://127.0.0.1:{port}"], "k", fp="ab" * 32))
        assert cfg.get("relay_url").startswith("wss://") and cfg.get("relay_fp") == "ab" * 32
        addr = cc.remember_direct(cfg, cc.encode_direct([f"wss://127.0.0.1:{port}"], "c", "cd" * 32))
    assert cfg.get("direct_targets")[addr]["fp"] == "cd" * 32


# ── 중계 서버 (wss) ──
class TlsRelay:
    def __init__(self, tmp, key="k"):
        self.lt = LoopThread()
        self.pem = tls.ensure_cert(str(tmp / "relay"), "relay")
        self.fp = tls.cert_fingerprint(self.pem)
        self.server = RelayServer(str(tmp / "relay"), key, str(tmp / "upd"))
        ctx = tls.server_context(self.pem)
        self.port = free_port()
        self.task = asyncio.run_coroutine_threadsafe(run_server(self.server, "127.0.0.1", self.port, ctx), self.lt.loop)
        eventually(self._up, msg="wss 서버 시작")

    def _up(self):
        try:
            socket.create_connection(("127.0.0.1", self.port), 0.3).close()
            return True
        except OSError:
            return False

    def close(self):
        self.task.cancel()
        self.lt.stop()


def user_client(tmp, relay, scheme="wss", fp=None, key="k", name="u"):
    cfg = WanConfig(str(tmp / name))
    cfg.update(relay_url=f"{scheme}://127.0.0.1:{relay.port}", relay_key=key, stun="",
               relay_fp=relay.fp if fp is None else fp)
    return WanClient(cfg, None, "2.16.0", log=lambda m: None), cfg


def test_client_connects_over_wss_with_pinned_fingerprint(tmp_path):
    r = TlsRelay(tmp_path)
    c, cfg = user_client(tmp_path, r)
    c.start()
    try:
        eventually(lambda: c.state == "연결됨", 10, msg=f"wss 연결: {c.state}")
    finally:
        c.stop(); r.close()


def test_client_refuses_wrong_fingerprint(tmp_path):
    r = TlsRelay(tmp_path)
    c, cfg = user_client(tmp_path, r, fp="0" * 64)
    c.start()
    try:
        eventually(lambda: c.state == "인증서 불일치", 10, msg=f"가짜 서버 거부: {c.state}")
        assert c.state != "연결됨"
    finally:
        c.stop(); r.close()


def test_first_connection_remembers_fingerprint_then_pins_it(tmp_path):
    r = TlsRelay(tmp_path)
    c, cfg = user_client(tmp_path, r, fp="")
    c.start()
    try:
        eventually(lambda: c.state == "연결됨", 10)
        assert cfg.get("relay_fp") == r.fp                               # 처음 한 번 기억
    finally:
        c.stop()
    cfg.update(relay_fp="1" * 64)                                        # 이후 서버가 바뀐 것처럼
    c2 = WanClient(cfg, None, "2.16.0", log=lambda m: None)
    c2.start()
    try:
        eventually(lambda: c2.state == "인증서 불일치", 10)
    finally:
        c2.stop(); r.close()


def test_plain_ws_client_cannot_talk_to_tls_relay(tmp_path):
    r = TlsRelay(tmp_path)
    c, _ = user_client(tmp_path, r, scheme="ws", fp="")
    c.start()
    try:
        eventually(lambda: c.state in ("끊김", "연결 중"), 5)
        import time; time.sleep(1.5)
        assert c.state != "연결됨"
    finally:
        c.stop(); r.close()


def test_field_agent_and_user_work_through_wss_relay_end_to_end(tmp_path):
    r = TlsRelay(tmp_path)
    local = FakeLocal()
    ag = AgentRunner(str(tmp_path / "field"), f"wss://127.0.0.1:{r.port}", local.port, code="77", key="k")
    ag.cfg.update(relay_fp=r.fp)
    c, cfg = user_client(tmp_path, r)
    c.start()
    try:
        eventually(lambda: c.state == "연결됨" and any(a["online"] for a in c.agents), 15, msg="현장이 목록에 나타남")
        c.direct_override = False                                        # 중계 경로로
        s = c.open_session(c.agents[0]["id"], "77")
        assert s.mode == "relay" and s.request("GET", "/info").json()["hostname"] == "FAKE-PC"
        s.close()
    finally:
        c.stop(); ag.stop(); local.stop(); r.close()


def test_update_download_pins_the_relay(tmp_path):
    r = TlsRelay(tmp_path)
    class Cfg:
        def __init__(self, fp): self.fp = fp
        def get(self, k, d=None):
            return {"relay_url": f"wss://127.0.0.1:{r.port}", "relay_key": "k", "relay_fp": self.fp}.get(k, d)
    try:
        bad = U.check_and_download(Cfg("0" * 64), "2.16.0", dest_dir=str(tmp_path / "d1"))
        assert bad["status"] == "error" and "인증서" in bad["msg"]
        ok = U.check_and_download(Cfg(r.fp), "2.16.0", dest_dir=str(tmp_path / "d2"))
        assert "인증서" not in ok.get("msg", "")                          # 지문이 맞으면 연결은 통과 (새 버전은 없어서 up-to-date 등)
    finally:
        r.close()


# ── 주소 접속 (wss) ──
class DirectSite:
    def __init__(self, tmp):
        self.tmp = tmp
        self.local = FakeLocal()
        self.port = free_port()
        self.agent = AgentRunner(str(tmp / "field"), "", self.local.port, code="1234", direct_port=self.port)
        eventually(lambda: self.agent.agent.direct_state == "listening")
        self.fp = tls.cert_fingerprint(tls.ensure_cert(str(tmp / "field"), "direct"))
        self.client = DirectClient(WanConfig(str(tmp / "user")), None, "2.16.0", log=lambda m: None)
        self.address = f"127.0.0.1:{self.port}"

    def close(self):
        for fn in (self.client.stop, self.agent.stop, self.local.stop):
            try:
                fn()
            except Exception:
                pass


@pytest.fixture
def dsite(tmp_path):
    s = DirectSite(tmp_path)
    yield s
    s.close()


def test_address_session_reports_fingerprint_and_accepts_pin(dsite):
    s = dsite.client.open_session(dsite.address, "1234")                 # 지문을 몰라도 처음에는 접속 (지문을 알려 줌)
    assert s.fp == dsite.fp
    s.close()
    dsite.client.open_session(dsite.address, "1234", pin=dsite.fp).close()


def test_address_session_refuses_different_certificate(dsite):
    with pytest.raises(tls.PinMismatch):
        dsite.client.open_session(dsite.address, "1234", pin="0" * 64)
    with pytest.raises(P.WanDenied, match="인증서"):
        dsite.client.open_session(dsite.address, "1234", pin="f" * 64)


def test_nothing_readable_on_the_wire(tmp_path):
    """현장 PC 와 사용자 사이에 끼어든 관찰자(TCP 중계)가 접근 코드와 화면 내용을 평문으로 볼 수 없다"""
    s = DirectSite(tmp_path)
    seen = bytearray()
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()
    proxy_port = srv.getsockname()[1]

    def pump(a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                seen.extend(d)
                b.sendall(d)
        except OSError:
            pass
        finally:
            for x in (a, b):
                try: x.close()
                except OSError: pass

    def serve_one():
        c, _ = srv.accept()
        up = socket.create_connection(("127.0.0.1", s.port))
        threading.Thread(target=pump, args=(c, up), daemon=True).start()
        pump(up, c)
    threading.Thread(target=serve_one, daemon=True).start()
    try:
        sess = s.client.open_session(f"127.0.0.1:{proxy_port}", "1234", pin=s.fp)
        assert sess.request("GET", "/info").json()["hostname"] == "FAKE-PC"
        sess.close()
        data = bytes(seen)
        assert len(data) > 500 and b"1234" not in data and b"FAKE-PC" not in data and b"hello" not in data
    finally:
        srv.close(); s.close()
