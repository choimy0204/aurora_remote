"""
안내 서버 (회사 중계 PC) — 화면 없음
====================================
- 현장 PC(agent)가 켜지면 접속해 고유 ID 로 등록하고, 사용자 PC(user)는 켜져 있는 현장 목록을 받는다.
- 원격 설정(이름·접속 코드·최고 화질·파일 허용)을 보관했다가 현장 PC 가 접속하면 전달한다.
- 직접 연결에 필요한 연결 정보(SDP)를 두 PC 사이에서 전달한다.
- 직접 연결이 안 되면 그때만 데이터를 대신 전달한다 (비상 중계). 내용은 열어 보지 않는다.
- 새 버전 파일(update 폴더)을 보관했다가 내려준다.

실행:  python -m wan.server --port 9100 --data D:\\AuroraRelay --key 공용키 --update-dir D:\\AuroraRelay\\update
공인 IP 로 열어야 하는 곳은 이 PC 의 포트 하나뿐이다 (현장 PC 쪽 포트 설정은 필요 없음).
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from http import HTTPStatus

from . import config as wcfg
from .netutil import is_public_ip

log = logging.getLogger("aurora.relay")

AGENT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
SID_RE      = re.compile(r"^[0-9a-f]{16}$")
SIGNAL_TTL  = 120.0          # 직접 연결 신호가 이 시간 안에 끝나지 않으면 정리
OLD_KEY_DAYS = 90            # 옛 공용 키로도 (비밀 값을 아는) 현장 PC 만 접속해 새 키를 받을 수 있는 기간
OLD_KEYS_MAX = 8
UPDATE_CHUNK = 64 * 1024
SERVER_CAPS = ["forget", "code"]   # 이 안내 서버가 지원하는 기능 (옛 서버는 이 목록이 없다 — 새 프로그램이 "서버를 업데이트하세요" 라고 알려 주려고)
KEY_FAILS   = 8              # 같은 곳에서 이 횟수 키를 틀리면
KEY_LOCK    = 300.0          # 이 시간(초) 동안 그곳의 접속을 모두 거부


def parse_ver(s) -> tuple:
    parts = []
    for p in str(s).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


class _Agent:
    def __init__(self, ws, agent_id):
        self.ws, self.id = ws, agent_id


class _Session:
    def __init__(self, user_ws, agent_id, now):
        self.user_ws, self.agent_id, self.created = user_ws, agent_id, now
        self.relay = False       # 비상 중계가 열렸는지
        self.bytes = 0


def remember_old_key(data_dir: str, key: str, now=None, days: float = OLD_KEY_DAYS):
    """공용 키를 바꾸기 직전의 옛 키를 기록한다 — 안내 서버가 다시 시작되면 읽어서, 켜져 있지 않던 현장 PC 도 나중에 새 키를 받게 한다"""
    if not key:
        return
    now = time.time() if now is None else now
    path = os.path.join(data_dir, "old_keys.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        items = []
    items = [i for i in items if isinstance(i, dict) and i.get("until", 0) > now and i.get("key") != key]
    items.append({"key": key, "until": now + days * 86400})
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items[-OLD_KEYS_MAX:], f)
    os.replace(tmp, path)


class RelayServer:
    def __init__(self, data_dir: str, key: str = "", update_dir: str | None = None, now=time.time):
        self.data_dir, self.key, self.update_dir, self._now = data_dir, key, update_dir, now
        self._key_fails: dict = {}          # 접속 출처(IP) → 키를 틀린 시각들
        self.old_keys = self._load_old_keys()
        self.path = os.path.join(data_dir, "relay_agents.json")
        self.store: dict = {}
        self.agents: dict = {}          # id → _Agent (접속 중)
        self.users: set = set()
        self.sessions: dict = {}        # sid(16자 hex) → _Session
        self._dirty = False
        self._sha_cache: dict = {}
        self._load()

    # ── 저장 ──
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.store = json.load(f)
        except FileNotFoundError:
            self.store = {}
        except Exception:
            log.exception("저장 파일을 읽지 못해 새로 시작합니다")
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass
            self.store = {}

    def save(self):
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.store, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self._dirty = False

    def _load_old_keys(self) -> list:
        try:
            with open(os.path.join(self.data_dir, "old_keys.json"), "r", encoding="utf-8") as f:
                return [i for i in json.load(f) if isinstance(i, dict) and isinstance(i.get("key"), str)]
        except (OSError, ValueError):
            return []

    def _old_key_ok(self, hello: dict) -> bool:
        """공용 키를 바꾼 뒤 옛 키로 접속한 **현장 PC** 인지 — 옛 키가 기록되어 있고(기간 안), 이 현장이 처음 등록 때 알린 비밀 값을
        그대로 대면 통과.  비밀 값은 사용자 PC 에도 내려가지 않으므로 옛 키만 새어 나간 사람은 통과하지 못한다"""
        if hello.get("role") != "agent":
            return False
        rec = self.store.get(str(hello.get("id", "")))
        secret = (rec or {}).get("secret") or ""
        given = hello.get("secret")
        if not secret or not isinstance(given, str) or not hmac.compare_digest(secret.encode(), given.encode()):
            return False
        now, key = self._now(), str(hello.get("key", ""))
        return any(i.get("until", 0) > now and hmac.compare_digest(key.encode(), i["key"].encode()) for i in self.old_keys)

    @staticmethod
    def _clean_code(code) -> str:
        return code.strip()[:64] if isinstance(code, str) else ""

    def _record(self, agent_id: str) -> dict:
        return self.store.setdefault(agent_id, {"name": "", "config": {}, "rev": 0, "applied_rev": 0,
                                                "last_seen": 0, "ver": ""})

    # ── 목록 ──
    def agent_list(self) -> list:
        out = []
        for aid, rec in self.store.items():
            out.append({"id": aid, "name": rec["config"].get("name") or rec.get("name") or aid[:4],
                        "online": aid in self.agents, "last_seen": rec.get("last_seen", 0),
                        "ver": rec.get("ver", ""), "rev": rec["rev"], "applied_rev": rec["applied_rev"],
                        "code": rec.get("code", "")})
        out.sort(key=lambda a: (not a["online"], a["name"].lower()))
        return out

    async def _broadcast_agents(self):
        msg = json.dumps({"t": "agents", "agents": self.agent_list()}, ensure_ascii=False)
        for ws in list(self.users):
            await self._send(ws, msg)

    @staticmethod
    async def _send(ws, msg) -> bool:
        try:
            await ws.send(msg)
            return True
        except Exception:
            return False

    @staticmethod
    def _peer(ws) -> str:
        return str((getattr(ws, "remote_address", None) or ("",))[0])

    def _locked(self, peer: str) -> bool:
        """같은 곳에서 키를 계속 틀리면 한동안 모든 접속을 막는다 (무작위 대입 방지)"""
        now = self._now()
        recent = [t for t in self._key_fails.get(peer, []) if now - t < KEY_LOCK]
        self._key_fails[peer] = recent
        return len(recent) >= KEY_FAILS

    def _key_ok(self, given: str) -> bool:
        # 한글 키도 쓸 수 있게 바이트로 비교 (str 끼리 비교하면 ASCII 가 아닐 때 오류)
        return not self.key or hmac.compare_digest(str(given).encode("utf-8"), self.key.encode("utf-8"))

    # ── 접속 처리 ──
    async def handle(self, ws):
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if not isinstance(hello, dict) or hello.get("t") != "hello":
                raise ValueError("hello 가 아님")
        except Exception:
            return await ws.close(4000, "hello 필요")
        peer = self._peer(ws)
        if self._locked(peer):
            await self._send(ws, json.dumps({"t": "error", "reason": "locked"}))
            return await ws.close(4003, "잠시 막혔습니다")
        if is_public_ip(peer) and (not self.key or self.key == wcfg.DEFAULT_RELAY_KEY):
            log.warning("기본(또는 빈) 공용 키라 인터넷(%s)에서 온 접속을 거부했습니다", peer)
            await self._send(ws, json.dumps({"t": "error", "reason": "default_key"}))
            return await ws.close(4001, "공용 키를 바꾸세요")
        hello["_old_key"] = False
        if not self._key_ok(hello.get("key", "")) and self._old_key_ok(hello):
            hello["_old_key"] = True                           # 옛 키로 온 현장 PC — 접속시키고 새 키를 내려 준다
        elif not self._key_ok(hello.get("key", "")):
            self._key_fails.setdefault(peer, []).append(self._now())
            await self._send(ws, json.dumps({"t": "error", "reason": "key"}))
            return await ws.close(4001, "키가 맞지 않습니다")
        role = hello.get("role")
        try:
            if role == "agent":
                await self._agent(ws, hello)
            elif role == "user":
                await self._user(ws, hello)
            elif role == "update":
                await self._update(ws, hello)
            else:
                await ws.close(4000, "알 수 없는 역할")
        except Exception as e:
            if type(e).__name__ not in ("ConnectionClosedOK", "ConnectionClosedError", "ConnectionClosed"):
                log.exception("접속 처리 오류")

    # ── 현장 PC ──
    async def _agent(self, ws, hello):
        aid = str(hello.get("id", ""))
        if not AGENT_ID_RE.match(aid):
            return await ws.close(4000, "잘못된 ID")
        rec = self._record(aid)
        rec["name"] = str(hello.get("name", ""))[:40] or rec["name"]
        rec["ver"], rec["last_seen"] = str(hello.get("ver", ""))[:20], self._now()
        rec["applied_rev"] = int(hello.get("rev", 0) or 0)
        rec["code"] = self._clean_code(hello.get("code"))          # 현장이 알려 준 접근 코드 — 인증된 사용자 PC 에 전달
        secret = hello.get("secret")
        if isinstance(secret, str) and secret and not rec.get("secret"):
            rec["secret"] = secret[:128]                             # 처음 등록할 때만 기억 (그 뒤에 바꾸지 못하게)
        old = self.agents.get(aid)
        conn = self.agents[aid] = _Agent(ws, aid)
        if old is not None:
            await old.ws.close(4002, "같은 ID 가 다시 접속했습니다")
        welcome = {"t": "welcome", "rev": rec["rev"]}
        if hello.get("_old_key"):
            welcome["rekey"] = self.key                              # 현장 PC 가 새 공용 키를 저장하고 이후에는 새 키로 접속
        if rec["rev"] and rec["rev"] != rec["applied_rev"]:
            welcome["config"] = rec["config"]
        await ws.send(json.dumps(welcome, ensure_ascii=False))
        self.save()
        log.info("현장 접속: %s (%s)", aid, rec["name"])
        await self._broadcast_agents()
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    await self._route_binary(ws, msg)
                    continue
                data = json.loads(msg)
                t = data.get("t")
                if t == "hb":
                    rec["last_seen"] = self._now()
                elif t == "code":
                    new = self._clean_code(data.get("code"))
                    if new != rec.get("code"):
                        rec["code"] = new
                        self.save()
                        await self._broadcast_agents()
                elif t == "config_ack":
                    rec["applied_rev"] = int(data.get("rev", 0))
                    self._dirty = True
                    await self._broadcast_agents()
                elif t in ("answer", "deny", "relay_ok", "bye"):
                    await self._from_agent(aid, data)
        finally:
            if self.agents.get(aid) is conn:
                del self.agents[aid]
                rec["last_seen"] = self._now()
                self._dirty = True
                await self._end_sessions(lambda s: s.agent_id == aid, "agent")
                log.info("현장 종료: %s", aid)
                await self._broadcast_agents()

    async def _from_agent(self, aid: str, data: dict):
        sid = str(data.get("sid", ""))
        sess = self.sessions.get(sid)
        if sess is None or sess.agent_id != aid:
            return
        t = data["t"]
        if t == "relay_ok":
            sess.relay = True
        await self._send(sess.user_ws, json.dumps(data, ensure_ascii=False))
        if t in ("deny", "bye"):
            self.sessions.pop(sid, None)

    # ── 사용자 PC ──
    async def _user(self, ws, hello):
        self.users.add(ws)
        await ws.send(json.dumps({"t": "welcome", "agents": self.agent_list(), "caps": SERVER_CAPS}, ensure_ascii=False))
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    await self._route_binary(ws, msg)
                    continue
                data = json.loads(msg)
                t = data.get("t")
                if t == "set_config":
                    await self._set_config(ws, data)
                elif t == "forget":
                    await self._forget(ws, data)
                elif t in ("connect", "relay"):
                    await self._open_session(ws, data)
                elif t == "bye":
                    await self._user_bye(ws, data)
                elif t == "list":
                    await self._send(ws, json.dumps({"t": "agents", "agents": self.agent_list()}, ensure_ascii=False))
        finally:
            self.users.discard(ws)
            await self._end_sessions(lambda s: s.user_ws is ws, "user")

    async def _forget(self, ws, data: dict):
        """꺼져 있는 현장을 목록(저장소)에서 지운다.  켜져 있으면 거부 — 곧 다시 등록되기 때문"""
        q, aid = data.get("q"), str(data.get("id", ""))
        if aid not in self.store:
            return await self._send(ws, json.dumps({"t": "err", "q": q, "reason": "unknown_agent"}))
        if aid in self.agents:
            return await self._send(ws, json.dumps({"t": "err", "q": q, "reason": "online"}))
        del self.store[aid]
        self.save()
        log.info("현장 삭제: %s", aid)
        await self._send(ws, json.dumps({"t": "ok", "q": q}))
        await self._broadcast_agents()

    async def _set_config(self, ws, data: dict):
        q, aid = data.get("q"), str(data.get("id", ""))
        if aid not in self.store:
            return await self._send(ws, json.dumps({"t": "err", "q": q, "reason": "unknown_agent"}))
        clean = wcfg.sanitize_remote(data.get("config") or {})
        rec = self.store[aid]
        rec["config"].update(clean)
        if "access_code" in clean:
            rec["code"] = clean["access_code"]                     # 현장이 꺼져 있어도 사용자 PC 는 새 코드를 바로 받는다
        rec["rev"] += 1
        self.save()
        online = self.agents.get(aid)
        if online:
            await self._send(online.ws, json.dumps({"t": "config", "rev": rec["rev"], "config": rec["config"]},
                                                    ensure_ascii=False))
        await self._send(ws, json.dumps({"t": "ok", "q": q, "rev": rec["rev"], "online": bool(online)}))
        await self._broadcast_agents()

    async def _open_session(self, ws, data: dict):
        sid, aid = str(data.get("sid", "")), str(data.get("id", ""))
        reply = lambda reason: self._send(ws, json.dumps({"t": "deny", "sid": sid, "reason": reason}))
        if not SID_RE.match(sid):
            return await reply("bad_sid")
        agent = self.agents.get(aid)
        if agent is None:
            return await reply("offline")
        self.sessions[sid] = _Session(ws, aid, self._now())
        fwd = {"t": data["t"], "sid": sid, "code": data.get("code", "")}
        if data["t"] == "connect":
            fwd["offer"] = data.get("offer")
        if not await self._send(agent.ws, json.dumps(fwd, ensure_ascii=False)):
            self.sessions.pop(sid, None)
            await reply("offline")

    async def _user_bye(self, ws, data: dict):
        sess = self.sessions.get(str(data.get("sid", "")))
        if sess is None or sess.user_ws is not ws:
            return
        self.sessions.pop(data["sid"], None)
        agent = self.agents.get(sess.agent_id)
        if agent:
            await self._send(agent.ws, json.dumps({"t": "bye", "sid": data["sid"]}))

    # ── 비상 중계 (바이너리: 앞 8바이트 = 세션) ──
    async def _route_binary(self, ws, msg: bytes):
        if len(msg) <= 8:
            return
        sid = msg[:8].hex()
        sess = self.sessions.get(sid)
        if sess is None or not sess.relay:
            return
        agent = self.agents.get(sess.agent_id)
        if sess.user_ws is ws and agent:
            target = agent.ws
        elif agent and agent.ws is ws:
            target = sess.user_ws
        else:
            return
        sess.bytes += len(msg)
        await self._send(target, msg)

    async def _end_sessions(self, match, who: str):
        for sid, sess in list(self.sessions.items()):
            if not match(sess):
                continue
            self.sessions.pop(sid, None)
            bye = json.dumps({"t": "bye", "sid": sid})
            if who == "agent":
                await self._send(sess.user_ws, bye)
            else:
                agent = self.agents.get(sess.agent_id)
                if agent:
                    await self._send(agent.ws, bye)

    # ── 새 버전 내려주기 ──
    def update_info(self, kind: str = "client") -> dict | None:
        if not self.update_dir:
            return None
        exe = os.path.join(self.update_dir, {"client": "AuroraClient.exe", "server": "AuroraServer.exe"}[kind])
        try:
            with open(os.path.join(self.update_dir, "version.json"), "r", encoding="utf-8-sig") as f:
                version = str(json.load(f).get(kind, "")).strip()
            st = os.stat(exe)
        except (OSError, ValueError):
            return None
        if not version:
            return None
        sig = (exe, st.st_mtime_ns, st.st_size)
        if sig not in self._sha_cache:
            h = hashlib.sha256()
            with open(exe, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            self._sha_cache = {sig: h.hexdigest()}
        return {"version": version, "size": st.st_size, "sha256": self._sha_cache[sig], "path": exe}

    async def _update(self, ws, hello):
        info = self.update_info(hello.get("kind", "client")) if hello.get("kind", "client") in ("client", "server") else None
        if info is None:
            return await ws.send(json.dumps({"t": "update_info", "available": False, "reason": "none"}))
        newer = parse_ver(info["version"]) > parse_ver(hello.get("ver", "0"))
        await ws.send(json.dumps({"t": "update_info", "available": newer, "version": info["version"],
                                  "size": info["size"], "sha256": info["sha256"]}))
        if not newer:
            return
        msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
        if msg.get("t") != "get":
            return
        with open(info["path"], "rb") as f:
            for block in iter(lambda: f.read(UPDATE_CHUNK), b""):
                await ws.send(block)
        await ws.send(json.dumps({"t": "end", "sha256": info["sha256"]}))

    # ── 주기 작업 ──
    async def housekeeping(self, interval: float = 30.0):
        while True:
            await asyncio.sleep(interval)
            now = self._now()
            for sid, sess in list(self.sessions.items()):
                if not sess.relay and now - sess.created > SIGNAL_TTL:
                    self.sessions.pop(sid, None)
            for aid in self.agents:
                self.store[aid]["last_seen"] = now
                self._dirty = True
            if self._dirty:
                self.save()

    # ── 서버 시작 ──
    def process_request(self, connection, request):
        if request.path == "/health":
            return connection.respond(HTTPStatus.OK, "ok\n")
        return None


async def run_server(server: RelayServer, host: str, port: int, ssl_context=None):
    from websockets.asyncio.server import serve
    async with serve(server.handle, host, port, max_size=2 ** 20, compression=None, ping_interval=20,
                     ping_timeout=20, process_request=server.process_request, ssl=ssl_context) as s:
        log.info("안내 서버 시작: %s:%s%s%s", host, port, " (공용 키 사용)" if server.key else " (공용 키 없음 — 누구나 접속 가능)",
                 " · 암호화(wss)" if ssl_context else " · 암호화 없음(ws)")
        task = asyncio.create_task(server.housekeeping())
        try:
            await s.serve_forever()
        finally:
            task.cancel()


def main():
    ap = argparse.ArgumentParser(description="Aurora WAN 안내 서버")
    ap.add_argument("--host", default=os.environ.get("AURORA_RELAY_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("AURORA_RELAY_PORT", "9100")))
    ap.add_argument("--data", default=os.environ.get("AURORA_RELAY_DATA", os.path.join(os.getcwd(), "relay_data")))
    ap.add_argument("--key", default=os.environ.get("AURORA_RELAY_KEY", ""), help="현장·사용자 PC 가 접속할 때 필요한 공용 키")
    ap.add_argument("--update-dir", default=os.environ.get("AURORA_RELAY_UPDATE", ""),
                    help="dist\\latest 내용(AuroraClient.exe, version.json)을 넣어 두는 폴더")
    ap.add_argument("--tls", action="store_true", help="wss:// 로 암호화 (인증서는 --data 폴더에 자동으로 만듦). 인터넷에 열 때는 켜세요")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = RelayServer(args.data, args.key, args.update_dir or None)
    ctx = None
    if args.tls:
        from . import tls
        pem = tls.ensure_cert(args.data)
        ctx = tls.server_context(pem)
        logging.getLogger("aurora.relay").info("인증서 지문(SHA-256): %s", tls.cert_fingerprint(pem))
    try:
        asyncio.run(run_server(server, args.host, args.port, ctx))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
