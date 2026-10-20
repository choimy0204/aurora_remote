"""
WAN 런타임 — AuroraClient 가 WAN 기능을 켜고 끄는 단 하나의 접점
===============================================================
aurora_client.py 에는 이 객체를 만들고 start/shutdown 을 부르는 몇 줄만 두고, 나머지는 여기서 한다.
(그래야 LAN 프로그램 코드가 거의 바뀌지 않고, 이 부분은 Windows 없이도 시험할 수 있다)

- 현장 모드(enabled): 에이전트를 별도 프로세스로 띄우고 감독한다 (죽으면 WAN 만 다시 시작).
- 사용자 모드: 안내 서버 주소가 있으면 현장 목록을 받는다. (현장 모드인 PC 는 탭에서 연결을 눌렀을 때만)
- 새 버전 확인: 안내 서버에서 받아 두었다가 apply_update 콜백이 교체한다.
모든 단계의 오류는 여기서 잡아 로그로만 남기고 밖(LAN 쪽)으로 던지지 않는다.
"""

import os
import threading

from . import update as wan_update
from .client import WanClient
from .config import WanConfig
from .server import parse_ver
from .usage import UsageStore

UPDATE_FIRST_DELAY = 60.0          # 시작 후 첫 확인까지 (화면이 뜨고 안정된 뒤)
UPDATE_EVERY       = 6 * 3600.0


