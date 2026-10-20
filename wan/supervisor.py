"""
WAN 에이전트 감독자
===================
- 메인 프로그램(AuroraClient.exe)이 WAN 에이전트를 **별도 프로세스**(같은 exe + --wan-agent)로 띄우고 지켜본다.
  WAN 쪽 코드가 죽거나 메모리를 다 먹어도 LAN 기능이 있는 메인 프로그램은 영향을 받지 않는다.
- 에이전트가 어떤 이유로든 끝나면 간격을 늘려 가며(backoff) 다시 띄운다. 포기하지 않고, 끝 간격을 계속 쓴다.
- 오래(stable_after) 잘 돌다 끝난 경우에는 "연속 실패" 를 0 으로 되돌려 곧바로 다시 띄운다.
- 멈춘 에이전트(프로세스는 살아 있는데 일을 안 함)는 alive 파일의 수정 시각으로 알아채고 강제로 끝낸 뒤 다시 띄운다.
- 감독 스레드는 어떤 예외로도 죽지 않는다 (예: 실행 파일이 없어 Popen 이 실패해도 같은 간격으로 재시도).
"""

import os
import subprocess
import threading
import time

TERMINATE_GRACE = 3.0        # terminate 후 이 시간 안에 안 끝나면 kill
DEFAULT_BACKOFF = (2, 5, 10, 30, 60, 120, 300, 600)


def build_agent_command(frozen: bool, executable: str, script: str, parent_pid: int) -> list:
    """에이전트를 띄울 명령줄. 묶음(PyInstaller) 실행이면 exe 자신을, 아니면 python + 스크립트를 쓴다"""
    tail = ["--wan-agent", "--parent-pid", str(parent_pid)]
    return [executable] + tail if frozen else [executable, script] + tail


# ─────────────────────────────────────────
# 프로세스 끝내기
# ─────────────────────────────────────────
def _descendants(pid: int) -> list:
    """자식·손자 프로세스. PyInstaller onefile 은 부트로더 밑에 실제 프로세스가 하나 더 있다"""
    try:
        import psutil
        return psutil.Process(pid).children(recursive=True)
    except Exception:
        return []


def _signal_all(procs: list, method: str):
    for p in procs:
        try:
            getattr(p, method)()
        except Exception:
            pass                 # 이미 끝났거나 권한이 없음


