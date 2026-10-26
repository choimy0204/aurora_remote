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

import json
import os
import threading
import time

from . import connect_code, connfile, gitsync, tls
from . import update as wan_update
from .client import WanClient
from .netutil import lan_addresses
from .server import remember_old_key as server_remember_old_key
from .config import WanConfig
from .direct import DirectClient
from .server import parse_ver
from .usage import UsageStore

UPDATE_FIRST_DELAY = 60.0          # 시작 후 첫 확인까지 (화면이 뜨고 안정된 뒤)
UPDATE_EVERY       = 6 * 3600.0
GIT_FIRST_DELAY    = 3.0           # 시작 후 첫 Git 확인까지
GIT_EVERY          = 600.0         # 그 뒤 10분마다
GIT_RETRY          = 30.0          # 키가 안 맞아 접속이 막혀 있으면 30초마다 (중계 PC 가 키를 새로 만든 직후 빨리 따라오게)
GIT_TICK           = 5.0


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
        self.direct = DirectClient(self.cfg, self.usage, version, log=lambda m: self.log(f"[WAN] {m}"))
        self.supervisor = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._bg_stop = threading.Event()     # 켜기 때마다 새로 만드는 '주기 작업 멈춤' 신호
        self._running = False
        self._update_thread = None
        self.git_state = {"state": "off", "msg": "", "at": 0}
        self._rot_lock = threading.Lock()

    # ── 시작/종료 ──
    def wan_on(self) -> bool:
        return self.cfg.wan_on()

    def start(self):
        """WAN 이 켜져 있을 때만 시작 (꺼져 있으면 아무것도 가져오거나 접속하지 않는다)"""
        if self.wan_on():
            self._start_features()

    def set_wan_enabled(self, enabled: bool):
        """WAN 전체 켜기/끄기.  끄면 에이전트 · 안내 서버 운영 · 접속 · 주기 확인을 모두 멈춘다 (개별 설정은 그대로 두어 다시 켜면 돌아옴)"""
        with self._lock:
            self.cfg.update(wan_enabled=bool(enabled))
            if enabled:
                self._start_features()
            else:
                self._stop_features()

    def _start_features(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._bg_stop = threading.Event()
        self._safely("접속 설정 파일 적용", self.apply_conn_file)
        self._safely("안내 서버 운영 시작", lambda: self.cfg.get("relay_host") and self.set_relay_host(True))
        self._safely("현장 모드 시작", lambda: self.cfg.get("enabled") and self.set_field_mode(True))
        if self.cfg.get("relay_url") and not self.cfg.get("enabled"):
            self._safely("안내 서버 연결", self._start_client)
        self._update_thread = threading.Thread(target=self._update_loop, args=(self._bg_stop,), name="wan-update", daemon=True)
        self._update_thread.start()
        threading.Thread(target=self._git_loop, args=(self._bg_stop,), name="wan-git", daemon=True).start()

    def _stop_features(self):
        with self._lock:
            self._running = False
            self._bg_stop.set()
        self.stop_agent()
        self.stop_relay()
        self._safely("클라이언트 종료", self.client.stop)
        self._safely("직접 접속 종료", self.direct.stop)

    def _start_client(self):
        with self._lock:                     # 접속 설정 파일을 적용하는 스레드와 겹쳐 시작·중지되지 않게
            self.client.start()

    def shutdown(self):
        self._stop.set()
        self._stop_features()
        self._safely("사용량 저장", self.usage.flush)

    def _safely(self, what: str, fn):
        try:
            return fn()
        except Exception as e:
            self.log(f"[WAN] {what} 실패: {type(e).__name__}: {e}")

    # ── 접속 설정 파일 (exe 옆에 둔 aurora_wan.conn) ──
    def apply_conn_file(self):
        """실행 파일 옆의 aurora_wan.conn 이 있고 처음 보는 내용이면 적용 (주소를 확인하느라 걸리는 시간이 있어 별도 스레드)"""
        path = connfile.find(connfile.search_dirs(self.cfg.dir))
        if path is None:
            return
        try:
            text = connfile.read(path)
        except ValueError as e:
            self.log(f"[WAN] 설정 파일 {path}: {e}")
            return
        if self._is_new_conn(text):
            threading.Thread(target=self._apply_conn_text, args=(text, path), name="wan-conn-file", daemon=True).start()

    def _is_new_conn(self, text: str) -> bool:
        """이미 적용한 내용이거나(중계 PC 자신이 올린 것 포함) 중계 PC 가 다른 중계 PC 의 정보를 받으려는 경우는 건너뜀"""
        if connfile.fingerprint(text) == self.cfg.get("conn_hash"):
            return False
        if connect_code.kind_of(text) == "relay" and self.cfg.get("relay_host"):
            self.log("[WAN] 이 PC 는 중계 PC 라 다른 중계 PC 의 접속 정보는 적용하지 않습니다")
            return False
        return True

    def _apply_conn_text(self, text: str, origin: str):
        def work():
            if connect_code.kind_of(text) == "relay":
                url = connect_code.apply_relay_code(self.cfg, text)
                self.log(f"[WAN] 접속 정보 적용({origin}): 안내 서버 {url}")
                if not self.cfg.get("enabled"):
                    with self._lock:
                        self.client.stop()
                        self.client.start()
            else:
                address = connect_code.remember_direct(self.cfg, text)
                self.log(f"[WAN] 접속 정보 적용({origin}): 현장 PC {address}")
            self.cfg.update(conn_hash=connfile.fingerprint(text))
            self._conn_applied = origin
        self._safely("접속 정보 적용", work)

    # ── 사내 Git 에서 접속 정보 가져오기 · 올리기 ──
    def _git_dir(self) -> str:
        return os.path.join(self.cfg.dir, "git_conn")

    def git_sync_now(self) -> dict:
        """Git 저장소에서 접속 정보를 가져와 새 내용이면 적용.  WAN 이 꺼져 있거나 저장소가 없으면 아무것도 하지 않는다.
        → {"state": off · ok · error, "msg", "at"}  (예외를 내지 않는다)"""
        source = (self.cfg.get("git_source") or "").strip()
        if not self.wan_on() or not source:
            return self._set_git("off", "")
        try:
            text = gitsync.fetch_conn(source, self._git_dir(), self.cfg.get("git_file") or gitsync.DEFAULT_FILE,
                                      self.cfg.get("git_branch") or "")
            if self._is_new_conn(text):
                self._apply_conn_text(text, "Git")
            return self._set_git("ok", "접속 정보를 확인했습니다")
        except gitsync.GitError as e:
            return self._set_git("error", str(e))
        except Exception as e:
            return self._set_git("error", f"{type(e).__name__}: {e}")

    def _set_git(self, state: str, msg: str) -> dict:
        if state == "error" and msg != self.git_state.get("msg"):
            self.log(f"[WAN] Git 접속 정보 가져오기 실패: {msg}")
        self.git_state = {"state": state, "msg": msg, "at": time.time()}
        return self.git_state

    def git_publish(self, source: str, code: str) -> bool:
        """접속 정보를 Git 저장소에 올린다 (이 PC 의 저장소 주소도 기억).  → 실제로 올렸으면 True, 이미 같으면 False.  실패하면 GitError"""
        source = (source or "").strip()
        if not source:
            raise gitsync.GitError("Git 저장소 주소를 입력하세요")
        pushed = gitsync.publish(source, self._git_dir(), code, self.cfg.get("git_file") or gitsync.DEFAULT_FILE,
                                 self.cfg.get("git_branch") or "")
        self.cfg.update(git_source=source, conn_hash=connfile.fingerprint(code))      # 올린 이 PC 가 자기 것을 다시 받아 적용하지 않게
        return pushed

    def set_git_source(self, source: str):
        self.cfg.update(git_source=(source or "").strip())
        if not (source or "").strip():
            self._set_git("off", "")

    def _git_loop(self, stop):
        """Git 에서 접속 정보를 주기적으로 확인. 접속이 키 · 인증서 불일치로 막혀 있으면(중계 PC 가 키를 새로 만든 직후) 더 자주 확인"""
        if stop.wait(GIT_FIRST_DELAY):
            return
        last = None
        while not stop.is_set():
            now = time.monotonic()
            blocked = self.client.state in ("키 불일치", "인증서 불일치")
            if (self.cfg.get("git_source") or "").strip() and (
                    last is None or now - last >= (GIT_RETRY if blocked else GIT_EVERY)):
                self.git_sync_now()
                last = time.monotonic()
            if stop.wait(GIT_TICK):
                return

    # ── 중계 PC: 접속 코드(공용 키) 새로고침 ──
    def relay_code(self) -> str:
        """다른 PC 에 줄 접속 정보 (외부 주소 · 이 PC 의 내부 주소들 · 공용 키 · 인증서 지문)"""
        fp = self.relay_fp() if self.relay_command is not None else ""
        urls = connect_code.relay_urls(int(self.cfg.get("relay_port") or 9100), self.cfg.get("relay_external") or "",
                                       lan_addresses(), bool(fp))
        return connect_code.encode(urls, self.cfg.get("relay_key") or "", fp=fp) if urls else ""

    def rotate_relay_key(self) -> dict:
        """'새로고침' 버튼: 공용 키를 무작위로 새로 만들어 안내 서버를 다시 시작하고(기존 키는 즉시 무효 · 연결 중인 세션은 끊김)
        Git 저장소가 설정되어 있으면 새 접속 정보를 올린다.  Git 에는 **사람이 이 버튼(또는 '올리기')을 눌렀을 때만** 올라간다 — 자동으로는 올리지 않는다.
        → {"key", "code", "published", "error"}  (Git 에 못 올렸으면 error 에 이유가 있고, 복구된 뒤 '올리기'로 다시 올리면 된다)"""
        with self._rot_lock:
            server_remember_old_key(os.path.join(self.cfg.dir, "relay_data"), self.cfg.get("relay_key") or "")
            self.cfg.update(relay_key=connect_code.new_key())
            if self.relay_supervisor is not None:
                self.restart_relay()
            result = {"key": self.cfg.get("relay_key"), "code": self.relay_code(), "published": False, "error": ""}
            source = (self.cfg.get("git_source") or "").strip()
            if source:
                try:
                    self.git_publish(source, result["code"])
                    result["published"] = True
                except gitsync.GitError as e:
                    result["error"] = str(e)
                    self._set_git("error", f"올리기 실패 — {e}")
            self.log("[WAN] 접속 코드를 새로 만들었습니다" + (" (Git 에 올림)" if result["published"] else ""))
            return result

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
        self.cfg.ensure_access_code()
        alive = os.path.join(self.cfg.dir, "wan_agent.alive")
        self.supervisor = AgentSupervisor(self.agent_command(), alive, log=lambda m: self.log(f"[WAN] {m}"))
        self.supervisor.start()

    def stop_agent(self):
        """에이전트 프로세스를 끝냄 (enabled 설정은 그대로 — 다음 실행 때 다시 켜짐)"""
        with self._lock:
            sup, self.supervisor = self.supervisor, None
        if sup is not None:
            self._safely("에이전트 종료", sup.stop)

    def relay_fp(self) -> str:
        """이 PC 에서 운영할 안내 서버의 인증서 지문 (인증서가 없으면 지금 만든다 — 안내 서버 프로세스가 같은 파일을 쓴다)"""
        return tls.cert_fingerprint(tls.ensure_cert(os.path.join(self.cfg.dir, "relay_data"), "relay"))

    def direct_fp(self) -> str:
        return tls.cert_fingerprint(tls.ensure_cert(self.cfg.dir, "direct"))

    def agent_info(self) -> dict:
        """에이전트가 5초마다 적는 상태 파일 → {"direct": off · listening · busy, "online": 안내 서버 접속 여부}.  오래됐으면 빈 값"""
        try:
            path = os.path.join(self.cfg.dir, "wan_agent.alive")
            if time.time() - os.path.getmtime(path) > 20:
                return {}
            with open(path, "r", encoding="utf-8") as f:
                info = json.load(f)
            return info if isinstance(info, dict) else {}
        except (OSError, ValueError):
            return {}

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
    def _update_loop(self, stop):
        if stop.wait(UPDATE_FIRST_DELAY):
            return
        while not stop.is_set():
            self._safely("업데이트 확인", self.check_update)
            if stop.wait(UPDATE_EVERY):
                return

    def check_update(self) -> dict:
        """안내 서버에서 새 버전을 받아 두고(또는 이미 받아 둔 것을 찾아) apply_update 에 넘김"""
        if not (self.wan_on() and self.cfg.get("auto_update") and self.cfg.get("relay_url")):
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
    def tab_context(self, open_remote, px=lambda v: v, log=None, relaunch_admin=None) -> dict:
        return {"relaunch_admin": relaunch_admin, "cfg": self.cfg, "client": self.client, "usage": self.usage,
                "supervisor_state": self.supervisor_state, "set_field_mode": self.set_field_mode,
                "wan_enabled": self.wan_on, "set_wan_enabled": self.set_wan_enabled,
                "git_sync": self.git_sync_now, "git_publish": self.git_publish, "git_state": lambda: self.git_state,
                "set_git_source": self.set_git_source, "rotate_relay_key": self.rotate_relay_key,
                "direct": self.direct, "agent_info": self.agent_info, "relay_fp": self.relay_fp, "direct_fp": self.direct_fp, "conn_dirs": connfile.search_dirs(self.cfg.dir),
                "relay_state": self.relay_state, "set_relay_host": self.set_relay_host,
                "restart_relay": self.restart_relay, "relay_available": self.relay_command is not None,
                "open_remote": open_remote, "px": px, "log": log or self.log}
