"""컨트롤러 일부 — 이 PC 에서 안내 서버(중계 PC) 운영 · 접속 코드 새로고침 · 사내 Git"""

import math
import os

from . import connect_code
from .config import DEFAULT_RELAY_KEY, RELAY_EXIT_PORT_BUSY
from .netutil import is_admin, lan_addresses, open_firewall
from .ui_base import validate_port


class RelayMixin:
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
                 "접속 코드(공용 키)를 새로 만들까요?\n\n· 기존 접속 정보는 즉시 사라집니다 (접속하는 PC 는 새 정보를 다시 받아야 합니다).\n· 지금 연결 중인 원격 화면은 끊깁니다.\n· 현장 PC 는 자동으로 새 코드를 받습니다 (꺼져 있던 PC 는 다시 켜질 때).\n"
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
