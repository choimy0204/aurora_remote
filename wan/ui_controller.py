"""WAN 탭 컨트롤러 — 판단(검사 · 표시 글자 · 스레드 시작)은 전부 여기 (tkinter 없음).  기능별로 ui_relay · ui_field · ui_targets 에 나눠 둠

ctx (aurora_client 가 넘겨 줌)
    cfg               wan.config.WanConfig
    client            wan.client.WanClient       (start() / stop() / state / agents / on_change)
    usage             wan.usage.UsageStore
    supervisor_state  () -> {"state","pid","restarts","next_restart_in","last_exit"}
    set_field_mode    (bool) -> None             현장 에이전트 시작/중지
    relay_available / relay_state / set_relay_host / restart_relay
                      (선택) 이 PC 에서 안내 서버 운영 — relay_available 이 거짓이면 그 칸을 만들지 않는다
    direct            (선택) wan.direct.DirectClient — 있어야 주소로 접속 가능
    agent_info        (선택) () -> {"direct": off|listening|busy, "online": bool}  에이전트가 적어 둔 상태
    wan_enabled / set_wan_enabled
                      (선택) WAN 전체 스위치 — 없으면 항상 켜짐으로 보고 스위치를 만들지 않는다
    relay_fp / direct_fp   (선택) 이 PC 의 안내 서버 · 주소 접속 인증서 지문 (있으면 접속 정보에 넣고 wss:// 로 안내)
    git_sync / git_publish / git_state / set_git_source / rotate_relay_key
                      (선택) 사내 Git 로 접속 정보 주고받기 · 접속 코드 새로고침
    conn_dirs         (선택) 접속 설정 파일(aurora_wan.conn)을 찾는 폴더들 (첫 번째가 저장 기본 위치)
    public_address / reachable   (선택, 시험용) 공인 주소 질의 · 현장 주소 도달 확인 대체
    open_remote       (session, agent_dict) -> None   원격 창 열기 (화면 스레드에서 불림)
    px                (int) -> int               (선택, 기본: 그대로)
    log               (str) -> None              (선택) 앱 로그
"""

import threading
import time
from collections import deque
from datetime import datetime

from .ui_base import LOG_LINES, NullUI, PUBLIC_RETRY, classify_target, level_for_state, validate_url
from .ui_relay import RelayMixin
from .ui_field import FieldMixin
from .ui_targets import TargetsMixin


