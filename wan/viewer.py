"""
WAN 원격 화면 창 — 기존 RemoteViewer 를 그대로 쓰되 WAN 에 맞게 몇 가지만 바꾼다
==================================================================
- 화질은 항상 "절약" 으로 시작 (기억하지 않음 · 설정 파일에 저장하지 않음)
- "절약" 이면 요청하는 화면 크기를 SAVER_BOX 로 제한 (현장 PC 도 같은 제한을 걸지만 미리 줄여 데이터 절약)
- 툴바 오른쪽에 연결 종류 · 이번 연결 사용량 · 현재 속도 · 화질을 1초마다 표시
- 창을 닫으면 Session 도 함께 닫음 (close_session=False 로 끌 수 있음)
- 인터넷 지연 대응: 화면을 화질에 따라 1~2장씩 겹쳐 요청 (config.PREFETCH) — 같은 조각이 두 번 오지 않게 장비에 알림. "빠르게" 는 한 장 더.
  초당 화면 수는 거의 2배가 되지만 **데이터도 약 1.9배** 쓴다(겹친 요청이 같은 변화를 두 번 받음) → 기본은 꺼짐.
  현장은 "보는 쪽이 보낸 last 번호 이후의 누적 변경분" 을 돌려주므로(직전에 보낸 화면 기준이 아님)
  요청이 겹쳐도 안전하다 — 이미 더 새 화면이 있으면 늦게 온 응답은 버리고, 기준이 내 화면보다 오래된 조각은
  더 새 화면 위에 붙여도 맞다 (바뀌지 않은 칸은 같은 내용이므로).

RemoteViewer 는 Windows 전용 모듈(aurora_remote)에 있어서 이 파일이 직접 import 하지 않고,
make_viewer_class(RemoteViewer) 로 받아서 파생 클래스를 만든다. tkinter 도 import 하지 않는다
(기반 클래스가 가진 self._tk 를 쓴다).
"""

import sys
import threading
import time
from collections import deque

from .config import MAX_PREFETCH, PREFETCH, PREFETCH_FAST, QUALITY, QUALITY_DEFAULT, SAVER_BOX
from .usage import fmt_bytes

SLOW_RTT = 0.15      # 요청 하나의 왕복이 이보다 길면 겹쳐 보냄 (초)
ACT_SECONDS = 1.0    # 입력을 보낸 뒤 이 시간 동안은 화면 요청을 서두름
SMALL_REPLY = 2048      # 이보다 작은 응답 = 화면 변화 없음 (왕복 시간 재는 용도)
BIG_REPLY = 16384       # 이보다 큰 응답만 회선 속도 계산에 씀
FULL_FRAME_BYTES = 60000   # 화면 전체 한 장의 대략적인 크기 (WebP 기준)
SLOW_FULL_S = 0.40      # 전체 한 장이 이보다 오래 걸리면 느린 회선 → 거친 화면 허용
ACT_POLL = 0.05      # 조작 중 화면 요청 사이 최대 간격 (변화가 없는 응답은 100바이트 안팎이라 데이터는 거의 안 듦)

SAVER_NAME = "절약"          # 이 화질일 때만 화면 크기를 제한
REFRESH_MS = 1000            # 정보 줄 갱신 간격

