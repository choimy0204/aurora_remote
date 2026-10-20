"""
"WAN 원격" 탭
=============
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
    open_remote       (session, agent_dict) -> None   원격 창 열기 (화면 스레드에서 불림)
    px                (int) -> int               (선택, 기본: 그대로)
    log               (str) -> None              (선택) 앱 로그
스레드 규칙: 오래 걸리는 일(연결·설정 보내기·재시작)은 작업 스레드에서, 화면 갱신은 schedule() 로 화면 스레드에서
"""

import math
import threading
import time
from collections import deque
from datetime import datetime

from .config import QUALITY, RELAY_EXIT_PORT_BUSY, format_id
from .netutil import lan_addresses, open_firewall
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

COLUMNS = ("이름", "ID", "상태", "마지막 접속", "버전", "설정")
COLUMN_WIDTHS = (120, 130, 50, 90, 55, 70)


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
        return u, "주소는 ws:// 또는 wss:// 로 시작해야 합니다 (예: ws://203.0.113.5:8765)"
    host = u.split("://", 1)[1]
    if not host or any(c.isspace() for c in u):
        return u, "주소 형식이 올바르지 않습니다 (예: ws://203.0.113.5:8765)"
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
    """접속 코드 검사 → (정리된 코드, 오류 글자)"""
    c = (code or "").strip()
    if not c:
        return "", "접속 코드를 입력하세요"
    if len(c) > CODE_MAX:
        return c, f"접속 코드는 {CODE_MAX}자 이하여야 합니다"
    return c, ""


def level_for_state(state: str) -> str:
    """안내 서버 연결 상태 → ok / warn / bad (색 결정)"""
    return {"연결됨": "ok", "연결 중": "warn"}.get(state, "bad")


