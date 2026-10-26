"""
"WAN 원격" 탭
=============
화면 구성: ① 현장 PC 목록(중계 PC 를 거치는 현장 + 주소로 접속했던 현장) ② 이 PC 를 원격 대상으로 쓰기(주소 · 접근 코드)
③ 접어 둔 "중계 PC 로 사용" ④ 사용량 한 줄 · 로그.  연결 정보를 넣는 곳은 "연결 추가" 창 하나다
(중계 PC 접속 정보 · 현장 PC 접속 정보 · 현장 PC 주소 어느 것이든 붙여넣으면 알아서 구분).
판단(검사·표시 글자 만들기·스레드 시작)은 전부 WanTabController 에 있고 tkinter 를 쓰지 않는다.
WanTab 은 위젯을 만들고 컨트롤러가 부르는 화면 갱신 함수(NullUI 와 같은 모양)만 구현하는 얇은 층이다.
tkinter 는 build_tab() 안에서 불러오므로 이 파일은 tkinter 가 없는 환경에서도 import 된다.

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
    open_remote       (session, agent_dict) -> None   원격 창 열기 (화면 스레드에서 불림)
    px                (int) -> int               (선택, 기본: 그대로)
    log               (str) -> None              (선택) 앱 로그
스레드 규칙: 오래 걸리는 일(연결·설정 보내기·재시작)은 작업 스레드에서, 화면 갱신은 schedule() 로 화면 스레드에서
"""

import math
import os
import socket
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import urlsplit

from . import connect_code, connfile, discover
from .config import DEFAULT_RELAY_KEY, DIRECT_PORT, QUALITY, RELAY_EXIT_PORT_BUSY, format_id
from .netutil import is_admin, lan_addresses, open_firewall, public_address
from .protocol import WanDenied, WanError
from .usage import fmt_bytes

NAME_MAX = 40
CODE_MAX = 64
LOG_LINES = 6
SLOW_MS = 5000       # 현장 목록 · 안내 서버 상태 갱신 간격
FAST_MS = 2000       # 사용량 · 에이전트 상태 갱신 간격

KEEP = "변경 안 함"
FILE_CHOICES = {KEEP: None, "허용": True, "차단": False}
USAGE_HINT = "1시간 사용 시 대략 — 절약 30~150MB · 보통 200MB~1GB · 고화질 1GB+"

COLUMNS = ("이름", "방식", "상태", "마지막 접속")
COLUMN_WIDTHS = (190, 55, 60, 100)
DIRECT_ID = "d:"             # 목록에서 '주소로 접속했던 현장' 행의 id 앞에 붙는 표시 (나머지는 안내 서버가 준 현장 ID)
PUBLIC_RETRY = 300.0         # 공인 주소를 못 알아냈을 때 다시 물어보는 간격(초)
PROBE_TIMEOUT = 0.8          # 주소로 접속했던 현장에 닿는지 확인하는 시간(초)


# ─────────────────────────────────────────
# 순수 함수 (tkinter 없음)
# ─────────────────────────────────────────
def relative_time(last_seen, now=None) -> str:
    """에포크 초 → 방금 / N분 전 / N시간 전 / 어제 / 날짜(YYYY-MM-DD).  값이 없으면 '-'"""
    try:
        ts = float(last_seen)
    except (TypeError, ValueError):
        return "-"
    if ts <= 0:
        return "-"
    now = time.time() if now is None else now
    delta = now - ts
    if delta < 60:
        return "방금"
    if delta < 3600:
        return f"{int(delta // 60)}분 전"
    then, today = datetime.fromtimestamp(ts).date(), datetime.fromtimestamp(now).date()
    days = (today - then).days
    if days <= 0:
        return f"{int(delta // 3600)}시간 전"
    if days == 1:
        return "어제"
    return then.isoformat()


def validate_url(url) -> tuple:
    """→ (정리된 주소, 오류 글자).  오류가 없으면 오류 글자는 ''"""
    u = (url or "").strip()
    if not u:
        return "", "안내 서버 주소를 입력하세요"
    if not u.lower().startswith(("ws://", "wss://")):
        return u, "주소는 ws:// 또는 wss:// 로 시작해야 합니다 (예: ws://203.0.113.5:9100)"
    host = u.split("://", 1)[1]
    if not host or any(c.isspace() for c in u):
        return u, "주소 형식이 올바르지 않습니다 (예: ws://203.0.113.5:9100)"
    return u, ""


def validate_port(text) -> tuple:
    """안내 서버 포트 검사 → (포트, 오류 글자 또는 None)"""
    try:
        port = int(str(text).strip())
    except ValueError:
        return 0, "포트는 숫자로 입력하세요 (예: 9100)"
    if not 1024 <= port <= 65535:
        return 0, "포트는 1024 ~ 65535 사이로 입력하세요"
    return port, None


def validate_code(code) -> tuple:
    """접근 코드 검사 → (정리된 코드, 오류 글자)"""
    c = (code or "").strip()
    if not c:
        return "", "접근 코드를 입력하세요"
    if len(c) > CODE_MAX:
        return c, f"접근 코드는 {CODE_MAX}자 이하여야 합니다"
    return c, ""


def level_for_state(state: str) -> str:
    """안내 서버 연결 상태 → ok / warn / bad (색 결정)"""
    return {"연결됨": "ok", "연결 중": "warn"}.get(state, "bad")


def classify_target(text) -> str:
    """'연결 추가' 창에 넣은 글자의 종류:  relay_code(중계 PC 접속 정보) · direct_code(현장 PC 접속 정보) ·
    relay_url(ws://… 안내 서버 주소) · address(현장 PC 주소) · ''(비어 있음)"""
    t = (text or "").strip()
    if not t:
        return ""
    kind = connect_code.kind_of(t)
    if kind:
        return f"{kind}_code"
    if connect_code.is_git_source(t):
        return "git"
    return "relay_url" if t.lower().startswith(("ws://", "wss://")) else "address"


def is_direct_id(row_id) -> bool:
    return str(row_id).startswith(DIRECT_ID)


# ─────────────────────────────────────────
# 화면 갱신 함수 모음 (컨트롤러 → 화면).  WanTab 과 시험용 가짜가 이 모양을 따른다
# ─────────────────────────────────────────
class NullUI:
    def set_server_status(self, text, level): pass            # 목록 위쪽의 중계 PC 연결 상태. level: ok / warn / bad / off
    def set_rows(self, rows): pass                            # [(행 id, (이름, 방식, 상태, 마지막 접속))]
    def set_field_info(self, id_text, name_text, warn): pass  # warn: 비어 있지 않으면 빨간 경고로 표시
    def set_field_status(self, text, level): pass
    def set_field_enabled(self, enabled): pass                # 체크 상태 + 주소·코드 칸 보이기/숨기기
    def set_field_address(self, text): pass                   # 다른 PC 가 접속할 이 PC 의 주소
    def set_field_code(self, text): pass                      # 이 PC 의 접근 코드 (입력 중이 아닐 때만 갱신)
    def set_relay_status(self, text, level): pass             # 이 PC 에서 안내 서버 운영 상태
    def set_relay_info(self, port_text, hint, warn): pass
    def set_relay_enabled(self, enabled): pass
    def set_relay_code(self, code): pass                      # 다른 PC 에 붙여넣을 접속 정보
    def set_admin(self, is_admin): pass                       # 관리자 권한 여부 (아니면 '관리자로 다시 시작' 버튼 표시)
    def set_relay_git(self, source, status, level): pass          # 중계 PC 카드의 Git 저장소 주소 · 상태 글자
    def set_found_code(self, code): pass                      # 같은 망에서 찾은 접속 정보를 연결 추가 창에 채움
    def ask_yes_no(self, title, message, on_answer): pass     # 예/아니오 물어보고 on_answer(bool) 을 부름 (화면 스레드)
    def ask_code(self, row_id, label, prefill, on_answer): pass   # 접근 코드를 물어 on_answer(코드 또는 None) 을 부름
    def ask_save_path(self, directory, filename, on_answer): pass  # 저장할 파일 위치를 물어 on_answer(경로 또는 None)
    def copy_text(self, text, done_msg): pass                 # 클립보드에 복사
    def set_wan_enabled(self, enabled): pass                  # WAN 전체 스위치 (꺼지면 나머지 칸을 숨김)
    def set_usage(self, day_text, month_text): pass
    def set_log(self, lines): pass
    def set_busy(self, what, busy): pass                      # what: server / connect / config
    def show_error(self, title, message): pass
    def show_info(self, title, message): pass


