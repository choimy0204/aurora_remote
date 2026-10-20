"""
현장 PC 의 WAN 에이전트
=======================
- 안내 서버에 상시 연결해 고유 ID 로 등록한다 ("켜져 있음" 신호). 끊기면 간격을 늘려 가며 다시 접속한다.
- 사용자가 연결을 요청하면 **접속 코드를 이 PC 가 직접 확인**하고, 직접 연결(aiortc)을 맺거나
  안 되면 안내 서버를 거치는 비상 중계로 받는다.
- 받은 요청은 Bridge 를 통해 이 PC 의 로컬 서버(127.0.0.1)로 넘긴다.
- 안내 서버가 보낸 설정(이름·접속 코드·최고 화질·파일 허용)을 wan_config.json 에 저장한다.
이 에이전트는 별도 프로세스(AuroraClient.exe --wan-agent)로 돌아서, 문제가 생겨도 LAN 쪽에 번지지 않는다.
"""

import asyncio
import hmac
import json
import os
import random
import socket
import threading
import time

from . import protocol as P
from .bridge import Bridge, Policy
from .config import WanConfig, new_agent_id
from .links import DirectLink, RelayLink, direct_available, make_peer_connection, session_description, wait_open
from .mux import Mux

HEARTBEAT   = 15.0
RECONNECT_BASE = 1.0     # 재접속 간격 = 이 값 × 2^시도횟수 (최대 60초)
DUP_LIMIT   = 3          # 같은 ID 로 이 횟수 밀려나면 ID 가 겹친 것으로 보고 새 ID 를 받음 (디스크 복제 대비)
DUP_WINDOW  = 300.0
CODE_FAILS  = 5          # 이 횟수 연속 실패하면
CODE_LOCK   = 60.0       # 이 시간 동안 모든 접속 거부


class FileLog:
    """크기가 커지면 .1 로 넘기는 간단한 로그 파일"""

    def __init__(self, path: str, max_bytes: int = 512 * 1024):
        self.path, self.max_bytes = path, max_bytes
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def __call__(self, msg: str):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
        with self._lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass


class WanAgent:
    def __init__(self, cfg: WanConfig, bridge_port: int, key_getter, version: str = "", log=print,
                 direct_mode: str = "normal", on_bytes=None):
        """direct_mode: normal | ignore (직접 연결 요청에 응답하지 않음 — 막힌 망을 흉내 내는 시험용)"""
        self.cfg, self.version, self.log = cfg, version, log
        self.direct_mode, self.on_bytes = direct_mode, on_bytes
        self.bridge = Bridge(bridge_port, key_getter, Policy(cfg), log=log)
        self.online = False
        self.sessions: dict = {}      # sid(16자 hex) → Mux
        self._ws = None
        self._links: dict = {}        # sid 8바이트 → RelayLink
        self._fails: list = []
        self._kicked: list = []
        self._stop = asyncio.Event()
        self._tasks: set = set()

    # ── 접속 유지 ──
    async def run(self):
        attempt = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._connected_session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"안내 서버 연결 끊김: {type(e).__name__}: {e}")
            self.online = False
            attempt = 0 if time.monotonic() - started > 30 else attempt + 1
            delay = min(60.0, RECONNECT_BASE * 2.0 ** min(attempt, 6)) * random.uniform(0.8, 1.2)
            try:
                await asyncio.wait_for(self._stop.wait(), delay)
            except asyncio.TimeoutError:
                pass

    def stop(self):
        self._stop.set()

    async def _connected_session(self):
        from websockets.asyncio.client import connect
        url = self.cfg.get("relay_url")
        if not url:
            self.log("안내 서버 주소가 설정되지 않았습니다")
            await asyncio.sleep(10)
            return
        async with connect(url, max_size=2 ** 20, compression=None, ping_interval=20, ping_timeout=20,
                           open_timeout=10) as ws:
            self._ws = ws
            await ws.send(json.dumps({
                "t": "hello", "role": "agent", "key": self.cfg.get("relay_key"), "id": self.cfg.ensure_id(),
                "name": self.cfg.get("name") or socket.gethostname(), "ver": self.version,
                "rev": self.cfg.get("config_rev")}, ensure_ascii=False))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if welcome.get("t") == "error":
                raise P.WanDenied(f"안내 서버가 거부했습니다 ({welcome.get('reason')})")
            await self._apply_config(welcome.get("config"), welcome.get("rev", 0))
            self.online = True
            self.log(f"안내 서버 연결됨 ({url})")
            hb = asyncio.create_task(self._heartbeat(ws))
            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        link = self._links.get(msg[:8])
                        if link:
                            link.deliver(msg[8:])
                        continue
                    await self._on_message(ws, json.loads(msg))
            finally:
                hb.cancel()
                self._ws = None
                if getattr(ws, "close_code", None) == 4002:
                    self._note_kicked()
                for link in list(self._links.values()):     # 중계로 받던 세션은 서버 연결과 함께 끝남
                    link.close()
                self._links.clear()

    def _note_kicked(self):
        """안내 서버가 "같은 ID 가 다시 접속했다" 며 이 연결을 닫음. 자주 반복되면 다른 PC 와 ID 가 겹친 것"""
        now = time.monotonic()
        self._kicked = [t for t in self._kicked if now - t < DUP_WINDOW] + [now]
        if len(self._kicked) >= DUP_LIMIT:
            self._kicked.clear()
            self.cfg.update(agent_id=new_agent_id())
            self.log("다른 PC 와 ID 가 겹쳐 새 ID 로 바꿨습니다 (디스크 복제로 만든 PC 일 수 있음)")

    async def _heartbeat(self, ws):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT)
                await ws.send('{"t":"hb"}')
        except Exception:
            pass        # 연결이 끊긴 것 — 바깥 루프가 다시 접속한다

    async def _on_message(self, ws, m: dict):
        t = m.get("t")
        if t == "config":
            await self._apply_config(m.get("config"), m.get("rev", 0))
        elif t == "connect":
            self._spawn(self._on_connect(ws, m))
        elif t == "relay":
            await self._on_relay(ws, m)
        elif t == "bye":
            sid = str(m.get("sid", ""))
            mux = self.sessions.get(sid)
            if mux:
                mux.close("사용자가 연결을 끊었습니다")

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _apply_config(self, config, rev):
        if not isinstance(config, dict):
            return
        changed = self.cfg.apply_remote(config, rev)
        if changed:
            shown = {k: ("****" if k == "access_code" else v) for k, v in changed.items()}
            self.log(f"설정 변경 적용 (#{rev}): {shown}")
        if self._ws is not None:
            await self._ws.send(json.dumps({"t": "config_ack", "rev": int(rev)}))

    # ── 접속 코드 (현장 PC 가 직접 확인) ──
    def check_code(self, code: str) -> str | None:
        """통과면 None, 거부면 이유(no_code · bad_code · locked)"""
        now = time.monotonic()
        self._fails = [t for t in self._fails if now - t < CODE_LOCK]
        if len(self._fails) >= CODE_FAILS:
            return "locked"
        real = self.cfg.get("access_code")
        if not real:
            return "no_code"
        if not hmac.compare_digest(str(code).encode("utf-8"), real.encode("utf-8")):
            self._fails.append(now)
            self.log("접속 코드가 맞지 않는 접속 시도")
            return "bad_code"
        self._fails.clear()
        return None

    async def _reply(self, ws, **msg):
        try:
            await ws.send(json.dumps(msg, ensure_ascii=False))
        except Exception:
            pass

    # ── 직접 연결 ──
    async def _on_connect(self, ws, m: dict):
        sid = str(m.get("sid", ""))
        denied = self.check_code(m.get("code", ""))
        if denied:
            return await self._reply(ws, t="deny", sid=sid, reason=denied)
        if self.direct_mode == "ignore":
            return                                        # 막힌 망 흉내 — 사용자는 시간 초과 후 중계로 넘어감
        if not direct_available():
            return await self._reply(ws, t="deny", sid=sid, reason="no_direct")
        t0 = time.monotonic()
        self.log(f"직접 연결 요청 수신 ({sid})")
        pc = make_peer_connection(self.cfg.get("stun"))
        got = asyncio.get_running_loop().create_future()
        pc.on("datachannel", lambda ch: got.set_result(ch) if not got.done() else None)
        try:
            await pc.setRemoteDescription(session_description(m["offer"]))
            await pc.setLocalDescription(await pc.createAnswer())
            sdp = pc.localDescription.sdp
            await self._reply(ws, t="answer", sid=sid, answer={"sdp": sdp, "type": pc.localDescription.type})
            self.log(f"직접 연결 응답 전송 ({sid}, 후보 {sdp.count('a=candidate')}개, "
                     f"{time.monotonic() - t0:.1f}초 — 후보 수집 포함)")
            ch = await asyncio.wait_for(got, 30)
            await wait_open(ch, 30)
        except Exception as e:
            self.log(f"직접 연결 실패 ({sid}): {type(e).__name__}: {e}")
            await pc.close()
            return
        self._attach(sid, DirectLink(ch, pc), "직접")

    # ── 비상 중계 ──
    async def _on_relay(self, ws, m: dict):
        sid = str(m.get("sid", ""))
        denied = self.check_code(m.get("code", ""))
        if denied:
            return await self._reply(ws, t="deny", sid=sid, reason=denied)
        link = RelayLink(ws, bytes.fromhex(sid), on_bye=self._relay_bye)
        self._links[link.sid] = link
        self._attach(sid, link, "중계")
        await self._reply(ws, t="relay_ok", sid=sid)

    def _relay_bye(self, sid8: bytes):
        self._links.pop(sid8, None)
        if self._ws is not None:
            self._spawn(self._reply(self._ws, t="bye", sid=sid8.hex()))

    def _attach(self, sid: str, link, how: str):
        loop = asyncio.get_running_loop()
        mux = Mux(link, loop, name=sid, bridge=self.bridge, on_bytes=self.on_bytes,
                  on_dead=lambda reason, s=sid: self._session_ended(s, reason))
        self.sessions[sid] = mux
        self.log(f"원격 세션 시작 [{how}] {sid}")

    def _session_ended(self, sid: str, reason: str):
        self.sessions.pop(sid, None)
        self.log(f"원격 세션 종료 {sid}: {reason}")