# ─────────────────────────────────────────
# 화면 갱신 함수 모음 (컨트롤러 → 화면).  WanTab 과 시험용 가짜가 이 모양을 따른다
# ─────────────────────────────────────────
class NullUI:
    def set_server_status(self, text, level): pass            # level: ok / warn / bad
    def set_rows(self, rows): pass                            # [(agent_id, (이름, ID, 상태, ...))]
    def set_field_info(self, id_text, name_text, code_text): pass
    def set_field_status(self, text, level): pass
    def set_field_enabled(self, enabled): pass
    def set_relay_status(self, text, level): pass             # 이 PC 에서 안내 서버 운영 상태
    def set_relay_info(self, port_text, hint, warn): pass
    def set_relay_enabled(self, enabled): pass
    def set_relay_status(self, text, level):
        if hasattr(self, "relay_lbl"):
            self.relay_lbl.configure(text=f"● {text}", fg=self._color(level))

    def set_relay_info(self, port_text, hint, warn):
        if hasattr(self, "relay_lbl"):
            self.relay_hint_lbl.configure(text=hint)
            self.relay_warn_lbl.configure(text=warn)
            self._relay_layout()

    def set_relay_enabled(self, enabled):
        if hasattr(self, "relay_var"):
            self.relay_var.set(bool(enabled))
            self._relay_layout()

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
        self._codes: dict = {}              # agent_id → 마지막으로 쓴 접속 코드 (이번 실행 동안 메모리에만)
        self._connecting: set = set()
        self._restart_lock = threading.Lock()
        self._lines = deque(maxlen=LOG_LINES)
        self._lines_lock = threading.Lock()

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

    # ── 1. 안내 서버 ──
    def server_status(self) -> tuple:
        state = self.client.state
        return f"● {state}", level_for_state(state)

    def refresh_server(self):
        self._ui("set_server_status", *self.server_status())

    def apply_server(self, url, key) -> bool:
        """주소·키를 저장하고 안내 서버에 다시 연결 (작업 스레드).  입력이 잘못되면 False"""
        url, err = validate_url(url)
        if err:
            self._ui("show_error", "안내 서버", err)
            return False
        key = (key or "").strip()
        self.cfg.update(relay_url=url, relay_key=key)
        self._note(f"안내 서버 적용: {url}")
        self._ui("set_busy", "server", True)
        self._spawn(self._restart_client)
        if self.cfg.get("relay_host"):                  # 공용 키가 바뀌었을 수 있으니 이 PC 의 안내 서버도 다시 시작
            self._spawn(self._restart_relay)
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
        warn = "" if (self.cfg.get("relay_key") or "").strip() else "공용 키가 비어 있어 누구나 접속을 시도할 수 있습니다 (위 '공용 키' 칸에 입력)"
        return str(port), hint, warn

    def refresh_relay(self):
        if self.relay_available():
            self._ui("set_relay_status", *self.relay_status())
            self._ui("set_relay_info", *self.relay_info())

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
            self.log("공용 키가 없어 누구나 접속을 시도할 수 있습니다 — 위 '공용 키' 칸에 입력 후 '적용 후 연결'을 누르세요")

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

    # ── 2. 이 PC (현장) ──
    def field_info(self) -> tuple:
        """→ (ID 글자, 이름 글자, 접속 코드 상태 글자)"""
        agent_id = self.cfg.ensure_id()
        name = self.cfg.get("name") or ""
        code_set = bool(self.cfg.get("access_code"))
        return (format_id(agent_id), name.strip() or "(이름 없음)",
                "접속 코드 설정됨" if code_set else "접속 코드 미설정 — 미설정이면 모두 거부")

    def field_status(self) -> tuple:
        """에이전트 프로세스 상태 → (글자, ok / warn / off)"""
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
            return f"실행 중{f' (PID {pid})' if pid else ''}{tail}", "ok"
        return f"꺼짐{tail}", "off"

    def field_status_text(self) -> str:
        return self.field_status()[0]

    def refresh_field(self, reload=False):
        if reload:        # 에이전트(다른 프로세스)가 안내 서버에서 받은 설정을 파일에 적었을 수 있음
            try:
                self.cfg.load()
            except Exception:
                pass
        self._ui("set_field_info", *self.field_info())
        self._ui("set_field_status", *self.field_status())

    def field_toggled(self, enabled: bool):
        """'이 PC를 WAN 원격 대상으로 사용' 체크가 바뀜 → 설정 저장 + 에이전트 시작/중지 (작업 스레드)"""
        enabled = bool(enabled)
        self.cfg.update(enabled=enabled)
        self.cfg.ensure_id()
        self._note("이 PC 를 WAN 원격 대상으로 사용합니다" if enabled else "이 PC 의 WAN 원격 대상을 끕니다")
        if enabled and not self.cfg.get("access_code"):
            self.log("접속 코드가 없어 모든 접속이 거부됩니다 — 목록에서 이 PC 를 골라 '설정 보내기'로 지정하세요")

        def work():
            try:
                self.ctx["set_field_mode"](enabled)
            except Exception as e:
                self.cfg.update(enabled=not enabled)
                self._note(f"현장 모드 변경 실패: {type(e).__name__}: {e}")
                self._ui("set_field_enabled", not enabled)
                self._ui("show_error", "이 PC (현장)", f"변경하지 못했습니다.\n{e}")
            self.refresh_field()
        self._spawn(work)

    # ── 3. 현장 PC 목록 ──
    def rows_with_ids(self) -> list:
        """client.agents → [(agent_id, 표시용 글자 튜플)].  켜진 PC 먼저, 그 안에서는 이름순"""
        now = self._clock()
        out = []
        for a in list(self.client.agents or []):
            aid = str(a.get("id", ""))
            online = bool(a.get("online"))
            name = (a.get("name") or "").strip() or "(이름 없음)"
            applied = int(a.get("applied_rev") or 0) >= int(a.get("rev") or 0)
            values = (name, format_id(aid), "켜짐" if online else "꺼짐",
                      "지금" if online else relative_time(a.get("last_seen"), now),
                      str(a.get("ver") or "-"), "적용됨" if applied else "대기 중")
            out.append((not online, name.casefold(), aid, values))
        out.sort(key=lambda r: r[:3])
        return [(aid, values) for _o, _n, aid, values in out]

    def rows(self) -> list:
        return [values for _aid, values in self.rows_with_ids()]

    def agent(self, agent_id: str):
        for a in list(self.client.agents or []):
            if a.get("id") == agent_id:
                return dict(a)
        return None

    def refresh_agents(self):
        self._ui("set_rows", self.rows_with_ids())

    def on_client_change(self, what):
        """WanClient.on_change — 클라이언트 스레드에서 불림.  화면 갱신은 화면 스레드로"""
        if what == "agents":
            self.refresh_agents()
        elif what == "state":
            self.refresh_server()
            self.refresh_agents()

    def remembered_code(self, agent_id: str) -> str:
        return self._codes.get(agent_id, "")

    def connect(self, agent_id: str, code) -> bool:
        """원격 연결 (작업 스레드).  성공하면 ctx["open_remote"](session, agent) 를 화면 스레드에서 부름"""
        agent = self.agent(agent_id)
        if agent is None:
            self._ui("show_error", "원격 연결", "목록에서 현장 PC 를 먼저 선택하세요")
            return False
        code, err = validate_code(code)
        if err:
            self._ui("show_error", "원격 연결", err)
            return False
        if agent_id in self._connecting:
            self.log("이미 연결하는 중입니다")
            return False
        self._connecting.add(agent_id)
        self._codes[agent_id] = code
        name = (agent.get("name") or "").strip() or format_id(agent_id)
        self._ui("set_busy", "connect", True)
        self.log(f"{name} 에 연결하는 중…")

        def work():
            try:
                session = self.client.open_session(agent_id, code, name)
            except WanDenied as e:
                self._codes.pop(agent_id, None)      # 틀린 코드는 다음에 미리 채우지 않음
                self._fail(name, e)
            except WanError as e:
                self._fail(name, e)
            except Exception as e:
                self._fail(name, f"{type(e).__name__}: {e}")
            else:
                self._note(f"{name} 연결됨 ({getattr(session, 'mode', '')})")
                self.schedule(lambda: self.ctx["open_remote"](session, agent))
            finally:
                self._connecting.discard(agent_id)
                self._ui("set_busy", "connect", bool(self._connecting))
        self._spawn(work)
        return True

    def _fail(self, name, err):
        text = str(err) or "연결하지 못했습니다"
        self._note(f"{name} 연결 실패: {text}")
        self._ui("show_error", "원격 연결 실패", text)

    def send_config(self, agent_id: str, name=None, code=None, max_quality=None, allow_files=None) -> bool:
        """현장 설정 보내기 (작업 스레드).  비어 있는 항목(None / '')은 보내지 않는다"""
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
        self.refresh_server()
        self.refresh_relay()
        self.refresh_agents()
        self.refresh_field()
        self.refresh_usage()

    def tick_slow(self):
        self.refresh_server()
        self.refresh_agents()

    def tick_fast(self):
        self.refresh_relay()
        self.refresh_usage()
        self.refresh_field(reload=True)


