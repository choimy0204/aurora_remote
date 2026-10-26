"""
사용자 PC 쪽 — 안내 서버에서 현장 목록을 받고, 연결하고, 설정을 보낸다
=================================================================
- 전용 스레드의 이벤트 루프에서 돈다. 화면(tkinter) 쪽은 아래 블로킹 함수만 부른다.
    WanClient.agents            켜져 있는 현장 목록
    WanClient.set_config(...)   현장 설정 보내기
    WanClient.open_session(...) 현장에 연결 → Session (request() 로 RemoteViewer · 반입/반출이 쓴다)
- 연결은 직접 연결을 먼저 시도하고, direct_timeout 안에 안 되면 비상 중계로 넘어간다.
- 중간에 끊기면 Session 이 스스로 다시 연결한다 (직접 → 중계 순).
"""

import asyncio
import json
import secrets
import socket
import threading
import time

from . import protocol as P
from .config import WanConfig
from .links import DirectLink, RelayLink, direct_available, make_peer_connection, session_description, wait_open
from .mux import Mux

DENY_TEXT = {
    "bad_code": "접근 코드가 맞지 않습니다",
    "no_code":  "이 현장 PC 에 접근 코드가 없습니다 (현장 PC 에서 접근 코드를 정하거나 중계 PC 의 공용 키를 받아야 합니다)",
    "locked":   "접근 코드를 여러 번 틀려 잠시 막혔습니다 (1분 뒤 다시 시도)",
    "offline":  "현장 PC 가 꺼져 있거나 안내 서버에 연결되어 있지 않습니다",
    "bad_sid":  "잘못된 요청입니다",
}


class Session:
    """현장 PC 와의 연결 하나. RemoteViewer 에 request 함수를 넘기고, 끊기면 스스로 다시 연결한다"""

    def __init__(self, client: "WanClient", agent_id: str, code: str, name: str = ""):
        self.client, self.agent_id, self.code, self.name = client, agent_id, code, name
        self.mux = None
        self.mode = ""                 # direct | relay
        self.state = "연결 중"
        self.closed = False
        self.tx = self.rx = 0
        self.started = time.monotonic()
        self._keeper = None

    # ── RemoteViewer · 반입/반출이 쓰는 함수 ──
    def request(self, method, route, params=None, headers=None, data=None, stream=False, timeout=(3, 30)):
        mux = self.mux
        if self.closed:
            raise P.WanError("연결이 닫혔습니다")
        if mux is None or not mux.alive:
            raise P.WanError(f"다시 연결 중… ({self.state})")
        return mux.request(method, route, params, headers, data, stream, timeout)

    def viewer_request(self, route, params, timeout):
        """RemoteViewer(request) 모양"""
        return self.request("GET", route, params, timeout=timeout)

    def stats(self) -> dict:
        mux = self.mux
        return {"mode": self.mode, "state": self.state, "tx": self.tx, "rx": self.rx,
                "rtt_ms": getattr(mux, "rtt_ms", None) if mux else None}

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.state = "닫힘"
        self.client._run(self._close_async(), wait=False)

    async def _close_async(self):
        if self._keeper:
            self._keeper.cancel()
        if self.mux:
            self.mux.close("사용자가 닫았습니다")

    # ── 이벤트 루프 스레드 ──
    def _count(self, tx, rx):
        self.tx += tx
        self.rx += rx
        if self.client.usage:
            self.client.usage.add(tx, rx)

    def _attach(self, mux: Mux, mode: str):
        self.mux, self.mode, self.state = mux, mode, "연결됨"
        self.client._changed("session")

    async def _keep(self):
        """연결이 죽으면 다시 연결 (1, 2, 4 … 15초 간격)"""
        delay = 1.0
        while not self.closed:
            mux = self.mux
            while mux is not None and mux.alive and not self.closed:
                await asyncio.sleep(0.3)
            if self.closed:
                return
            self.state, self.mode = "다시 연결 중", ""
            self.client._changed("session")
            try:
                mux = await self.client._establish(self)
                self._attach(mux, mux.link.kind)
                delay = 1.0
            except P.WanDenied as e:
                self.state, self.closed = f"거부됨: {e}", True
                self.client._changed("session")
                return
            except Exception:
                await asyncio.sleep(delay)
                delay = min(15.0, delay * 2)


