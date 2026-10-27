"""컨트롤러 일부 — 접속 설정 파일 · 연결 추가 · 이 PC 를 원격 대상으로 쓰기(현장)"""

import math
import os
import socket
from urllib.parse import urlsplit

from . import connect_code, connfile, discover
from .config import DEFAULT_RELAY_KEY, DIRECT_PORT, format_id
from .netutil import lan_addresses, open_firewall, public_address
from .ui_base import CODE_MAX, DIRECT_ID, NAME_MAX, PUBLIC_RETRY, classify_target


class FieldMixin:
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
            ok = self.set_git_source(text)
            if ok and as_field:
                self.field_toggled(True)
            return ok
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
        if (self.cfg.get("relay_url") or "").strip() or self.cfg.get("relay_host"):
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

    def needs_relay_info(self) -> bool:
        """중계 PC 에 붙을 방법(주소 · Git 저장소)이 아직 하나도 없는지"""
        return not ((self.cfg.get("relay_url") or "").strip() or (self.cfg.get("git_source") or "").strip()
                    or self.cfg.get("relay_host"))

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