# ─────────────────────────────────────────
# 화면 (얇은 위젯 층)
# ─────────────────────────────────────────
class WanTab(NullUI):
    def __init__(self, app, parent, ctx, tk, ttk, messagebox, simpledialog):
        self.app, self.parent, self.ctx = app, parent, ctx
        self.tk, self.ttk, self.mb, self.sd = tk, ttk, messagebox, simpledialog
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
    def _section(self, title):
        app, tk, px = self.app, self.tk, self.px
        card = app._card(self.parent)
        card.pack(fill="x", pady=(0, px(8)))
        inner = tk.Frame(card, bg=app.PANEL)
        inner.pack(fill="x", padx=px(16), pady=px(10))
        app._section_title(inner, title).pack(anchor="w", pady=(0, px(6)))
        return inner

    def _build(self):
        self._build_server()
        self._build_relay()
        self._build_field()
        self._build_list()
        self._build_usage()
        self._build_log()

    def _build_server(self):
        app, tk, px = self.app, self.tk, self.px
        cfg = self.ctx["cfg"]
        box = self._section("안내 서버")
        self.url_var = tk.StringVar(value=cfg.get("relay_url") or "")
        self.key_var = tk.StringVar(value=cfg.get("relay_key") or "")
        for text, var, show in (("주소", self.url_var, None), ("공용 키", self.key_var, "*")):
            row = tk.Frame(box, bg=app.PANEL)
            row.pack(fill="x", pady=(0, px(4)))
            app._field_label(row, text, width=8).pack(side="left")
            ent = app._entry(row, var, 38)
            if show:
                ent.configure(show=show)
            ent.pack(side="left", fill="x", expand=True, ipady=px(1))
            ent.bind("<Return>", lambda e: self._apply_server())
            app._tip(ent, "ws://주소:포트 (예: ws://203.0.113.5:8765)" if not show
                     else "안내 서버에 키를 걸었을 때만 입력합니다")
        row = tk.Frame(box, bg=app.PANEL)
        row.pack(fill="x", pady=(px(4), 0))
        self.server_btn = app._button(row, "적용 후 연결", "primary", self._apply_server)
        self.server_btn.pack(side="left")
        self.server_lbl = tk.Label(row, text="● 끊김", font=app.FONT_BOLD, fg=app.RED, bg=app.PANEL)
        self.server_lbl.pack(side="left", padx=(px(12), 0))

    def _build_relay(self):
        """이 PC 가 중계 PC 일 때 — 안내 서버를 앱 안에서 켜고 끈다 (공용 키는 위 '안내 서버' 칸의 값을 씀)"""
        app, tk, px = self.app, self.tk, self.px
        if not self.ctx.get("relay_available"):
            return
        box = self._section("안내 서버 운영 (이 PC 가 중계 PC 일 때)")
        self.relay_var = tk.BooleanVar(value=bool(self.ctx["cfg"].get("relay_host")))
        row = tk.Frame(box, bg=app.PANEL)
        row.pack(fill="x")
        tk.Checkbutton(row, text="이 PC 에서 안내 서버 운영", variable=self.relay_var, command=self._relay_toggled,
                       bg=app.PANEL, fg=app.TEXT, font=app.FONT, activebackground=app.PANEL,
                       selectcolor=app.PANEL, bd=0, anchor="w").pack(side="left")
        app._field_label(row, "포트", width=5).pack(side="left", padx=(px(16), 0))
        self.relay_port_var = tk.StringVar(value=str(self.ctx["cfg"].get("relay_port") or 9100))
        app._entry(row, self.relay_port_var, 6).pack(side="left", ipady=px(1))
        app._button(row, "방화벽 열기", "secondary", lambda: self.controller.open_firewall(), small=True
                    ).pack(side="left", padx=(px(12), 0))
        self.relay_lbl = tk.Label(row, text="● 꺼짐", font=app.FONT_BOLD, fg=app.TEXT_DIM, bg=app.PANEL)
        self.relay_lbl.pack(side="right")
        self.relay_more = tk.Frame(box, bg=app.PANEL)            # 켰을 때만 보임 — 창이 좁아서 평소엔 접어 둔다
        self.relay_hint_lbl = tk.Label(self.relay_more, text="", font=app.FONT_SMALL, fg=app.TEXT_DIM,
                                       bg=app.PANEL, anchor="w", justify="left")
        self.relay_hint_lbl.pack(fill="x", pady=(px(4), 0))
        self.relay_warn_lbl = tk.Label(self.relay_more, text="", font=app.FONT_SMALL, fg=app.RED,
                                       bg=app.PANEL, anchor="w")
        self.relay_warn_lbl.pack(fill="x")
        self._relay_layout()

    def _relay_layout(self):
        if self.relay_var.get():
            self.relay_more.pack(fill="x")
        else:
            self.relay_more.pack_forget()

    def _build_field(self):
        app, tk, px = self.app, self.tk, self.px
        box = self._section("이 PC (현장)")
        self.enabled_var = tk.BooleanVar(value=bool(self.ctx["cfg"].get("enabled")))
        tk.Checkbutton(box, text="이 PC를 WAN 원격 대상으로 사용", variable=self.enabled_var,
                       command=self._field_toggled, bg=app.PANEL, fg=app.TEXT, font=app.FONT,
                       activebackground=app.PANEL, selectcolor=app.PANEL, bd=0, anchor="w"
                       ).pack(anchor="w")
        row = tk.Frame(box, bg=app.PANEL)
        row.pack(fill="x", pady=(px(4), 0))
        self.id_lbl = tk.Label(row, text="", font=app.FONT_BOLD, fg=app.TEXT, bg=app.PANEL, anchor="w")
        self.id_lbl.pack(side="left")
        app._button(row, "ID 복사", "secondary", self._copy_id, small=True).pack(side="left", padx=(px(8), 0))
        self.name_lbl = tk.Label(box, text="", font=app.FONT, fg=app.TEXT_DIM, bg=app.PANEL, anchor="w")
        self.name_lbl.pack(fill="x", pady=(px(2), 0))
        self.field_lbl = tk.Label(box, text="", font=app.FONT_BOLD, fg=app.TEXT_DIM, bg=app.PANEL, anchor="w")
        self.field_lbl.pack(fill="x", pady=(px(2), 0))

    def _build_list(self):
        app, tk, ttk, px = self.app, self.tk, self.ttk, self.px
        box = self._section("현장 PC 목록")
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
        row = tk.Frame(box, bg=app.PANEL)
        row.pack(fill="x", pady=(px(8), 0))
        self.connect_btn = app._button(row, "원격 연결", "start", self._connect)
        self.connect_btn.pack(side="left")
        self.config_btn = app._button(row, "설정 보내기", "secondary", self._config_dialog)
        self.config_btn.pack(side="left", padx=(px(8), 0))
        app._tip(self.config_btn, "선택한 현장 PC 의 이름·접속 코드·최고 화질·파일 전송을 바꿉니다 (꺼져 있으면 다음 접속 때 적용)")

    def _build_usage(self):
        app, tk, px = self.app, self.tk, self.px
        box = self._section("데이터 사용량")
        row = tk.Frame(box, bg=app.PANEL)
        row.pack(fill="x")
        self.day_lbl = tk.Label(row, text="오늘 -", font=app.FONT_BOLD, fg=app.TEXT, bg=app.PANEL)
        self.day_lbl.pack(side="left")
        self.month_lbl = tk.Label(row, text="이번 달 -", font=app.FONT_BOLD, fg=app.TEXT, bg=app.PANEL)
        self.month_lbl.pack(side="left", padx=(px(20), 0))
        tk.Label(box, text=USAGE_HINT, font=app.FONT, fg=app.TEXT_DIM, bg=app.PANEL, anchor="w"
                 ).pack(fill="x", pady=(px(4), 0))

    def _build_log(self):
        app, tk, px = self.app, self.tk, self.px
        card = app._card(self.parent)
        card.pack(fill="both", expand=True)
        self.log_text = tk.Text(card, height=LOG_LINES, font=app.FONT_MONO_SMALL, bg=app.LOG_BG, fg="#D5DADD",
                                relief="flat", bd=px(6), wrap="none", state="disabled", highlightthickness=0)
        self.log_text.pack(fill="both", expand=True)

    # ── 버튼 처리 ──
    def _apply_server(self):
        self.controller.apply_server(self.url_var.get(), self.key_var.get())

    def _field_toggled(self):
        self.controller.field_toggled(self.enabled_var.get())

    def _relay_toggled(self):
        self._relay_layout()
        self.controller.relay_toggled(self.relay_var.get(), self.relay_port_var.get())

    def _copy_id(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.ctx["cfg"].ensure_id())
            self.controller.log("이 PC 의 ID 를 복사했습니다")
        except Exception:
            pass

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            self.mb.showinfo("현장 PC 선택", "목록에서 현장 PC 를 먼저 선택하세요", parent=self.root)
            return None
        return sel[0]

    def _connect(self):
        agent_id = self._selected()
        if not agent_id:
            return
        agent = self.controller.agent(agent_id) or {}
        label = (agent.get("name") or "").strip() or format_id(agent_id)
        code = self.sd.askstring("접속 코드", f"'{label}' 의 접속 코드를 입력하세요", show="*", parent=self.root,
                                 initialvalue=self.controller.remembered_code(agent_id))
        if code is None:
            return
        self.controller.connect(agent_id, code)

    def _config_dialog(self):
        agent_id = self._selected()
        if not agent_id:
            return
        app, tk, ttk, px = self.app, self.tk, self.ttk, self.px
        agent = self.controller.agent(agent_id) or {}
        label = (agent.get("name") or "").strip() or format_id(agent_id)
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
        for text, var, show in (("이름", name_var, None), ("접속 코드", code_var, "*")):
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
                agent_id, name_var.get(), code_var.get(), "" if quality == KEEP else quality,
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
        ids = [aid for aid, _v in rows]
        selected = tree.selection()
        if list(tree.get_children()) == ids:                # 같은 목록이면 값만 고쳐 선택·스크롤 유지
            for aid, values in rows:
                tree.item(aid, values=values, tags=(() if values[2] == "켜짐" else ("off",)))
            return
        for iid in tree.get_children():
            tree.delete(iid)
        for aid, values in rows:
            tree.insert("", "end", iid=aid, values=values, tags=(() if values[2] == "켜짐" else ("off",)))
        keep = [s for s in selected if s in ids]
        if keep:
            tree.selection_set(keep)

    def set_field_info(self, id_text, name_text, code_text):
        self.id_lbl.configure(text=f"이 PC ID  {id_text}")
        self.name_lbl.configure(text=f"이름  {name_text}   ·   {code_text}")

    def set_field_status(self, text, level):
        self.field_lbl.configure(text=f"● {text}", fg=self._color(level))

    def set_field_enabled(self, enabled):
        self.enabled_var.set(bool(enabled))

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
        btn = {"server": self.server_btn, "connect": self.connect_btn, "config": self.config_btn}.get(what)
        if btn:
            btn.configure(state="disabled" if busy else "normal")

    def show_error(self, title, message):
        self.mb.showerror(title, message, parent=self.root)

    def show_info(self, title, message):
        self.mb.showinfo(title, message, parent=self.root)


def build_tab(app, parent, ctx) -> WanTab:
    """aurora_client 의 탭 만들기에서 부름.  tkinter 는 여기서 불러온다 (이 파일 import 만으로는 필요 없음)"""
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
    return WanTab(app, parent, ctx, tk, ttk, messagebox, simpledialog)
