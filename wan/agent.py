"""
현장 PC 의 WAN 에이전트
=======================
- (선택) 안내 서버 없이도 주소로 바로 들어오는 접속을 direct_port 로 받는다 — 같은 접근 코드 확인을 거친다.
- 안내 서버에 상시 연결해 고유 ID 로 등록한다 ("켜져 있음" 신호). 끊기면 간격을 늘려 가며 다시 접속한다.
- 사용자가 연결을 요청하면 **접근 코드를 이 PC 가 직접 확인**하고, 안내 서버를 거치는 중계(기본)로 받거나
  직접 연결(aiortc, force_relay 를 껐을 때)을 맺는다.
- 처음 등록할 때 이 PC 만 아는 비밀 값과 접근 코드를 안내 서버에 알린다 (공용 키가 바뀌어도 새 키를 받아 오기 위해).
- 받은 요청은 Bridge 를 통해 이 PC 의 로컬 서버(127.0.0.1)로 넘긴다.
- 안내 서버가 보낸 설정(이름·접근 코드·최고 화질·파일 허용)을 wan_config.json 에 저장한다.
- 이 PC 가 중계 PC 이기도 하면 주소를 따로 정하지 않아도 자기 안내 서버(127.0.0.1)에 등록한다.
이 에이전트는 별도 프로세스(AuroraClient.exe --wan-agent)로 돌아서, 문제가 생겨도 LAN 쪽에 번지지 않는다.
"""

import asyncio
import hmac
import json
import os
import random
import secrets
import socket
import threading
import time

from . import protocol as P
from . import tls
from .bridge import Bridge, Policy
from .config import DEFAULT_RELAY_KEY, DIRECT_PORT, WanConfig, new_agent_id
from .netutil import is_public_ip
from .links import (DirectLink, RelayLink, WsLink, direct_available, make_peer_connection, session_description,
                    wait_open)
from .mux import Mux

