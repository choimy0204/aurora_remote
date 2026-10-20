"""
NAT 실험실 실행기 — 서로 다른 NAT 뒤의 두 망에서 실제 WAN 스택(안내 서버 · 현장 에이전트 · 사용자, aiortc)을 돌린다
================================================================================================
root 권한과 ip · iptables · conntrack 이 필요하다 (Linux).  사용:
    python tools/nat_lab/run_lab.py                      # 전체 시나리오
    python tools/nat_lab/run_lab.py cone-cone rebind     # 일부만
    python tools/nat_lab/run_lab.py --list

시나리오                    NAT A / NAT B        기대
  cone-cone                  cone / cone          직접 연결 (srflx 후보로 홀 펀칭)
  cone-symmetric             cone / symmetric     (결과를 기록 — 직접이든 중계든 연결은 되어야 함)
  symmetric-symmetric        symmetric / symmetric 홀 펀칭 불가 → 비상 중계로 전환
  udp-blocked                udpblock / udpblock  UDP 차단 → 비상 중계로 전환
  rebind                     cone / cone          NAT 가 매핑을 잃은 뒤(공유기 재부팅) 자동 복구
  holepunch-script           cone / cone          0단계 시험 스크립트(tests/holepunch_test.py)를 두 NAT 사이에서 실행
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PY = sys.executable
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "WS_PROXY", "WSS_PROXY")
RELAY_A, RELAY_B = "203.0.113.1", "198.51.100.1"        # 인터넷 쪽 주소 (NAT A 쪽 / NAT B 쪽에서 본 안내 서버)


def clean_env(**extra) -> dict:
    """실험실 안의 프로세스는 실제 인터넷 프록시를 쓰면 안 됨 (websockets 가 환경 변수의 프록시를 자동으로 따라감)"""
    env = {k: v for k, v in os.environ.items() if k.upper() not in PROXY_VARS}
    env.update(extra)
    return env


def ns(name, *cmd):
    return ["ip", "netns", "exec", name, *map(str, cmd)]


def available() -> str | None:
    """실행 가능하면 None, 아니면 이유"""
    if os.name != "posix" or os.geteuid() != 0:
        return "root 권한의 Linux 가 필요함"
    for tool in ("ip", "iptables", "conntrack"):
        if not shutil.which(tool):
            return f"{tool} 명령이 없음"
    r = subprocess.run(["ip", "netns", "add", "natlab_probe"], capture_output=True)
    if r.returncode != 0:
        return "네트워크 네임스페이스를 만들 수 없음: " + r.stderr.decode(errors="replace").strip()
    probe = subprocess.run(ns("natlab_probe", "iptables", "-t", "nat", "-L"), capture_output=True)
    subprocess.run(["ip", "netns", "del", "natlab_probe"], capture_output=True)
    return None if probe.returncode == 0 else "iptables NAT 를 쓸 수 없음"


class Lab:
    def __init__(self, a_mode: str, b_mode: str):
        self.a_mode, self.b_mode = a_mode, b_mode
        self.dir = tempfile.mkdtemp(prefix="natlab_")
        self.procs = []

    def sh(self, *cmd, check=True):
        return subprocess.run(list(map(str, cmd)), check=check, capture_output=True)

    def spawn(self, name, cmd, env_extra=None):
        log = open(os.path.join(self.dir, f"{name}.log"), "w", encoding="utf-8")
        env = clean_env(PYTHONPATH=ROOT, PYTHONUNBUFFERED="1", HOME=self.dir, **(env_extra or {}))
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
        self.procs.append(p)
        return p

    def up(self):
        self.sh(os.path.join(HERE, "topology.sh"), "up", self.a_mode, self.b_mode)
        self.spawn("stun", ns("inet", PY, os.path.join(HERE, "stun_server.py"), 3478))
        self.spawn("relay", ns("inet", PY, "-m", "wan.server", "--host", "0.0.0.0", "--port", 9100,
                               "--data", os.path.join(self.dir, "relay")))
        deadline = time.time() + 15
        while time.time() < deadline:
            if "안내 서버 시작" in open(os.path.join(self.dir, "relay.log"), encoding="utf-8", errors="replace").read():
                return
            time.sleep(0.2)
        raise RuntimeError("안내 서버가 시작되지 않음: " + open(os.path.join(self.dir, "relay.log")).read()[-500:])

    def agent(self):
        d = os.path.join(self.dir, "agent")
        return self.spawn("agent", ns("hosta", PY, os.path.join(HERE, "lab_procs.py"), "agent",
                                      "--relay", f"ws://{RELAY_A}:9100", "--stun", f"stun:{RELAY_A}:3478", "--dir", d))

    def user(self, name, extra=()):
        out = os.path.join(self.dir, f"{name}.json")
        p = self.spawn(f"user_{name}", ns("hostb", PY, os.path.join(HERE, "lab_procs.py"), "user",
                                          "--relay", f"ws://{RELAY_B}:9100", "--stun", f"stun:{RELAY_B}:3478",
                                          "--dir", os.path.join(self.dir, "user"), "--out", out, "--name", name, *extra))
        return p, out

    def agent_status(self):
        try:
            with open(os.path.join(self.dir, "agent", "status.json")) as f:
                return json.load(f)
        except Exception:
            return {}

    def down(self):
        for p in self.procs:
            try:
                p.kill()
            except Exception:
                pass
        for p in self.procs:
            try:
                p.wait(5)
            except Exception:
                pass
        self.sh(os.path.join(HERE, "topology.sh"), "down", check=False)

    def logs(self, name):
        try:
            return open(os.path.join(self.dir, f"{name}.log"), encoding="utf-8", errors="replace").read()
        except OSError:
            return ""


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def srflx(pair: dict) -> bool:
    """NAT 를 건너 직접 닿았는지 — 후보 쌍의 한쪽이라도 공인 주소(srflx/prflx/relay)여야 함.
    (홀 펀칭이 되면 "내 host ↔ 상대 srflx" 로 잡히는 것이 정상 — 둘 다 host 면 같은 사설망끼리라는 뜻)"""
    return bool(pair) and bool({pair.get("local_type"), pair.get("remote_type")} & {"srflx", "prflx"})


# ── 시나리오 ──
def run_connect(name, a_mode, b_mode):
    lab = Lab(a_mode, b_mode)
    try:
        lab.up()
        lab.agent()
        p, out = lab.user(name)
        p.wait(120)
        res = load(out) if os.path.exists(out) else {"error": "사용자 프로세스가 결과를 남기지 않음", "log": lab.logs(f"user_{name}")[-800:]}
        res["agent_status"] = lab.agent_status()
        res["agent_log"] = lab.logs("agent")[-1500:]
        res["user_log"] = lab.logs(f"user_{name}")[-1500:]
        if os.environ.get("NATLAB_ICE_DEBUG"):
            res["ice_debug"] = {"agent": lab.logs("agent"), "user": lab.logs(f"user_{name}")}
        return res
    finally:
        lab.down()


def run_rebind(name, a_mode, b_mode):
    lab = Lab(a_mode, b_mode)
    try:
        lab.up()
        lab.agent()
        p, out = lab.user(name, ["--rebind", "--mb", "5"])
        deadline = time.time() + 90
        while time.time() < deadline and not os.path.exists(out + ".ready"):
            time.sleep(0.2)
        if not os.path.exists(out + ".ready"):
            return {"error": "연결 단계에서 막힘", "log": lab.logs(f"user_{name}")[-800:]}
        for nat in ("nata", "natb"):
            lab.sh(*ns(nat, "conntrack", "-F"), check=False)           # NAT 가 모든 매핑을 잃음 (공유기 재부팅과 같음)
        open(out + ".flushed", "w").close()
        p.wait(120)
        res = load(out) if os.path.exists(out) else {"error": "복구 결과 없음", "log": lab.logs(f"user_{name}")[-800:]}
        return res
    finally:
        lab.down()


def run_holepunch_script(name, a_mode, b_mode):
    """0단계 시험 스크립트 — 코드를 복사·붙여넣기 하는 대신 파이프로 주고받음 (offer: NAT A 뒤, answer: NAT B 뒤)"""
    import re
    lab = Lab(a_mode, b_mode)
    script = os.path.join(ROOT, "tests", "holepunch_test.py")
    try:
        lab.up()
        env = clean_env(PYTHONUNBUFFERED="1", HOME=lab.dir)
        a = subprocess.Popen(ns("hosta", PY, script, "offer", "--stun", f"stun:{RELAY_A}:3478", "--seconds", 5),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        b = subprocess.Popen(ns("hostb", PY, script, "answer", "--stun", f"stun:{RELAY_B}:3478", "--seconds", 5),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        lab.procs += [a, b]

        def code_of(p):
            for line in p.stdout:
                if re.fullmatch(r"[A-Za-z0-9+/=]{200,}", line.strip()):
                    return line.strip()
        offer = code_of(a)
        b.stdin.write(offer + "\n"); b.stdin.flush()
        answer = code_of(b)
        a.stdin.write(answer + "\n"); a.stdin.flush()
        out_a = a.communicate(timeout=90)[0]
        b.communicate(timeout=90)
        line = next((l for l in reversed(out_a.splitlines()) if l.startswith("{")), None)
        return json.loads(line) if line else {"error": "결과 한 줄이 없음", "output": out_a[-600:]}
    finally:
        lab.down()


SCENARIOS = {
    "cone-cone":           (run_connect, ("cone", "cone")),
    "cone-symmetric":      (run_connect, ("cone", "symmetric")),
    "symmetric-symmetric": (run_connect, ("symmetric", "symmetric")),
    "udp-blocked":         (run_connect, ("udpblock", "udpblock")),
    "rebind":              (run_rebind, ("cone", "cone")),
    "holepunch-script":    (run_holepunch_script, ("cone", "cone")),
}


def check(name: str, res: dict) -> list:
    """기대와 다른 점 목록 (없으면 통과)"""
    bad = []
    if name == "holepunch-script":
        pair = res.get("pair", {})
        if not srflx(pair):
            bad.append(f"후보가 srflx/prflx 가 아님: {pair}")
        if "send_mbps" not in res:
            bad.append("속도 측정 결과가 없음")
        return bad
    if "error" in res:
        return [res["error"]]
    if name == "cone-cone":
        if res.get("mode") != "direct":
            bad.append(f"직접 연결이어야 하는데 {res.get('mode')}")
        elif not srflx(res.get("pair", {})):
            bad.append(f"홀 펀칭(srflx)이 아님: {res.get('pair')}")
    elif name in ("symmetric-symmetric", "udp-blocked"):
        if res.get("mode") != "relay":
            bad.append(f"비상 중계로 넘어가야 하는데 {res.get('mode')}")
        elif "시간 안에 맺어지지 않았습니다" not in res.get("user_log", ""):
            bad.append("직접 연결 실패 원인(연결 검사 시간 초과)이 로그에 구분되어 남지 않음")
    elif name == "cone-symmetric":
        if res.get("mode") not in ("direct", "relay"):
            bad.append("연결되지 않음")
    elif name == "rebind":
        rb = res.get("rebind", {})
        if rb.get("recovered_after_s") is None:
            bad.append("NAT 매핑이 사라진 뒤 복구되지 않음")
    if not res.get("download", {}).get("complete", True):
        bad.append("다운로드가 완전하지 않음")
    return bad


def main():
    ap = argparse.ArgumentParser(description="NAT 실험실")
    ap.add_argument("scenarios", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--json", help="결과를 이 파일에 저장")
    a = ap.parse_args()
    if a.list:
        print("\n".join(SCENARIOS))
        return 0
    why = available()
    if why:
        print(f"NAT 실험실을 쓸 수 없음: {why}")
        return 77
    names = a.scenarios or list(SCENARIOS)
    results, failed = {}, 0
    for name in names:
        fn, (am, bm) = SCENARIOS[name]
        t0 = time.time()
        print(f"\n━━ {name}  (NAT A: {am} / NAT B: {bm}) ━━", flush=True)
        res = fn(name, am, bm)
        res["wall_s"] = round(time.time() - t0, 1)
        bad = check(name, res)
        res["ok"] = not bad
        results[name] = res
        failed += bool(bad)
        print(json.dumps({k: v for k, v in res.items() if k not in ("agent_status", "log", "agent_log", "user_log")}, ensure_ascii=False, indent=1))
        print("  → " + ("통과" if not bad else "실패: " + "; ".join(bad)))
        if bad:
            for key in ("agent_log", "user_log"):
                if res.get(key):
                    print(f"  [{key}]\n" + "\n".join("    " + l for l in res[key].splitlines()[-12:]))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
    print(f"\n{len(names) - failed}/{len(names)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