class WanClient:
    def __init__(self, cfg: WanConfig, usage=None, version: str = "", on_change=None, log=print):
        self.cfg, self.usage, self.version, self.on_change, self.log = cfg, usage, version, on_change, log
        self.state = "끊김"
        self.agents: list = []
        self._loop = asyncio.new_event_loop()
        self._thread = None
        self._ws = None
        self._ws_ready = None
        self._replies: dict = {}       # q → Future
        self._pending: dict = {}       # sid → Future (answer / deny / relay_ok)
        self._links: dict = {}         # sid 8바이트 → RelayLink
        self._q = 0
        self._main_task = None
        self.direct_override = None    # 시험용: False 면 직접 연결을 건너뜀

    # ── 시작/종료 ──
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop.run_forever, name="wan-client", daemon=True)
        self._thread.start()
        self._run(self._start_main(), wait=True)

    async def _start_main(self):
        self._ws_ready = asyncio.Event()
        self._main_task = asyncio.ensure_future(self._main())

    def stop(self):
        if not self._thread:
            return
        async def _stop():
            if self._main_task:
                self._main_task.cancel()
        self._run(_stop(), wait=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(3)
        self._thread = None

    def _run(self, coro, wait=True, timeout=30):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout) if wait else fut

    def _changed(self, what: str):
        if self.on_change:
            try:
                self.on_change(what)
            except Exception:
                pass

    def _set_state(self, state: str):
        if state != self.state:
            self.state = state
            self._changed("state")

    # ── 안내 서버 연결 유지 ──
    async def _main(self):
        from websockets.asyncio.client import connect
        attempt = 0
        while True:
            url = self.cfg.get("relay_url")
            started = time.monotonic()
            if not url:
                self._set_state("주소 없음")
                await asyncio.sleep(3)
                continue
            try:
                self._set_state("연결 중")
                async with connect(url, max_size=2 ** 20, compression=None, ping_interval=20, ping_timeout=20,
                                   open_timeout=10) as ws:
                    await ws.send(json.dumps({"t": "hello", "role": "user", "key": self.cfg.get("relay_key"),
                                              "who": socket.gethostname(), "ver": self.version}))
                    first = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if first.get("t") == "error":
                        self._set_state("키 불일치")
                        raise P.WanDenied("안내 서버 키가 맞지 않습니다")
                    self._ws = ws
                    self.agents = first.get("agents", [])
                    self._set_state("연결됨")
                    self._changed("agents")
                    self._ws_ready.set()
                    await self._read(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.state != "키 불일치":
                    self._set_state(f"끊김")
                self.log(f"안내 서버 연결 끊김: {type(e).__name__}: {e}")
            finally:
                self._ws = None
                if self._ws_ready:
                    self._ws_ready.clear()
                for link in list(self._links.values()):
                    link.close()
                self._links.clear()
                for fut in list(self._replies.values()) + list(self._pending.values()):
                    if not fut.done():
                        fut.set_exception(P.WanError("안내 서버와 연결이 끊겼습니다"))
            attempt = 0 if time.monotonic() - started > 30 else attempt + 1
            await asyncio.sleep(min(30.0, 1.5 ** min(attempt, 8)))

    async def _read(self, ws):
        async for msg in ws:
            if isinstance(msg, bytes):
                link = self._links.get(msg[:8])
                if link:
                    link.deliver(msg[8:])
                continue
            m = json.loads(msg)
            t = m.get("t")
            if t == "agents":
                self.agents = m.get("agents", [])
                self._changed("agents")
            elif t in ("ok", "err"):
                fut = self._replies.pop(m.get("q"), None)
                if fut and not fut.done():
                    fut.set_result(m)
            elif t in ("answer", "deny", "relay_ok"):
                fut = self._pending.pop(m.get("sid"), None)
                if fut and not fut.done():
                    fut.set_result(m)
            elif t == "bye":
                link = self._links.get(bytes.fromhex(m["sid"])) if len(str(m.get("sid", ""))) == 16 else None
                if link:
                    link.close()

    async def _send(self, obj: dict):
        ws = self._ws
        if ws is None:
            raise P.WanError("안내 서버에 연결되어 있지 않습니다")
        await ws.send(json.dumps(obj, ensure_ascii=False))

    # ── 설정 보내기 ──
    def set_config(self, agent_id: str, config: dict, timeout: float = 10) -> dict:
        """→ {"online": 현장이 켜져 있어 바로 적용되는지, "rev": 설정 번호}. 실패하면 예외"""
        async def _go():
            self._q += 1
            q = self._q
            fut = self._replies[q] = self._loop.create_future()
            await self._send({"t": "set_config", "q": q, "id": agent_id, "config": config})
            return await asyncio.wait_for(fut, timeout)
        reply = self._run(_go(), timeout=timeout + 2)
        if reply.get("t") == "err":
            raise P.WanError("알 수 없는 현장 PC 입니다" if reply.get("reason") == "unknown_agent" else str(reply))
        return reply

    # ── 현장 연결 ──
    def open_session(self, agent_id: str, code: str, name: str = "") -> Session:
        if self.state != "연결됨":
            raise P.WanError("안내 서버에 연결되어 있지 않습니다")
        s = Session(self, agent_id, code, name)

        async def _go():
            mux = await self._establish(s)
            s._attach(mux, mux.link.kind)
            s._keeper = asyncio.ensure_future(s._keep())
        self._run(_go(), timeout=self.cfg.get("direct_timeout") + 40)
        return s

    async def _establish(self, s: Session) -> Mux:
        """직접 연결을 먼저, 안 되면 비상 중계. 접속 코드가 틀리거나 현장이 꺼져 있으면 바로 실패"""
        direct_ok = direct_available() if self.direct_override is None else self.direct_override
        if direct_ok:
            try:
                return await self._direct(s)
            except P.WanDenied:
                raise
            except Exception as e:
                self.log(f"직접 연결 실패 → 비상 중계로 전환: {type(e).__name__}: {e}")
        return await self._relay(s)

    def _mux(self, link, s: Session) -> Mux:
        return Mux(link, self._loop, name=s.agent_id, on_bytes=s._count)

    @staticmethod
    def _denied(reply: dict):
        reason = reply.get("reason", "")
        text = DENY_TEXT.get(reason, f"현장 PC 가 거부했습니다 ({reason})")
        return P.WanError(text) if reason == "offline" else P.WanDenied(text)

    async def _direct(self, s: Session) -> Mux:
        sid = secrets.token_hex(8)
        fut = self._pending[sid] = self._loop.create_future()
        timeout = float(self.cfg.get("direct_timeout"))
        t0 = time.monotonic()
        pc = make_peer_connection(self.cfg.get("stun"))
        try:
            ch = pc.createDataChannel("aurora")
            await pc.setLocalDescription(await pc.createOffer())
            sdp = pc.localDescription.sdp
            self.log(f"직접 연결 요청 전송 (후보 {sdp.count('a=candidate')}개, 후보 수집 {time.monotonic() - t0:.1f}초)")
            await self._send({"t": "connect", "id": s.agent_id, "sid": sid, "code": s.code,
                              "offer": {"sdp": sdp, "type": pc.localDescription.type}})
            reply = await asyncio.wait_for(fut, timeout)
            self.log(f"현장 응답 수신 ({reply.get('t')}, {time.monotonic() - t0:.1f}초)")
            if reply["t"] == "deny":
                if reply.get("reason") == "no_direct":
                    raise P.WanError("현장 PC 가 직접 연결을 지원하지 않습니다")
                raise self._denied(reply)
            await pc.setRemoteDescription(session_description(reply["answer"]))
            await wait_open(ch, max(0.5, timeout - (time.monotonic() - t0)))
        except P.WanTimeout:                  # 응답은 받았지만 연결 검사(ICE)가 시간 안에 끝나지 않음 — NAT·방화벽이 막은 경우
            await pc.close()
            raise
        except asyncio.TimeoutError:          # 현장이 연결 요청에 답하지 않음
            await pc.close()
            raise P.WanTimeout("현장이 직접 연결 요청에 응답하지 않습니다") from None
        except BaseException:
            await pc.close()
            raise
        finally:
            self._pending.pop(sid, None)
        self.log(f"직접 연결됨 ({time.monotonic() - t0:.1f}초)")
        return self._mux(DirectLink(ch, pc), s)

    async def _relay(self, s: Session) -> Mux:
        sid = secrets.token_hex(8)
        fut = self._pending[sid] = self._loop.create_future()
        try:
            await self._send({"t": "relay", "id": s.agent_id, "sid": sid, "code": s.code})
            reply = await asyncio.wait_for(fut, 15)
        except asyncio.TimeoutError:
            raise P.WanTimeout("비상 중계 응답이 없습니다") from None
        finally:
            self._pending.pop(sid, None)
        if reply["t"] == "deny":
            raise self._denied(reply)

        def bye(sid8: bytes):
            self._links.pop(sid8, None)
            if self._ws is not None:
                asyncio.ensure_future(self._send({"t": "bye", "sid": sid8.hex()}))
        link = RelayLink(self._ws, bytes.fromhex(sid), on_bye=bye)
        self._links[link.sid] = link
        self.log("비상 중계로 연결됨")
        return self._mux(link, s)
