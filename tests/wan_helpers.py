"""WAN 시험용 도구 — 이벤트 루프 스레드 · 느린 회선을 흉내 내는 메모리 통로 · 가짜 로컬 서버"""

import asyncio
import os
import hashlib
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from wan import protocol as P
from wan.links import Link
from wan.mux import Mux
from wan.bridge import Bridge, Policy


class LoopThread:
    """운영과 같은 구조 — 이벤트 루프는 전용 스레드, 호출은 다른 스레드에서"""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call(self, fn, *a):
        async def _w():
            return fn(*a)
        return self.run(_w())

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


class LoopLink(Link):
    """메모리 안에서 두 Mux 를 잇는 통로. rate(바이트/초)·latency(초)로 느린 회선을 흉내 낸다"""

    def __init__(self, loop, rate=None, latency=0.0, kind="direct"):
        super().__init__()
        self.kind, self.loop, self.rate, self.latency = kind, loop, rate, latency
        self.peer = None
        self.drop = False            # True 면 보낸 것이 모두 사라짐 (상대가 죽은 것처럼)
        self._pipe, self._bytes, self._pump = deque(), 0, None
        self.max_buffered = 0

    @staticmethod
    def pair(loop, rate=None, latency=0.0, kind="direct"):
        a, b = LoopLink(loop, rate, latency, kind), LoopLink(loop, rate, latency, kind)
        a.peer, b.peer = b, a
        return a, b

    async def send(self, frame):
        if self.closed:
            raise P.WanError("닫힘")
        self._pipe.append(frame)
        self._bytes += len(frame)
        self.max_buffered = max(self.max_buffered, self._bytes)
        if self._pump is None or self._pump.done():
            self._pump = asyncio.ensure_future(self._run())

    @property
    def buffered(self):
        return self._bytes

    async def _run(self):
        while self._pipe:
            frame = self._pipe.popleft()
            if self.rate:
                await asyncio.sleep(len(frame) / self.rate)
            else:
                await asyncio.sleep(0)
            self._bytes -= len(frame)
            if self.latency:
                self.loop.call_later(self.latency, self._arrive, frame)
            else:
                self._arrive(frame)
            if self._bytes < self.LOW_WATER:
                self.drained()

    def _arrive(self, frame):
        if not self.drop and self.peer is not None:
            self.peer.deliver(frame)

    def close(self):
        super().close()
        if self.peer is not None and not self.peer.closed:
            self.loop.call_soon(self.peer._mark_closed)


# ── 가짜 로컬 서버 (기존 CommandHandler 흉내) ──
LOCAL_KEY = "test-key"


