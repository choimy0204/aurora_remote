"""NAT 실험실에서 각 네임스페이스 안에서 도는 프로세스들 (안내 서버는 `python -m wan.server` 를 그대로 씀)

  agent   현장 PC — WAN 에이전트 + 가짜 로컬 서버(파일 API 흉내)
  user    사용자 PC — 현장에 연결해 연결 방식 · 후보 종류 · 왕복 시간 · 전송 속도를 재서 JSON 으로 저장
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from wan.agent import WanAgent          # noqa: E402
from wan.client import WanClient        # noqa: E402
from wan.config import WanConfig        # noqa: E402


def selected_pair(pc) -> dict:
    """연결에 실제로 쓰인 후보 쌍 (host / srflx / prflx / relay)"""
    try:
        ice = pc.sctp.transport.transport
        pair = next(iter(ice._connection._nominated.values()))
        loc, rem = pair.local_candidate, pair.remote_candidate
        return {"local": f"{loc.type} {loc.host}:{loc.port}", "remote": f"{rem.type} {rem.host}:{rem.port}",
                "local_type": loc.type, "remote_type": rem.type}
    except Exception as e:
        return {"error": repr(e)}


def run_agent(a):
    from wan_helpers import FakeLocal, LOCAL_KEY
    local = FakeLocal()
    cfg = WanConfig(a.dir)
    cfg.update(relay_url=a.relay, name=a.name, access_code=a.code, stun=a.stun, enabled=True)
    agent = WanAgent(cfg, local.port, lambda: LOCAL_KEY, "2.16.0", lambda m: print(f"[agent {time.strftime('%H:%M:%S')}] {m}", flush=True))

    async def status():
        while True:
            sessions = []
            for sid, mux in list(agent.sessions.items()):
                info = {"kind": mux.link.kind, "alive": mux.alive}
                if mux.link.kind == "direct":
                    info["pair"] = selected_pair(mux.link.pc)
                sessions.append(info)
            with open(os.path.join(a.dir, "status.json"), "w") as f:
                json.dump({"id": cfg.get("agent_id"), "online": agent.online, "sessions": sessions, "t": time.time()}, f)
            await asyncio.sleep(0.5)

    async def main():
        asyncio.ensure_future(status())
        await agent.run()
    cfg.ensure_id()
    asyncio.run(main())


def run_user(a):
    cfg = WanConfig(a.dir)
    cfg.update(relay_url=a.relay, stun=a.stun, direct_timeout=a.direct_timeout)
    client = WanClient(cfg, None, "2.16.0", log=lambda m: print(f"[user {time.strftime('%H:%M:%S')}] {m}", flush=True))
    client.start()
    out = {"scenario": a.name}

    def finish(code=0):
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print("RESULT", json.dumps(out, ensure_ascii=False), flush=True)
        client.stop()
        sys.exit(code)

    deadline = time.time() + 40
    while time.time() < deadline and not (client.state == "연결됨" and any(x["online"] for x in client.agents)):
        time.sleep(0.2)
    online = [x for x in client.agents if x["online"]]
    if not online:
        out["error"] = f"현장이 목록에 나타나지 않음 (안내 서버 {client.state})"
        finish(1)
    agent = online[0]
    t0 = time.time()
    try:
        s = client.open_session(agent["id"], a.code)
    except Exception as e:
        out["error"] = f"연결 실패: {type(e).__name__}: {e}"
        out["establish_s"] = round(time.time() - t0, 2)
        finish(1)
    out.update(mode=s.mode, establish_s=round(time.time() - t0, 2))
    if s.mode == "direct":
        out["pair"] = selected_pair(s.mux.link.pc)
    lat = []
    for _ in range(20):
        t = time.monotonic()
        s.request("GET", "/info", timeout=(3, 5))
        lat.append((time.monotonic() - t) * 1000)
        time.sleep(0.05)
    out["rtt_ms"] = {"min": round(min(lat), 1), "median": round(statistics.median(lat), 1), "max": round(max(lat), 1)}
    size = a.mb * 1024 * 1024
    t = time.monotonic()
    got = 0
    with s.request("GET", "/fs/file", {"path": f"size:{size}"}, stream=True, timeout=(3, 30)) as r:
        for chunk in r.iter_content(1 << 20):
            got += len(chunk)
    el = time.monotonic() - t
    out["download"] = {"MB": a.mb, "seconds": round(el, 2), "MB_per_s": round(got / el / 1e6, 2), "complete": got == size}

    if a.rebind:                       # NAT 가 매핑을 잃은 상황(공유기 재부팅 등) — 오케스트레이터가 conntrack 을 비운 뒤 복구를 기다림
        open(a.out + ".ready", "w").close()
        while not os.path.exists(a.out + ".flushed"):
            time.sleep(0.1)
        t_flush = time.monotonic()
        recovered = None
        while time.monotonic() - t_flush < 90:
            try:
                if s.request("GET", "/info", timeout=(1, 2)).ok:
                    recovered = time.monotonic() - t_flush
                    break
            except Exception:
                pass
            time.sleep(0.3)
        out["rebind"] = {"recovered_after_s": None if recovered is None else round(recovered, 1),
                         "mode_after": s.mode, "state_after": s.state}
    s.close()
    finish(0)


def main():
    if os.environ.get("NATLAB_ICE_DEBUG"):
        import logging
        logging.basicConfig(level=logging.WARNING, format="%(asctime)s.%(msecs)03d %(name)s %(message)s", datefmt="%H:%M:%S")
        logging.getLogger("aioice").setLevel(logging.DEBUG)
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="role", required=True)
    p = sub.add_parser("agent")
    p.add_argument("--relay", required=True); p.add_argument("--stun", default="")
    p.add_argument("--dir", required=True); p.add_argument("--code", default="1234"); p.add_argument("--name", default="현장")
    p = sub.add_parser("user")
    p.add_argument("--relay", required=True); p.add_argument("--stun", default="")
    p.add_argument("--dir", required=True); p.add_argument("--code", default="1234"); p.add_argument("--out", required=True)
    p.add_argument("--name", default="user"); p.add_argument("--direct-timeout", type=float, default=8.0)
    p.add_argument("--mb", type=int, default=30); p.add_argument("--rebind", action="store_true")
    a = ap.parse_args()
    {"agent": run_agent, "user": run_user}[a.role](a)


if __name__ == "__main__":
    main()