KIND_TEXT = {"direct": "직접 연결", "relay": "중계 PC 경유"}
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
        PREFETCH = dict(PREFETCH)

        def __init__(self, root, title, session, px=lambda v: v, font=("맑은 고딕", 9),
                     side=None, side_width=0, on_closed=None, close_session=True, fast=False):
            # 기반 클래스 생성자가 스레드를 띄우기 전에 필요한 값부터 준비
            self._session = session
            self._close_session = close_session
            self._speed = SpeedMeter()
            self._info = None
            self._info_job = None
            self._pf_lock = threading.RLock()      # 화면 상태(_base · _pf_*)를 건드리는 스레드가 여럿
            self._pf_key, self._pf_gen, self._pf_last = None, 0, -1
            self._pf_rtt = 0.0
            self._pending = []                      # 앞선 응답을 기다리는 응답 [(meta, 본문)]
            self._act_until = 0.0                   # 이 시각까지는 조작 중 — 화면 요청을 서두름
            self._rtt_floor, self._bps, self._slow = 0.0, 0.0, False   # 회선 추정: 변화 없는 응답의 시간 · 초당 바이트 · 느린 회선 여부
            self._fmt_param = {"fmt": "webp"} if getattr(sys.modules[base.__module__], "WEBP_OK", False) else {}     # WebP 로 받을 수 있으면 요청 (같은 화질에서 데이터 40~60%)
            self._wake_gen = 0                      # 입력을 보낼 때마다 올림 — 쉬는 중인 화면 요청이 이 값이 바뀌면 깨어남
            self._fast = bool(fast)                 # "빠르게" — 데이터를 더 쓰고 화면을 더 부드럽게
            super().__init__(root, title, session.viewer_request, px=px, quality=QUALITY_DEFAULT,
                             on_quality=None, on_closed=on_closed, font=font,
                             side=side, side_width=side_width)
            self._build_info(font, px)
            self._refresh_info()

        # ── 화면 스레드: 요청을 화질별로 1~2장씩 동시에 ──
        def _frame_loop(self):
            """기반 클래스의 _frame_loop 를 대신함 — 슬롯(요청 하나를 계속 돌리는 스레드)을 여러 개 둔다"""
            for slot in range(1, MAX_PREFETCH):
                threading.Thread(target=self._slot_loop, args=(slot,), daemon=True).start()
            self._slot_loop(0)

        def _slot_loop(self, slot: int):
            unpack = sys.modules[base.__module__]._unpack
            staggered_for = None
            while self.alive:
                depth = self._depth()
                if slot >= depth:                      # 이 화질에서는 쉬는 슬롯
                    staggered_for = None
                    self._sleep(0.25)
                    continue
                q, fps = self.QUALITY[self._quality]
                with self._pf_lock:
                    key = (self._mon, self._want, q)
                    if key != self._pf_key:            # 화면·크기·화질이 바뀌면 처음부터 (이전 요청의 응답은 버림)
                        self._pf_key, self._pf_gen = key, self._pf_gen + 1
                        self._pf_last, self._base, self._pending = -1, None, []
                    gen, last = self._pf_gen, self._pf_last
                if slot and staggered_for != gen:      # 슬롯끼리 시작을 엇갈리게 — 한꺼번에 요청하지 않도록
                    staggered_for = gen
                    self._sleep(slot * max(self._pf_rtt, 1.0 / fps) / depth)
                    continue
                t0 = time.monotonic()
                try:
                    r = self._req("/remote/frame", {"mon": self._mon, "w": self._want[0], "h": self._want[1],
                                                    "q": q, "last": last, "wan": 1, "rough": int(self._rough_allowed()),
                                                    "pipe": int(depth > 1), **self._fmt_param},
                                  timeout=(3, 8))
                except Exception:
                    self._lost_since = self._lost_since or time.monotonic()
                    self._error = f"연결 끊김 — 다시 연결 중… ({int(time.monotonic() - self._lost_since)}초)"
                    self._sleep(1.5)
                    continue
                self._lost_since = 0.0
                if r.status_code != 200:
                    self._error = self._error_text(r)
                    self._sleep(3.0)
                    continue
                try:
                    meta, jpeg = unpack(r.content)
                    ops = None
                    with self._pf_lock:
                        if gen != self._pf_gen:        # 설정이 바뀌기 전에 보낸 요청의 응답
                            continue
                        if not meta.get("err") and not meta.get("busy"):
                            ops = self._apply(meta, jpeg)
                            if ops is None:            # 가진 화면과 조각이 안 맞음 → 통째로 다시 받음
                                self._base, self._pf_last, self._pending = None, -1, []
                            else:
                                self._pf_last = max(self._pf_last, self._base_seq if self._base is not None else -1)   # 실제로 붙인 번호까지만 (보관 중인 응답은 제외)
                except Exception:
                    self._error = "받은 화면을 읽지 못했습니다"
                    with self._pf_lock:
                        self._base, self._pf_last, self._pending = None, -1, []
                    self._sleep(1.0)
                    continue
                if meta.get("err"):
                    self._error = meta["err"]
                    self._sleep(1.0)
                    continue
                if meta.get("busy"):
                    self._sleep(0.3)
                    continue
                if ops is None:
                    continue
                self._error = ""
                self._note, self._gdi = meta.get("note", ""), bool(meta.get("gdi"))
                elapsed = time.monotonic() - t0
                self._pf_rtt = elapsed if not self._pf_rtt else 0.7 * self._pf_rtt + 0.3 * elapsed
                self._note_transfer(len(r.content), elapsed)
                if jpeg:
                    self._net = f"{max(1, len(jpeg) // 1024)} KB · {int(elapsed * 1000)} ms"
                if not self._push_ops(ops, meta):
                    return
                try:
                    nxt = min(max(float(meta.get("next", 0.15)), 0.0), 1.0)
                except (TypeError, ValueError):
                    nxt = 0.15
                if meta.get("rough"):                  # 거친 화면은 잠깐용 — 선명한 화면을 바로 받으러 감 (fps 간격을 기다리지 않음)
                    self._nap(nxt)
                    continue
                # 슬롯이 depth 개이므로 슬롯 하나당 주기는 depth/fps → 전체로는 fps 를 넘지 않음
                self._nap(max(depth / fps - elapsed, nxt) if jpeg else max(nxt, 0.04))

        def _note_transfer(self, nbytes: int, elapsed: float):
            """응답 하나로 회선 속도를 어림 — 작은 응답(변화 없음)은 왕복 바닥값, 큰 응답은 초당 바이트"""
            if nbytes < SMALL_REPLY:
                # 가장 짧았던 응답을 기준으로, 회선이 느려지면 조금씩 따라 올라감 (가끔 느린 응답 하나에 끌려가지 않게)
                self._rtt_floor = elapsed if not self._rtt_floor else min(elapsed, self._rtt_floor * 1.02)
                return
            if nbytes < BIG_REPLY:
                return
            bps = nbytes / max(0.02, elapsed - self._rtt_floor)
            self._bps = bps if not self._bps else 0.6 * self._bps + 0.4 * bps
            full = FULL_FRAME_BYTES / self._bps            # 화면 전체 한 장이 걸릴 시간
            if full > SLOW_FULL_S:
                self._slow = True
            elif full < SLOW_FULL_S * 0.6:
                self._slow = False

        def _rough_allowed(self) -> bool:
            """거친(작은) 화면을 받아도 되는지. 절약은 늘 허용(데이터 우선), 그 밖에는 회선이 느릴 때만 — 빠른 회선이면 바로 선명하게"""
            return self._quality == SAVER_NAME or self._slow

        def _after_input(self, ok):
            """입력을 장비에 보낸 직후 — 결과(글자 · 메뉴)가 바로 보이도록 잠깐 동안 화면 요청을 서두름"""
            self._act_until = time.monotonic() + ACT_SECONDS
            self._wake_gen += 1

        def _nap(self, sec: float):
            """화면 요청 사이 쉬기. 조작 중이면 짧게(ACT_POLL), 입력이 나가면 바로 깨어남"""
            if time.monotonic() < self._act_until:
                sec = min(sec, ACT_POLL)
            gen, end = self._wake_gen, time.monotonic() + sec
            while self.alive and self._wake_gen == gen:
                left = end - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(0.01, left))

        def _depth(self) -> int:
            """동시에 날릴 요청 수 — 겹쳐 보내도 같은 조각이 두 번 오지 않으므로(장비가 'pipe' 를 알아봄) 기본으로 겹쳐 보냄"""
            table = PREFETCH_FAST if self._fast else self.PREFETCH
            depth = table.get(self._quality, 1)
            if depth == 1 and self._pf_rtt > SLOW_RTT:     # 왕복이 느리면 "절약" 도 겹쳐 보내야 목표 fps 에 닿음 (데이터는 안 늘어남)
                depth = 2
            return max(1, min(MAX_PREFETCH, depth))

        def _push_ops(self, ops, meta) -> bool:
            """기반 클래스 _frame_loop 와 같은 방식으로 그릴 작업을 UI 에 넘김. 창이 없어졌으면 False"""
            with self._frame_lock:
                if ops and ops[0][0] == "full":
                    self._ops = ops
                else:
                    self._ops.extend(ops)
                if len(self._ops) > 48:                # UI 가 밀림 → 조각 대신 지금 화면 한 장으로
                    with self._pf_lock:
                        if self._base is not None:
                            self._ops = [("full", self._base.copy())]
                self._meta = meta
                schedule = not self._ui_pending
                self._ui_pending = True
            if schedule:
                try:
                    self.win.after(0, self._show)
                except Exception:
                    return False
            return True

        def _apply(self, meta, data):
            """겹쳐 날아온 응답을 순서에 맞게 적용. 앞선 응답이 아직 안 왔으면 잠시 보관 (모자라면 None → 통째로 다시)"""
            if not data or not meta.get("parts"):
                return []
            seq, base = meta.get("seq", -1), meta.get("base")
            size = tuple(meta.get("size") or ())
            if self._base is not None and self._base.size == size:
                if seq <= self._base_seq:
                    return []                          # 이미 더 새 화면이 있음 — 늦게 온 응답
                if base is not None and base > self._base_seq:
                    self._pending.append((meta, data)) # 이 조각이 붙을 화면(앞선 응답)이 아직 안 옴
                    if len(self._pending) > MAX_PREFETCH:
                        self._pending.clear()
                        return None
                    return []
                if base is not None and base < self._base_seq:
                    self._base_seq = base              # 누적 변경분이라 더 새 화면 위에 붙여도 맞음 (기반 클래스 검사 통과용)
            ops = super()._apply(meta, data)
            while ops is not None and self._pending:   # 보관해 둔 뒤따르는 응답 중 이제 붙일 수 있는 것
                self._pending = [p for p in self._pending if p[0].get("seq", -1) > self._base_seq]
                ready = next((p for p in self._pending if p[0].get("base", -1) <= self._base_seq), None)
                if ready is None:
                    break
                self._pending.remove(ready)
                more = super()._apply(*ready)
                if more is None:
                    return None
                ops = ops + more
            return ops

        # ── 요청: 절약 화질이면 화면 크기 제한 ──
        def _req(self, route, params, timeout):
            if route == "/remote/frame" and self._quality == SAVER_NAME:
                params = cap_frame_params(params)
            return super()._req(route, params, timeout)

        # ── 툴바 정보 줄 ──
        def _build_info(self, font, px):
            """도구 막대 아래에 전용 줄을 둠 — 막대에 끼우면 창이 좁을 때 뒤에 붙인 것부터 잘려 안 보인다"""
            tk = self._tk
            row = self._info_row = tk.Frame(self.win, bg=self.BG)
            row.pack(fill="x", padx=px(10), pady=(0, px(4)), before=self._area)
            self._fast_var = tk.BooleanVar(value=self._fast)
            tk.Checkbutton(row, text="빠르게 (요청을 더 겹쳐 지연을 줄임 · 데이터를 조금 더 씀)", variable=self._fast_var,
                           command=self._fast_changed, bg=self.BG, fg=self.FG, selectcolor=self.BG,
                           activebackground=self.BG, activeforeground=self.FG, font=font).pack(side="left")
            self._info = tk.Label(row, text="", bg=self.BG, fg=self.DIM, font=font, anchor="e")
            self._info.pack(side="right")

        def _fast_changed(self):
            self._fast = bool(self._fast_var.get())
            self.canvas.focus_set()

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
