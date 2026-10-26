"""
주소로 바로 접속 (안내 서버 없이)
=================================
사용자가 현장 PC 의 주소(IP[:포트]) 와 접근 코드를 입력하면 현장 PC 가 열어 둔 포트(agent.run_direct)에 websocket 으로 붙는다.
  - 같은 망 · VPN · 공유기 포트포워딩이 된 현장처럼 주소가 닿는 곳에서 쓴다 (닿지 않는 곳은 안내 서버 방식을 쓴다).
  - 연결 뒤에는 WanClient 와 같은 Session 을 돌려주므로 원격 창 · 반입/반출은 방식과 상관없이 똑같이 동작한다.
  - 전용 스레드의 이벤트 루프에서 돌아 안내 서버 연결(WanClient)과 서로 영향을 주지 않는다.
"""

import asyncio
import json
import socket
import threading

from . import protocol as P
from . import tls
from .client import Session, WanClient
from .links import WsLink
from .mux import Mux

OPEN_TIMEOUT = 8.0       # 주소에 닿기까지 기다리는 시간
HELLO_TIMEOUT = 10.0     # 접근 코드 확인 응답을 기다리는 시간


class DirectClient:
    def __init__(self, cfg, usage=None, version: str = "", on_change=None, log=print):
        self.cfg, self.usage, self.version, self.on_change, self.log = cfg, usage, version, on_change, log
        self._loop = asyncio.new_event_loop()
        self._thread = None
        self._guard = threading.Lock()
        self._sessions: list = []

    # ── 이벤트 루프 스레드 (처음 쓸 때 시작) ──
    def _ensure_loop(self):
        with self._guard:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop.run_forever, name="wan-direct", daemon=True)
                self._thread.start()

    def _run(self, coro, wait=True, timeout=30):
        self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout) if wait else fut

    def _changed(self, what: str):
        if self.on_change:
            try:
                self.on_change(what)
            except Exception:
                pass

    def stop(self):
        """열려 있는 세션을 모두 닫고 스레드를 끝냄"""
        open_ = [s for s in self._sessions if not s.closed]
        for s in open_:
            s.close()
        if open_ and self._thread is not None:
            try:
                self._run(asyncio.sleep(0.2), timeout=3)           # 끊는 인사가 상대에게 나갈 시간
            except Exception:
                pass
        with self._guard:
            thread, self._thread = self._thread, None
        if thread is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            thread.join(3)

    # ── 접속 ──
    def open_session(self, address: str, code: str, name: str = "", pin: str = "") -> Session:
        """address: '주소:포트'.  pin: 알고 있는 현장 PC 인증서 지문 (없으면 처음 접속에서 알아내 session.fp 에 담는다).
        접근 코드가 틀리거나 닿지 않으면 예외 (WanDenied · WanError)"""
        s = Session(self, address, code, name)
        s.pin = pin

        async def _go():
            mux = await self._establish(s)
            s._attach(mux, mux.link.kind)
            s._keeper = asyncio.ensure_future(s._keep())
        self._run(_go(), timeout=OPEN_TIMEOUT + HELLO_TIMEOUT + 5)
        self._sessions = [x for x in self._sessions if not x.closed] + [s]
        return s

    async def _establish(self, s: Session) -> Mux:
        try:
            ws = await tls.connect_ws(f"wss://{s.agent_id}", s.pin, max_size=2 ** 20, compression=None,
                                      ping_interval=20, ping_timeout=20, open_timeout=OPEN_TIMEOUT)
        except tls.PinMismatch:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise P.WanTimeout("응답이 없습니다 — 주소와 포트, 현장 PC 의 방화벽(공유기 포트포워딩)을 확인하세요") from None
        except ConnectionRefusedError:
            raise P.WanError("연결이 거부되었습니다 — 현장 PC 가 꺼져 있거나 이 포트를 받고 있지 않습니다") from None
        except OSError as e:
            raise P.WanError(f"현장 PC 에 닿지 않습니다 ({e.strerror or type(e).__name__})") from None
        except Exception as e:                       # 포트는 열려 있지만 Aurora 현장 PC 가 아닌 경우 등
            raise P.WanError(f"현장 PC 가 아닙니다 ({type(e).__name__})") from None
        try:
            await ws.send(json.dumps({"t": "hello", "code": s.code, "who": socket.gethostname(),
                                      "ver": self.version}, ensure_ascii=False))
            reply = json.loads(await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT))
        except BaseException:
            await ws.close()
            raise
        if reply.get("t") != "ok":
            await ws.close()
            raise WanClient._denied(reply)
        s.fp = ws.peer_fp
        if not s.name and reply.get("name"):
            s.name = str(reply["name"])
        link = WsLink(ws)
        asyncio.ensure_future(link.pump())
        self.log(f"주소로 연결됨: {s.agent_id}")
        return Mux(link, self._loop, name=s.agent_id, on_bytes=s._count)