# ─────────────────────────────────────────
# 컨트롤러
# ─────────────────────────────────────────
class WanTabController:
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

    # ── 1-2. 이 PC 에서 안내 서버 운영 (중계 PC) ──
    def relay_available(self) -> bool:
        return bool(self.ctx.get("relay_available"))

    def relay_port(self) -> int:
        try:
            return int(self.cfg.get("relay_port"))
        except (TypeError, ValueError):
            return 9100

    def relay_status(self) -> tuple:
        try:
            st = self.ctx["relay_state"]() or {}
        except Exception:
            st = {}
        state = str(st.get("state") or "").lower()
        nxt, restarts = st.get("next_restart_in"), st.get("restarts") or 0
        tail = f" · 재시작 {restarts}회" if restarts else ""
        if isinstance(nxt, (int, float)) and not isinstance(nxt, bool) and nxt > 0:
            if st.get("last_exit") == RELAY_EXIT_PORT_BUSY:
                return f"포트 {self.relay_port()} 를 다른 프로그램이 쓰고 있습니다 — {math.ceil(nxt)}초 뒤 다시 시도", "bad"
            return f"{math.ceil(nxt)}초 뒤 재시작{tail}", "warn"
        if state == "running":
            pid = st.get("pid")
            return f"운영 중{f' (PID {pid})' if pid else ''}{tail}", "ok"
        return "꺼짐", "off"

    def relay_info(self) -> tuple:
        """→ (포트 글자, 다른 PC 에 알려 줄 주소 안내, 경고 글자)"""
        port = self.relay_port()
        ips = lan_addresses()
        same = "  ·  ".join(f"ws://{ip}:{port}" for ip in ips[:3]) or "(내부 주소를 찾지 못했습니다)"
        hint = (f"같은 망의 PC 에 입력할 주소:  {same}\n"
                f"다른 망(출장지)에서 쓰려면 공유기에서 TCP {port} 를 이 PC 로 포트포워딩하고  ws://공인IP:{port}  를 입력")
        key = (self.cfg.get("relay_key") or "").strip()
        if not key:
            warn = "공용 키가 비어 있어 누구나 접속을 시도할 수 있습니다"
        elif key == DEFAULT_RELAY_KEY and (self.cfg.get("relay_external") or "").strip():
            warn = "기본 공용 키라 인터넷에서 오는 접속은 거부됩니다 — '자동 설정'을 다시 누르면 새 키가 만들어집니다"
        else:
            warn = ""
        return str(port), hint, warn

    def refresh_relay(self):
        if self.relay_available():
            self._ui("set_relay_status", *self.relay_status())
            self._ui("set_relay_info", *self.relay_info())
            self._ui("set_relay_code", self.relay_code())
            self._ui("set_relay_git", self.cfg.get("git_source") or "", *self.git_status())
            self._ui("set_admin", is_admin() or not self.ctx.get("relaunch_admin"))

    def relay_toggled(self, enabled: bool, port_text) -> bool:
        """'이 PC 에서 안내 서버 운영' 체크가 바뀜 → 포트·설정 저장 + 서버 프로세스 시작/중지 (작업 스레드).  포트가 잘못되면 False"""
        enabled = bool(enabled)
        if enabled:
            port, err = validate_port(port_text)
            if err:
                self._ui("show_error", "안내 서버 운영", err)
                self._ui("set_relay_enabled", False)
                return False
            self.cfg.update(relay_port=port)
        self._note("이 PC 에서 안내 서버를 운영합니다" if enabled else "이 PC 의 안내 서버 운영을 끕니다")
        if enabled and not (self.cfg.get("relay_key") or "").strip():
            self.log("공용 키가 없어 누구나 접속을 시도할 수 있습니다 — '자동 설정'을 누르면 공용 키가 채워집니다")

        def work():
            try:
                self.ctx["set_relay_host"](enabled)
            except Exception as e:
                self.cfg.update(relay_host=not enabled)
                self._note(f"안내 서버 운영 변경 실패: {type(e).__name__}: {e}")
                self._ui("set_relay_enabled", not enabled)
                self._ui("show_error", "안내 서버 운영", f"변경하지 못했습니다.\n{e}")
            self.refresh_relay()
        self._spawn(work)
        return True

    def relay_urls(self) -> list:
        """다른 PC 가 접속할 안내 서버 주소 후보: 외부 주소(적었으면) → 이 PC 의 내부 주소들"""
        return connect_code.relay_urls(self.relay_port(), self.cfg.get("relay_external") or "", lan_addresses(),
                                       bool(self.relay_fp()))

    def relay_fp(self) -> str:
        """이 PC 가 운영하는 안내 서버의 인증서 지문 (암호화를 쓰지 않으면 '')"""
        fn = self.ctx.get("relay_fp")
        try:
            return (fn() if fn else "") or ""
        except Exception:
            return ""

    def relay_code(self) -> str:
        urls = self.relay_urls()
        return connect_code.encode(urls, self.cfg.get("relay_key") or "", fp=self.relay_fp()) if urls else ""

    def copy_relay_code(self):
        code = self.relay_code()
        if code:
            self._copy(code, "접속 정보를 복사했습니다 — 다른 PC 의 '연결 추가'에 붙여넣으세요")

    # ── 접속 코드 새로고침 · Git ──
    def git_status(self) -> tuple:
        try:
            st = (self.ctx["git_state"]() if self.ctx.get("git_state") else {}) or {}
        except Exception:
            st = {}
        if st.get("state") == "ok":
            return "Git 연결됨", "ok"
        if st.get("state") == "error":
            return str(st.get("msg") or "Git 오류"), "bad"
        return "", "off"

    def check_git_alert(self):
        """Git 서버에서 접속 정보를 가져오거나 올리지 못하면 사람이 알 수 있게 알림 창을 한 번 띄운다 (복구되면 다시 알림 가능).
        WAN 이 꺼져 있거나 Git 저장소를 쓰지 않으면 아무 일도 없다"""
        if not self.wan_on() or not (self.cfg.get("git_source") or "").strip():
            self._git_alerted = False
            return
        try:
            st = (self.ctx["git_state"]() if self.ctx.get("git_state") else {}) or {}
        except Exception:
            return
        if st.get("state") != "error":
            if st.get("state") == "ok" and self._git_alerted:
                self._note("Git 서버와 다시 연결되었습니다")
            self._git_alerted = self._git_alerted and st.get("state") not in ("ok", "off")
            return
        if self._git_alerted:
            return
        self._git_alerted = True
        self._note(f"Git 서버 오류: {st.get('msg')}")
        self._ui("show_error", "Git 서버에 연결할 수 없습니다",
                 f"{st.get('msg')}\n\n접속 정보를 새로 받거나 올리지 못했습니다.\n"
                 "· 이미 저장된 접속 정보로는 계속 접속됩니다 (중계 PC 의 키가 바뀌지 않았다면).\n"
                 "· 키가 새로 바뀐 뒤에는 Git 서버가 복구될 때까지 접속이 안 될 수 있습니다.\n"
                 "서버가 복구되면 자동으로 다시 시도합니다.")

    def relay_refresh_code(self) -> bool:
        """'새로고침': 공용 키를 무작위로 새로 만들어 Git 에 올린다.  기존 접속 정보는 사라지고 연결 중인 원격 세션은 끊긴다"""
        if not self.ctx.get("rotate_relay_key"):
            self._ui("show_error", "접속 코드 새로고침", "이 프로그램에서는 사용할 수 없습니다")
            return False
        git = bool((self.cfg.get("git_source") or "").strip())
        self._ui("ask_yes_no", "접속 코드 새로고침",
                 "접속 코드(공용 키)를 새로 만들까요?\n\n· 기존 접속 정보는 즉시 사라집니다.\n· 지금 연결 중인 원격 화면은 끊깁니다.\n"
                 + ("· 새 접속 정보가 지금 Git 저장소에 올라가고, 다른 PC 들이 몇 분 안에 자동으로 따라옵니다."
                    if git else "· Git 저장소가 없어서 다른 PC 에는 새 접속 정보를 직접 전달해야 합니다."),
                 lambda yes: self._do_refresh_code() if yes else None)
        return True

    def _do_refresh_code(self):
        def work():
            try:
                res = self.ctx["rotate_relay_key"]()
            except Exception as e:
                self._note(f"접속 코드 새로고침 실패: {type(e).__name__}: {e}")
                self._ui("show_error", "접속 코드 새로고침", f"새로 만들지 못했습니다.\n{e}")
                return
            self._ui("set_relay_code", res.get("code") or self.relay_code())
            if res.get("error"):
                self._note(f"새 접속 코드를 만들었지만 Git 에 올리지 못했습니다: {res['error']}")
                self._ui("show_error", "Git", f"새 접속 코드를 만들었지만 Git 에 올리지 못했습니다.\nGit 서버가 복구되면 '올리기'를 눌러 올려 주세요 (그동안 다른 PC 는 새 코드를 받지 못합니다).\n\n{res['error']}")
            else:
                self._note("접속 코드를 새로 만들었습니다" + (" — Git 에 올렸습니다" if res.get("published") else ""))
            self.refresh_relay()
        self._spawn(work)

    def publish_git(self, source) -> bool:
        """'올리기': 지금의 접속 정보를 Git 저장소에 올림 (저장소 주소도 기억)"""
        source = (source or "").strip()
        if not source:
            self._ui("show_error", "Git", "Git 저장소 주소를 입력하세요 (예: https://git.회사/aurora/wan-conn.git)")
            return False
        code = self.relay_code()
        if not code or not self.ctx.get("git_publish"):
            self._ui("show_error", "Git", "올릴 접속 정보가 없습니다 — 먼저 '자동 설정'을 누르세요")
            return False

        def work():
            try:
                pushed = self.ctx["git_publish"](source, code)
            except Exception as e:
                text = str(e) or type(e).__name__
                self._note(f"Git 올리기 실패: {text}")
                self._ui("show_error", "Git", text)
            else:
                self._note("접속 정보를 Git 에 올렸습니다" if pushed else "Git 의 접속 정보가 이미 최신입니다")
                self._ui("show_info", "Git", "접속 정보를 Git 에 올렸습니다.\n다른 PC 들은 그 저장소 주소를 '+ 연결 추가'에 한 번 넣으면 이후 자동으로 가져옵니다."
                         if pushed else "Git 의 접속 정보가 이미 최신입니다.")
            self.refresh_relay()
        self._spawn(work)
        return True

    def set_git_source(self, source) -> bool:
        """사용자·현장 PC: 접속 정보를 가져올 Git 저장소를 정하고 바로 한 번 가져온다 (빈 글자면 연동 해제)"""
        source = (source or "").strip()
        if not self.ctx.get("set_git_source"):
            self._ui("show_error", "Git", "이 프로그램에서는 사용할 수 없습니다")
            return False
        self.ctx["set_git_source"](source)
        if not source:
            self._note("Git 연동을 끕니다")
            return True
        self._note(f"Git 저장소에서 접속 정보를 가져옵니다: {source}")
        self._ui("set_busy", "server", True)

        def work():
            res = self.ctx["git_sync"]() if self.ctx.get("git_sync") else {"state": "error", "msg": "동기화를 쓸 수 없습니다"}
            if res.get("state") == "error":
                self._note(f"Git 가져오기 실패: {res.get('msg')}")
                self._ui("show_error", "Git", str(res.get("msg")))
            else:
                self._note("Git 에서 접속 정보를 확인했습니다")
            self._ui("set_busy", "server", False)
            self.refresh_server()
        self._spawn(work)
        return True

    def set_relay_external(self, text):
        text = (text or "").strip()
        changed = text != (self.cfg.get("relay_external") or "")
        self.cfg.update(relay_external=text)
        self._ui("set_relay_code", self.relay_code())
        self._ui("set_relay_info", str(self.relay_port()), *self.relay_info()[1:])
        if changed and self.cfg.get("relay_host") and self.ctx.get("restart_relay"):
            self._spawn(self._restart_relay)               # 같은 망 자동 찾기는 인터넷용 설정이면 꺼진다 — 반영하려고 다시 시작

    def relay_auto_setup(self, external=""):
        """버튼 한 번: 공용 키 만들기(없으면) · 포트 정하기 · 안내 서버 시작 · 방화벽 열기 · 접속 정보 만들기 (작업 스레드)"""
        if not self.relay_available():
            self._ui("show_error", "자동 설정", "이 프로그램에서는 안내 서버를 운영할 수 없습니다")
            return False
        if not is_admin() and self.ctx.get("relaunch_admin"):
            self._ui("ask_yes_no", "관리자 권한",
                     "방화벽을 자동으로 열려면 관리자 권한이 필요합니다.\n\n관리자 권한으로 다시 시작할까요?\n"
                     "(Windows 확인 창이 뜨면 '예'를 누르세요. 다시 시작한 뒤 자동 설정이 이어집니다)\n\n"
                     "'아니요'를 누르면 방화벽은 직접 열어야 합니다.",
                     lambda yes: self.relaunch_admin(["--wan-autosetup"]) if yes else self._auto_setup_now(external))
            return True
        return self._auto_setup_now(external)

    def relaunch_admin(self, extra_args=()) -> bool:
        """관리자 권한으로 다시 시작 (성공하면 이 프로그램은 곧 끝남)"""
        fn = self.ctx.get("relaunch_admin")
        if fn is None:
            return False
        ok, msg = fn(list(extra_args))
        self._note(msg)
        if not ok:
            self._ui("show_error", "관리자 권한", msg)
        return ok

    def _auto_setup_now(self, external=""):
        old_key = (self.cfg.get("relay_key") or "").strip()
        external = (external or "").strip()
        key = old_key or DEFAULT_RELAY_KEY
        if external and key == DEFAULT_RELAY_KEY:             # 인터넷에 여는데 누구나 아는 기본 키면 새 키를 만든다
            key = connect_code.new_key()
            self._note("인터넷용이라 공용 키를 새로 만들었습니다 — 다른 PC 에는 새 접속 정보를 전달하세요")
        port = self.relay_port()
        self.cfg.update(relay_key=key, relay_port=port, relay_external=external, relay_url="")
        self._ui("set_relay_info", str(port), *self.relay_info()[1:])
        self._note(f"중계 PC 자동 설정: 공용 키 준비, 포트 {port}")

        def work():
            try:
                self.ctx["set_relay_host"](True)
                if key != old_key:
                    self.ctx["restart_relay"]()                # 이미 돌고 있었다면 새 키로 다시 시작
                self._ui("set_relay_enabled", True)
            except Exception as e:
                self.cfg.update(relay_host=False)
                self._note(f"안내 서버 시작 실패: {type(e).__name__}: {e}")
                self._ui("show_error", "자동 설정", f"안내 서버를 시작하지 못했습니다.\n{e}")
                return
            try:
                self.client.stop()                   # 이 PC 는 안내 서버 운영용 — 다른 안내 서버에 접속해 있었다면 끊음
            except Exception:
                pass
            ok, msg = open_firewall(port)
            self._note(msg.splitlines()[0])
            self._ui("set_relay_code", self.relay_code())
            self.refresh_server()
            self.refresh_relay()
            if not ok and os.name == "nt":
                self._ui("show_error", "방화벽", msg)
        self._spawn(work)
        return True

    def _restart_relay(self):
        try:
            self.ctx["restart_relay"]()
        except Exception as e:
            self._note(f"안내 서버 다시 시작 실패: {type(e).__name__}: {e}")
        self.refresh_relay()

    def open_firewall(self):
        """Windows 방화벽에서 안내 서버 포트 열기 (작업 스레드)"""
        port = self.relay_port()

        def work():
            ok, msg = open_firewall(port)
            self._note(msg.splitlines()[0])
            self._ui("show_info" if ok else "show_error", "방화벽", msg)
        self._spawn(work)

    # ── 1-3. 접속 설정 파일 ──
    def default_conn_dir(self) -> str:
        dirs = self.ctx.get("conn_dirs") or []
        return dirs[0] if dirs else os.getcwd()

    def save_connection_file(self, path) -> bool:
        """중계 PC 의 접속 정보를 파일로 저장 — exe 와 같은 폴더에 두면 다른 PC 가 실행할 때 자동으로 적용"""
        code = self.relay_code()
        if not code:
            self._ui("show_error", "설정 파일", "저장할 접속 정보가 없습니다 — 먼저 '자동 설정'을 누르세요")
            return False
        try:
            connfile.write(path, code)
        except (OSError, ValueError) as e:
            self._ui("show_error", "설정 파일", f"저장하지 못했습니다.\n{e}")
            return False
        self.cfg.update(conn_hash=connfile.fingerprint(code))      # 이 PC 는 이미 이 설정이다 — 옆에 둔 파일을 다시 적용하지 않게
        self._note(f"설정 파일을 저장했습니다: {path}")
        self._ui("show_info", "설정 파일",
                 f"저장했습니다.\n{path}\n\n이 파일을 AuroraClient.exe 와 같은 폴더에 두면 실행할 때 자동으로 연결됩니다.")
        return True

    def save_connection_dialog(self):
        """'파일로 저장' 버튼: 저장 위치를 물어보고 저장"""
        if not self.relay_code():
            self._ui("show_error", "설정 파일", "저장할 접속 정보가 없습니다 — 먼저 '자동 설정'을 누르세요")
            return
        self._ui("ask_save_path", self.default_conn_dir(), connfile.FILE_NAME,
                 lambda path: self.save_connection_file(path) if path else None)

    def read_connection_file(self, path) -> str:
        """설정 파일에서 접속 정보 글자를 꺼냄 (못 읽으면 오류 창 + 빈 글자)"""
        try:
            return connfile.read(path)
        except ValueError as e:
            self._ui("show_error", "설정 파일", str(e))
            return ""

    # ── 1-4. 연결 추가 (중계 PC 접속 정보 · 현장 PC 접속 정보 · 현장 PC 주소) ──
    def find_on_lan(self):
        """같은 망의 중계 PC 를 찾아 연결 추가 창에 채운다 (작업 스레드)"""
        if not self.wan_on():
            return

        def work():
            code = discover.find_relay()
            if code:
                self._note("같은 망에서 중계 PC 를 찾았습니다")
                self._ui("set_found_code", code)
            else:
                self._note("같은 망에서 중계 PC 를 찾지 못했습니다 — 접속 정보를 붙여넣으세요")
                self._ui("set_found_code", "")
        self._spawn(work)

    def add_connection(self, text, code="", as_field=False) -> bool:
        """'연결 추가' 창의 확인: 넣은 글자의 종류에 따라 중계 PC 에 연결하거나 현장 PC 에 바로 접속"""
        kind = classify_target(text)
        if kind == "":
            self._ui("show_error", "연결 추가", "접속 정보를 붙여넣거나 현장 PC 의 주소를 입력하세요")
            return False
        if not self._need_wan():
            return False
        if kind == "git":
            return self.set_git_source(text)
        if kind == "relay_code":
            return self.import_connection(text, as_field)
        if kind == "relay_url":
            ok = self.apply_server(text, code if (code or "").strip() else self.cfg.get("relay_key"))
            if ok and as_field:
                self.field_toggled(True)
            return ok
        if kind == "direct_code":
            return self._add_direct_code(text)
        try:
            address = connect_code.parse_address(text)
        except ValueError as e:
            self._ui("show_error", "연결 추가", str(e))
            return False
        return self._connect_address(address, (code or "").strip() or self.default_code(DIRECT_ID + address),
                                     validate=True)

    def _add_direct_code(self, text) -> bool:
        try:
            urls, code, fp = connect_code.decode_full(text, connect_code.PREFIX_DIRECT)
        except ValueError as e:
            self._ui("show_error", "연결 추가", str(e))
            return False
        self._ui("set_busy", "connect", True)

        def work():
            address = urlsplit(connect_code.pick_reachable(urls)).netloc
            self._ui("set_busy", "connect", False)
            self._connect_address(address, code, validate=False, fp=fp)
        self._spawn(work)
        return True

    def import_connection(self, text, as_field=False, name="", code="") -> bool:
        """중계 PC 의 접속 정보를 붙여넣어 주소·공용 키를 채우고 바로 연결 (현장이면 이 PC 도 원격 대상으로 켬)"""
        try:
            urls, key, fp = connect_code.decode_full(text)
        except ValueError as e:
            self._ui("show_error", "접속 정보", str(e))
            return False
        code = (code or "").strip()
        if as_field and len(code) > CODE_MAX:
            self._ui("show_error", "접속 정보", f"접근 코드는 {CODE_MAX}자 이하여야 합니다")
            return False
        self._ui("set_busy", "server", True)

        def work():
            try:
                url = connect_code.pick_reachable(urls)
                self.cfg.update(relay_url=url, relay_key=key, relay_fp=fp)
                self._note(f"접속 정보 적용: {url}" + (" (암호화)" if fp else ""))
                if as_field:
                    name_ = (name or "").strip() or (self.cfg.get("name") or "").strip() or socket.gethostname()
                    self.cfg.update(name=name_[:NAME_MAX], **({"access_code": code} if code else {}))   # 코드를 비우면 공용 키로 접속
                    self.field_toggled(True)
                self._restart_client()
            except Exception as e:
                self._note(f"접속 정보 적용 실패: {type(e).__name__}: {e}")
                self._ui("set_busy", "server", False)
        self._spawn(work)
        return True

    # ── 2. 이 PC (현장) ──
    def field_code(self) -> str:
        """이 PC 의 접근 코드: 따로 정한 코드, 없으면 공용 키"""
        return (self.cfg.get("access_code") or self.cfg.get("relay_key") or "").strip()

    def set_access_code(self, text) -> bool:
        """화면에서 접근 코드를 바꿈 — 공용 키와 같거나 비우면 '따로 정한 코드 없음'(공용 키로 접속).  에이전트는 파일 변경을 바로 따라간다"""
        text = (text or "").strip()
        if len(text) > CODE_MAX:
            self._ui("show_error", "접근 코드", f"접근 코드는 {CODE_MAX}자 이하여야 합니다")
            return False
        shared = (self.cfg.get("relay_key") or "").strip()
        new = "" if text == shared else text
        if new != (self.cfg.get("access_code") or ""):
            self.cfg.update(access_code=new)
            self._note("접근 코드를 바꿨습니다" if new else "접근 코드를 공용 키로 되돌렸습니다")
        self.refresh_field()
        return True

    def direct_port(self) -> int:
        try:
            return int(self.cfg.get("direct_port"))
        except (TypeError, ValueError):
            return DIRECT_PORT

    def field_address_text(self) -> str:
        """다른 PC 가 이 PC 에 접속할 주소 (같은 망 주소 + 알아냈다면 공인 주소)"""
        port = self.direct_port()
        parts = [f"{ip}:{port}" for ip in lan_addresses()[:1]]
        if self._public:
            parts.append(f"공인 {self._public}:{port}")
        return "  ·  ".join(parts) or "(주소를 찾지 못했습니다)"

    def field_direct_code(self) -> str:
        """이 PC 하나에 바로 접속하는 정보 한 줄 (주소들 + 접근 코드)"""
        port = self.direct_port()
        urls = [f"wss://{ip}:{port}" for ip in lan_addresses()[:3]]
        if self._public:
            urls.append(f"wss://{self._public}:{port}")
        return connect_code.encode_direct(urls, self.field_code(), self.direct_fp()) if urls else ""

    def direct_fp(self) -> str:
        """이 PC 의 주소 접속용 인증서 지문"""
        fn = self.ctx.get("direct_fp")
        try:
            return (fn() if fn else "") or ""
        except Exception:
            return ""

    def copy_field_code(self):
        code = self.field_direct_code()
        if code:
            self._copy(code, "접속 정보를 복사했습니다 — 접속할 PC 의 '연결 추가'에 붙여넣으세요")

    def refresh_public(self, force=False):
        """인터넷에서 보이는 공인 IP 를 STUN 으로 알아냄 (작업 스레드, 몇 분에 한 번만)"""
        now = self._clock()
        if not self.wan_on() or self._public_busy or (not force and now - self._public_at < PUBLIC_RETRY):
            return
        self._public_busy, self._public_at = True, now
        lookup = self.ctx.get("public_address") or public_address

        def work():
            try:
                ip = lookup(self.cfg.get("stun") or "")
            except Exception:
                ip = None
            finally:
                self._public_busy = False
            if ip and ip != self._public:
                self._public = ip
                self._ui("set_field_address", self.field_address_text())
        self._spawn(work)

    def field_info(self) -> tuple:
        """→ (ID 글자, 이름 글자, 경고 글자)"""
        agent_id = self.cfg.ensure_id()
        name = self.cfg.get("name") or ""
        code = self.field_code()
        if not code:
            warn = "접근 코드도 공용 키도 없어 모든 접속을 거부합니다"
        elif code == DEFAULT_RELAY_KEY and self._public:
            warn = "기본 코드라 인터넷에서는 접속할 수 없습니다 — 인터넷에서 쓰려면 접근 코드를 바꾸세요"
        else:
            warn = ""
        return (format_id(agent_id), name.strip() or "(이름 없음)", warn)

    def field_status(self) -> tuple:
        """에이전트 프로세스 상태 → (글자, ok / warn / off / bad)"""
        try:
            st = self.ctx["supervisor_state"]() or {}
        except Exception:
            st = {}
        state = str(st.get("state") or "").lower()
        nxt = st.get("next_restart_in")
        restarts = st.get("restarts") or 0
        tail = f" · 재시작 {restarts}회" if restarts else ""
        if isinstance(nxt, (int, float)) and not isinstance(nxt, bool) and nxt > 0:
            return f"{math.ceil(nxt)}초 뒤 재시작{tail}", "warn"
        if state in ("starting", "시작 중"):
            return "시작 중", "warn"
        if state in ("running", "실행 중", "실행중") or "run" in state or "실행" in state:
            pid = st.get("pid")
            return self._with_agent_info(f"실행 중{f' (PID {pid})' if pid else ''}{tail}")
        return f"꺼짐{tail}", "off"

    def _with_agent_info(self, text) -> tuple:
        """실행 중인 에이전트의 상태 파일에서 '주소 접속 대기 · 중계 연결' 정보를 덧붙임"""
        fn = self.ctx.get("agent_info")
        try:
            info = (fn() if fn else {}) or {}
        except Exception:
            info = {}
        if not info:
            return text, "ok"
        if info.get("direct") == "busy":
            return f"포트 {self.direct_port()} 를 다른 프로그램이 쓰고 있어 주소 접속을 받지 못합니다", "bad"
        parts = [text]
        if info.get("direct") == "listening":
            parts.append("주소 접속 대기")
        if (self.cfg.get("relay_url") or "").strip():
            parts.append("중계 연결됨" if info.get("online") else "중계 연결 중")
        return " · ".join(parts), "ok"

    def field_status_text(self) -> str:
        return self.field_status()[0]

    def refresh_field(self, reload=False):
        if reload:        # 에이전트(다른 프로세스)가 안내 서버에서 받은 설정을 파일에 적었을 수 있음
            try:
                self.cfg.load()
            except Exception:
                pass
        enabled = bool(self.cfg.get("enabled"))
        self._ui("set_field_info", *self.field_info())
        self._ui("set_field_status", *self.field_status())
        self._ui("set_field_enabled", enabled)
        self._ui("set_field_code", self.field_code())
        self._ui("set_field_address", self.field_address_text())
        if enabled:
            self.refresh_public()

    def field_toggled(self, enabled: bool):
        """'이 PC를 원격 대상으로 사용' 체크가 바뀜 → 설정 저장 + 에이전트 시작/중지 (작업 스레드)"""
        enabled = bool(enabled)
        self.cfg.update(enabled=enabled)
        self.cfg.ensure_id()
        self._note("이 PC 를 원격 대상으로 사용합니다" if enabled else "이 PC 의 원격 대상 사용을 끕니다")
        if enabled:
            self.cfg.ensure_access_code()              # 접근 코드가 없으면 무작위로 만든다 (중계 PC 가 접속하는 PC 에 알려 줌)

        def work():
            try:
                self.ctx["set_field_mode"](enabled)
            except Exception as e:
                self.cfg.update(enabled=not enabled)
                self._note(f"현장 모드 변경 실패: {type(e).__name__}: {e}")
                self._ui("set_field_enabled", not enabled)
                self._ui("show_error", "이 PC (현장)", f"변경하지 못했습니다.\n{e}")
            else:
                if enabled and os.name == "nt":
                    self._open_direct_firewall()
            self.refresh_field()
        self._spawn(work)

    def _open_direct_firewall(self):
        """주소 접속 포트를 방화벽에서 열어 봄 (관리자가 아니면 조용히 실패 — Windows 가 처음 접속 때 허용을 물어본다)"""
        ok, msg = open_firewall(self.direct_port(), direct=True)
        self._note(msg.splitlines()[0] if ok else "방화벽 포트를 자동으로 열지 못했습니다 (관리자 권한 필요) — 허용 창이 뜨면 '허용'")

    # ── 3. 현장 PC 목록 ──
    def direct_targets(self) -> dict:
        return dict(self.cfg.get("direct_targets") or {})

    def rows_with_ids(self) -> list:
        """목록 행 → [(행 id, 표시용 글자 튜플)].  켜진 PC 먼저, 그 안에서는 이름순.
        행 id: 안내 서버를 거치는 현장은 현장 ID, 주소로 접속했던 현장은 'd:주소:포트'"""
        now = self._clock()
        out = []
        for a in list(self.client.agents or []):
            aid = str(a.get("id", ""))
            online = bool(a.get("online"))
            name = (a.get("name") or "").strip() or "(이름 없음)"
            applied = int(a.get("applied_rev") or 0) >= int(a.get("rev") or 0)
            state = ("켜짐" if online else "꺼짐") + ("" if applied else " · 설정 대기")
            out.append((not online, name.casefold(), aid, name, aid,
                        (name, "중계", state, "지금" if online else relative_time(a.get("last_seen"), now))))
        for address, t in self.direct_targets().items():
            reach = self._reach.get(address)
            online = reach is True
            name = (t.get("name") or "").strip() or address
            out.append((not online, name.casefold(), DIRECT_ID + address, name, DIRECT_ID + address,
                        (name, "주소", "켜짐" if online else "꺼짐" if reach is False else "확인 중",
                         "지금" if online else relative_time(t.get("last"), now))))
        out.sort(key=lambda r: r[:3])
        same = {}
        for r in out:
            same[r[3].casefold()] = same.get(r[3].casefold(), 0) + 1
        rows = []
        for _o, _n, _k, name, rid, values in out:
            if same[name.casefold()] > 1 and not is_direct_id(rid):          # 이름이 겹치면 ID 앞 4자로 구분
                values = (f"{name} ({format_id(rid)[:4]})",) + values[1:]
            rows.append((rid, values))
        return rows

    def rows(self) -> list:
        return [values for _rid, values in self.rows_with_ids()]

    def agent(self, row_id: str):
        """행 id → 현장 정보 dict (id · name …).  주소로 접속했던 현장도 같은 모양으로 만들어 줌"""
        if is_direct_id(row_id):
            address = str(row_id)[len(DIRECT_ID):]
            t = self.direct_targets().get(address)
            if t is None:
                return None
            return {"id": row_id, "name": (t.get("name") or "").strip() or address, "address": address, "direct": True}
        for a in list(self.client.agents or []):
            if a.get("id") == row_id:
                return dict(a)
        return None

    def refresh_agents(self):
        self._ui("set_rows", self.rows_with_ids())

    def probe_targets(self):
        """주소로 접속했던 현장이 지금 닿는지 확인해 목록의 켜짐/꺼짐을 갱신 (작업 스레드)"""
        targets = list(self.direct_targets())
        if not targets or self._probing or not self.wan_on():
            return
        self._probing = True
        check = self.ctx.get("reachable") or (lambda a: connect_code.reachable(a, PROBE_TIMEOUT))

        def work():
            try:
                for address in targets:
                    try:
                        self._reach[address] = bool(check(address))
                    except Exception:
                        self._reach[address] = False
            finally:
                self._probing = False
            self.refresh_agents()
        self._spawn(work)

    def forget_target(self, row_id: str) -> bool:
        """목록에서 지우기.  주소로 접속했던 현장은 바로 지우고, 중계 PC 의 현장은 (꺼져 있을 때만) 확인을 받아 중계 PC 목록에서 지운다"""
        if not is_direct_id(row_id):
            return self._forget_relay(row_id)
        targets = self.direct_targets()
        if targets.pop(str(row_id)[len(DIRECT_ID):], None) is None:
            return False
        self.cfg.update(direct_targets=targets)
        self._codes.pop(row_id, None)
        self.refresh_agents()
        return True

    def rename_target(self, row_id: str, name) -> bool:
        """목록의 이름 바꾸기.  주소로 접속했던 현장은 이 PC 에만 저장하고, 중계 PC 의 현장은 '설정 보내기'와 같은 경로로 현장에 보낸다
        (꺼져 있으면 다음 접속 때 적용되고, 중계 PC 목록에는 바로 바뀐 이름이 보인다)"""
        name = (name or "").strip()
        if not name:
            self._ui("show_error", "이름 바꾸기", "이름을 입력하세요")
            return False
        if len(name) > NAME_MAX:
            self._ui("show_error", "이름 바꾸기", f"이름은 {NAME_MAX}자 이하여야 합니다")
            return False
        if not is_direct_id(row_id):
            if self.agent(row_id) is None:
                self._ui("show_error", "이름 바꾸기", "목록에서 현장 PC 를 먼저 선택하세요")
                return False
            return self.send_config(row_id, name=name)
        address = str(row_id)[len(DIRECT_ID):]
        targets = self.direct_targets()
        if address not in targets:
            return False
        targets[address] = dict(targets[address], name=name)
        self.cfg.update(direct_targets=targets)
        self.refresh_agents()
        return True

    def _forget_relay(self, agent_id: str) -> bool:
        agent = self.agent(agent_id)
        if agent is None:
            return False
        if agent.get("online"):
            self._ui("show_info", "삭제", "켜져 있는 현장은 지울 수 없습니다 — 현장 PC 를 끈 뒤 지우세요")
            return False
        label = self._label(agent_id, agent)
        self._ui("ask_yes_no", "현장 삭제",
                 f"'{label}' 을(를) 중계 PC 의 현장 목록에서 지울까요?\n(이 중계 PC 를 쓰는 모든 PC 의 목록에서 사라집니다. 그 PC 가 다시 켜지면 새로 나타납니다)",
                 lambda yes: self._do_forget_relay(agent_id, label) if yes else None)
        return True

    def _do_forget_relay(self, agent_id: str, label: str):
        def work():
            try:
                self.client.forget_agent(agent_id)
            except Exception as e:
                text = str(e) or type(e).__name__
                self._note(f"{label} 삭제 실패: {text}")
                self._ui("show_error", "삭제", text)
            else:
                self._saved_forget(agent_id)
                self._note(f"{label} 을(를) 목록에서 지웠습니다")
                self.refresh_agents()
        self._spawn(work)

    def _saved_forget(self, agent_id: str):
        self._forget_code(agent_id)
        self._codes.pop(agent_id, None)

    def on_client_change(self, what):
        """WanClient.on_change — 클라이언트 스레드에서 불림.  화면 갱신은 화면 스레드로"""
        if what == "agents":
            self.refresh_agents()
        elif what == "state":
            self.refresh_server()
            self.refresh_agents()

    def remembered_code(self, row_id: str) -> str:
        if is_direct_id(row_id):
            saved = (self.direct_targets().get(str(row_id)[len(DIRECT_ID):]) or {}).get("code", "")
            return self._codes.get(row_id) or saved
        return self._codes.get(row_id) or (self.cfg.get("saved_codes") or {}).get(row_id, "")

    def default_code(self, row_id: str) -> str:
        """더블클릭 때 쓸 코드: 중계 PC 가 알려 준 현장의 코드 → 한 번 입력해 둔 코드 → 없으면 공용 키"""
        if not is_direct_id(row_id):
            given = ((self.agent(row_id) or {}).get("code") or "").strip()      # 중계 PC 가 현장의 접근 코드를 알려 줌
            if given:
                return given
        return self.remembered_code(row_id) or (self.cfg.get("relay_key") or "").strip()

    def quick_connect(self, row_id: str) -> bool:
        """더블클릭 / '원격 연결': 코드를 묻지 않고 바로 연결. 코드가 안 맞을 때만 한 번 물어본다"""
        if not self._need_wan():
            return False
        agent = self.agent(row_id)
        if agent is None:
            self._ui("show_error", "원격 연결", "목록에서 현장 PC 를 먼저 선택하세요")
            return False
        code = self.default_code(row_id)
        if not code:
            self._ask_code(row_id, agent, "")
            return True
        if agent.get("direct"):
            return self._connect_address(agent["address"], code, ask_on_deny=True, validate=False)
        return self.connect(row_id, code, ask_on_deny=True, validate=False)

    def _label(self, row_id, agent):
        return (agent.get("name") or "").strip() or format_id(row_id)

    def _ask_code(self, row_id, agent, why):
        retry = ((lambda code: self._connect_address(agent["address"], code)) if agent.get("direct")
                 else (lambda code: self.connect(row_id, code)))
        self._ui("ask_code", row_id, self._label(row_id, agent), why,
                 lambda code: code is not None and retry(code))

    def _save_code(self, agent_id, code):
        """공용 키가 아닌, 따로 입력한 코드만 기억 (다음부터 더블클릭만으로 연결)"""
        if code == (self.cfg.get("relay_key") or "").strip():
            return
        saved = dict(self.cfg.get("saved_codes") or {})
        saved[agent_id] = code
        self.cfg.update(saved_codes=saved)

    def _forget_code(self, agent_id):
        saved = dict(self.cfg.get("saved_codes") or {})
        if saved.pop(agent_id, None) is not None:
            self.cfg.update(saved_codes=saved)

    def connect(self, agent_id: str, code, ask_on_deny=False, validate=True) -> bool:
        """중계 PC 를 거치는 현장에 연결 (작업 스레드).  성공하면 ctx["open_remote"](session, agent) 를 화면 스레드에서 부름.
        ask_on_deny: 코드가 맞지 않으면 오류 대신 접근 코드를 물어봄"""
        agent = self.agent(agent_id)
        if agent is None:
            self._ui("show_error", "원격 연결", "목록에서 현장 PC 를 먼저 선택하세요")
            return False
        name = self._label(agent_id, agent)
        return self._run_connect(agent_id, agent, name, code, lambda c: self.client.open_session(agent_id, c, name),
                                 ask_on_deny, validate, on_ok=lambda c, s: self._save_code(agent_id, c))

    def _connect_address(self, address: str, code, ask_on_deny=False, validate=True, fp="") -> bool:
        """현장 PC 의 주소로 바로 접속 (안내 서버 없이)"""
        direct = self.ctx.get("direct")
        if direct is None:
            self._ui("show_error", "연결 추가", "이 프로그램에서는 주소로 접속할 수 없습니다")
            return False
        row_id = DIRECT_ID + address
        agent = self.agent(row_id) or {"id": row_id, "name": address, "address": address, "direct": True}
        known = "" if agent["name"] == address else agent["name"]              # 이름을 모르면 현장이 알려 주는 이름을 쓴다
        pin = fp or (self.direct_targets().get(address) or {}).get("fp", "")      # 알고 있는 인증서 지문으로 고정
        return self._run_connect(row_id, agent, agent["name"], code,
                                 lambda c: direct.open_session(address, c, known, pin),
                                 ask_on_deny, validate, on_ok=lambda c, s: self._remember_target(address, c, s))

    def _remember_target(self, address, code, session):
        targets = self.direct_targets()
        old = targets.get(address) or {}
        name = (getattr(session, "name", "") or old.get("name") or "").strip()
        targets[address] = {"name": name if name != address else "", "code": code, "last": self._clock(),
                            "fp": getattr(session, "fp", "") or old.get("fp", "")}
        self.cfg.update(direct_targets=targets)
        self._reach[address] = True
        self.refresh_agents()

    def _run_connect(self, row_id, agent, name, code, opener, ask_on_deny, validate, on_ok) -> bool:
        if validate:
            code, err = validate_code(code)
            if err:
                self._ui("show_error", "원격 연결", err)
                return False
        if row_id in self._connecting:
            self.log("이미 연결하는 중입니다")
            return False
        self._connecting.add(row_id)
        self._codes[row_id] = code
        self._ui("set_busy", "connect", True)
        self.log(f"{name} 에 연결하는 중…")

        def work():
            retry = False
            try:
                session = opener(code)
            except WanDenied as e:
                self._codes.pop(row_id, None)      # 틀린 코드는 다음에 미리 채우지 않음
                if not is_direct_id(row_id):
                    self._forget_code(row_id)
                if ask_on_deny and "맞지 않" in str(e):
                    retry = True
                    self._note(f"{name}: 접근 코드가 필요합니다")
                else:
                    self._fail(name, e)
            except WanError as e:
                self._fail(name, e)
            except Exception as e:
                self._fail(name, f"{type(e).__name__}: {e}")
            else:
                on_ok(code, session)
                self._note(f"{name} 연결됨 ({getattr(session, 'mode', '')})")
                agent_ = self.agent(row_id) or agent
                self.schedule(lambda: self.ctx["open_remote"](session, agent_))
            finally:
                self._connecting.discard(row_id)
                self._ui("set_busy", "connect", bool(self._connecting))
            if retry:
                self._ask_code(row_id, agent, "이 현장 PC 는 따로 접근 코드를 정해 두었습니다")
        self._spawn(work)
        return True

    def _fail(self, name, err):
        text = str(err) or "연결하지 못했습니다"
        self._note(f"{name} 연결 실패: {text}")
        self._ui("show_error", "원격 연결 실패", text)

    def send_config(self, agent_id: str, name=None, code=None, max_quality=None, allow_files=None) -> bool:
        """현장 설정 보내기 (작업 스레드).  비어 있는 항목(None / '')은 보내지 않는다"""
        if is_direct_id(agent_id):
            self._ui("show_info", "설정 보내기", "주소로 접속한 현장은 설정을 보낼 수 없습니다 — 그 PC 에서 직접 바꾸세요")
            return False
        cfg, problems = {}, []
        name = (name or "").strip()
        if name:
            if len(name) > NAME_MAX:
                problems.append(f"이름은 {NAME_MAX}자 이하여야 합니다")
            else:
                cfg["name"] = name
        code = (code or "").strip()
        if code:
            code, err = validate_code(code)
            if err:
                problems.append(err)
            else:
                cfg["access_code"] = code
        if max_quality:
            if max_quality in QUALITY:
                cfg["max_quality"] = max_quality
            else:
                problems.append("최고 화질이 올바르지 않습니다")
        if allow_files is not None:
            cfg["allow_files"] = bool(allow_files)
        if problems:
            self._ui("show_error", "설정 보내기", "\n".join(problems))
            return False
        if not cfg:
            self._ui("show_error", "설정 보내기", "보낼 항목이 없습니다 — 바꿀 항목만 채워 주세요")
            return False
        agent = self.agent(agent_id)
        label = ((agent or {}).get("name") or "").strip() or format_id(agent_id)
        self._ui("set_busy", "config", True)
        self.log(f"{label} 에 설정 보내는 중 ({', '.join(cfg)})")

        def work():
            try:
                reply = self.client.set_config(agent_id, cfg)
            except Exception as e:
                text = str(e) or type(e).__name__
                self._note(f"{label} 설정 보내기 실패: {text}")
                self._ui("show_error", "설정 보내기 실패", text)
            else:
                if "access_code" in cfg:
                    self._codes[agent_id] = cfg["access_code"]
                if reply.get("online"):
                    msg = f"{label} 설정 적용됨"
                else:
                    msg = f"{label} 는 꺼져 있어 다음 접속 때 적용됩니다"
                self._note(msg)
                self._ui("show_info", "설정 보내기", msg)
                self.refresh_agents()
            finally:
                self._ui("set_busy", "config", False)
        self._spawn(work)
        return True

    # ── 4. 데이터 사용량 ──
    def usage_texts(self) -> tuple:
        return (f"오늘 {fmt_bytes(self.usage.day_total())}",
                f"이번 달 {fmt_bytes(self.usage.month_total())}")

    def usage_text(self) -> str:
        return " · ".join(self.usage_texts())

    def refresh_usage(self):
        self._ui("set_usage", *self.usage_texts())

    # ── 주기 갱신 ──
    def refresh_all(self):
        self._ui("set_wan_enabled", self.wan_on())
        self.refresh_server()
        self.refresh_relay()
        self.refresh_agents()
        self.refresh_field()
        self.refresh_usage()
        self.probe_targets()

    def tick_slow(self):
        self.refresh_server()
        self.refresh_agents()
        self.probe_targets()

    def tick_fast(self):
        self.check_git_alert()
        self.refresh_relay()
        self.refresh_usage()
        self.refresh_field(reload=True)