# ─────────────────────────────────────────
# 프로세스 진입점
# ─────────────────────────────────────────
def _watch_parent(parent_pid: int):
    """부모(AuroraClient)가 사라지면 같이 끝낸다 — 남아서 exe 를 잠그지 않도록"""
    import psutil

    def _loop():
        while True:
            time.sleep(3)
            if not psutil.pid_exists(parent_pid):
                os._exit(0)
    threading.Thread(target=_loop, daemon=True).start()


async def _alive_marker(path: str):
    """감독자가 멈춘 에이전트를 알아채도록 5초마다 파일 시각을 갱신"""
    while True:
        try:
            with open(path, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
        await asyncio.sleep(5)


def run_agent(cfg_dir: str | None, bridge_port: int, key_getter, version: str = "",
              parent_pid: int | None = None, log=None, alive_path: str | None = None):
    cfg = WanConfig(cfg_dir)
    log = log or FileLog(os.path.join(cfg.dir, "wan_agent.log"))
    alive_path = alive_path or os.path.join(cfg.dir, "wan_agent.alive")
    if parent_pid:
        _watch_parent(parent_pid)
    log(f"WAN 에이전트 시작 (ID {cfg.ensure_id()}, v{version})")

    async def main():
        agent = WanAgent(cfg, bridge_port, key_getter, version, log)
        marker = asyncio.create_task(_alive_marker(alive_path))
        try:
            await agent.run()
        finally:
            marker.cancel()
    asyncio.run(main())
