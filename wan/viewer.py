"""
WAN 원격 화면 창 — 기존 RemoteViewer 를 그대로 쓰되 WAN 에 맞게 몇 가지만 바꾼다
==================================================================
- 화질은 항상 "절약" 으로 시작 (기억하지 않음 · 설정 파일에 저장하지 않음)
- "절약" 이면 요청하는 화면 크기를 SAVER_BOX 로 제한 (현장 PC 도 같은 제한을 걸지만 미리 줄여 데이터 절약)
- 툴바 오른쪽에 연결 종류 · 이번 연결 사용량 · 현재 속도 · 화질을 1초마다 표시
- 창을 닫으면 Session 도 함께 닫음 (close_session=False 로 끌 수 있음)

RemoteViewer 는 Windows 전용 모듈(aurora_remote)에 있어서 이 파일이 직접 import 하지 않고,
make_viewer_class(RemoteViewer) 로 받아서 파생 클래스를 만든다. tkinter 도 import 하지 않는다
(기반 클래스가 가진 self._tk 를 쓴다).
"""

import time
from collections import deque

from .config import QUALITY, QUALITY_DEFAULT, SAVER_BOX
from .usage import fmt_bytes

SAVER_NAME = "절약"          # 이 화질일 때만 화면 크기를 제한
REFRESH_MS = 1000            # 정보 줄 갱신 간격

KIND_TEXT = {"direct": "직접 연결", "relay": "비상 중계"}
RECONNECTING = "다시 연결 중…"


class SpeedMeter:
    """누적 바이트 수 → 현재 속도(바이트/초).  최근 window 초 동안의 증가량으로 계산해 들쭉날쭉함을 줄인다"""

    def __init__(self, window: float = 3.0, clock=time.monotonic):
        self.window = window
        self._clock = clock
        self._samples = deque()          # (시각, 누적 바이트)

    def update(self, total: int) -> float:
        now = self._clock()
        self._samples.append((now, total))
        # window 밖의 오래된 표본은 버리되, 기준이 될 표본 하나는 남긴다
        while len(self._samples) > 2 and now - self._samples[1][0] >= self.window:
            self._samples.popleft()
        return self.speed()

    def speed(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        if t1 <= t0:
            return 0.0
        return max(0.0, (b1 - b0) / (t1 - t0))


def kind_text(mode: str, closed: bool = False) -> str:
    """Session.mode → 화면에 보일 연결 종류.  모드가 비어 있으면(끊겨서 재연결 중) '다시 연결 중…'"""
    if closed:
        return "연결 닫힘"
    return KIND_TEXT.get(mode, RECONNECTING)


def fmt_speed(bytes_per_sec: float) -> str:
    return f"{bytes_per_sec / 1024:.0f} KB/s"


def info_line(mode: str, total: int, speed: float, quality: str, closed: bool = False) -> str:
    """'직접 연결 · 이번 연결 12.3 MB · 85 KB/s · 절약'"""
    return " · ".join((kind_text(mode, closed), f"이번 연결 {fmt_bytes(total)}", fmt_speed(speed), quality))


def cap_frame_params(params: dict, box=SAVER_BOX) -> dict:
    """화면 요청의 w, h 를 box 이하로 제한한 '새' dict (원본은 건드리지 않음)"""
    out = dict(params)
    for key, limit in zip(("w", "h"), box):
        v = out.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = min(int(v), limit)
    return out


def make_viewer_class(base):
    """base = aurora_remote.RemoteViewer → WAN 용 파생 클래스"""

    class WanRemoteViewer(base):
        QUALITY = dict(QUALITY)

        def __init__(self, root, title, session, px=lambda v: v, font=("맑은 고딕", 9),
                     side=None, side_width=0, on_closed=None, close_session=True):
            # 기반 클래스 생성자가 스레드를 띄우기 전에 필요한 값부터 준비
            self._session = session
            self._close_session = close_session
            self._speed = SpeedMeter()
            self._info = None
            self._info_job = None
            super().__init__(root, title, session.viewer_request, px=px, quality=QUALITY_DEFAULT,
                             on_quality=None, on_closed=on_closed, font=font,
                             side=side, side_width=side_width)
            self._build_info(font, px)
            self._refresh_info()

        # ── 요청: 절약 화질이면 화면 크기 제한 ──
        def _req(self, route, params, timeout):
            if route == "/remote/frame" and self._quality == SAVER_NAME:
                params = cap_frame_params(params)
            return super()._req(route, params, timeout)

        # ── 툴바 정보 줄 ──
        def _build_info(self, font, px):
            tk = self._tk
            self._info = tk.Label(self._status.master, text="", bg=self.BG, fg=self.DIM, font=font, anchor="e")
            self._info.pack(side="right", padx=(px(8), 0))

        def _info_text(self) -> str:
            s = self._session
            total = int(getattr(s, "tx", 0)) + int(getattr(s, "rx", 0))
            speed = self._speed.update(total)
            return info_line(getattr(s, "mode", ""), total, speed, self._quality,
                             closed=bool(getattr(s, "closed", False)))

        def _refresh_info(self):
            """1초마다 정보 줄 갱신 — 창이 닫히면 멈춤, 어떤 경우에도 예외를 밖으로 내지 않음"""
            if not self.alive:
                return
            try:
                text = self._info_text()
            except Exception:
                text = ""
            try:
                self._info.configure(text=text)
                self._info_job = self.win.after(REFRESH_MS, self._refresh_info)
            except self._tk.TclError:
                pass     # 창이 이미 없어짐

        # ── 닫기 ──
        def close(self):
            was_alive = self.alive
            try:
                super().close()
            finally:
                if was_alive and self._close_session:
                    try:
                        self._session.close()
                    except Exception:
                        pass

    return WanRemoteViewer
