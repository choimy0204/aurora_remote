"""WAN 탭 공용 — 상수 · 순수 함수(tkinter 없음) · 화면 갱신 함수 모음(NullUI).  wan/ui.py 를 참고"""

import time
from datetime import datetime

from . import connect_code


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
    def set_wan_enabled(self, enabled): pass                  # WAN 전체 스위치 (꺼지면 나머지 칸을 숨기고 '접속 코드로 시작' 칸을 보임)
    def set_usage(self, day_text, month_text): pass
    def set_log(self, lines): pass
    def set_busy(self, what, busy): pass                      # what: server / connect / config
    def show_error(self, title, message): pass
    def show_info(self, title, message): pass


# ─────────────────────────────────────────