# ─────────────────────────────────────────
# 화면 (얇은 위젯 층)
# ─────────────────────────────────────────
class WanTab(NullUI):
    def __init__(self, app, parent, ctx, tk, ttk, messagebox, simpledialog, filedialog=None):
        self.app, self.parent, self.ctx = app, parent, ctx
        self.tk, self.ttk, self.mb, self.sd, self.fd = tk, ttk, messagebox, simpledialog, filedialog
        self.root = app.root
        self.px = ctx.get("px") or (lambda v: v)
        self._closed = False
        self._build()
        self.controller = WanTabController(ctx, self._schedule, ui=self)
        self._hook_client()
        self.controller.refresh_all()
        self.root.after(SLOW_MS, self._loop_slow)
        self.root.after(FAST_MS, self._loop_fast)
        parent.bind("<Destroy>", lambda e: self.stop() if e.widget is parent else None, add="+")

    # ── 스레드 · 주기 ──
    def _schedule(self, fn):
        """어느 스레드에서든 fn 을 화면 스레드에서 실행하게 맡김 (창이 이미 없으면 조용히 버림)"""
        def run():
            if self._closed:
                return
            try:
                fn()
            except self.tk.TclError:
                pass
            except Exception as e:
                self._log_error(e)
        try:
            self.root.after(0, run)
        except Exception:
            pass

    def _log_error(self, e):
        try:
            self.controller.log(f"화면 갱신 오류: {type(e).__name__}: {e}")
        except Exception:
            pass

    def _hook_client(self):
        """WanClient.on_change 에 이 탭의 갱신을 덧붙임 (원래 콜백은 그대로 부름)"""
        client = self.ctx["client"]
        prev = getattr(client, "on_change", None)

        def on_change(what):
            if callable(prev):
                prev(what)
            self.controller.on_client_change(what)
        client.on_change = on_change

    def _loop(self, ms, fn, again):
        if self._closed:
            return
        try:
            fn()
        except Exception as e:
            self._log_error(e)
        try:
            self.root.after(ms, again)
        except Exception:
            pass

    def _loop_slow(self):
        self._loop(SLOW_MS, self.controller.tick_slow, self._loop_slow)

    def _loop_fast(self):
        self._loop(FAST_MS, self.controller.tick_fast, self._loop_fast)

    def stop(self):
        self._closed = True

    def log(self, msg: str):
        """작은 로그 창에 한 줄 (어느 스레드에서든 가능)"""
        self.controller.log(msg)

    # ── 위젯 만들기 ──
    def _card_body(self):
        app, tk, px = self.app, self.tk, self.px
        card = app._card(self.body)
        card.pack(fill="x", pady=(0, px(8)))
        inner = tk.Frame(card, bg=app.PANEL)
        inner.pack(fill="x", padx=px(16), pady=px(10))
        return inner

    def _check(self, parent, text, var, command):
        app, tk = self.app, self.tk
        return tk.Checkbutton(parent, text=text, variable=var, command=command, bg=app.PANEL, fg=app.TEXT,
                              font=app.FONT, activebackground=app.PANEL, selectcolor=app.PANEL, bd=0, anchor="w")

    def _build(self):
        self._build_switch()
        self.body = self.tk.Frame(self.parent, bg=self.app.BG)         # WAN 이 꺼지면 통째로 숨김
        self.body.pack(fill="both", expand=True)
        self._build_list()
        self._build_field()
        self._build_relay()
        self._build_footer()
        self._build_log()

    def _build_switch(self):
        """맨 위 WAN 사용 스위치 — 꺼 두면 아래 칸이 모두 사라지고 WAN 은 아무것도 하지 않는다"""
        app, tk, px = self.app, self.tk, self.px
        self.wan_var = tk.BooleanVar(value=True)
        row = tk.Frame(self.parent, bg=app.BG)
        row.pack(fill="x", pady=(0, px(8)))
        self.wan_check = tk.Checkbutton(row, text="WAN 사용", variable=self.wan_var, command=self._wan_toggled,
                                        bg=app.BG, fg=app.TEXT, font=app.FONT_BOLD, activebackground=app.BG,
                                        selectcolor=app.PANEL, bd=0, anchor="w")
        self.wan_check.pack(side="left")
        app._tip(self.wan_check, "끄면 중계 PC 접속 · 접속 받기 · 목록 확인 · 새 버전 확인 · 접속 정보 가져오기를 모두 하지 않습니다")
        self.wan_off_lbl = tk.Label(row, text="꺼져 있음 — 인터넷으로 아무것도 하지 않습니다", font=app.FONT, fg=app.TEXT_DIM, bg=app.BG)
        if not self.ctx.get("set_wan_enabled"):
            row.pack_forget()                                           # 스위치가 없는 구성은 항상 켜짐

    def _wan_toggled(self):
        self.set_wan_enabled(self.wan_var.get())
        self.controller.wan_toggled(self.wan_var.get())

    def _build_list(self):
        app, tk, ttk, px = self.app, self.tk, self.ttk, self.px
        box = self._card_body()
        head = tk.Frame(box, bg=app.PANEL)
        head.pack(fill="x", pady=(0, px(6)))
        app._section_title(head, "현장 PC").pack(side="left")
        self.server_lbl = tk.Label(head, text="", font=app.FONT, fg=app.TEXT_DIM, bg=app.PANEL, cursor="hand2")
        self.server_lbl.pack(side="right")
        self.server_lbl.bind("<Button-1>", lambda e: self._connect_dialog(self._current_relay()))
        app._tip(self.server_lbl, "중계 PC 연결 상태 — 클릭하면 중계 PC 를 바꿉니다")
        style = ttk.Style()
        style.configure("Wan.Treeview", font=app.FONT_INPUT, rowheight=px(24),
                        background=app.PANEL, fieldbackground=app.PANEL, foreground=app.TEXT,
                        bordercolor=app.BORDER, borderwidth=0)
        style.map("Wan.Treeview", background=[("selected", app.SELECT_BG)],
                  foreground=[("selected", app.TEXT)])
        style.configure("Wan.Treeview.Heading", font=app.FONT_SMALL, background=app.BG,
                        foreground=app.TEXT_DIM, relief="flat", borderwidth=0, padding=(px(6), px(5)))
        wrap = tk.Frame(box, bg=app.PANEL, highlightthickness=1, highlightbackground=app.INPUT_BORDER)
        wrap.pack(fill="x")
        self.tree = ttk.Treeview(wrap, columns=COLUMNS, show="headings", height=4,
                                 selectmode="browse", style="Wan.Treeview")
        for col, w in zip(COLUMNS, COLUMN_WIDTHS):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=px(w), anchor="w", stretch=(col == "이름"))
        self.tree.tag_configure("off", foreground=app.TEXT_DIM)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="x", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._connect())
        self.tree.bind("<Return>", lambda e: self._connect())
        self.tree.bind("<Delete>", lambda e: self._forget(quiet=True))
        self.tree.bind("<Button-3>", self._row_menu)
        row = tk.Frame(box, bg=app.PANEL)
        row.pack(fill="x", pady=(px(8), 0))
        self.connect_btn = app._button(row, "원격 연결", "start", self._connect)
        self.connect_btn.pack(side="left")
        self.add_btn = app._button(row, "+ 연결 추가", "secondary", lambda: self._connect_dialog())
        self.add_btn.pack(side="left", padx=(px(8), 0))
        app._tip(self.add_btn, "중계 PC 접속 정보 · 현장 PC 접속 정보를 붙여넣거나, 현장 PC 주소와 접근 코드를 입력합니다")
        self.config_btn = app._button(row, "설정 보내기", "secondary", self._config_dialog, small=True)
        self.config_btn.pack(side="right")
        self.delete_btn = app._button(row, "삭제", "danger", self._forget, small=True)
        self.delete_btn.pack(side="right", padx=(0, px(6)))
        self.rename_btn = app._button(row, "이름 바꾸기", "secondary", self._rename, small=True)
        self.rename_btn.pack(side="right", padx=(0, px(6)))
        app._tip(self.delete_btn, "선택한 현장을 목록에서 지웁니다 (꺼져 있는 현장 · 주소로 접속했던 현장)")
        app._tip(self.config_btn, "선택한 현장 PC 의 이름·접근 코드·최고 화질·파일 전송을 바꿉니다 (꺼져 있으면 다음 접속 때 적용)")

    def _build_field(self):
        app, tk, px = self.app, self.tk, self.px
        box = self._card_body()
        top = tk.Frame(box, bg=app.PANEL)
        top.pack(fill="x")
        self.enabled_var = tk.BooleanVar(value=bool(self.ctx["cfg"].get("enabled")))
        self._check(top, "이 PC를 원격 대상으로 사용", self.enabled_var, self._field_toggled).pack(side="left")
        self.field_lbl = tk.Label(top, text="", font=app.FONT_BOLD, fg=app.TEXT_DIM, bg=app.PANEL, anchor="e")
        self.field_lbl.pack(side="right")
        self.field_box = tk.Frame(box, bg=app.PANEL)          # 켰을 때만 보임
        row = tk.Frame(self.field_box, bg=app.PANEL)
        row.pack(fill="x", pady=(px(6), 0))
        app._field_label(row, "주소", width=8).pack(side="left")
        self.address_lbl = tk.Label(row, text="", font=app.FONT_BOLD, fg=app.TEXT, bg=app.PANEL, anchor="w")
        self.address_lbl.pack(side="left")
        copy_btn = app._button(row, "복사", "secondary", lambda: self.controller.copy_field_code(), small=True)
        copy_btn.pack(side="right")
        app._tip(copy_btn, "주소와 접근 코드를 한 줄로 복사 — 접속할 PC 의 '연결 추가'에 붙여넣으면 됩니다")
        row = tk.Frame(self.field_box, bg=app.PANEL)
        row.pack(fill="x", pady=(px(4), 0))
        app._field_label(row, "접근 코드", width=8).pack(side="left")
        self.code_var = tk.StringVar(value="")
        self.code_entry = app._entry(row, self.code_var, 24)
        self.code_entry.pack(side="left", fill="x", expand=True, ipady=px(1))
        self.code_entry.bind("<Return>", lambda e: self._apply_code())
        self.code_entry.bind("<FocusOut>", lambda e: self._apply_code())
        app._tip(self.code_entry, "이 PC 에 접속하려면 입력해야 하는 코드. 따로 정하지 않으면 중계 PC 의 공용 키가 쓰입니다")
        self.id_lbl = tk.Label(self.field_box, text="", font=app.FONT_SMALL, fg=app.TEXT_DIM, bg=app.PANEL, anchor="w",
                               cursor="hand2")
        self.id_lbl.pack(fill="x", pady=(px(4), 0))
        self.id_lbl.bind("<Button-1>", lambda e: self._copy_id())
        app._tip(self.id_lbl, "클릭하면 이 PC 의 ID 를 복사합니다")
        self.field_warn_lbl = tk.Label(self.field_box, text="", font=app.FONT_SMALL, fg=app.RED, bg=app.PANEL,
                                       anchor="w")
        self._field_layout()

    def _field_layout(self):
        if self.enabled_var.get():
            self.field_box.pack(fill="x")
        else:
            self.field_box.pack_forget()

    def _build_relay(self):
        """이 PC 가 중계 PC 일 때 — 평소에는 접어 두고, 버튼 한 번으로 안내 서버를 세팅하고 다른 PC 에 줄 접속 정보를 만든다"""
        app, tk, px = self.app, self.tk, self.px
        if not self.ctx.get("relay_available"):
            return
        cfg = self.ctx["cfg"]
        box = self._card_body()
        head = tk.Frame(box, bg=app.PANEL)
        head.pack(fill="x")
        self.relay_open = bool(cfg.get("relay_host"))
        self.relay_toggle_lbl = tk.Label(head, text="", font=app.FONT_BOLD, fg=app.TEXT, bg=app.PANEL, anchor="w",
                                         cursor="hand2")
        self.relay_toggle_lbl.pack(side="left")
        self.relay_toggle_lbl.bind("<Button-1>", lambda e: self._relay_expand(not self.relay_open))
        self.relay_lbl = tk.Label(head, text="", font=app.FONT_BOLD, fg=app.TEXT_DIM, bg=app.PANEL)
        self.relay_lbl.pack(side="right")
        self.relay_more = tk.Frame(box, bg=app.PANEL)
        self.relay_var = tk.BooleanVar(value=bool(cfg.get("relay_host")))
        top = tk.Frame(self.relay_more, bg=app.PANEL)
        top.pack(fill="x", pady=(px(8), 0))
        self.auto_btn = app._button(top, "자동 설정", "primary", lambda: self._relay_auto())
        self.auto_btn.pack(side="left")
        app._tip(self.auto_btn, "공용 키 만들기 · 안내 서버 시작 · 방화벽 열기 · 접속 정보 만들기를 한 번에 합니다")
        self._check(top, "서버 켜기", self.relay_var, self._relay_toggled).pack(side="left", padx=(px(10), 0))
        self.admin_btn = app._button(top, "관리자로 다시 시작", "secondary", lambda: self.controller.relaunch_admin(),
                                     small=True)
        code = tk.Frame(self.relay_more, bg=app.PANEL)
        code.pack(fill="x", pady=(px(6), 0))
        app._field_label(code, "접속 정보", width=8).pack(side="left")
        self.relay_code_var = tk.StringVar(value="")
        app._entry(code, self.relay_code_var, 20).pack(side="left", fill="x", expand=True, ipady=px(1))
        app._button(code, "복사", "secondary", lambda: self.controller.copy_relay_code(), small=True
                    ).pack(side="left", padx=(px(6), 0))
        save = app._button(code, "파일 저장", "secondary", lambda: self.controller.save_connection_dialog(), small=True)
        save.pack(side="left", padx=(px(4), 0))
        refresh = app._button(code, "새로고침", "danger", lambda: self.controller.relay_refresh_code(), small=True)
        refresh.pack(side="left", padx=(px(4), 0))
        app._tip(refresh, "접속 코드(공용 키)를 새로 만들어 Git 에 올립니다. 기존 접속 정보는 사라지고 연결 중인 원격 화면은 끊깁니다")
        app._tip(save, "접속 정보를 aurora_wan.conn 파일로 저장 — AuroraClient.exe 와 같은 폴더에 두면 다른 PC 가 실행할 때 자동으로 연결됩니다")
        git = tk.Frame(self.relay_more, bg=app.PANEL)
        git.pack(fill="x", pady=(px(4), 0))
        app._field_label(git, "Git 저장소", width=8).pack(side="left")
        self.git_var = tk.StringVar(value=cfg.get("git_source") or "")
        self.git_entry = app._entry(git, self.git_var, 20)
        self.git_entry.pack(side="left", fill="x", expand=True, ipady=px(1))
        self.git_entry.bind("<Return>", lambda e: self.controller.publish_git(self.git_var.get()))
        up = app._button(git, "올리기", "secondary", lambda: self.controller.publish_git(self.git_var.get()), small=True)
        up.pack(side="left", padx=(px(6), 0))
        app._tip(up, "사내 Git 저장소에 접속 정보를 올립니다. 다른 PC 는 이 저장소 주소를 '+ 연결 추가'에 한 번 넣으면 자동으로 가져옵니다 (접근이 제한된 사내 저장소만 쓰세요)")
        self.git_status_lbl = tk.Label(self.relay_more, text="", font=app.FONT_SMALL, fg=app.TEXT_DIM, bg=app.PANEL,
                                       anchor="w")
        self.git_status_lbl.pack(fill="x")
        opt = tk.Frame(self.relay_more, bg=app.PANEL)
        opt.pack(fill="x", pady=(px(4), 0))
        app._field_label(opt, "외부 주소", width=8).pack(side="left")
        self.relay_ext_var = tk.StringVar(value=cfg.get("relay_external") or "")
        ent = app._entry(opt, self.relay_ext_var, 16)
        ent.pack(side="left", fill="x", expand=True, ipady=px(1))
        ent.bind("<FocusOut>", lambda e: self.controller.set_relay_external(self.relay_ext_var.get()))
        ent.bind("<Return>", lambda e: self.controller.set_relay_external(self.relay_ext_var.get()))
        app._tip(ent, "다른 망(출장지)에서 이 PC 에 닿는 공인 IP 또는 도메인 (선택). 공유기에서 포트포워딩이 필요합니다")
        app._field_label(opt, "포트", width=5).pack(side="left", padx=(px(10), 0))
        self.relay_port_var = tk.StringVar(value=str(cfg.get("relay_port") or 9100))
        app._entry(opt, self.relay_port_var, 6).pack(side="left", ipady=px(1))
        app._button(opt, "방화벽 열기", "secondary", lambda: self.controller.open_firewall(), small=True
                    ).pack(side="left", padx=(px(8), 0))
        self.relay_warn_lbl = tk.Label(self.relay_more, text="", font=app.FONT_SMALL, fg=app.RED, bg=app.PANEL,
                                       anchor="w")
        self._relay_expand(self.relay_open)

    def _relay_expand(self, opened):
        self.relay_open = bool(opened)
        self.relay_toggle_lbl.configure(text=("▾ " if self.relay_open else "▸ ") + "중계 PC 로 사용")
        if self.relay_open:
            self.relay_more.pack(fill="x")
        else:
            self.relay_more.pack_forget()

    def _build_footer(self):
        app, tk, px = self.app, self.tk, self.px
        row = tk.Frame(self.body, bg=app.BG)
        row.pack(fill="x", pady=(0, px(6)))
        self.day_lbl = tk.Label(row, text="오늘 -", font=app.FONT_SMALL, fg=app.TEXT_DIM, bg=app.BG)
        self.day_lbl.pack(side="left")
        self.month_lbl = tk.Label(row, text="이번 달 -", font=app.FONT_SMALL, fg=app.TEXT_DIM, bg=app.BG)
        self.month_lbl.pack(side="left", padx=(px(14), 0))
        app._tip(self.day_lbl, USAGE_HINT)
        app._tip(self.month_lbl, USAGE_HINT)

    def _build_log(self):
        app, tk, px = self.app, self.tk, self.px
        card = app._card(self.body)
        card.pack(fill="both", expand=True)
        self.log_text = tk.Text(card, height=LOG_LINES, font=app.FONT_MONO_SMALL, bg=app.LOG_BG, fg="#D5DADD",
                                relief="flat", bd=px(6), wrap="none", state="disabled", highlightthickness=0)
        self.log_text.pack(fill="both", expand=True)

    # ── 버튼 처리 ──
    def _field_toggled(self):
        self._field_layout()
        self.controller.field_toggled(self.enabled_var.get())

    def _apply_code(self):
        self.controller.set_access_code(self.code_var.get())

    def _relay_auto(self):
        self._relay_expand(True)
        self.controller.relay_auto_setup(self.relay_ext_var.get())

    def _relay_toggled(self):
        self.controller.relay_toggled(self.relay_var.get(), self.relay_port_var.get())

    def _copy_id(self):
        self.copy_text(self.ctx["cfg"].ensure_id(), "이 PC 의 ID 를 복사했습니다")

    def _current_relay(self) -> str:
        return (self.ctx["cfg"].get("relay_url") or "").strip()

    def _selected(self, quiet=False):
        sel = self.tree.selection()
        if not sel:
            if not quiet:
                self.mb.showinfo("현장 PC 선택", "목록에서 현장 PC 를 먼저 선택하세요", parent=self.root)
            return None
        return sel[0]

    def _connect(self):
        row_id = self._selected()
        if row_id:
            self.controller.quick_connect(row_id)

    def _rename(self):
        row_id = self._selected()
        if not row_id:
            return
        agent = self.controller.agent(row_id) or {}
        current = (agent.get("name") or "").strip()
        new = self.sd.askstring("이름 바꾸기", "새 이름을 입력하세요", parent=self.root,
                                initialvalue="" if current == (agent.get("address") or "") else current)
        if new is not None:
            self.controller.rename_target(row_id, new)

    def _forget(self, quiet=False):
        row_id = self._selected(quiet=quiet)
        if row_id:
            self.controller.forget_target(row_id)

    def _row_menu(self, event):
        """목록에서 오른쪽 클릭 — 연결 · 설정 보내기 · 목록에서 지우기"""
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        menu = self.tk.Menu(self.tree, tearoff=0)
        menu.add_command(label="원격 연결", command=self._connect)
        if not is_direct_id(row):
            menu.add_command(label="설정 보내기", command=self._config_dialog)
        menu.add_command(label="이름 바꾸기", command=self._rename)
        menu.add_command(label="목록에서 삭제", command=self._forget)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _connect_dialog(self, prefill=""):
        """연결 추가: 중계 PC 접속 정보 · 현장 PC 접속 정보 · 주소(+접근 코드) 어느 것이든 한 칸에 넣는다.
        같은 망이면 중계 PC 를 자동으로 찾아 채운다"""
        app, tk, px = self.app, self.tk, self.px
        cfg = self.ctx["cfg"]
        win = tk.Toplevel(self.root)
        win.title("연결 추가")
        win.configure(bg=app.PANEL)
        win.transient(self.root)
        win.resizable(False, False)
        pad = tk.Frame(win, bg=app.PANEL)
        pad.pack(fill="both", expand=True, padx=px(16), pady=px(12))
        tk.Label(pad, text="접속 정보 또는 현장 PC 주소", font=app.FONT_BOLD, fg=app.TEXT, bg=app.PANEL,
                 anchor="w").pack(fill="x")
        text_var, code_var, field_var = tk.StringVar(), tk.StringVar(), tk.BooleanVar(value=bool(cfg.get("enabled")))
        top = tk.Frame(pad, bg=app.PANEL)
        top.pack(fill="x", pady=(px(6), px(2)))
        entry = app._entry(top, text_var, 44)
        entry.pack(side="left", fill="x", expand=True, ipady=px(2))
        hint = tk.Label(pad, text="", font=app.FONT_SMALL, fg=app.TEXT_DIM, bg=app.PANEL, anchor="w")
        hint.pack(fill="x", pady=(0, px(4)))
        code_row = tk.Frame(pad, bg=app.PANEL)                   # 주소 · ws:// 주소를 넣었을 때만
        code_lbl = app._field_label(code_row, "접근 코드", width=8)
        code_lbl.pack(side="left")
        app._entry(code_row, code_var, 24).pack(side="left", fill="x", expand=True, ipady=px(1))
        field_chk = self._check(pad, "이 PC도 원격 대상으로 사용", field_var, lambda: None)     # 중계 PC 정보를 넣었을 때만
        btns = tk.Frame(pad, bg=app.PANEL)
        btns.pack(fill="x", pady=(px(10), 0))

        def refresh(*_):
            kind = classify_target(text_var.get())
            needs_code = kind in ("address", "relay_url")
            if needs_code:
                code_lbl.configure(text="공용 키" if kind == "relay_url" else "접근 코드")
                if not code_var.get():
                    code_var.set((cfg.get("relay_key") or "").strip())
                code_row.pack(fill="x", pady=(px(2), 0), before=btns)
            else:
                code_row.pack_forget()
            if kind in ("relay_code", "relay_url"):
                field_chk.pack(fill="x", pady=(px(4), 0), before=btns)
            else:
                field_chk.pack_forget()
            if kind == "relay_code":
                hint.configure(text="중계 PC 에 연결합니다")
            elif kind == "git":
                hint.configure(text="사내 Git 저장소에서 접속 정보를 가져옵니다 (이후 자동으로 최신 정보를 따라감)")
            elif kind == "direct_code":
                hint.configure(text="현장 PC 에 바로 접속합니다")
            elif kind == "address":
                hint.configure(text="현장 PC 에 바로 접속합니다 (같은 망 · VPN · 포트포워딩된 주소)")

        def pick_file():
            fd = self.fd
            path = fd.askopenfilename(parent=win, initialdir=self.controller.default_conn_dir(),
                                      filetypes=[("Aurora 접속 설정", "*.conn"), ("모든 파일", "*.*")])
            if path:
                text = self.controller.read_connection_file(path)
                if text:
                    text_var.set(text.strip().splitlines()[-1])

        def ok():
            if self.controller.add_connection(text_var.get(), code_var.get(), field_var.get()):
                win.destroy()

        def find():
            hint.configure(text="같은 망에서 중계 PC 를 찾는 중…")
            self.controller.find_on_lan()
        app._button(top, "파일…", "secondary", pick_file, small=True).pack(side="left", padx=(px(6), 0))
        app._button(btns, "연결", "primary", ok).pack(side="left")
        app._button(btns, "취소", "secondary", win.destroy).pack(side="left", padx=(px(8), 0))
        app._button(btns, "같은 망에서 찾기", "secondary", find, small=True).pack(side="right")
        if (cfg.get("git_source") or "").strip():
            app._button(btns, "Git 연동 끄기", "secondary",
                        lambda: (self.controller.set_git_source(""), win.destroy()), small=True
                        ).pack(side="right", padx=(0, px(6)))
        entry.bind("<Return>", lambda e: ok())
        text_var.trace_add("write", refresh)
        self._connect_win = ConnectDialog(win, text_var, code_var, field_var, ok)
        self._connect_win.hint, self._connect_win.btns, self._connect_win.refresh = hint, btns, refresh
        self._connect_win.code_row, self._connect_win.field_chk = code_row, field_chk
        if prefill:
            text_var.set(prefill)
        else:
            try:
                clip = self.root.clipboard_get()
                if connect_code.kind_of(clip):
                    text_var.set(clip.strip())
                    hint.configure(text="복사해 둔 접속 정보를 넣었습니다")
            except Exception:
                pass
        refresh()
        if not text_var.get() and not self._current_relay():
            find()                                                # 아직 중계 PC 가 없으면 같은 망에서 자동으로 찾는다
        try:
            entry.focus_set()
        except Exception:
            pass

    def _config_dialog(self):
        row_id = self._selected()
        if not row_id:
            return
        if is_direct_id(row_id):
            self.controller.send_config(row_id)                  # 주소로 접속한 현장은 보낼 수 없다는 안내가 뜬다
            return
        app, tk, ttk, px = self.app, self.tk, self.ttk, self.px
        agent = self.controller.agent(row_id) or {}
        label = (agent.get("name") or "").strip() or format_id(row_id)
        dlg = tk.Toplevel(self.root)
        dlg.title("설정 보내기")
        dlg.configure(bg=app.PANEL)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        body = tk.Frame(dlg, bg=app.PANEL)
        body.pack(fill="both", padx=px(20), pady=px(16))
        tk.Label(body, text=f"'{label}' — 바꿀 항목만 채우세요 (비워 두면 그대로)", font=app.FONT_BOLD,
                 fg=app.TEXT, bg=app.PANEL).pack(anchor="w", pady=(0, px(8)))
        name_var, code_var = tk.StringVar(), tk.StringVar()
        q_var, f_var = tk.StringVar(value=KEEP), tk.StringVar(value=KEEP)
        for text, var, show in (("이름", name_var, None), ("접근 코드", code_var, "*")):
            row = tk.Frame(body, bg=app.PANEL)
            row.pack(fill="x", pady=(0, px(4)))
            app._field_label(row, text, width=10).pack(side="left")
            ent = app._entry(row, var, 28)
            if show:
                ent.configure(show=show)
            ent.pack(side="left", fill="x", expand=True)
        for text, var, values in (("최고 화질", q_var, [KEEP] + list(QUALITY)),
                                  ("파일 전송", f_var, list(FILE_CHOICES))):
            row = tk.Frame(body, bg=app.PANEL)
            row.pack(fill="x", pady=(0, px(4)))
            app._field_label(row, text, width=10).pack(side="left")
            ttk.Combobox(row, textvariable=var, values=values, state="readonly", width=14,
                         font=app.FONT_INPUT).pack(side="left")

        def send():
            quality = q_var.get()
            ok = self.controller.send_config(
                row_id, name_var.get(), code_var.get(), "" if quality == KEEP else quality,
                FILE_CHOICES.get(f_var.get()))
            if ok:
                dlg.destroy()

        btns = tk.Frame(body, bg=app.PANEL)
        btns.pack(fill="x", pady=(px(10), 0))
        app._button(btns, "보내기", "primary", send).pack(side="left")
        app._button(btns, "취소", "secondary", dlg.destroy).pack(side="left", padx=(px(8), 0))
        try:
            dlg.grab_set()
        except Exception:
            pass

    # ── 컨트롤러 → 화면 (화면 스레드에서 불림) ──
    def _color(self, level):
        app = self.app
        return {"ok": app.GREEN, "warn": app.YELLOW, "bad": app.RED}.get(level, app.TEXT_DIM)

    def set_server_status(self, text, level):
        self.server_lbl.configure(text=text, fg=self._color(level))

    def set_rows(self, rows):
        tree = self.tree
        ids = [rid for rid, _v in rows]
        selected = tree.selection()
        tags = lambda values: (() if str(values[2]).startswith("켜짐") else ("off",))
        if list(tree.get_children()) == ids:                # 같은 목록이면 값만 고쳐 선택·스크롤 유지
            for rid, values in rows:
                tree.item(rid, values=values, tags=tags(values))
            return
        for iid in tree.get_children():
            tree.delete(iid)
        for rid, values in rows:
            tree.insert("", "end", iid=rid, values=values, tags=tags(values))
        keep = [s for s in selected if s in ids]
        if keep:
            tree.selection_set(keep)

    def set_field_info(self, id_text, name_text, warn):
        self.id_lbl.configure(text=f"{name_text}  ·  ID {id_text}")
        self._show_warn(self.field_warn_lbl, warn)

    def _show_warn(self, label, text):
        """경고 글자가 있을 때만 줄을 차지하게"""
        label.configure(text=text)
        if text:
            label.pack(fill="x")
        else:
            label.pack_forget()

    def set_field_status(self, text, level):
        self.field_lbl.configure(text=f"● {text}", fg=self._color(level))

    def set_field_enabled(self, enabled):
        self.enabled_var.set(bool(enabled))
        self._field_layout()

    def set_field_address(self, text):
        self.address_lbl.configure(text=text)

    def set_field_code(self, text):
        try:
            if self.root.focus_get() is self.code_entry:        # 입력 중인 칸을 덮어쓰지 않는다
                return
        except Exception:
            pass
        if self.code_var.get() != text:
            self.code_var.set(text)

    def set_relay_status(self, text, level):
        if hasattr(self, "relay_lbl"):
            self.relay_lbl.configure(text=f"● {text}", fg=self._color(level))

    def set_relay_info(self, port_text, hint, warn):
        if hasattr(self, "relay_lbl"):
            self._show_warn(self.relay_warn_lbl, warn)

    def set_relay_code(self, code):
        if hasattr(self, "relay_code_var") and self.relay_code_var.get() != code:
            self.relay_code_var.set(code)

    def set_relay_git(self, source, status, level):
        if not hasattr(self, "git_var"):
            return
        try:
            typing = self.root.focus_get() is self.git_entry                 # 입력 중인 칸을 덮어쓰지 않는다
        except Exception:
            typing = False
        if not typing and self.git_var.get() != source:
            self.git_var.set(source)
        self.git_status_lbl.configure(text=status, fg=self._color(level))

    def set_relay_enabled(self, enabled):
        if hasattr(self, "relay_var"):
            self.relay_var.set(bool(enabled))
            if enabled:
                self._relay_expand(True)

    def set_admin(self, is_admin):
        if hasattr(self, "admin_btn"):
            if is_admin:
                self.admin_btn.master.pack_forget()      # _button 은 테두리 프레임에 담겨 있어 프레임을 숨겨야 함
            else:
                self.admin_btn.pack(side="left", padx=(self.px(10), 0))

    def set_found_code(self, code):
        d = getattr(self, "_connect_win", None)
        try:
            if d is not None and d.win.winfo_exists():
                if code and not d.text.get():
                    d.text.set(code)
                d.hint.configure(text="같은 망에서 중계 PC 를 찾았습니다" if code else "같은 망에서 중계 PC 를 찾지 못했습니다")
        except self.tk.TclError:
            pass

    def ask_yes_no(self, title, message, on_answer):
        on_answer(bool(self.mb.askyesno(title, message, parent=self.root)))

    def ask_code(self, row_id, label, why, on_answer):
        msg = f"'{label}' 의 접근 코드를 입력하세요" + (f"\n({why})" if why else "")
        on_answer(self.sd.askstring("접근 코드", msg, show="*", parent=self.root,
                                    initialvalue=self.controller.remembered_code(row_id)))

    def ask_save_path(self, directory, filename, on_answer):
        path = self.fd.asksaveasfilename(parent=self.root, initialdir=directory, initialfile=filename,
                                         defaultextension=".conn",
                                         filetypes=[("Aurora 접속 설정", "*.conn"), ("모든 파일", "*.*")])
        on_answer(path or None)

    def copy_text(self, text, done_msg):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.controller.log(done_msg)
        except Exception:
            pass

    def set_wan_enabled(self, enabled):
        self.wan_var.set(bool(enabled))
        if enabled:
            self.wan_off_lbl.pack_forget()
            self.body.pack(fill="both", expand=True)
        else:
            self.body.pack_forget()
            self.wan_off_lbl.pack(side="left", padx=(self.px(10), 0))

    def set_usage(self, day_text, month_text):
        self.day_lbl.configure(text=day_text)
        self.month_lbl.configure(text=month_text)

    def set_log(self, lines):
        t = self.log_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", "\n".join(lines))
        t.see("end")
        t.configure(state="disabled")

    def set_busy(self, what, busy):
        btn = {"server": self.add_btn, "connect": self.connect_btn, "config": self.config_btn}.get(what)
        if btn:
            btn.configure(state="disabled" if busy else "normal")

    def show_error(self, title, message):
        self.mb.showerror(title, message, parent=self.root)

    def show_info(self, title, message):
        self.mb.showinfo(title, message, parent=self.root)


class ConnectDialog:
    """연결 추가 창의 입력값 묶음 (시험이 창을 조작할 수 있게 보관)"""
    def __init__(self, win, text, code, field, ok):
        self.win, self.text, self.code, self.field, self.ok = win, text, code, field, ok


def build_tab(app, parent, ctx) -> WanTab:
    """aurora_client 의 탭 만들기에서 부름.  tkinter 는 여기서 불러온다 (이 파일 import 만으로는 필요 없음)"""
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog, filedialog
    return WanTab(app, parent, ctx, tk, ttk, messagebox, simpledialog, filedialog)