def pattern(n: int) -> bytes:
    block = hashlib.sha256(b"aurora").digest() * 2048      # 64KB
    return (block * (n // len(block) + 2))[:n]


class FakeLocal:
    """127.0.0.1 에서 도는 가짜 CommandHandler. 받은 요청을 hits 에 기록한다"""

    def __init__(self):
        self.hits, self.uploads, self.last_params = [], {}, {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self):
                outer.hits.append((self.command, self.path.split("?")[0], self.headers.get("X-Aurora-Key")))
                if self.headers.get("X-Aurora-Key") != LOCAL_KEY:
                    self._json({"error": "인증 실패"}, 403)
                    return False
                return True

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                outer.last_params = q
                if not self._auth():
                    return
                if u.path == "/info":
                    return self._json({"hostname": "FAKE-PC", "api": 2})
                if u.path == "/discover":
                    return self._json([{"ip": "10.0.0.5"}])
                if u.path == "/remote/input":
                    return self._json({"ok": True, "ev": q.get("ev", "")})
                if u.path == "/remote/clip":
                    return self._json({"ok": True, "len": len(q.get("text", ""))})
                if u.path == "/remote/frame":
                    if "t" in q:                       # 느린 응답 흉내
                        time.sleep(float(q["t"]))
                    if "trunc" in q:                   # 약속한 길이보다 적게 보내고 끊음
                        self.send_response(200)
                        self.send_header("Content-Length", "100000")
                        self.end_headers()
                        self.wfile.write(pattern(30000))
                        self.wfile.flush()
                        self.connection.close()
                        return
                    body = pattern(int(q.get("size", 40000)))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                if u.path == "/fs/file":
                    return self._file(q.get("path", "size:0"))
                self._json({"error": "not found"}, 404)

            def _file(self, spec):
                size = int(spec.split(":")[1])
                data = pattern(size)
                start, code = 0, 200
                rng = self.headers.get("Range", "")
                if rng.startswith("bytes=") and rng.endswith("-"):
                    start, code = int(rng[6:-1]), 206
                body = data[start:]
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
                self.end_headers()
                try:
                    for i in range(0, len(body), 65536):
                        self.wfile.write(body[i:i + 65536])
                except OSError:
                    outer.hits.append(("ABORT", spec, None))

            def do_PUT(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if not self._auth():
                    return
                n = int(self.headers.get("Content-Length", 0))
                got, h = 0, hashlib.sha256()
                while got < n:
                    chunk = self.rfile.read(min(65536, n - got))
                    if not chunk:
                        break
                    h.update(chunk)
                    got += len(chunk)
                outer.uploads[q.get("path", "")] = (got, h.hexdigest())
                self._json({"ok": True, "size": got})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class FakeCfg:
    """WanConfig 대용 — get() 만 있으면 됨"""

    def __init__(self, **kw):
        self.d = {"max_quality": "고화질", "allow_files": True}
        self.d.update(kw)

    def get(self, key, default=None):
        return self.d.get(key, default)


class Rig:
    """사용자 Mux ↔ (통로) ↔ 현장 Mux + Bridge + 가짜 로컬 서버를 한 번에 세움"""

    def __init__(self, rate=None, latency=0.0, cfg=None, dead_after=20.0, ping_every=5.0,
                 local_port=None, key_getter=lambda: LOCAL_KEY):
        """local_port 를 주면 가짜 서버 대신 그 포트(예: 진짜 CommandHandler)로 넘김"""
        self.lt = LoopThread()
        self.local = None if local_port else FakeLocal()
        self.cfg = cfg or FakeCfg()
        self.bridge = Bridge(local_port or self.local.port, key_getter, Policy(self.cfg))
        self.dead = {"user": [], "field": []}
        self.bytes = {"user": [0, 0], "field": [0, 0]}
        self.link_u, self.link_f = LoopLink.pair(self.lt.loop, rate, latency)

        def make():
            def counter(side):
                def _c(tx, rx):
                    self.bytes[side][0] += tx
                    self.bytes[side][1] += rx
                return _c
            u = Mux(self.link_u, self.lt.loop, name="user", on_bytes=counter("user"),
                    on_dead=lambda r: self.dead["user"].append(r), ping_every=ping_every, dead_after=dead_after)
            f = Mux(self.link_f, self.lt.loop, name="field", bridge=self.bridge, on_bytes=counter("field"),
                    on_dead=lambda r: self.dead["field"].append(r), ping_every=ping_every, dead_after=dead_after)
            return u, f
        self.user, self.field = self.lt.call(make)

    def close(self):
        self.user.close()
        self.field.close()
        time.sleep(0.05)
        self.bridge.shutdown()
        if self.local:
            self.local.stop()
        self.lt.stop()


# ─────────────────────────────────────────
# 통합 시험용: 안내 서버 · 현장 에이전트 · 사용자 클라이언트를 실제로 띄움
# ─────────────────────────────────────────
def eventually(cond, timeout=10.0, step=0.05, msg=""):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"시간 안에 조건이 맞지 않음: {msg}")


class RelayRunner:
    def __init__(self, data_dir, key="", update_dir=None, port=0):
        from wan.server import RelayServer
        self.data_dir, self.key, self.update_dir = data_dir, key, update_dir
        self.lt = LoopThread()
        self.port = port
        self.start()

    def start(self):
        from wan.server import RelayServer
        from websockets.asyncio.server import serve
        self.server = RelayServer(self.data_dir, self.key, self.update_dir)

        async def go():
            # close_timeout 을 짧게: 시험에서는 상대(이벤트 루프)가 먼저 멈춰 종료 인사에 답하지 못함
            self.ws = await serve(self.server.handle, "127.0.0.1", self.port, max_size=2 ** 20, compression=None,
                                  process_request=self.server.process_request, close_timeout=0.3)
            return self.ws.sockets[0].getsockname()[1]
        self.port = self.lt.run(go())

    def stop_server(self):
        async def go():
            self.ws.close(close_connections=True)
            await self.ws.wait_closed()
        self.lt.run(go())

    def restart(self):
        self.stop_server()
        self.start()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"

    def close(self):
        try:
            self.stop_server()
        finally:
            self.lt.stop()


class AgentRunner:
    def __init__(self, cfg_dir, relay_url, local_port, name="현장1", code="", key="", direct_mode="normal"):
        from wan.agent import WanAgent
        from wan.config import WanConfig
        self.cfg = WanConfig(cfg_dir)
        self.cfg.update(relay_url=relay_url, relay_key=key, name=name, access_code=code, stun="", enabled=True)
        self.agent_id = self.cfg.ensure_id()
        self.logs = []
        self.lt = LoopThread()
        self.agent = WanAgent(self.cfg, local_port, lambda: LOCAL_KEY, "2.16.0", self.logs.append, direct_mode)
        self.task = asyncio.run_coroutine_threadsafe(self.agent.run(), self.lt.loop)

    def stop(self):
        if getattr(self, "stopped", False):
            return
        self.stopped = True
        self.lt.call(self.agent.stop)
        # 진행 중인 접속을 끊고 끝냄
        async def kill():
            ws = self.agent._ws
            if ws:
                await ws.close()
            for mux in list(self.agent.sessions.values()):
                mux.close("종료")
        self.lt.run(kill())
        self.task.result(10)
        self.lt.stop()


class World:
    """서버 + 현장 + 가짜 로컬 서버 + 사용자 클라이언트"""

    def __init__(self, tmp, code="1234", key="", direct_mode="normal", direct_timeout=4.0, update_dir=None):
        from wan.client import WanClient
        from wan.config import WanConfig
        from wan.usage import UsageStore
        self.tmp = str(tmp)
        self.relay = RelayRunner(os.path.join(self.tmp, "relay"), key, update_dir)
        self.local = FakeLocal()
        self.agent = AgentRunner(os.path.join(self.tmp, "field"), self.relay.url, self.local.port,
                                 code=code, key=key, direct_mode=direct_mode)
        self.user_cfg = WanConfig(os.path.join(self.tmp, "user"))
        self.user_cfg.update(relay_url=self.relay.url, relay_key=key, stun="", direct_timeout=direct_timeout)
        self.usage = UsageStore(os.path.join(self.tmp, "user", "wan_usage.json"))
        self.client = WanClient(self.user_cfg, self.usage, "2.16.0", log=lambda m: None)
        self.client.start()
        eventually(lambda: self.client.state == "연결됨", msg="사용자가 안내 서버에 연결")
        self.agent_id = self.agent.agent_id
        eventually(lambda: self.entry() and self.entry()["online"], msg="현장이 목록에 나타남")

    def entry(self):
        return next((a for a in self.client.agents if a["id"] == self.agent_id), None)

    def close(self):
        for fn in (self.client.stop, self.agent.stop, self.local.stop, self.relay.close):
            try:
                fn()
            except Exception:
                pass