# ─────────────────────────────────────────
class WanTabController(RelayMixin, FieldMixin, TargetsMixin):
    def __init__(self, ctx: dict, schedule, ui=None, spawn=None, clock=time.time):
        self.ctx = ctx
        self.cfg, self.client, self.usage = ctx["cfg"], ctx["client"], ctx["usage"]
        self.schedule = schedule            # fn → 화면 스레드에서 실행하도록 맡김 (어느 스레드에서 불러도 됨)
        self.ui = ui or NullUI()
        self._spawn = spawn or self._thread_spawn
        self._clock = clock
        self._codes: dict = {}              # 행 id → 마지막으로 쓴 접근 코드 (이번 실행 동안 메모리에만)
        self._connecting: set = set()
        self._restart_lock = threading.Lock()
        self._lines = deque(maxlen=LOG_LINES)
        self._lines_lock = threading.Lock()
        self._reach: dict = {}              # '주소:포트' → 닿는지 (주소로 접속했던 현장의 켜짐/꺼짐 표시용)
        self._probing = False
        self._public = None                 # 인터넷에서 보이는 이 망의 공인 IP
        self._public_at = -PUBLIC_RETRY
        self._public_busy = False
        self._git_alerted = False           # Git 오류 알림 창을 이미 띄웠는지 (복구될 때까지 한 번만)

    @staticmethod
    def _thread_spawn(fn):
        threading.Thread(target=fn, name="wan-ui-worker", daemon=True).start()

    # ── 화면 스레드로 넘기기 ──
    def _ui(self, name, *args):
        self.schedule(lambda: getattr(self.ui, name)(*args))

    # ── 로그 ──
    def log(self, msg: str):
        """작은 로그 창에 한 줄 추가 (어느 스레드에서든 가능)"""
        with self._lines_lock:
            self._lines.append(f"{datetime.fromtimestamp(self._clock()):%H:%M:%S}  {msg}")
            lines = list(self._lines)
        self._ui("set_log", lines)

    def _note(self, msg: str):
        """로그 창 + 앱 로그"""
        self.log(msg)
        fn = self.ctx.get("log")
        if fn:
            try:
                fn(f"[WAN] {msg}")
            except Exception:
                pass

    def _copy(self, text: str, done_msg: str):
        self._ui("copy_text", text, done_msg)

    # ── 0. WAN 전체 켜기/끄기 ──
    def wan_switch_available(self) -> bool:
        return self.ctx.get("set_wan_enabled") is not None

    def wan_on(self) -> bool:
        """꺼져 있으면 중계 PC 접속 · 접속 받기 · 목록 확인 · 정보 가져오기를 하나도 하지 않는다 (스위치가 없는 구성은 항상 켜짐)"""
        fn = self.ctx.get("wan_enabled")
        try:
            return bool(fn()) if fn else True
        except Exception:
            return True

    def wan_toggled(self, enabled: bool) -> bool:
        """WAN 스위치 (작업 스레드)"""
        enabled = bool(enabled)
        self._note("WAN 을 켭니다" if enabled else "WAN 을 끕니다 — 중계 PC 접속과 접속 받기를 모두 멈춥니다")

        def work():
            try:
                self.ctx["set_wan_enabled"](enabled)
            except Exception as e:
                self._note(f"WAN {'켜기' if enabled else '끄기'} 실패: {type(e).__name__}: {e}")
                self._ui("show_error", "WAN", f"변경하지 못했습니다.\n{e}")
            self._ui("set_wan_enabled", self.wan_on())
            if self.wan_on():
                self.refresh_all()
        self._spawn(work)
        return True

    def quick_setup(self, text, as_field=True) -> bool:
        """처음 설정 한 번에: 접속 코드(또는 Git 저장소 주소)만 넣으면 WAN 켜기 → 중계 PC 접속 → (현장이면) 이 PC 를 원격 대상으로 켜고
        방화벽까지 시도한다.  WAN 이 꺼져 있을 때의 첫 화면에서 쓴다"""
        kind = classify_target(text)
        if kind in ("", "address"):
            self._ui("show_error", "시작", "중계 PC 의 접속 코드(AW1-…)나 Git 저장소 주소를 붙여넣으세요")
            return False
        if not self.wan_switch_available():
            self._ui("show_error", "시작", "이 프로그램에서는 사용할 수 없습니다")
            return False
        self._note("접속 코드로 WAN 을 설정합니다" + (" (이 PC 를 현장 PC 로 사용)" if as_field else ""))

        def work():
            try:
                self.ctx["set_wan_enabled"](True)
            except Exception as e:
                self._note(f"WAN 켜기 실패: {type(e).__name__}: {e}")
                self._ui("show_error", "WAN", f"WAN 을 켜지 못했습니다.\n{e}")
                return
            self._ui("set_wan_enabled", True)
            if self.add_connection(text, "", as_field):
                self.refresh_all()
        self._spawn(work)
        return True

    def _need_wan(self) -> bool:
        if self.wan_on():
            return True
        self._ui("show_info", "WAN", "WAN 이 꺼져 있습니다 — 위의 'WAN 사용'을 먼저 켜세요")
        return False

    # ── 1. 중계 PC(안내 서버) 연결 ──
    def server_status(self) -> tuple:
        if not (self.cfg.get("relay_url") or "").strip():
            if self.cfg.get("relay_host"):
                return "이 PC 가 중계 PC", "off"
            return "중계 PC 없음", "off"
        state = self.client.state
        return f"● {state}", level_for_state(state)

    def refresh_server(self):
        self._ui("set_server_status", *self.server_status())

    def apply_server(self, url, key) -> bool:
        """주소·키를 저장하고 안내 서버에 다시 연결 (작업 스레드).  입력이 잘못되면 False"""
        if not (url or "").strip() and self.relay_available():
            return self._apply_key_only(key)
        url, err = validate_url(url)
        if err:
            self._ui("show_error", "안내 서버", err)
            return False
        key = (key or "").strip()
        changed = {"relay_fp": ""} if url != (self.cfg.get("relay_url") or "") else {}      # 다른 서버면 옛 인증서 지문을 버림
        self.cfg.update(relay_url=url, relay_key=key, **changed)
        self._note(f"안내 서버 적용: {url}")
        self._ui("set_busy", "server", True)
        self._spawn(self._restart_client)
        if self.cfg.get("relay_host"):                  # 공용 키가 바뀌었을 수 있으니 이 PC 의 안내 서버도 다시 시작
            self._spawn(self._restart_relay)
        return True

    def _apply_key_only(self, key) -> bool:
        """중계 PC: 주소 없이 공용 키만 저장 — 접속할 안내 서버가 없으니 연결은 끊고, 운영 중인 안내 서버는 새 키로 다시 시작"""
        self.cfg.update(relay_url="", relay_key=(key or "").strip())
        self._note("공용 키를 저장했습니다 (안내 서버 주소 없음 — 이 PC 는 안내 서버 운영용)")

        def work():
            try:
                self.client.stop()
            except Exception as e:
                self._note(f"연결 끊기 실패: {type(e).__name__}: {e}")
            self.refresh_server()
        self._spawn(work)
        if self.cfg.get("relay_host"):
            self._spawn(self._restart_relay)
        else:
            self.refresh_relay()
        return True

    def _restart_client(self):
        try:
            with self._restart_lock:
                self.client.stop()
                self.client.start()
        except Exception as e:
            self._note(f"안내 서버 다시 연결 실패: {type(e).__name__}: {e}")
        finally:
            self._ui("set_busy", "server", False)
            self.refresh_server()
