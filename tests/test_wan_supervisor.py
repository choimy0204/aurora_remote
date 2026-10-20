"""
WAN 에이전트 감독자(wan/supervisor.py) 시험
- 가짜 자식 프로세스는 sys.executable 로 띄우는 작은 파이썬 스크립트
- 모든 대기 시간(backoff · poll · stale_after ...)은 아주 작게 줘서 파일 전체가 빨리 끝난다
"""

import os
import random
import signal
import sys
import textwrap
import time

import psutil
import pytest

from wan import supervisor as S
from wan.supervisor import AgentSupervisor, build_agent_command

# 가짜 자식의 첫머리 — 시작할 때마다 "pid 시각" 한 줄을 기록 (argv[1]: 기록 파일, argv[2]: alive 파일)
PROLOGUE = """
import os, sys, time
with open(sys.argv[1], "a") as f:
    f.write(f"{os.getpid()} {time.time()}\\n")
"""

TINY = dict(backoff=(0.05,), poll=0.02)


# ─────────────────────────────────────────
# 도구
# ─────────────────────────────────────────
def make_child(tmp_path, body: str = "", name: str = "child"):
    """(명령줄, 시작 기록 파일, alive 파일) — body 는 PROLOGUE 다음에 실행됨"""
    script = tmp_path / f"{name}.py"
    script.write_text(PROLOGUE + textwrap.dedent(body))
    starts, alive = tmp_path / f"{name}.starts", tmp_path / f"{name}.alive"
    return [sys.executable, str(script), str(starts), str(alive)], str(starts), str(alive)


def read_starts(path: str) -> list:
    """[(pid, 시각), ...]"""
    try:
        with open(path) as f:
            return [(int(a), float(b)) for a, b in (line.split() for line in f if line.strip())]
    except FileNotFoundError:
        return []