HEARTBEAT   = 15.0
RECONNECT_BASE = 1.0     # 재접속 간격 = 이 값 × 2^시도횟수 (최대 60초)
DUP_LIMIT   = 3          # 같은 ID 로 이 횟수 밀려나면 ID 가 겹친 것으로 보고 새 ID 를 받음 (디스크 복제 대비)
DUP_WINDOW  = 300.0
CODE_FAILS  = 5          # 이 횟수 연속 실패하면
CODE_LOCK   = 60.0       # 이 시간 동안 모든 접속 거부
DIRECT_RETRY = 30.0      # 주소 접속용 포트를 못 잡았을 때 다시 시도하는 간격
DIRECT_HELLO_WAIT = 10.0 # 주소로 들어온 접속이 인사(코드)를 보내기를 기다리는 시간


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
        self._sent_code = ""          # 안내 서버에 마지막으로 알린 접근 코드
        self.direct_state = "off"     # 주소 접속 수신: off · listening · busy(포트를 다른 프로그램이 씀)
        self.sessions: dict = {}      # sid(16자 hex) → Mux
        self._ws = None
        self._links: dict = {}        # sid 8바이트 → RelayLink
        self._fails: dict = {}        # 접속 출처(IP) → 최근 틀린 시각들. 안내 서버를 거친 접속은 출처를 몰라 "*" 하나로 묶는다
        self._kicked: list = []
        self._stop = asyncio.Event()
        self._tasks: set = set()

    # ── 접속 유지 ──
    async def run(self):
        """안내 서버 접속 유지와 주소 접속 수신을 함께 돌린다 (한쪽이 안 돼도 다른 쪽은 계속)"""
        direct = asyncio.ensure_future(self.run_direct())
        try:
            await self._run_relay()
        finally:
            direct.cancel()
            await asyncio.gather(direct, return_exceptions=True)      # 열린 접속까지 정리된 뒤에 끝낸다

    async def _run_relay(self):
        attempt = 0
        while not self._stop.is_set():
            self._reload_cfg()
            if not self._relay_target()[0]:                  # 안내 서버 없이 주소 접속만 쓰는 PC — 조용히 기다림
                await self._sleep(2.0)
                continue
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

    def _reload_cfg(self) -> bool:
        """화면(다른 프로세스)이 설정 파일을 고쳤으면 다시 읽음 (설정 객체가 이를 지원할 때)"""
        fn = getattr(self.cfg, "reload_if_changed", None)
        return bool(fn()) if fn else False

    def _relay_target(self) -> tuple:
        """접속할 안내 서버 → (주소, 인증서 지문).  정해 둔 주소가 없어도 이 PC 가 직접 안내 서버를 운영하는 중계 PC 라면
        자기 안내 서버(127.0.0.1)에 붙는다 — 중계 PC 도 원격 대상이 되려면 자기 목록에 올라 있어야 한다"""
        url = (self.cfg.get("relay_url") or "").strip()
        if url:
            return url, self.cfg.get("relay_fp") or ""
        if self.cfg.get("relay_host"):
            try:
                port = int(self.cfg.get("relay_port") or 9100)
            except (TypeError, ValueError):
                port = 9100
            fp = tls.cert_fingerprint(os.path.join(self.cfg.dir, "relay_data", "relay_cert.pem"))
            return f"{'wss' if fp else 'ws'}://127.0.0.1:{port}", fp
        return "", ""

    async def _connected_session(self):
        self._reload_cfg()                         # 화면에서 안내 서버 주소를 바꿨으면 다음 접속부터 따라감
        url, pin = self._relay_target()
        if not url:                                          # 안내 서버 없이 주소 접속만 쓰는 PC — 조용히 기다림
            await asyncio.sleep(3)
            return
        async with tls.open_ws(url, pin, max_size=2 ** 20, compression=None, ping_interval=20,
                               ping_timeout=20, open_timeout=10) as ws:
            if self.cfg.get("relay_url"):
                tls.remember_relay_pin(self.cfg, ws)
            self._ws = ws
            await ws.send(json.dumps({
                "t": "hello", "role": "agent", "key": self.cfg.get("relay_key"), "id": self.cfg.ensure_id(),
                "name": self.cfg.get("name") or socket.gethostname(), "ver": self.version,
                "rev": self.cfg.get("config_rev"), "code": self.cfg.get("access_code") or "",
                "secret": self.cfg.ensure_agent_secret()}, ensure_ascii=False))
            self._sent_code = self.cfg.get("access_code") or ""
            welcome = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if welcome.get("t") == "error":
                raise P.WanDenied(f"안내 서버가 거부했습니다 ({welcome.get('reason')})")
            if isinstance(welcome.get("rekey"), str) and welcome["rekey"]:
                self.cfg.update(relay_key=welcome["rekey"])           # 중계 PC 가 공용 키를 바꿨다 — 새 키를 받아 저장
                self.log("중계 PC 의 공용 키가 바뀌어 새 키를 받았습니다")
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
                await self._push_code(ws)
        except Exception:
            pass        # 연결이 끊긴 것 — 바깥 루프가 다시 접속한다

    async def _push_code(self, ws):
        """화면에서 접근 코드를 바꿨으면 안내 서버에 알린다 (사용자 PC 가 새 코드를 받도록)"""
        self._reload_cfg()
        code = self.cfg.get("access_code") or ""
        if code != self._sent_code:
            await ws.send(json.dumps({"t": "code", "code": code}, ensure_ascii=False))
            self._sent_code = code

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
            await self._push_code(self._ws)

    # ── 접속 코드 (현장 PC 가 직접 확인) ──
    def check_code(self, code: str, peer: str = "") -> str | None:
        """통과면 None, 거부면 이유(no_code · bad_code · locked · weak_code).
        peer: 주소로 직접 들어온 접속의 IP — 틀린 횟수를 출처별로 세어, 한 곳이 계속 틀려도 다른 사람은 막히지 않는다.
        기본값(aurora-wan)이 코드인 PC 는 인터넷(공인 IP)에서 오는 접속을 거부한다 (누구나 아는 값이라)"""
        self._reload_cfg()                         # 화면에서 접근 코드를 바꿨으면 바로 반영
        now = time.monotonic()
        who = peer or "*"
        fails = [t for t in self._fails.get(who, []) if now - t < CODE_LOCK]
        if len(fails) >= CODE_FAILS:
            self._fails[who] = fails
            return "locked"
        real = self.cfg.get("access_code") or self.cfg.get("relay_key")      # 코드를 따로 안 정했으면 공용 키가 접근 코드
        if not real:
            return "no_code"
        if real == DEFAULT_RELAY_KEY and is_public_ip(peer):
            self.log(f"기본 코드라 인터넷({peer})에서 온 접속을 거부했습니다 — 접근 코드를 정하세요")
            return "weak_code"
        if not hmac.compare_digest(str(code).encode("utf-8"), real.encode("utf-8")):
            self._fails[who] = fails + [now]
            self.log(f"접근 코드가 맞지 않는 접속 시도{f' ({peer})' if peer else ''}")
            return "bad_code"
        self._fails.pop(who, None)
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

    # ── 주소로 바로 들어오는 접속 (안내 서버 없이) ──
    async def run_direct(self):
        """direct_port 로 들어오는 접속을 받는다.  설정(켬/끔 · 포트)이 바뀌면 다시 열고, 포트가 막히면 간격을 두고 재시도"""
        from websockets.asyncio.server import serve
        while not self._stop.is_set():
            self._reload_cfg()
            want = (bool(self.cfg.get("direct_listen")), self._direct_port())
            if not want[0]:
                self.direct_state = "off"
                await self._sleep(2.0)
                continue
            try:
                ctx = tls.server_context(tls.ensure_cert(self.cfg.dir, "direct"))
                async with serve(self._on_direct, "0.0.0.0", want[1], max_size=2 ** 20, compression=None,
                                 ping_interval=20, ping_timeout=20, close_timeout=2, ssl=ctx):
                    self.direct_state = "listening"
                    self.log(f"주소 접속 대기 중 (포트 {want[1]})")
                    while not self._stop.is_set() and self._want_direct() == want:
                        await self._sleep(1.0)
                self.direct_state = "off"
            except OSError as e:
                self.direct_state = "busy"
                self.log(f"주소 접속 포트 {want[1]} 를 열지 못했습니다: {e}")
                await self._sleep(DIRECT_RETRY)

    def _direct_port(self) -> int:
        try:
            return int(self.cfg.get("direct_port"))
        except (TypeError, ValueError):
            return DIRECT_PORT

    def _want_direct(self) -> tuple:
        self._reload_cfg()
        return bool(self.cfg.get("direct_listen")), self._direct_port()

    async def _sleep(self, seconds: float):
        try:
            await asyncio.wait_for(self._stop.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _on_direct(self, ws):
        """주소로 들어온 접속: 인사(접근 코드)를 이 PC 가 직접 확인한 뒤 같은 연결을 세션으로 쓴다"""
        sid = secrets.token_hex(8)
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), DIRECT_HELLO_WAIT))
            if not isinstance(hello, dict) or hello.get("t") != "hello":
                return
            peer = (getattr(ws, "remote_address", None) or ("",))[0]
            denied = self.check_code(hello.get("code", ""), peer)
        except Exception:
            return
        if denied:
            await self._reply(ws, t="deny", sid=sid, reason=denied)
            return
        await self._reply(ws, t="ok", sid=sid, name=self.cfg.get("name") or socket.gethostname(), ver=self.version)
        link = WsLink(ws)
        self._attach(sid, link, "주소")
        await link.pump()

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


async def _alive_marker(path: str, agent=None):
    """감독자가 멈춘 에이전트를 알아채도록 5초마다 파일 시각을 갱신. 화면이 읽을 상태(JSON)도 함께 적는다"""
    while True:
        try:
            with open(path, "w") as f:
                json.dump({"t": time.time(), "direct": getattr(agent, "direct_state", "off"),
                           "online": bool(getattr(agent, "online", False))}, f)
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
    cfg.ensure_access_code()
    log(f"WAN 에이전트 시작 (ID {cfg.ensure_id()}, v{version})")

    async def main():
        agent = WanAgent(cfg, bridge_port, key_getter, version, log)
        marker = asyncio.create_task(_alive_marker(alive_path, agent))
        try:
            await agent.run()
        finally:
            marker.cancel()
    asyncio.run(main())
