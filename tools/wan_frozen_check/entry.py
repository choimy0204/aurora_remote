"""PyInstaller 로 묶은 상태에서 WAN 전체(안내 서버 + 현장 에이전트 + 사용자, 직접 · 중계)를 한 번 돌려 보는 자체 시험

사용법 (Windows 빌드 PC 에서):  tools/wan_frozen_check/README.txt 참고
성공하면 마지막 줄에 FROZEN SELFTEST OK 가 나온다 (직접 연결이 실패하면 aiortc 번들 문제).
"""
import asyncio, json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wan import runtime, ui, viewer, supervisor, update, agent, client, server, bridge, mux, links, config, usage, protocol  # noqa
from wan import direct, connfile, connect_code, netutil, tls  # noqa
from wan.agent import WanAgent
from wan.client import WanClient
from wan.config import WanConfig
from wan.server import RelayServer

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"hostname": "FROZEN-FIELD", "api": 2}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

def loop_thread():
    loop = asyncio.new_event_loop(); threading.Thread(target=loop.run_forever, daemon=True).start(); return loop

def wait(cond, t=15):
    end = time.time() + t
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    raise SystemExit("TIMEOUT")

def main():
    tmp = tempfile.mkdtemp()
    local = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=local.serve_forever, daemon=True).start()
    rloop = loop_thread()
    relay = RelayServer(os.path.join(tmp, "relay"))
    from websockets.asyncio.server import serve
    async def start():
        return await serve(relay.handle, "127.0.0.1", 0, max_size=2**20, compression=None)
    ws = asyncio.run_coroutine_threadsafe(start(), rloop).result(10)
    url = f"ws://127.0.0.1:{ws.sockets[0].getsockname()[1]}"
    fcfg = WanConfig(os.path.join(tmp, "field")); fcfg.update(relay_url=url, name="F", access_code="77", stun="", enabled=True)
    aloop = loop_thread()
    agent_ = WanAgent(fcfg, local.server_address[1], lambda: "k", "2.16.0", lambda m: None)
    asyncio.run_coroutine_threadsafe(agent_.run(), aloop)
    ucfg = WanConfig(os.path.join(tmp, "user")); ucfg.update(relay_url=url, stun="", direct_timeout=5, force_relay=False)
    cl = WanClient(ucfg, None, "2.16.0", log=lambda m: None); cl.start()
    wait(lambda: cl.state == "연결됨"); wait(lambda: any(a["online"] for a in cl.agents))
    aid = cl.agents[0]["id"]
    print("aiortc 사용 가능:", links.direct_available())
    s = cl.open_session(aid, "77")
    print("연결 방식:", s.mode, "|", s.request("GET", "/info").json())
    if links.direct_available():
        assert s.mode == "direct", "frozen 에서 직접 연결이 안 됨 (aiortc 번들 문제)"
    else:
        assert s.mode == "relay", "aiortc 없는 구성은 항상 중계여야 함"
    s.close()
    cl.direct_override = False
    s = cl.open_session(aid, "77")
    print("연결 방식:", s.mode, "|", s.request("GET", "/info").json())
    assert s.mode == "relay"
    s.close(); cl.stop()
    # 안내 서버 없이 주소 + 접근 코드로 바로 접속
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    dcfg = WanConfig(os.path.join(tmp, "field2")); dcfg.update(relay_url="", name="F2", access_code="88", stun="", enabled=True,
                                                               direct_listen=True, direct_port=port)
    dloop = loop_thread()
    dagent = WanAgent(dcfg, local.server_address[1], lambda: "k", "2.16.0", lambda m: None)
    asyncio.run_coroutine_threadsafe(dagent.run(), dloop)
    wait(lambda: dagent.direct_state == "listening")
    dc = direct.DirectClient(WanConfig(os.path.join(tmp, "user2")), None, "2.16.0", log=lambda m: None)
    s = dc.open_session(f"127.0.0.1:{port}", "88")
    print("주소 접속:", s.mode, "|", s.request("GET", "/info").json())
    assert s.request("GET", "/info").json()["hostname"] == "FROZEN-FIELD"
    assert s.fp == tls.cert_fingerprint(os.path.join(tmp, "field2", "direct_cert.pem")), "주소 접속 인증서 지문이 다름"
    s.close(); dc.stop()
    print("암호화(wss) + 지문 확인: OK", s.fp[:12])
    print("FROZEN SELFTEST OK")

if __name__ == "__main__":
    main()