def wait_until(pred, timeout: float = 10.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def gone(pid: int) -> bool:
    return not psutil.pid_exists(pid)


@pytest.fixture
def sup_factory():
    """만든 감독자는 시험이 끝나면 반드시 멈춘다 (자식이 남지 않게)"""
    made = []

    def make(cmd, **kw):
        logs = []
        kw.setdefault("log", logs.append)
        sup = AgentSupervisor(cmd, **kw)
        sup.logs = logs
        made.append(sup)
        return sup
    yield make
    for sup in made:
        sup.stop()


# ─────────────────────────────────────────
# build_agent_command
# ─────────────────────────────────────────
def test_build_command_frozen():
    cmd = build_agent_command(True, r"C:\Aurora\AuroraClient.exe", "aurora_client.py", 1234)
    assert cmd == [r"C:\Aurora\AuroraClient.exe", "--wan-agent", "--parent-pid", "1234"]


def test_build_command_script():
    cmd = build_agent_command(False, "/usr/bin/python3", "/app/aurora_client.py", 77)
    assert cmd == ["/usr/bin/python3", "/app/aurora_client.py", "--wan-agent", "--parent-pid", "77"]


# ─────────────────────────────────────────
# 재시작
# ─────────────────────────────────────────
def test_immediate_exit_restarts_with_growing_delay(tmp_path, sup_factory):
    cmd, starts, _ = make_child(tmp_path, "sys.exit(3)")
    sup = sup_factory(cmd, backoff=(0.1, 0.4, 0.8), poll=0.02, stable_after=100.0)
    sup.start()
    assert wait_until(lambda: len(read_starts(starts)) >= 4)
    times = [t for _, t in read_starts(starts)[:4]]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps[1] > gaps[0] + 0.1 and gaps[2] > gaps[1] + 0.1, gaps
    st = sup.state
    assert st["restarts"] >= 3
    assert st["last_exit"] == 3


def test_stable_child_is_not_restarted(tmp_path, sup_factory):
    cmd, starts, _ = make_child(tmp_path, "time.sleep(60)")
    sup = sup_factory(cmd, **TINY)
    sup.start()
    sup.start()                                    # 두 번 불러도 자식은 하나
    assert wait_until(lambda: len(read_starts(starts)) >= 1)
    time.sleep(1.0)
    assert len(read_starts(starts)) == 1
    st = sup.state
    assert st["state"] == "running" and st["restarts"] == 0 and st["next_restart_in"] is None


def test_failure_counter_resets_after_stable_run(tmp_path, sup_factory):
    # 0.5초 돌고 끝나는 자식 — stable_after(0.3) 를 넘겼으니 매번 첫 번째 간격(0.1)만 기다려야 함
    # (리셋이 없으면 두 번째부터 2.0초 기다려 간격이 2.5초 가까이 됨)
    cmd, starts, _ = make_child(tmp_path, "time.sleep(0.5)")
    sup = sup_factory(cmd, backoff=(0.1, 2.0), poll=0.02, stable_after=0.3)
    sup.start()
    assert wait_until(lambda: len(read_starts(starts)) >= 4, timeout=15)
    times = [t for _, t in read_starts(starts)[:4]]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert max(gaps) < 1.3, gaps


def test_child_gets_env_with_pyinstaller_reset(tmp_path, sup_factory):
    out = tmp_path / "env.txt"
    cmd, starts, _ = make_child(tmp_path, f"""
        open({str(out)!r}, "w").write(os.environ.get("PYINSTALLER_RESET_ENVIRONMENT", "") + "|" + os.environ.get("WAN_TEST_X", ""))
        time.sleep(60)
    """)
    sup = sup_factory(cmd, env={"WAN_TEST_X": "hello", "PYINSTALLER_RESET_ENVIRONMENT": "0"}, **TINY)
    sup.start()
    assert wait_until(out.exists)
    assert wait_until(lambda: out.read_text() == "1|hello")


# ─────────────────────────────────────────
# 멈춤 감지
# ─────────────────────────────────────────
def test_hung_child_is_killed_and_restarted(tmp_path, sup_factory):
    # 0.3초 동안 alive 파일을 갱신하다가 멈춘 척 (프로세스는 살아 있음)
    cmd, starts, alive = make_child(tmp_path, """
        end = time.time() + 0.3
        while time.time() < end:
            open(sys.argv[2], "w").write("x")
            time.sleep(0.05)
        time.sleep(60)
    """)
    sup = sup_factory(cmd, alive_path=alive, stale_after=0.5, startup_grace=0.2, stable_after=100.0, **TINY)
    sup.start()
    assert wait_until(lambda: len(read_starts(starts)) >= 2, timeout=15)
    first_pid = read_starts(starts)[0][0]
    assert wait_until(lambda: gone(first_pid))
    assert any("응답하지 않아" in m for m in sup.logs)


def test_child_that_never_writes_alive_file_is_killed(tmp_path, sup_factory):
    cmd, starts, alive = make_child(tmp_path, "time.sleep(60)")
    sup = sup_factory(cmd, alive_path=alive, stale_after=0.5, startup_grace=0.2, **TINY)
    sup.start()
    assert wait_until(lambda: len(read_starts(starts)) >= 2, timeout=15)
    assert wait_until(lambda: gone(read_starts(starts)[0][0]))


def test_healthy_child_is_not_killed(tmp_path, sup_factory):
    cmd, starts, alive = make_child(tmp_path, """
        while True:
            open(sys.argv[2], "w").write("x")
            time.sleep(0.1)
    """)
    sup = sup_factory(cmd, alive_path=alive, stale_after=0.5, startup_grace=0.2, **TINY)
    sup.start()
    assert wait_until(lambda: len(read_starts(starts)) >= 1)
    time.sleep(2.0)                                # stale_after 의 4배 넘게
    assert len(read_starts(starts)) == 1
    assert sup.state["state"] == "running" and sup.state["restarts"] == 0


# ─────────────────────────────────────────
# stop
# ─────────────────────────────────────────
def test_stop_kills_child_and_no_more_spawns(tmp_path, sup_factory):
    cmd, starts, _ = make_child(tmp_path, "time.sleep(60)")
    sup = sup_factory(cmd, **TINY)
    sup.start()
    assert wait_until(lambda: sup.state["pid"] is not None)
    pid = sup.state["pid"]
    sup.stop()
    assert gone(pid)
    assert sup.state["state"] == "stopped" and sup.state["pid"] is None
    time.sleep(0.5)                                # backoff 가 0.05초이므로 버그였다면 이미 다시 떴을 시간
    assert len(read_starts(starts)) == 1
    sup.stop()                                     # 두 번째 stop 도 문제없음


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM 무시 시험은 POSIX 전용")
def test_stop_kills_child_ignoring_sigterm(tmp_path, sup_factory, monkeypatch):
    monkeypatch.setattr(S, "TERMINATE_GRACE", 0.5)     # 시험이 빨리 끝나도록 (실제 값은 3초)
    ready = tmp_path / "ready"
    cmd, starts, _ = make_child(tmp_path, f"""
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        open({str(ready)!r}, "w").write("1")
        time.sleep(60)
    """)
    sup = sup_factory(cmd, **TINY)
    sup.start()
    assert wait_until(ready.exists)
    pid = sup.state["pid"]
    t0 = time.monotonic()
    sup.stop()
    assert gone(pid)
    assert time.monotonic() - t0 < 4.0


def test_stop_while_waiting_returns_promptly(tmp_path, sup_factory):
    cmd, starts, _ = make_child(tmp_path, "sys.exit(0)")
    sup = sup_factory(cmd, backoff=(30,), poll=0.02)
    sup.start()
    assert wait_until(lambda: (sup.state["next_restart_in"] or 0) > 20)
    t0 = time.monotonic()
    sup.stop()
    assert time.monotonic() - t0 < 1.0
    assert not sup._thread.is_alive()
    assert len(read_starts(starts)) == 1


def test_start_stop_race_leaves_no_orphan(tmp_path, sup_factory):
    marker = f"wan_supervisor_marker_{os.getpid()}"
    cmd = [sys.executable, "-c", "import time; time.sleep(60)", marker]
    for _ in range(15):
        sup = sup_factory(cmd, backoff=(0.01,), poll=0.01)
        sup.start()
        time.sleep(random.uniform(0, 0.05))
        sup.stop()

    def leftovers():
        found = []
        for p in psutil.process_iter(["cmdline", "status"]):
            if marker in (p.info["cmdline"] or []) and p.info["status"] != psutil.STATUS_ZOMBIE:
                found.append(p.pid)
        return found
    assert wait_until(lambda: not leftovers(), timeout=3), leftovers()


# ─────────────────────────────────────────
# 오류 처리 · 상태
# ─────────────────────────────────────────
def test_missing_executable_does_not_kill_thread(tmp_path, sup_factory):
    sup = sup_factory([str(tmp_path / "no_such_program")], **TINY)
    sup.start()
    assert wait_until(lambda: sup.state["restarts"] >= 3)
    assert sup._thread.is_alive()
    assert any("FileNotFoundError" in m for m in sup.logs)
    assert sup.state["pid"] is None


def test_broken_log_function_does_not_kill_thread(tmp_path, sup_factory):
    def bad_log(_msg):
        raise RuntimeError("log failed")
    cmd, starts, _ = make_child(tmp_path, "sys.exit(1)")
    sup = sup_factory(cmd, log=bad_log, **TINY)
    sup.start()
    assert wait_until(lambda: len(read_starts(starts)) >= 3)
    assert sup._thread.is_alive()


def test_state_values(tmp_path, sup_factory):
    cmd, starts, _ = make_child(tmp_path, "time.sleep(0.3); sys.exit(7)")
    sup = sup_factory(cmd, backoff=(30,), poll=0.02)
    assert sup.state == {"state": "stopped", "pid": None, "restarts": 0, "next_restart_in": None, "last_exit": None}

    sup.start()
    assert wait_until(lambda: sup.state["state"] == "running")
    st = sup.state
    assert isinstance(st["pid"], int) and st["pid"] > 0
    assert st["next_restart_in"] is None and st["last_exit"] is None

    assert wait_until(lambda: sup.state["state"] == "waiting" and sup.state["last_exit"] == 7)
    st = sup.state
    assert st["pid"] is None
    assert isinstance(st["next_restart_in"], float) and 0 < st["next_restart_in"] <= 30
    assert st["restarts"] == 0                     # 아직 다시 뜨지 않았음

    sup.stop()
    st = sup.state
    assert st["state"] == "stopped" and st["pid"] is None and st["next_restart_in"] is None