class WanRuntime:
    def __init__(self, version: str, agent_command, apply_update=None, cfg_dir: str | None = None, log=print,
                 relay_command=None):
        """agent_command: () -> 에이전트 프로세스 명령줄(list)
        apply_update:  (info dict) -> None — 내려받은 새 버전으로 교체 (실행 파일을 바꾸는 쪽에서 구현)
        relay_command: () -> 안내 서버 프로세스 명령줄(list). 없으면 '이 PC 에서 안내 서버 운영' 은 쓸 수 없다"""
        self.version, self.agent_command, self.apply_update, self.log = version, agent_command, apply_update, log
        self.relay_command = relay_command
        self.relay_supervisor = None
        self.cfg = WanConfig(cfg_dir)
        self.usage = UsageStore(os.path.join(self.cfg.dir, "wan_usage.json"))
        self.client = WanClient(self.cfg, self.usage, version, log=lambda m: self.log(f"[WAN] {m}"))
        self.supervisor = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._update_thread = None

    # ── 시작/종료 ──
    def start(self):
        self._safely("안내 서버 운영 시작", lambda: self.cfg.get("relay_host") and self.set_relay_host(True))
        self._safely("현장 모드 시작", lambda: self.cfg.get("enabled") and self.set_field_mode(True))
        if self.cfg.get("relay_url") and not self.cfg.get("enabled"):
            self._safely("안내 서버 연결", self.client.start)
        self._update_thread = threading.Thread(target=self._update_loop, name="wan-update", daemon=True)
        self._update_thread.start()

    def shutdown(self):
        self._stop.set()
        self.stop_agent()
        self.stop_relay()
        self._safely("클라이언트 종료", self.client.stop)
        self._safely("사용량 저장", self.usage.flush)

    def _safely(self, what: str, fn):
        try:
            return fn()
        except Exception as e:
            self.log(f"[WAN] {what} 실패: {type(e).__name__}: {e}")

    # ── 현장 모드 (에이전트 프로세스) ──
    def set_field_mode(self, enabled: bool):
        with self._lock:
            self.cfg.update(enabled=bool(enabled))
            if enabled:
                self._start_agent()
            else:
                self.stop_agent()

    def _start_agent(self):
        if self.supervisor is not None:
            return
        from .supervisor import AgentSupervisor
        self.cfg.ensure_id()
        alive = os.path.join(self.cfg.dir, "wan_agent.alive")
        self.supervisor = AgentSupervisor(self.agent_command(), alive, log=lambda m: self.log(f"[WAN] {m}"))
        self.supervisor.start()

    def stop_agent(self):
        """에이전트 프로세스를 끝냄 (enabled 설정은 그대로 — 다음 실행 때 다시 켜짐)"""
        with self._lock:
            sup, self.supervisor = self.supervisor, None
        if sup is not None:
            self._safely("에이전트 종료", sup.stop)

    def supervisor_state(self) -> dict:
        sup = self.supervisor
        if sup is None:
            return {"state": "stopped", "pid": None, "restarts": 0, "next_restart_in": None, "last_exit": None}
        return sup.state

    # ── 안내 서버 운영 (이 PC 가 중계 PC 일 때 — 별도 프로세스) ──
    def set_relay_host(self, enabled: bool):
        with self._lock:
            self.cfg.update(relay_host=bool(enabled))
            if enabled:
                self._start_relay()
            else:
                self.stop_relay()

    def _start_relay(self):
        if self.relay_supervisor is not None:
            return
        if self.relay_command is None:
            raise RuntimeError("이 프로그램에서는 안내 서버를 운영할 수 없습니다")
        from .supervisor import AgentSupervisor
        d = self.cfg.dir
        cmd = list(self.relay_command()) + ["--port", str(self.cfg.get("relay_port")),
                                            "--data", os.path.join(d, "relay_data"),
                                            "--update-dir", os.path.join(d, "relay_update")]
        env = {"AURORA_RELAY_KEY": self.cfg.get("relay_key") or ""}      # 키는 명령줄이 아니라 환경 변수로
        self.relay_supervisor = AgentSupervisor(cmd, os.path.join(d, "wan_relay.alive"), env=env,
                                                log=lambda m: self.log(f"[WAN] {m.replace('에이전트', '안내 서버')}"))
        self.relay_supervisor.start()

    def stop_relay(self):
        """안내 서버 프로세스를 끝냄 (relay_host 설정은 그대로 — 다음 실행 때 다시 켜짐)"""
        with self._lock:
            sup, self.relay_supervisor = self.relay_supervisor, None
        if sup is not None:
            self._safely("안내 서버 종료", sup.stop)

    def restart_relay(self):
        """포트·공용 키를 바꾼 뒤 적용 — 운영 중일 때만 다시 시작"""
        with self._lock:
            if self.relay_supervisor is None:
                return
            self.stop_relay()
            self._start_relay()

    def relay_state(self) -> dict:
        sup = self.relay_supervisor
        if sup is None:
            return {"state": "stopped", "pid": None, "restarts": 0, "next_restart_in": None, "last_exit": None}
        return sup.state

    # ── 새 버전 ──
    def _update_loop(self):
        if self._stop.wait(UPDATE_FIRST_DELAY):
            return
        while not self._stop.is_set():
            self._safely("업데이트 확인", self.check_update)
            if self._stop.wait(UPDATE_EVERY):
                return

    def check_update(self) -> dict:
        """안내 서버에서 새 버전을 받아 두고(또는 이미 받아 둔 것을 찾아) apply_update 에 넘김"""
        if not (self.cfg.get("auto_update") and self.cfg.get("relay_url")):
            return {"status": "off"}
        dest = wan_update.default_dir(self.cfg)
        pending = wan_update.pending_update(dest)
        if pending and parse_ver(pending["version"]) > parse_ver(self.version):
            res = dict(pending, status="downloaded")
        else:
            if pending:
                wan_update.clear_pending(dest)          # 이미 현재 버전 이하 — 지움
            res = wan_update.check_and_download(self.cfg, self.version)
        if res["status"] == "downloaded" and self.apply_update:
            self.log(f"[WAN] 새 버전 v{res['version']} 을(를) 받았습니다 — 교체합니다")
            self.apply_update(res)
        elif res["status"] == "error":
            self.log(f"[WAN] 업데이트 확인 실패: {res.get('msg')}")
        return res

    # ── 화면(탭)에 넘길 값 ──
    def tab_context(self, open_remote, px=lambda v: v, log=None) -> dict:
        return {"cfg": self.cfg, "client": self.client, "usage": self.usage,
                "supervisor_state": self.supervisor_state, "set_field_mode": self.set_field_mode,
                "relay_state": self.relay_state, "set_relay_host": self.set_relay_host,
                "restart_relay": self.restart_relay, "relay_available": self.relay_command is not None,
                "open_remote": open_remote, "px": px, "log": log or self.log}