def terminate_process(proc: subprocess.Popen):
    """terminate → TERMINATE_GRACE 초 기다림 → kill. 이미 끝났으면 아무 일도 하지 않는다 (여러 번 불러도 안전)"""
    if proc.poll() is not None:
        return
    family = _descendants(proc.pid)
    try:
        proc.terminate()
    except OSError:
        pass
    _signal_all(family, "terminate")
    try:
        proc.wait(TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    _kill_survivors(family)


def _kill_survivors(family: list):
    """부모가 끝난 뒤에도 남은 자식 프로세스를 정리"""
    if not family:
        return
    try:
        import psutil
        _, alive = psutil.wait_procs(family, timeout=TERMINATE_GRACE)
        _signal_all(alive, "kill")
    except Exception:
        _signal_all(family, "kill")


# ─────────────────────────────────────────
# 감독자
# ─────────────────────────────────────────
class AgentSupervisor:
    def __init__(self, cmd: list, alive_path: str | None = None, log=print, env: dict | None = None,
                 backoff=DEFAULT_BACKOFF, stable_after: float = 300.0,
                 stale_after: float = 60.0, startup_grace: float = 30.0, poll: float = 1.0):
        """alive_path: 에이전트가 5초마다 갱신하는 파일 (없으면 멈춤 감지를 하지 않음)"""
        self.cmd, self.alive_path, self.log, self.env = list(cmd), alive_path, log, env
        self.backoff = tuple(backoff) or (10,)
        self.stable_after, self.stale_after = stable_after, stale_after
        self.startup_grace, self.poll = startup_grace, poll
        self._stop = threading.Event()
        self._lock = threading.Lock()         # 자식 실행 · 상태 · stop() 이 서로 엇갈리지 않게
        self._thread = None
        self._proc = None
        self._status = "stopped"
        self._attempts = 0                    # 실행 시도 횟수 (실패 포함)
        self._failures = 0                    # 연속 실패 횟수
        self._last_exit = None
        self._next_restart_at = None

    # ── 겉으로 보이는 동작 ──
    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._status, self._next_restart_at = "waiting", time.monotonic()
            self._thread = threading.Thread(target=self._loop, name="wan-supervisor", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0):
        """감독을 멈추고 자식을 끝낸다. 여러 번, 어느 스레드에서 불러도 안전하며 이후 새 자식은 뜨지 않는다"""
        self._stop.set()
        with self._lock:                      # 실행 중이던 spawn 이 끝나 self._proc 에 올라온 뒤에 가져온다
            proc, thread = self._proc, self._thread
        if proc is not None:
            terminate_process(proc)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            self._status, self._next_restart_at = "stopped", None
            if self._proc is proc and proc is not None and proc.poll() is not None:
                self._last_exit, self._proc = proc.returncode, None

    @property
    def state(self) -> dict:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None and self._status == "running"
            wait_left = None
            if self._status == "waiting" and self._next_restart_at is not None:
                wait_left = max(0.0, self._next_restart_at - time.monotonic())
            return {"state": self._status, "pid": self._proc.pid if running else None,
                    "restarts": max(0, self._attempts - 1), "next_restart_in": wait_left,
                    "last_exit": self._last_exit}

    # ── 감독 스레드 ──
    def _say(self, msg: str):
        try:
            self.log(msg)
        except Exception:
            pass

    def _loop(self):
        while not self._stop.is_set():
            ran = 0.0
            try:
                ran = self._run_once()
            except Exception as e:            # 무슨 일이 있어도 스레드는 살아서 같은 간격으로 재시도
                self._say(f"WAN 에이전트 실행 오류: {type(e).__name__}: {e}")
            if self._stop.is_set():
                break
            self._wait_before_restart(ran)

    def _run_once(self) -> float:
        """자식을 한 번 띄워 끝날 때까지 지켜본다. 돌았던 시간(초)을 돌려줌"""
        started = time.monotonic()
        proc = self._spawn()
        if proc is None:
            return 0.0
        self._watch(proc, started)
        return time.monotonic() - started

    def _spawn(self):
        """stop() 과 겹치지 않게 잠금 안에서 확인하고 띄운다. 이미 멈췄으면 None"""
        with self._lock:
            if self._stop.is_set():
                return None
            self._attempts += 1
            self._remove_alive_file()
            proc = subprocess.Popen(
                self.cmd, env=self._child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=self._creation_flags())
            self._proc, self._status, self._next_restart_at = proc, "running", None
        self._say(f"WAN 에이전트 시작 (pid {proc.pid})")
        return proc

    def _child_env(self) -> dict:
        env = dict(os.environ)
        env.update(self.env or {})
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"      # onefile 로 다시 띄운 exe 가 부모의 임시 폴더를 공유하지 않게
        return env

    @staticmethod
    def _creation_flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    def _remove_alive_file(self):
        """이전 실행이 남긴 옛 파일을 새 에이전트의 신호로 착각하지 않도록 지운다"""
        if self.alive_path:
            try:
                os.remove(self.alive_path)
            except OSError:
                pass

    def _watch(self, proc: subprocess.Popen, started: float):
        """자식이 끝나거나 멈춘 것으로 판단되거나 stop() 될 때까지 지켜본다"""
        while not self._stop.is_set():
            try:
                proc.wait(timeout=self.poll)
                break
            except subprocess.TimeoutExpired:
                pass
            if self._is_hung(started):
                self._say("WAN 에이전트가 응답하지 않아 강제로 끝냅니다")
                terminate_process(proc)
                break
        if proc.poll() is None:               # stop() 이 끝내는 중 — 정리는 stop() 몫
            return
        with self._lock:
            self._last_exit = proc.returncode
            if self._proc is proc:
                self._proc = None
        if not self._stop.is_set():
            self._say(f"WAN 에이전트가 종료됨 (코드 {proc.returncode})")

    def _is_hung(self, started: float) -> bool:
        if not self.alive_path or time.monotonic() - started < self.startup_grace:
            return False
        try:
            return time.time() - os.path.getmtime(self.alive_path) > self.stale_after
        except OSError:
            return True                       # 유예 시간이 지나도 파일이 없음

    def _wait_before_restart(self, ran: float):
        if ran >= self.stable_after:
            self._failures = 0                # 오래 잘 돌았으면 처음부터 다시
        delay = self.backoff[min(self._failures, len(self.backoff) - 1)]
        self._failures += 1
        with self._lock:
            if self._stop.is_set():
                return
            self._status, self._next_restart_at = "waiting", time.monotonic() + delay
        self._say(f"WAN 에이전트를 {delay:g}초 뒤 다시 시작합니다")
        self._stop.wait(delay)                # stop() 이 바로 깨울 수 있게 Event 로 기다림
