"""원격 화면 보기 · 조작

장비 쪽 (RemoteHost)  : 화면 캡처·JPEG 압축, 받은 마우스·키보드 입력 실행
보는 쪽 (RemoteViewer): 화면을 띄우고 마우스·키보드 입력을 보냄

경로: 내 PC → 중계 PC(LHS) → 장비. 요청은 기존 반출 API 와 같은 키·중계(via)를 쓰므로
중계 PC 는 옛 버전이어도 그대로 통과시킨다 (장비만 이 기능이 있으면 됨).

거쳐가는 PC·장비가 버벅이거나 오동작하지 않도록
- 보는 쪽은 화면 요청 1개, 입력 요청 1개만 동시에 보낸다 → 느리면 덜 보낼 뿐 요청이 쌓이지 않음
- 장비 캡처·압축은 한 번에 하나, 낮은 우선순위 스레드에서, 걸린 시간의 DUTY 배를 쉰다
  → CPU 한 코어의 1/(1+DUTY) 이하. 화면이 그대로면 압축·전송을 건너뜀
- 바뀐 곳만 복사·축소·압축해 조각으로 보낸다 (숫자 몇 개가 바뀌면 그 칸만) — 보는 쪽은 붙여 넣기만
- 입력은 재전송하지 않는다 (중복 클릭 방지). STALE 초 넘게 늦은 클릭·키 누름은 버린다
- 화면이 FRAME_STALE 초 넘게 갱신되지 않으면 보는 쪽이 조작을 잠근다 (옛 화면을 보고 누르지 않도록)
- 입력 요청마다 '지금 눌려 있어야 할 키·버튼' 을 함께 보내 장비가 어긋난 것을 스스로 뗀다
- 장비는 IDLE_RELEASE 초 동안 입력 소식이 없으면 눌린 키·버튼을 모두 뗀다 (보는 쪽이 누르고 있는 동안엔 1초마다 알림)
"""
import ctypes
import ctypes.wintypes as wt
import io
import json
import socket
import struct
import threading
import time
import uuid
from contextlib import contextmanager

from PIL import Image

_RESAMPLE = getattr(Image, "Resampling", Image).BILINEAR

# ─────────────────────────────────────────
# Win32 (핸들이 64비트에서 잘리지 않도록 모든 함수에 타입 지정 · 전용 DLL 객체 사용)
# ─────────────────────────────────────────
_user32   = ctypes.WinDLL("user32", use_last_error=True)
_gdi32    = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _proto(fn, res, *args):
    fn.restype, fn.argtypes = res, args
    return fn


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

_GetDC               = _proto(_user32.GetDC, wt.HDC, wt.HWND)
_ReleaseDC           = _proto(_user32.ReleaseDC, ctypes.c_int, wt.HWND, wt.HDC)
_GetSystemMetrics    = _proto(_user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_GetCursorPos        = _proto(_user32.GetCursorPos, wt.BOOL, ctypes.POINTER(wt.POINT))
_SendInput           = _proto(_user32.SendInput, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_int)
_MapVirtualKeyW      = _proto(_user32.MapVirtualKeyW, wt.UINT, wt.UINT, wt.UINT)
_EnumDisplayMonitors = _proto(_user32.EnumDisplayMonitors, wt.BOOL, wt.HDC, ctypes.POINTER(wt.RECT),
                              _MONITORENUMPROC, wt.LPARAM)
_GetMonitorInfoW     = _proto(_user32.GetMonitorInfoW, wt.BOOL, wt.HMONITOR, ctypes.POINTER(_MONITORINFO))
_CreateCompatibleDC  = _proto(_gdi32.CreateCompatibleDC, wt.HDC, wt.HDC)
_CreateDIBSection    = _proto(_gdi32.CreateDIBSection, wt.HBITMAP, wt.HDC, ctypes.POINTER(_BITMAPINFO),
                              wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
_SelectObject        = _proto(_gdi32.SelectObject, wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
_BitBlt              = _proto(_gdi32.BitBlt, wt.BOOL, wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD)
_DeleteObject        = _proto(_gdi32.DeleteObject, wt.BOOL, wt.HGDIOBJ)
_DeleteDC            = _proto(_gdi32.DeleteDC, wt.BOOL, wt.HDC)
_GdiFlush            = _proto(_gdi32.GdiFlush, wt.BOOL)
_GetCurrentThread    = _proto(_kernel32.GetCurrentThread, wt.HANDLE)
_GetThreadPriority   = _proto(_kernel32.GetThreadPriority, ctypes.c_int, wt.HANDLE)
_SetThreadPriority   = _proto(_kernel32.SetThreadPriority, wt.BOOL, wt.HANDLE, ctypes.c_int)
try:   # Windows 10 1607 이상
    _SetThreadDpiAwarenessContext = _proto(_user32.SetThreadDpiAwarenessContext,
                                           ctypes.c_void_p, ctypes.c_void_p)
except AttributeError:
    _SetThreadDpiAwarenessContext = None

_OpenClipboard       = _proto(_user32.OpenClipboard, wt.BOOL, wt.HWND)
_CloseClipboard      = _proto(_user32.CloseClipboard, wt.BOOL)
_EmptyClipboard      = _proto(_user32.EmptyClipboard, wt.BOOL)
_GetClipboardData    = _proto(_user32.GetClipboardData, wt.HANDLE, wt.UINT)
_SetClipboardData    = _proto(_user32.SetClipboardData, wt.HANDLE, wt.UINT, wt.HANDLE)
_GetClipboardSeq     = _proto(_user32.GetClipboardSequenceNumber, wt.DWORD)
_GlobalAlloc         = _proto(_kernel32.GlobalAlloc, wt.HGLOBAL, wt.UINT, ctypes.c_size_t)
_GlobalLock          = _proto(_kernel32.GlobalLock, ctypes.c_void_p, wt.HGLOBAL)
_GlobalUnlock        = _proto(_kernel32.GlobalUnlock, wt.BOOL, wt.HGLOBAL)
_GlobalFree          = _proto(_kernel32.GlobalFree, wt.HGLOBAL, wt.HGLOBAL)
_GlobalSize          = _proto(_kernel32.GlobalSize, ctypes.c_size_t, wt.HGLOBAL)
_CF_UNICODETEXT, _GMEM_MOVEABLE = 13, 0x0002

# 원격 창 — Windows 제목 표시줄 대신 직접 그린 제목 줄 (크기 조절 테두리·작업 표시줄·최소화는 그대로)
_LRESULT          = ctypes.c_ssize_t
_WNDPROC          = ctypes.WINFUNCTYPE(_LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_GetParent        = _proto(_user32.GetParent, wt.HWND, wt.HWND)
_GetWindowLongPtr = _proto(_user32.GetWindowLongPtrW, ctypes.c_ssize_t, wt.HWND, ctypes.c_int)
_SetWindowLongPtr = _proto(_user32.SetWindowLongPtrW, ctypes.c_ssize_t, wt.HWND, ctypes.c_int, ctypes.c_ssize_t)
_CallWindowProc   = _proto(_user32.CallWindowProcW, _LRESULT, ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM,
                           wt.LPARAM)
_SetWindowPos     = _proto(_user32.SetWindowPos, wt.BOOL, wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, wt.UINT)
_GetWindowRect    = _proto(_user32.GetWindowRect, wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
_MonitorFromWindow = _proto(_user32.MonitorFromWindow, wt.HMONITOR, wt.HWND, wt.DWORD)
_ReleaseCapture   = _proto(_user32.ReleaseCapture, wt.BOOL)
# SendMessage 금지 — Tk 콜백 안에서 옮기기 루프를 직접 열면 그 사이 Tk 가 다른 Python 콜백을 불러 프로그램이 죽음.
# PostMessage 로 넘기면 Tk 이벤트 루프가 정상 경로로 처리함
_PostMessageW     = _proto(_user32.PostMessageW, wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_GWL_STYLE, _GWL_WNDPROC = -16, -4
_WS_CAPTION, _WS_THICKFRAME, _WS_MAXIMIZEBOX = 0x00C00000, 0x00040000, 0x00010000
_WM_NCCALCSIZE, _WM_NCLBUTTONDOWN = 0x0083, 0x00A1
_HTCAPTION, _HTTOP = 2, 12
_SWP_FRAMECHANGED_ONLY = 0x0001 | 0x0002 | 0x0004 | 0x0020 | 0x0010   # NOSIZE·NOMOVE·NOZORDER·FRAMECHANGED·NOACTIVATE
_SWP_PLACE = 0x0004 | 0x0020                                          # NOZORDER·FRAMECHANGED
CLIP_MAX = 100_000             # 클립보드로 주고받는 글자 수 상한


@contextmanager
def _clipboard():
    """클립보드 열기 — 다른 프로그램이 쓰는 중이면 잠깐씩 다시 시도 (최대 약 0.3초). 못 열면 OSError.
    창 없이(NULL) 열어 소유 창이 없음 → 다른 프로그램이 클립보드를 비울 때 이 스레드에 메시지가 오지 않음"""
    for _ in range(15):
        if _OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        raise OSError("장비 클립보드를 다른 프로그램이 쓰는 중")
    try:
        yield
    finally:
        _CloseClipboard()


def _clip_get():
    """장비 클립보드의 글자 (글자가 아니면 None)"""
    with _clipboard():
        h = _GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return None
        p = _GlobalLock(h)
        if not p:
            return None
        try:
            n = _GlobalSize(h) // 2
            return ctypes.wstring_at(p, min(n, CLIP_MAX + 1)).split("\0", 1)[0][:CLIP_MAX]
        finally:
            _GlobalUnlock(h)


def _clip_set(text: str):
    data = ctypes.create_unicode_buffer(text)                  # 끝의 \0 포함
    size = ctypes.sizeof(data)
    h = _GlobalAlloc(_GMEM_MOVEABLE, size)
    if not h:
        raise OSError("메모리 부족")
    p = _GlobalLock(h)
    if not p:
        _GlobalFree(h)
        raise OSError("메모리 잠금 실패")
    ctypes.memmove(p, data, size)
    _GlobalUnlock(h)
    try:
        with _clipboard():
            _EmptyClipboard()
            if not _SetClipboardData(_CF_UNICODETEXT, h):
                raise OSError("장비 클립보드에 넣지 못했습니다")
    except OSError:
        _GlobalFree(h)                                         # 넘겨주지 못했으면 직접 반납
        raise


_SRCCOPY = 0x00CC0020          # CAPTUREBLT 는 넣지 않음 — 장비 화면의 마우스 커서가 깜빡임
_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_MOVE, _ABSOLUTE, _VIRTUALDESK, _WHEEL = 0x0001, 0x8000, 0x4000, 0x0800
_BTN_FLAGS = {1: (0x0002, 0x0004), 2: (0x0020, 0x0040), 3: (0x0008, 0x0010)}   # 왼쪽·가운데·오른쪽 (누름, 뗌)
_KEYUP, _EXTENDED = 0x0002, 0x0001
_EXTENDED_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E,
                0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5}
_MOUSE_VK = {0x01, 0x02, 0x04, 0x05, 0x06}
_EXTRA = 0xA0A0A0A0            # 오로라 원격이 넣은 입력 표시 (dwExtraInfo)
_THREAD_PRIORITY_ERROR = 0x7FFFFFFF


@contextmanager
def low_priority(level: int = -1):
    """현재 스레드 우선순위를 잠시 낮춤 (-1 보통 아래, -2 가장 낮음) — 장비 본래 작업이 먼저"""
    h = _GetCurrentThread()
    old = _GetThreadPriority(h)
    changed = old != _THREAD_PRIORITY_ERROR and old > level and _SetThreadPriority(h, level)
    try:
        yield
    finally:
        if changed:
            _SetThreadPriority(h, old)


@contextmanager
def _physical_pixels():
    """이 스레드만 모니터별 DPI 인식 → 배율이 다른 모니터에서도 캡처·좌표가 실제 픽셀과 일치"""
    old = None
    if _SetThreadDpiAwarenessContext:
        try:
            old = _SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
        except OSError:
            old = None
    try:
        yield
    finally:
        if old:
            _SetThreadDpiAwarenessContext(ctypes.c_void_p(old))


def _virtual_screen() -> tuple:
    return (_GetSystemMetrics(76), _GetSystemMetrics(77),
            max(1, _GetSystemMetrics(78)), max(1, _GetSystemMetrics(79)))


def _monitors() -> list:
    """[(x, y, w, h, 주모니터)] — 주모니터 먼저, 그다음 왼쪽부터"""
    mons = []

    def _cb(hmon, _hdc, _rect, _lp):
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        if _GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            mons.append((r.left, r.top, r.right - r.left, r.bottom - r.top, bool(mi.dwFlags & 1)))
        return True
    _EnumDisplayMonitors(None, None, _MONITORENUMPROC(_cb), 0)
    mons.sort(key=lambda m: (not m[4], m[0], m[1]))
    return mons


def _grab(rect) -> bytes:
    """화면 영역을 BGRX 원시 바이트로 (GDI). 만든 GDI 객체는 예외가 나도 모두 해제"""
    x, y, w, h = rect
    screen = _GetDC(None)
    if not screen:
        raise OSError("화면을 가져올 수 없습니다")
    mem = bmp = old = None
    try:
        mem = _CreateCompatibleDC(screen)
        if not mem:
            raise OSError("화면을 가져올 수 없습니다 (DC)")
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h      # 음수 = 위에서 아래로
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        bmp = _CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not bmp or not bits.value:
            raise OSError("화면을 가져올 수 없습니다 (메모리)")
        old = _SelectObject(mem, bmp)
        if not _BitBlt(mem, 0, 0, w, h, screen, x, y, _SRCCOPY):
            raise OSError("화면을 가져올 수 없습니다 (잠금 화면이거나 관리자 권한 확인 창이 떠 있음)")
        _GdiFlush()
        return ctypes.string_at(bits.value, w * h * 4)
    finally:
        if old:
            _SelectObject(mem, old)
        if bmp:
            _DeleteObject(bmp)
        if mem:
            _DeleteDC(mem)
        _ReleaseDC(None, screen)


STRIP_PIXELS = 256_000      # GDI 로 한 번에 찍는 최대 픽셀 수 (2560 폭이면 100줄)
STRIP_GAP    = 0.017        # 띠 사이에 쉬는 시간 — 화면 합성이 한 프레임씩 숨 쉴 틈
_slept = [0.0]              # 띠 사이에 쉬거나 GPU 를 기다린 시간 누적 — 캡처 비용(CPU)에서 빼기 위함


def _grab_strips(rect) -> list:
    """GDI 캡처를 얇은 띠로 나눠 사이사이 쉬며 찍음 → [(띠 위쪽 y, 줄 수, BGRX)].
    한 번에 전체를 찍으면 장비 화면이 멈칫하지만(실측 밀린 프레임 5개/회) 이렇게 하면 0개"""
    x, y, w, h = rect
    rows = max(8, STRIP_PIXELS // max(1, w))
    parts = []
    for top in range(0, h, rows):
        if top:
            time.sleep(STRIP_GAP)
            _slept[0] += STRIP_GAP
        n = min(rows, h - top)
        parts.append((top, n, _grab((x, y + top, w, n))))
    return parts


def _bgrx_image(raw, w: int, h: int, pitch: int = 0) -> Image.Image:
    """BGRX 원시 데이터 → RGB 이미지 (항상 복사본 — 원본 메모리를 바로 돌려줘도 됨)"""
    return Image.frombytes("RGB", (w, h), raw, "raw", "BGRX", pitch, 1)


def _bgrx_at(addr: int, pitch: int, x: int, y: int, w: int, h: int) -> Image.Image:
    """매핑된 메모리(주소·줄 간격)의 일부 사각형만 RGB 이미지로 — 필요한 바이트만 읽음"""
    start = addr + y * pitch + x * 4
    return _bgrx_image((ctypes.c_char * (pitch * (h - 1) + w * 4)).from_address(start), w, h, pitch)


# ─────────────────────────────────────────
# 바뀐 영역만 처리
#   화면 전체를 매번 복사·축소·압축하면 숫자 몇 개만 바뀌어도 한 장에 수십 ms 가 든다.
#   바뀐 사각형을 격자 칸으로 모아 그 칸만 복사·축소·압축해 보낸다 (보는 쪽은 붙여 넣기만).
# ─────────────────────────────────────────
def _merge_tiles(tiles: set, t: int, w: int, h: int) -> list:
    """격자 칸 {(열, 행)} → 겹치지 않는 사각형 [(x0, y0, x1, y1)] (가로로 잇고, 같은 폭은 세로로 이음)"""
    rows = {}
    for cx, cy in tiles:
        rows.setdefault(cy, []).append(cx)
    runs = {}   # (시작 열, 끝 열) → 마지막으로 이어 붙인 사각형
    out = []
    for cy in sorted(rows):
        cols = sorted(rows[cy])
        start = prev = cols[0]
        segs = []
        for c in cols[1:] + [None]:
            if c is not None and c == prev + 1:
                prev = c
                continue
            segs.append((start, prev))
            if c is not None:
                start = prev = c
        nxt = {}
        for seg in segs:
            r = runs.get(seg)
            if r is not None and r[3] == cy:          # 바로 윗줄에 같은 폭 → 아래로 늘림
                r[3] = cy + 1
            else:
                r = [seg[0], cy, seg[1] + 1, cy + 1]
                out.append(r)
            nxt[seg] = r
        runs = nxt
    return [(r[0] * t, r[1] * t, min(w, r[2] * t), min(h, r[3] * t)) for r in out]


def _tiles_of(rects, t: int, w: int, h: int, sx: float = 1.0, sy: float = 1.0, margin: int = 0) -> set:
    """사각형들(원본 좌표) → 크기 t 격자 칸 집합 (sx, sy 로 나눈 좌표 기준, margin 만큼 넓혀서)"""
    tiles = set()
    for x0, y0, x1, y1 in rects:
        a = max(0, int(x0 / sx) - margin) // t
        b = max(0, int(y0 / sy) - margin) // t
        c = min(w - 1, int(-(-x1 // sx)) + margin) // t
        d = min(h - 1, int(-(-y1 // sy)) + margin) // t
        for cy in range(b, d + 1):
            for cx in range(a, c + 1):
                tiles.add((cx, cy))
    return tiles


def _fit(size: tuple, box: tuple) -> tuple:
    """size 를 box 안에 맞춘 크기 (확대는 안 함)"""
    f = max(size[0] / box[0], size[1] / box[1], 1.0)
    return max(1, min(box[0], round(size[0] / f))), max(1, min(box[1], round(size[1] / f)))


def _scaled_part(im: Image.Image, ss: tuple, x0: int, y0: int, x1: int, y1: int) -> Image.Image:
    """im 을 ss 크기로 줄인 그림의 (x0, y0)~(x1, y1) 부분만 계산.
    정수배 축소(싸다) 후 나머지만 보간 — 조각으로 나눠 계산해도 전체를 줄인 것과 같음 (실측 차이 ≤ 2/255)"""
    W, H = im.size
    fx, fy = W / ss[0], H / ss[1]
    k = max(1, int(min(fx, fy)))
    if k == 1:
        if (fx, fy) == (1.0, 1.0):
            return im.crop((x0, y0, x1, y1))
        return im.resize((x1 - x0, y1 - y0), _RESAMPLE, box=(x0 * fx, y0 * fy, x1 * fx, y1 * fy))
    gx, gy = fx / k, fy / k
    sx0, sy0, sx1, sy1 = x0 * gx, y0 * gy, x1 * gx, y1 * gy
    m = 3                                        # 보간이 참조하는 주변 픽셀 여유
    rx0, ry0 = max(0, int(sx0) - m), max(0, int(sy0) - m)
    rx1, ry1 = min(-(-W // k), int(sx1) + m + 1), min(-(-H // k), int(sy1) + m + 1)
    r = im.reduce(k, box=(rx0 * k, ry0 * k, min(W, rx1 * k), min(H, ry1 * k)))
    if gx == 1.0 and gy == 1.0:
        return r.crop((x0 - rx0, y0 - ry0, x1 - rx0, y1 - ry0))
    return r.resize((x1 - x0, y1 - y0), _RESAMPLE, box=(sx0 - rx0, sy0 - ry0, sx1 - rx0, sy1 - ry0))


# ─────────────────────────────────────────
# DXGI 화면 복제 (Desktop Duplication)
#   GDI BitBlt 는 화면 합성(DWM)을 붙잡아 장비 화면 자체가 끊긴다 (실측: 초당 10회 캡처에 5초간
#   밀린 프레임 51개, 최대 54ms). DXGI 는 GPU 가 이미 합성해 둔 화면의 사본을 받으므로 합성을
#   멈추지 않는다 → 이것을 먼저 쓰고, 안 되는 PC(원격 데스크톱 세션 등)에서만 GDI 로 천천히.
# ─────────────────────────────────────────
class _GUID(ctypes.Structure):
    _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]


def _iid(s: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(s).bytes_le)


_IID_FACTORY1 = _iid("770aae78-f26f-4dba-a829-253c83d1b387")
_IID_OUTPUT1  = _iid("00cddea8-939b-4b83-a340-a685226666cc")
_IID_TEX2D    = _iid("6f15aaf2-d208-4e89-9ab4-489535d34f9c")


class _DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [("DeviceName", wt.WCHAR * 32), ("DesktopCoordinates", wt.RECT),
                ("AttachedToDesktop", wt.BOOL), ("Rotation", wt.UINT), ("Monitor", wt.HMONITOR)]


class _DXGI_OUTDUPL_DESC(ctypes.Structure):
    _fields_ = [("Width", wt.UINT), ("Height", wt.UINT), ("RefreshNum", wt.UINT), ("RefreshDen", wt.UINT),
                ("Format", wt.UINT), ("ScanlineOrdering", wt.UINT), ("Scaling", wt.UINT),
                ("Rotation", wt.UINT), ("DesktopImageInSystemMemory", wt.BOOL)]


class _DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
    _fields_ = [("LastPresentTime", ctypes.c_int64), ("LastMouseUpdateTime", ctypes.c_int64),
                ("AccumulatedFrames", wt.UINT), ("RectsCoalesced", wt.BOOL),
                ("ProtectedContentMaskedOut", wt.BOOL), ("PointerX", wt.LONG), ("PointerY", wt.LONG),
                ("PointerVisible", wt.BOOL), ("TotalMetadataBufferSize", wt.UINT),
                ("PointerShapeBufferSize", wt.UINT)]


class _DXGI_MAPPED_RECT(ctypes.Structure):
    _fields_ = [("Pitch", ctypes.c_int), ("pBits", ctypes.c_void_p)]


class _D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [("Width", wt.UINT), ("Height", wt.UINT), ("MipLevels", wt.UINT), ("ArraySize", wt.UINT),
                ("Format", wt.UINT), ("SampleCount", wt.UINT), ("SampleQuality", wt.UINT),
                ("Usage", wt.UINT), ("BindFlags", wt.UINT), ("CPUAccessFlags", wt.UINT),
                ("MiscFlags", wt.UINT)]


class _D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", wt.UINT), ("DepthPitch", wt.UINT)]


class _D3D11_BOX(ctypes.Structure):
    _fields_ = [("left", wt.UINT), ("top", wt.UINT), ("front", wt.UINT),
                ("right", wt.UINT), ("bottom", wt.UINT), ("back", wt.UINT)]


DIRTY_TILE = 64         # 바뀐 영역을 모으는 격자 크기 (장비 화면 픽셀)
DIRTY_FULL = 0.6        # 바뀐 칸이 이 비율을 넘으면 통째로 복사 (조각이 더 비쌈)
PATCH_TILE = 64         # 보내는 조각의 격자 크기 (줄인 화면 픽셀, JPEG 블록 16 의 배수)


_HR = ctypes.c_long
_P  = ctypes.c_void_p
_PP = ctypes.POINTER(ctypes.c_void_p)
_WAIT_TIMEOUT      = -0x7785FFD9    # 0x887A0027 — 그동안 바뀐 화면 없음
_ACCESS_LOST       = -0x7785FFDA    # 0x887A0026 — 화면 전환(잠금·관리자 확인·해상도 변경)
_E_ACCESSDENIED    = -0x7FF8FFFB    # 0x80070005 — 보안 화면(잠금·관리자 확인 창)이 떠 있음
_DXGI_NOT_FOUND    = -0x7785FFFE    # 0x887A0002


def _com(obj, idx: int, *argtypes, res=_HR):
    """COM 가상함수 idx 번을 호출 가능한 함수로 (첫 인자는 객체 자신)"""
    vt = ctypes.cast(obj, ctypes.POINTER(_PP))[0]
    return ctypes.WINFUNCTYPE(res, _P, *argtypes)(vt[idx])


def _rel(obj):
    if obj:
        _com(obj, 2, res=wt.ULONG)(obj)


def _qi(obj, iid: _GUID):
    out = _P()
    if _com(obj, 0, ctypes.POINTER(_GUID), _PP)(obj, ctypes.byref(iid), ctypes.byref(out)) < 0:
        return None
    return out


try:
    _dxgi  = ctypes.WinDLL("dxgi")
    _d3d11 = ctypes.WinDLL("d3d11")
    _CreateDXGIFactory1 = _proto(_dxgi.CreateDXGIFactory1, _HR, ctypes.POINTER(_GUID), _PP)
    _D3D11CreateDevice  = _proto(_d3d11.D3D11CreateDevice, _HR, _P, wt.UINT, _P, wt.UINT, _P, wt.UINT,
                                 wt.UINT, _PP, ctypes.POINTER(wt.UINT), _PP)
except (OSError, AttributeError):
    _CreateDXGIFactory1 = _D3D11CreateDevice = None


class _Unsupported(Exception):
    pass


class _DesktopDup:
    """모니터(출력)마다 화면 복제를 열어 두고, 바뀐 모니터만 GPU → 메모리로 복사.
    모든 메서드는 호출하는 쪽(RemoteHost._cap_lock)에서 한 번에 하나씩만 부른다."""
    RETRY = 1.0          # 보안 화면 등으로 복제를 못 열었을 때 다시 시도하는 간격
    ROUGH_MAX = 3.0      # 화면 전체가 계속 바뀌어도 이 시간마다 한 번은 선명하게 읽음

    def __init__(self):
        self._devs = []   # [(device, context)]
        self._outs = []   # [dict]
        self._sig  = None
        self._last_rect = None
        self._comp = None # 모니터 여럿에 걸친 영역을 붙여 둔 그림
        self.note  = ""

    # ── 열기·닫기 ──
    def close(self):
        for o in self._outs:
            self._close_dup(o)
            _rel(o["out1"])
        for dev, ctx in self._devs:
            _rel(ctx)
            _rel(dev)
        self._devs, self._outs, self._sig, self._last_rect, self._comp = [], [], None, None, None

    @staticmethod
    def _close_dup(o: dict):
        _rel(o.get("stage"))
        _rel(o.get("dup"))
        o["stage"] = o["dup"] = None
        if o.get("stale"):               # 선명한 화면을 읽을 GPU 사본이 없어짐 → 다시 열 때 처음부터 채움
            o["img"], o["stale"] = None, 0.0

    def _build(self, sig):
        self.close()
        if _CreateDXGIFactory1 is None:
            raise _Unsupported("DXGI 없음")
        factory = _P()
        if _CreateDXGIFactory1(ctypes.byref(_IID_FACTORY1), ctypes.byref(factory)) < 0:
            raise _Unsupported("DXGI 팩토리 실패")
        try:
            ai = 0
            while True:
                ad = _P()
                if _com(factory, 12, wt.UINT, _PP)(factory, ai, ctypes.byref(ad)) < 0:
                    break                                     # EnumAdapters1 끝
                ai += 1
                try:
                    self._build_adapter(ad)
                finally:
                    _rel(ad)
        finally:
            _rel(factory)
        if not self._outs:
            self.close()
            raise _Unsupported("복제할 모니터 없음")
        self._sig = sig

    def _build_adapter(self, ad):
        dev = ctx = None
        oi = 0
        while True:
            out = _P()
            if _com(ad, 7, wt.UINT, _PP)(ad, oi, ctypes.byref(out)) < 0:
                break                                         # EnumOutputs 끝
            oi += 1
            try:
                desc = _DXGI_OUTPUT_DESC()
                if _com(out, 7, ctypes.POINTER(_DXGI_OUTPUT_DESC))(out, ctypes.byref(desc)) < 0 \
                        or not desc.AttachedToDesktop:
                    continue
                if dev is None:
                    dev, ctx = _P(), _P()
                    lvl = wt.UINT()
                    # 어댑터를 지정하면 드라이버 종류는 UNKNOWN(0), SDK 버전 7
                    if _D3D11CreateDevice(ad, 0, None, 0, None, 0, 7, ctypes.byref(dev),
                                          ctypes.byref(lvl), ctypes.byref(ctx)) < 0:
                        return
                    self._devs.append((dev, ctx))
                out1 = _qi(out, _IID_OUTPUT1)
                if out1 is None:
                    continue
                # DesktopCoordinates 는 프로세스 DPI 모드에 따라 배율 적용 좌표(예 1707×1067)로 올 수 있음
                # → 실제 픽셀 위치는 모니터 정보로 (호출하는 쪽이 _physical_pixels 안)
                r = desc.DesktopCoordinates
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(mi)
                if desc.Monitor and _GetMonitorInfoW(desc.Monitor, ctypes.byref(mi)):
                    r = mi.rcMonitor
                self._outs.append({"out1": out1, "dev": dev, "ctx": ctx,
                                   "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
                                   "rot": desc.Rotation, "dup": None, "stage": None,
                                   "sysmem": False, "img": None, "full": True, "retry": 0.0,
                                   "stale": 0.0, "rough": None})
            finally:
                _rel(out)

    def _open_dup(self, o: dict) -> bool:
        now = time.monotonic()
        if now < o["retry"]:
            return False
        dup = _P()
        hr = _com(o["out1"], 22, _P, _PP)(o["out1"], o["dev"], ctypes.byref(dup))   # DuplicateOutput
        if hr < 0:
            o["retry"] = now + self.RETRY
            if hr == _E_ACCESSDENIED:
                self.note = "보안 화면(잠금·관리자 확인 창)이 떠 있어 화면을 가져올 수 없습니다"
                return False
            raise _Unsupported(f"DuplicateOutput 0x{hr & 0xFFFFFFFF:08X}")
        d = _DXGI_OUTDUPL_DESC()
        _com(dup, 7, ctypes.POINTER(_DXGI_OUTDUPL_DESC), res=None)(dup, ctypes.byref(d))
        o["dup"], o["sysmem"] = dup, bool(d.DesktopImageInSystemMemory)
        if o["sysmem"]:
            o["tex_size"] = (d.Width, d.Height)
        o["rot"] = d.Rotation or o["rot"]
        o["full"] = True       # 연 직후 첫 화면은 통째로 받음 (여는 사이에 바뀐 곳이 있을 수 있음)
        return True

    # ── 한 모니터 ──
    def _grab_out(self, o: dict, rough_ok: bool = False) -> list:
        """이 모니터에서 바뀐 사각형 [(x0, y0, x1, y1)] (모니터 기준 좌표) — 그대로면 [].
        rough_ok 이고 화면 대부분이 바뀌었으면 o["rough"] 에 거친 화면만 만들고 o["img"] 는 그대로 둠
        (o["stale"]) → 화면이 잠잠해지면 GPU 에 남은 사본에서 선명하게 다시 읽음"""
        w, h = o["rect"][2], o["rect"][3]
        whole = [(0, 0, w, h)]
        seeded = []
        if o["dup"] is None:
            if o["img"] is None and time.monotonic() >= o["retry"]:
                # 복제를 연 직후 첫 프레임은 비어 있음(검은 화면) → 여는 '앞'에 GDI 로 천천히 한 번 채움.
                # 복제가 열린 상태에서 GDI 를 찍으면 화면이 한 번 멈칫함(실측) — 그래서 순서가 중요
                try:
                    o["img"] = _bgrx_image(b"".join(s[2] for s in _grab_strips(o["rect"])), w, h)
                    seeded = whole
                except OSError:
                    pass
            if not self._open_dup(o):
                return seeded
        dup = o["dup"]
        info, res = _DXGI_OUTDUPL_FRAME_INFO(), _P()
        hr = _com(dup, 8, wt.UINT, ctypes.POINTER(_DXGI_OUTDUPL_FRAME_INFO), _PP)(
            dup, 0, ctypes.byref(info), ctypes.byref(res))         # AcquireNextFrame, 기다리지 않음
        if hr == _WAIT_TIMEOUT:
            return self._sharpen(o) or seeded
        if hr < 0:                                                 # 화면 전환 등 → 다음에 다시 엶
            self._close_dup(o)
            return seeded
        if info.LastPresentTime == 0:
            # 화면 갱신 없이 마우스만 움직임 (여는 직후 첫 프레임도 이렇게 옴 — 텍스처가 비어 있어 쓰지 않음)
            _rel(res)
            _com(dup, 14)(dup)                                     # ReleaseFrame
            return self._sharpen(o) or seeded
        rects = None                                               # None = 통째로
        rough = False
        tex = None
        try:
            if not o["full"] and o["img"] is not None:
                rects = self._dirty(dup, info, w, h)
                if rects == []:
                    rects = "same"
                elif rects is None:                                # 화면 대부분이 바뀜 → 거칠게 먼저
                    rough = rough_ok and not o["sysmem"] and \
                        (not o["stale"] or time.monotonic() - o["stale"] < self.ROUGH_MAX)
                elif o["stale"]:                                   # 잠잠해짐 → 선명하게 통째로
                    rects = None
            if rects == "same":
                pass
            elif o["sysmem"]:
                if o["tex_size"] != (w, h):
                    rects = None
                mr = _DXGI_MAPPED_RECT()
                if _com(dup, 12, ctypes.POINTER(_DXGI_MAPPED_RECT))(dup, ctypes.byref(mr)) < 0:
                    return seeded
                try:
                    self._read(o, mr.pBits, mr.Pitch, rects)
                finally:
                    _com(dup, 13)(dup)                             # UnMapDesktopSurface
                o["full"] = False
                return whole if rects is None else rects
            else:
                tex = _qi(res, _IID_TEX2D)
                if tex is None:
                    return seeded
                if o["stage"] is None:
                    td = _D3D11_TEXTURE2D_DESC()
                    _com(tex, 10, ctypes.POINTER(_D3D11_TEXTURE2D_DESC), res=None)(tex, ctypes.byref(td))
                    td.MipLevels = td.ArraySize = td.SampleCount = 1
                    td.SampleQuality = td.BindFlags = td.MiscFlags = 0
                    td.Usage, td.CPUAccessFlags = 3, 0x20000            # STAGING, CPU 읽기
                    stage = _P()
                    if _com(o["dev"], 5, ctypes.POINTER(_D3D11_TEXTURE2D_DESC), _P, _PP)(
                            o["dev"], ctypes.byref(td), None, ctypes.byref(stage)) < 0:
                        raise _Unsupported("CreateTexture2D 실패")
                    o["stage"], o["tex_size"] = stage, (td.Width, td.Height)
                if rects is None or o["tex_size"] != (w, h):
                    rects = None
                    rough = rough and o["tex_size"] == (w, h)
                    _com(o["ctx"], 47, _P, _P, res=None)(o["ctx"], o["stage"], tex)  # CopyResource (GPU 에서)
                else:
                    copy = _com(o["ctx"], 46, _P, wt.UINT, wt.UINT, wt.UINT, wt.UINT, _P, wt.UINT,
                                ctypes.POINTER(_D3D11_BOX), res=None)                 # CopySubresourceRegion
                    for x0, y0, x1, y1 in rects:
                        box = _D3D11_BOX(x0, y0, 0, x1, y1, 1)
                        copy(o["ctx"], o["stage"], 0, x0, y0, 0, tex, 0, ctypes.byref(box))
        finally:
            _rel(tex)
            _rel(res)
            _com(dup, 14)(dup)                    # ReleaseFrame — 복사는 GPU 에 맡기고 바로 돌려줌
        if rects == "same":
            return self._sharpen(o) or seeded
        ok = self._map(o, lambda addr, pitch: self._read_rough(o, addr, pitch) if rough
                       else self._read(o, addr, pitch, rects))
        if not ok:
            return seeded
        o["full"] = False
        if rough:
            o["stale"] = o["stale"] or time.monotonic()
            return whole
        if rects is None:
            o["stale"] = 0.0
        return whole if rects is None else rects

    @staticmethod
    def _map(o: dict, fn) -> bool:
        """GPU 사본(stage)을 메모리에 매핑해 fn(주소, 줄 간격) — 매핑된 메모리에서 바로 변환 (중간 복사 없음)"""
        ms = _D3D11_MAPPED_SUBRESOURCE()
        t = time.perf_counter()
        hr = _com(o["ctx"], 14, _P, wt.UINT, wt.UINT, wt.UINT, ctypes.POINTER(_D3D11_MAPPED_SUBRESOURCE))(
            o["ctx"], o["stage"], 0, 1, 0, ctypes.byref(ms))            # Map(READ)
        _slept[0] += time.perf_counter() - t     # GPU 복사를 기다린 시간 — CPU 는 거의 안 씀(실측 4%)
        if hr < 0:
            return False
        try:
            fn(ms.pData, ms.RowPitch)
        finally:
            _com(o["ctx"], 15, _P, wt.UINT, res=None)(o["ctx"], o["stage"], 0)    # Unmap
        return True

    def _sharpen(self, o: dict) -> list:
        """거칠게만 읽어 둔 화면을 GPU 사본에서 선명하게 다시 읽음 (그 뒤 새 화면이 없을 때)"""
        if not o["stale"] or o["stage"] is None:
            return []
        if not self._map(o, lambda addr, pitch: self._read(o, addr, pitch, None)):
            return []
        o["stale"] = 0.0
        return [(0, 0, o["rect"][2], o["rect"][3])]

    @staticmethod
    def _read_rough(o: dict, addr: int, pitch: int):
        """k 줄마다 한 줄만 읽고 가로로 k 배 줄임 → 폭 640 안팎의 거친 화면 (통째로 읽는 것의 1/3 이하 비용)"""
        w, h = o["rect"][2], o["rect"][3]
        k = max(2, min(4, w // 640))
        raw = (ctypes.c_char * (pitch * h)).from_address(addr)
        o["rough"] = Image.frombytes("RGB", (w, h // k), raw, "raw", "BGRX", pitch * k, 1).reduce((k, 1))

    @staticmethod
    def _dirty(dup, info, w: int, h: int):
        """이번 화면에서 바뀐 곳 → 격자로 모은 사각형. 알 수 없거나 넓으면 None (통째로)"""
        n = info.TotalMetadataBufferSize
        if n <= 0:
            return None
        buf, need = ctypes.create_string_buffer(n), wt.UINT()

        def get(idx):
            return _com(dup, idx, wt.UINT, _P, ctypes.POINTER(wt.UINT))(dup, n, buf, ctypes.byref(need))
        rects = []
        if get(10) < 0:                                   # GetFrameMoveRects — 옮겨진 곳(스크롤 등)의 도착 영역
            return None
        for i in range(need.value // 24):
            rects.append(struct.unpack_from("<6i", buf, i * 24)[2:])
        if get(9) < 0:                                    # GetFrameDirtyRects
            return None
        for i in range(need.value // 16):
            rects.append(struct.unpack_from("<4i", buf, i * 16))
        rects = [(max(0, a), max(0, b), min(w, c), min(h, d)) for a, b, c, d in rects]
        rects = [r for r in rects if r[0] < r[2] and r[1] < r[3]]
        tiles = _tiles_of(rects, DIRTY_TILE, w, h)
        if len(tiles) > (-(-w // DIRTY_TILE)) * (-(-h // DIRTY_TILE)) * DIRTY_FULL:
            return None
        return _merge_tiles(tiles, DIRTY_TILE, w, h)

    @staticmethod
    def _read(o: dict, addr: int, pitch: int, rects):
        w, h = o["rect"][2], o["rect"][3]
        if rects is None:
            tw, th = o["tex_size"]
            img = _bgrx_image((ctypes.c_char * (pitch * th)).from_address(addr), tw, th, pitch)
            if (tw, th) != (w, h):
                img = img.resize((w, h), _RESAMPLE)
            if o["img"] is not None and o["img"].size == (w, h):
                o["img"].paste(img)          # 같은 이미지에 덮어씀 → 보는 쪽 기록이 끊기지 않음
            else:
                o["img"] = img
            return
        for x0, y0, x1, y1 in rects:
            o["img"].paste(_bgrx_at(addr, pitch, x0, y0, x1 - x0, y1 - y0), (x0, y0))

    # ── 영역 ──
    def grab(self, rect: tuple, sig) -> tuple:
        """(화면, 바뀐 사각형 [(x0, y0, x1, y1)] — rect 기준, 그대로면 []). 이 PC 에서 복제를 못 쓰면 _Unsupported.
        화면 이미지는 제자리에서 고쳐지므로 호출하는 쪽 잠금 안에서만 읽을 것"""
        if sig != self._sig or not self._outs:
            self._build(sig)
        x, y, w, h = rect
        parts = [o for o in self._outs
                 if o["rect"][0] < x + w and x < o["rect"][0] + o["rect"][2]
                 and o["rect"][1] < y + h and y < o["rect"][1] + o["rect"][3]]
        if not parts:
            self._sig = None                     # 모니터 구성이 바뀜 → 다음에 다시 만듦
            raise OSError("화면을 찾을 수 없습니다")
        if any(o["rot"] not in (0, 1) for o in parts):
            raise _Unsupported("회전된 모니터")
        self.note = ""
        single = len(parts) == 1 and parts[0]["rect"] == rect
        fresh = self._last_rect != rect
        changes = [(o, self._grab_out(o, rough_ok=single and not fresh)) for o in parts]
        whole = [(0, 0, w, h)]
        self._last_rect = rect
        if single and parts[0]["img"] is not None:
            self._comp = None                    # 모니터 하나 → 복사 없이 그대로
            rough, parts[0]["rough"] = parts[0]["rough"], None
            return parts[0]["img"], (whole if fresh else changes[0][1]), rough
        comp = self._comp
        if fresh or comp is None or comp.size != (w, h):
            comp = self._comp = Image.new("RGB", (w, h))
            fresh = True
        dirty = []
        for o, d in changes:
            if o["img"] is None:
                continue
            ox, oy = o["rect"][0] - x, o["rect"][1] - y
            for x0, y0, x1, y1 in ([(0, 0, o["rect"][2], o["rect"][3])] if fresh else d):
                a, b, c, e = max(0, x0 + ox), max(0, y0 + oy), min(w, x1 + ox), min(h, y1 + oy)
                if a < c and b < e:
                    comp.paste(o["img"].crop((a - ox, b - oy, c - ox, e - oy)), (a, b))
                    dirty.append((a, b, c, e))
        return comp, (whole if fresh else dirty), None


class _Screen:
    """DXGI 를 먼저 쓰고, 못 쓰면 GDI 로 (띠로 나눠 천천히 — 한 장에 0.5초 정도)"""
    GDI_INTERVAL = 0.5

    def __init__(self, log):
        self._log = log
        self._dxgi = _DesktopDup()
        self._gdi_only = False
        self._gdi_prev = None    # (rect, [띠별 원시 데이터], 이미지)

    @property
    def gdi(self) -> bool:
        return self._gdi_only

    @property
    def note(self) -> str:
        return "" if self._gdi_only else self._dxgi.note

    def close(self):
        self._dxgi.close()
        self._gdi_prev = None
        self._gdi_only = False   # 다음 세션에서 DXGI 다시 시도

    def grab(self, rect: tuple, sig) -> tuple:
        """(화면, 바뀐 사각형 목록, 거친 화면 또는 None) — _DesktopDup.grab 과 같음"""
        if not self._gdi_only:
            try:
                return self._dxgi.grab(rect, sig)
            except _Unsupported as e:
                self._dxgi.close()
                self._gdi_only = True
                self._log(f"🖥 원격: 빠른 화면 복제를 쓸 수 없어 호환 방식으로 봅니다 ({e})")
        strips = _grab_strips(rect)
        w, h = rect[2], rect[3]
        prev = self._gdi_prev
        if prev is None or prev[0] != rect or len(prev[1]) != len(strips):
            img = _bgrx_image(b"".join(s[2] for s in strips), w, h)
            self._gdi_prev = (rect, [s[2] for s in strips], img)
            return img, [(0, 0, w, h)], None
        dirty = []
        for i, (top, n, raw) in enumerate(strips):       # 바뀐 띠만 바꿔 끼움
            if raw != prev[1][i]:
                prev[2].paste(_bgrx_image(raw, w, n), (0, top))
                prev[1][i] = raw
                dirty.append((0, top, w, top + n))
        return prev[2], dirty, None


def _jpeg(im: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _pack(meta: dict, jpeg: bytes = b"") -> bytes:
    """[메타 JSON 길이 4바이트][메타 JSON][JPEG] — 헤더를 안 쓰므로 옛 중계 PC 도 그대로 통과"""
    mj = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(mj)) + mj + jpeg


def _unpack(body: bytes) -> tuple:
    n = struct.unpack(">I", body[:4])[0]
    return json.loads(body[4:4 + n].decode("utf-8")), body[4 + n:]


def _qint(q: dict, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(max(int(q.get(name, default)), lo), hi)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────
# 장비 쪽
# ─────────────────────────────────────────
class RemoteHost:
    MIN_INTERVAL = 1 / 20   # 캡처 최소 간격 (초)
    DUTY         = 4.0      # 캡처·압축에 걸린 시간의 이 배수만큼 쉼 → 한 코어의 1/5 이하 (Radmin·원격 데스크톱 수준)
    HISTORY      = 300      # 바뀐 영역 기록 개수 (보는 쪽이 이보다 뒤처지면 통째로 보냄)
    IDLE_INTERVAL = 0.3     # 화면이 그대로일 때 캡처 간격
    MAX_FRESH_GAP = 1.0     # 처음 여는 캡처(준비 비용 포함) 뒤 쉬는 시간 상한
    ROUGH_Q      = 45       # 화면 전체가 바뀌는 동안 보내는 거친 화면의 화질 상한
    IDLE_RELEASE = 3.0      # 입력 소식이 이만큼 없으면 눌린 키·버튼을 뗌
    SESSION_END  = 15.0     # 보는 쪽 소식이 이만큼 없으면 세션 종료
    MAX_EVENTS   = 200      # 입력 요청 1개에 담을 수 있는 최대 이벤트
    LOCK_WAIT    = 2.0      # 앞 요청 처리 대기 한도 — 넘으면 기다리지 않고 바로 응답 (스레드가 쌓이지 않게)

    def __init__(self, log=None):
        self._log = log or (lambda msg: None)
        self._cap_lock = threading.Lock()    # 캡처·압축은 한 번에 하나
        self._in_lock  = threading.Lock()    # 입력은 한 번에 하나 (순서 보장)
        self._lock     = threading.Lock()    # 세션 목록
        self._sessions = {}
        self._watchdog = None
        self._screen   = _Screen(self._log)  # _cap_lock 안에서만 사용
        self._frame    = None                # {"rect", "img", "seq"}
        self._jpeg     = {}                  # (seq, last, 크기, 화질) → 보낼 조각 — 현재 화면 것만
        self._hist     = []                  # [(seq, 바뀐 사각형들)] — 보는 쪽 화면 위에 붙일 조각 계산용
        self._hist_base = 0                  # 이 번호 이후의 변화는 모두 _hist 에 있음
        self._streams  = {}                  # (영역, 줄인 크기) → 줄인 화면 (바뀐 칸만 다시 줄임)
        self._seq      = int(time.time() * 1000)   # 클라이언트를 다시 켜도 번호가 겹치지 않게
        self._next_cap = 0.0
        self._mons     = (0.0, [])

    # ── 세션 ─────────────────────────────
    def _session(self, q: dict, who: str) -> dict:
        sid = str(q.get("sid", ""))[:40] or "-"
        who = f"{str(q.get('who', ''))[:40]} ({who})" if q.get("who") else who
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                s = self._sessions[sid] = {"who": who, "seen": time.monotonic(), "in_seen": 0.0,
                                           "n": -1, "keys": set(), "btns": set(), "ctl": False,
                                           "clip_mark": None}
                self._log(f"🖥 원격 보기 시작: {who}")
                if self._watchdog is None:
                    self._watchdog = threading.Thread(target=self._watch, daemon=True)
                    self._watchdog.start()
            s["seen"] = time.monotonic()
            return s

    def _watch(self):
        while True:
            time.sleep(0.5)
            now = time.monotonic()
            with self._lock:
                items = list(self._sessions.items())
            for sid, s in items:
                if (s["keys"] or s["btns"]) and now - max(s["in_seen"], 0.0) > self.IDLE_RELEASE:
                    if self._release_now(s):
                        self._log(f"⚠ 원격 입력이 끊겨 눌려 있던 키·버튼을 뗐습니다: {s['who']}")
                if now - s["seen"] > self.SESSION_END:
                    self._close(sid, "응답 없음")
            with self._lock:
                idle = not self._sessions
            # 보는 쪽이 모두 떠나면 화면 복제·메모리 반납 (캡처 중이면 다음 바퀴에)
            if idle and self._cap_lock.acquire(timeout=self.LOCK_WAIT):
                try:
                    with self._lock:
                        if self._sessions:
                            continue
                        self._watchdog = None
                    self._screen.close()
                    self._frame, self._jpeg, self._hist, self._streams = None, {}, [], {}
                    return
                finally:
                    self._cap_lock.release()

    def _release_now(self, s: dict) -> bool:
        if not self._in_lock.acquire(timeout=self.LOCK_WAIT):
            return False
        try:
            inputs = []
            self._release(s, inputs, keep_keys=(), keep_btns=())
            self._send(inputs)
            return True
        finally:
            self._in_lock.release()

    def _close(self, sid: str, why: str = ""):
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s is None:
            return
        self._release_now(s)
        self._log(f"🖥 원격 종료: {s['who']}" + (f" ({why})" if why else ""))

    def end(self, q: dict, who: str) -> dict:
        self._close(str(q.get("sid", ""))[:40] or "-")
        return {"ok": True}

    # ── 화면 ─────────────────────────────
    def _monitor_list(self) -> list:
        t, mons = self._mons
        if time.monotonic() - t > 2.0 or not mons:
            mons = _monitors()
            self._mons = (time.monotonic(), mons)
        return mons

    def _rect(self, mon: int, mons: list) -> tuple:
        if 0 <= mon < len(mons):
            return tuple(mons[mon][:4])
        return _virtual_screen()

    def _capture(self, rect: tuple, mons: list) -> dict:
        """직전 캡처 후 쉬는 시간이 안 지났으면 직전 화면을 그대로 씀 (보는 쪽이 여럿이어도 캡처는 한 번)"""
        fr = self._frame
        if fr and fr["rect"] == rect and time.monotonic() < self._next_cap:
            return fr
        t0, s0 = time.perf_counter(), _slept[0]
        with low_priority(-2):
            img, dirty, rough = self._screen.grab(rect, tuple(mons))
        same = bool(fr) and fr["rect"] == rect and fr["img"] is img and not dirty
        fresh = not fr or fr["rect"] != rect or fr["img"] is not img
        if fresh:
            # 처음이거나 영역이 바뀜 → 기록을 새로 시작 (보는 쪽은 통째로 받음)
            self._seq += 1
            fr = self._frame = {"rect": rect, "img": img, "seq": self._seq, "rough": rough}
            self._hist, self._hist_base, self._streams, self._jpeg = [], self._seq, {}, {}
        elif dirty:
            # rough 가 있으면 이 번호의 화면은 거친 화면뿐 (img 는 아직 옛 화면) — 다음 번호에서 통째로 선명하게
            self._seq += 1
            fr["seq"], fr["rough"] = self._seq, rough
            self._hist.append((self._seq, dirty))
            if len(self._hist) > self.HISTORY:
                self._hist_base = self._hist.pop(0)[0]
            self._jpeg = {}
        cost = time.perf_counter() - t0
        if self._screen.gdi:
            # GDI 는 띠 사이 쉬는 시간이 cost 에 들어 있음 → 배수 없이 그만큼 더 쉼
            gap = max(self._screen.GDI_INTERVAL, cost * (2 if same else 1))
        else:
            # 화면이 그대로면 더 드물게 확인 (DXGI 는 안 바뀌었으면 거의 비용 없음).
            # 띠 사이에 쉬거나 GPU 복사를 기다린 시간은 CPU 를 안 쓰므로 뺌. 처음 여는 준비 비용(한 번뿐)은 1초까지만 쉼
            cost -= _slept[0] - s0
            if fresh:
                cost = min(cost, self.MAX_FRESH_GAP / self.DUTY)
            gap = max(self.IDLE_INTERVAL if same else self.MIN_INTERVAL, cost * self.DUTY)
        self._next_cap = time.monotonic() + gap
        return fr

    def _dirty_since(self, seq: int) -> list:
        return [r for s, rs in self._hist if s > seq for r in rs]

    def _tiles(self, fr: dict, ss: tuple, since: int):
        """seq 이후 바뀐 곳 → 줄인 화면(ss) 기준 격자 사각형. 너무 넓으면 None (통째로가 쌈)"""
        w, h = fr["img"].size
        tiles = _tiles_of(self._dirty_since(since), PATCH_TILE, ss[0], ss[1],
                          w / ss[0], h / ss[1], margin=2)   # 축소 보간이 주변 1~2 픽셀에 번짐
        if len(tiles) > (-(-ss[0] // PATCH_TILE)) * (-(-ss[1] // PATCH_TILE)) * DIRTY_FULL:
            return None
        return _merge_tiles(tiles, PATCH_TILE, ss[0], ss[1])

    def _stream(self, fr: dict, ss: tuple) -> Image.Image:
        """보는 쪽 창 크기(ss)로 줄인 화면 — 바뀐 칸만 다시 줄여 최신으로 유지"""
        key = (fr["rect"], ss)
        st = self._streams.get(key)
        rects = None
        if st is not None and st["seq"] >= self._hist_base:
            rects = [] if st["seq"] == fr["seq"] else self._tiles(fr, ss, st["seq"])
        if rects is None:
            if st is None and len(self._streams) >= 2:     # 창 크기가 다른 보는 쪽이 여럿 → 오래된 것 버림
                self._streams.pop(min(self._streams, key=lambda k: self._streams[k]["used"]))
            st = self._streams[key] = {"img": _scaled_part(fr["img"], ss, 0, 0, *ss)}
        else:
            for r in rects:
                st["img"].paste(_scaled_part(fr["img"], ss, *r), r[:2])
        st["seq"], st["used"] = fr["seq"], time.monotonic()
        return st["img"]

    def _encode(self, fr: dict, ss: tuple, qual: int, last: int) -> tuple:
        """(조각 목록 [[x, y, w, h, 길이]], JPEG 들, 기준 번호 또는 None=통째로)"""
        img = self._stream(fr, ss)
        rects = self._tiles(fr, ss, last) if self._hist_base <= last < fr["seq"] else None
        if rects is not None:
            parts, chunks = [], []
            for r in rects:
                j = _jpeg(img.crop(r), qual)
                parts.append([r[0], r[1], r[2] - r[0], r[3] - r[1], len(j)])
                chunks.append(j)
            return parts, b"".join(chunks), last
        j = _jpeg(img, qual)
        return [[0, 0, ss[0], ss[1], len(j)]], j, None

    def frame(self, q: dict, who: str) -> bytes:
        self._session(q, who)
        mon  = _qint(q, "mon", -1, -1, 32)
        box  = (_qint(q, "w", 1280, 64, 7680), _qint(q, "h", 720, 64, 4320))
        qual = _qint(q, "q", 60, 20, 90)
        last = _qint(q, "last", -1, -1, 1 << 62)
        meta = {}
        if not self._cap_lock.acquire(timeout=self.LOCK_WAIT):
            meta["busy"] = True
            return _pack(meta)
        try:
            with _physical_pixels():
                mons = self._monitor_list()
                rect = self._rect(mon, mons)
                meta.update(mons=[list(m) for m in mons], rect=list(rect), cursor=self._cursor(rect))
                fr = self._capture(rect, mons)
                meta["seq"] = fr["seq"]
                if self._screen.note:
                    meta["note"] = self._screen.note
                if self._screen.gdi:
                    meta["gdi"] = True
                if fr["seq"] == last:
                    meta["next"] = self._until_next()
                    return _pack(meta)                    # 화면 그대로 → 압축·전송 생략
                ss = _fit(fr["img"].size, box)
                rough = fr.get("rough")
                small = rough is not None and q.get("rough") == "1"   # 보는 쪽이 거친 화면을 직접 키울 수 있음
                key = (fr["seq"], -2 if rough is not None else last, None if small else ss, qual)
                hit = self._jpeg.get(key)
                if hit is None:
                    t0 = time.perf_counter()
                    with low_priority(-2):
                        if rough is not None:
                            im = rough if small else rough.resize(ss, Image.BILINEAR)
                            j = _jpeg(im, min(qual, self.ROUGH_Q))
                            hit = [[0, 0, im.width, im.height, len(j)]], j, None
                        else:
                            hit = self._encode(fr, ss, qual, last)
                    if len(self._jpeg) >= 8:
                        self._jpeg = {}
                    self._jpeg[key] = hit
                    spent = (time.perf_counter() - t0) * self.DUTY
                    if not self._hist:                     # 처음 한 장(압축기 준비 비용 포함)
                        spent = min(spent, self.MAX_FRESH_GAP)
                    self._next_cap += spent
                meta["size"] = list(ss)
                meta["parts"] = hit[0]
                if rough is not None:
                    meta["rough"] = 1                     # 잠깐 보여 줄 거친 화면 — 곧 선명한 화면이 옴
                if hit[2] is not None:
                    meta["base"] = hit[2]                 # 보는 쪽이 가진 이 번호 화면 위에 붙일 조각
                meta["next"] = self._until_next()
                return _pack(meta, hit[1])
        except OSError as e:
            meta["err"] = str(e)
            return _pack(meta)
        except Exception as e:                        # 예상 못 한 오류 → 캡처를 처음부터 다시 준비
            self._screen.close()
            self._frame = None
            self._log(f"⚠ 원격 화면 캡처 오류: {e!r}")
            meta["err"] = "장비 화면 캡처 오류 — 다시 시도 중"
            return _pack(meta)
        finally:
            self._cap_lock.release()

    def _until_next(self) -> float:
        """다음 캡처까지 남은 초 — 보는 쪽이 그때 맞춰 요청하도록 (헛걸음 줄임)"""
        return round(min(max(self._next_cap - time.monotonic(), 0.0), 1.0), 3)

    @staticmethod
    def _cursor(rect: tuple):
        pt = wt.POINT()
        if not _GetCursorPos(ctypes.byref(pt)):
            return None
        x, y, w, h = rect
        if not (x <= pt.x < x + w and y <= pt.y < y + h):
            return None
        return [round((pt.x - x) / max(1, w - 1), 4), round((pt.y - y) / max(1, h - 1), 4)]

    def clip(self, q: dict, who: str) -> dict:
        """장비에서 복사한 글자 — 복사 직전 기억한 번호에서 바뀌었을 때만 돌려줌 (아직이면 wait)"""
        s = self._session(q, who)
        mark = s["clip_mark"]
        if mark is None:
            return {"ok": True, "none": True}
        if _GetClipboardSeq() == mark:
            return {"ok": True, "wait": True}
        s["clip_mark"] = None
        try:
            text = _clip_get()
        except OSError as e:
            return {"ok": False, "err": str(e)}
        if text is None:
            return {"ok": True, "none": True}               # 글자가 아닌 것(그림·파일)을 복사함
        return {"ok": True, "text": text}

    # ── 입력 ─────────────────────────────
    def input(self, q: dict, who: str) -> dict:
        s = self._session(q, who)
        try:
            events = json.loads(q.get("ev", "[]"))
            n = int(q.get("n", "0"))
        except (TypeError, ValueError):
            return {"ok": False, "err": "잘못된 입력 데이터"}
        if not isinstance(events, list):
            return {"ok": False, "err": "잘못된 입력 데이터"}
        mon = _qint(q, "mon", -1, -1, 32)
        if not self._in_lock.acquire(timeout=self.LOCK_WAIT):
            return {"ok": False, "err": "장비가 입력을 처리하지 못했습니다 (바쁨)"}
        try:
            s["in_seen"] = time.monotonic()
            if n <= s["n"]:
                return {"ok": True, "dup": True}         # 이미 처리한 요청 → 다시 실행하지 않음
            s["n"] = n
            inputs = []
            with _physical_pixels():
                rect = self._rect(mon, self._monitor_list())
                vs = _virtual_screen()
                for ev in events[:self.MAX_EVENTS]:
                    try:
                        self._build(ev, s, rect, vs, inputs)
                    except (TypeError, ValueError, IndexError):
                        continue                          # 잘못된 이벤트 하나는 건너뜀
                if inputs and not s["ctl"]:
                    s["ctl"] = True
                    self._log(f"🖱 원격 조작 시작: {s['who']}")
                sent = self._send(inputs)
            if sent < len(inputs):
                return {"ok": False, "err": "장비가 입력을 거부했습니다"}
            return {"ok": True}
        finally:
            self._in_lock.release()

    @staticmethod
    def _to_abs(nx, ny, rect, vs) -> tuple:
        """0~65535 (화면 영역 기준) → SendInput 절대 좌표 (가상 데스크톱 전체 기준 0~65535)"""
        nx = min(max(int(nx), 0), 65535)
        ny = min(max(int(ny), 0), 65535)
        px = rect[0] + round(nx * (rect[2] - 1) / 65535)
        py = rect[1] + round(ny * (rect[3] - 1) / 65535)
        return (round((px - vs[0]) * 65535 / max(1, vs[2] - 1)),
                round((py - vs[1]) * 65535 / max(1, vs[3] - 1)))

    @staticmethod
    def _mouse(inputs, flags, x=0, y=0, data=0):
        i = _INPUT(type=_INPUT_MOUSE)
        i.u.mi = _MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, _EXTRA)
        inputs.append(i)

    @staticmethod
    def _key(inputs, vk, down):
        flags = (0 if down else _KEYUP) | (_EXTENDED if vk in _EXTENDED_VK else 0)
        i = _INPUT(type=_INPUT_KEYBOARD)
        i.u.ki = _KEYBDINPUT(vk, _MapVirtualKeyW(vk, 0) & 0xFFFF, flags, 0, _EXTRA)
        inputs.append(i)

    def _build(self, ev, s, rect, vs, inputs):
        kind = ev[0]
        move = _MOVE | _ABSOLUTE | _VIRTUALDESK
        if kind == "m":
            self._mouse(inputs, move, *self._to_abs(ev[1], ev[2], rect, vs))
        elif kind in ("d", "u"):
            b = int(ev[1])
            if b not in _BTN_FLAGS:
                return
            if kind == "u" and b not in s["btns"]:
                return                                   # 누른 적 없는 버튼은 떼지 않음
            self._mouse(inputs, move | _BTN_FLAGS[b][kind == "u"], *self._to_abs(ev[2], ev[3], rect, vs))
            (s["btns"].add if kind == "d" else s["btns"].discard)(b)
        elif kind == "w":
            delta = min(max(int(ev[1]), -1200), 1200)
            self._mouse(inputs, move, *self._to_abs(ev[2], ev[3], rect, vs))
            self._mouse(inputs, _WHEEL, data=delta)
        elif kind == "k":
            vk, down = int(ev[1]), bool(ev[2])
            if not 1 <= vk <= 254 or vk in _MOUSE_VK:
                return
            if not down and vk not in s["keys"]:
                return
            self._key(inputs, vk, down)
            (s["keys"].add if down else s["keys"].discard)(vk)
        elif kind == "s":        # 보는 쪽 기준으로 지금 눌려 있어야 할 것 외에는 모두 뗌
            self._release(s, inputs, keep_keys={int(k) for k in ev[1]}, keep_btns={int(b) for b in ev[2]})
        elif kind == "r":
            self._release(s, inputs, keep_keys=(), keep_btns=())
        elif kind == "c":        # 붙여넣기 직전 — 보는 쪽 클립보드 글자를 장비 클립보드에
            text = str(ev[1])[:CLIP_MAX]
            try:
                _clip_set(text)
                self._log(f"📋 원격 클립보드 받음 ({len(text)}자): {s['who']}")
            except OSError as e:
                self._log(f"⚠ 원격 클립보드 받기 실패: {e}")
        elif kind == "g":        # 복사 직전 — 지금 클립보드 번호를 기억 → 바뀌면 /remote/clip 이 돌려줌
            s["clip_mark"] = _GetClipboardSeq()

    def _release(self, s, inputs, keep_keys, keep_btns):
        for b in sorted(s["btns"] - set(keep_btns)):
            self._mouse(inputs, _BTN_FLAGS[b][1])
            s["btns"].discard(b)
        for vk in sorted(s["keys"] - set(keep_keys)):
            self._key(inputs, vk, False)
            s["keys"].discard(vk)

    @staticmethod
    def _send(inputs) -> int:
        if not inputs:
            return 0
        arr = (_INPUT * len(inputs))(*inputs)
        return _SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


# ─────────────────────────────────────────
# 보는 쪽
# ─────────────────────────────────────────
class RemoteViewer:
    QUALITY     = {"낮음": (45, 8), "보통": (60, 15), "높음": (75, 20)}   # (JPEG 화질, 최대 fps)
    STALE       = 1.0    # 이보다 늦게 보내게 된 클릭·키 누름은 버림
    FRAME_STALE = 2.5    # 화면이 이만큼 갱신 안 되면 조작 잠금
    INPUT_HZ    = 30     # 입력 요청 최대 횟수 (초당)
    HEARTBEAT   = 1.0    # 누르고 있는 동안 이 간격으로 장비에 알림
    CLIP_SEND   = 30000  # 붙여넣기 때 보낼 클립보드 글자 상한 (JSON 길이 — 요청 주소 길이 제한 안쪽)
    ACTUAL_BOX  = (7680, 4320)   # 실제 크기 보기 — 장비가 줄이지 않고 보냄
    COMBOS      = [("Alt+Tab (창 전환)", (0xA4, 0x09)), ("Win (시작 메뉴)", (0x5B,)),
                   ("Win+D (바탕 화면)", (0x5B, 0x44)), ("Win+R (실행)", (0x5B, 0x52)),
                   ("Alt+F4 (창 닫기)", (0xA4, 0x73)), ("Ctrl+Shift+Esc (작업 관리자)", (0xA2, 0xA0, 0x1B)),
                   ("Print Screen", (0x2C,))]
    BG, FG, DIM = "#1D2327", "#D5DADD", "#8A949A"

    def __init__(self, root, title: str, request, px=lambda v: v, quality: str = "보통",
                 on_quality=None, on_closed=None, font=("맑은 고딕", 9), side=None, side_width=0):
        import tkinter as tk
        from tkinter import ttk
        from PIL import ImageTk
        self._tk, self._ImageTk = tk, ImageTk
        self._request    = request
        self._on_quality = on_quality
        self._on_closed  = on_closed
        self._who        = socket.gethostname()
        self.sid         = uuid.uuid4().hex[:16]
        self.alive       = True

        # 화면 스레드 ↔ UI
        self._want       = (1280, 720)
        self._mon        = -1
        self._quality    = quality if quality in self.QUALITY else "보통"
        self._frame_lock = threading.Lock()
        self._ops        = []            # UI 가 그릴 작업 (통째 또는 조각) — 화면 스레드가 쌓음
        self._meta       = None
        self._base       = None          # 받은 화면 (화면 스레드 전용) — 조각을 이 위에 붙임
        self._base_seq   = -1
        self._ui_pending = False
        self._photo      = None
        self._box        = None          # 캔버스 안 화면 위치 (x, y, w, h)
        self._frame_time = 0.0
        self._mons       = None
        self._shown      = []            # 최근 표시 시각 (fps 계산)
        self._net        = ""
        self._error      = ""
        # 입력 (UI → 입력 스레드)
        self._cv         = threading.Condition()
        self._events     = []            # [(시각, 이벤트)]
        self._keys       = set()         # 보는 쪽 기준 눌려 있는 키·버튼
        self._btns       = set()
        self._input_err  = ""
        self._note       = ""            # 장비가 알려준 상황 (보안 화면 등) — 있으면 조작 잠금
        self._gdi        = False         # 장비가 호환 캡처(GDI)로 동작 중
        self._lost_since = 0.0           # 연결이 끊긴 시각 (0 = 연결됨)
        self._actual     = False         # 실제 크기(1:1) 보기
        self._view       = (1280, 720)   # 캔버스 크기
        self._notice     = ("", 0.0)     # 잠깐 보여 줄 안내 (글자, 사라질 시각)
        self._clip_sent  = None          # 마지막으로 장비에 보낸(또는 장비에서 받은) 클립보드 글자
        self._clip_busy  = False

        w = self.win = tk.Toplevel(root)
        w.title(title)
        w.configure(bg=self.BG)
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"{min(px(1180), sw - 80)}x{min(px(760), sh - 120)}")
        w.protocol("WM_DELETE_WINDOW", self.close)
        self._px       = px
        self._hwnd     = None        # Windows 바깥 창 (제목 표시줄을 없앤 뒤)
        self._wndproc  = None        # (새 창 처리 함수, 원래 함수)
        self._full     = False
        self._restore  = None        # 전체 화면 전 창 위치 (x, y, w, h)
        self._build_title(title, font)

        bar = tk.Frame(w, bg=self.BG)
        bar.pack(fill="x", padx=px(10), pady=px(6))
        self._ctl = tk.BooleanVar(value=False)   # 기본은 보기 전용 — 실수로 장비를 누르지 않도록
        tk.Checkbutton(bar, text="조작", variable=self._ctl, command=self._ctl_changed,
                       bg=self.BG, fg=self.FG, selectcolor=self.BG, activebackground=self.BG,
                       activeforeground=self.FG, font=font).pack(side="left")
        tk.Label(bar, text="화면", bg=self.BG, fg=self.DIM, font=font).pack(side="left", padx=(px(12), px(4)))
        self._mon_var = tk.StringVar(value="전체")
        self._mon_cb = ttk.Combobox(bar, textvariable=self._mon_var, values=["전체"], state="readonly",
                                    width=12, font=font)
        self._mon_cb.pack(side="left")
        self._mon_cb.bind("<<ComboboxSelected>>", self._mon_changed)
        tk.Label(bar, text="화질", bg=self.BG, fg=self.DIM, font=font).pack(side="left", padx=(px(12), px(4)))
        self._q_var = tk.StringVar(value=self._quality)
        q_cb = ttk.Combobox(bar, textvariable=self._q_var, values=list(self.QUALITY), state="readonly",
                            width=5, font=font)
        q_cb.pack(side="left")
        q_cb.bind("<<ComboboxSelected>>", self._q_changed)
        self._actual_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="실제 크기", variable=self._actual_var, command=self._actual_changed,
                       bg=self.BG, fg=self.FG, selectcolor=self.BG, activebackground=self.BG,
                       activeforeground=self.FG, font=font).pack(side="left", padx=(px(12), 0))
        keys = tk.Menubutton(bar, text="키 보내기 ▾", bg=self.BG, fg=self.FG, activebackground="#2C3439",
                             activeforeground=self.FG, relief="flat", font=font)
        menu = tk.Menu(keys, tearoff=0, font=font)
        for label, vks in self.COMBOS:
            menu.add_command(label=label, command=lambda v=vks: self._combo(v))
        keys["menu"] = menu
        keys.pack(side="left", padx=(px(8), 0))
        # 버튼이 많아 창이 좁으면 글자가 잘림 → 앞부분(중요한 말)이 보이게 왼쪽 정렬
        self._status = tk.Label(bar, text="연결 중…", bg=self.BG, fg=self.DIM, font=font, anchor="w")
        self._status.pack(side="left", fill="x", expand=True, padx=(px(16), 0))
        # 창을 좁혀도 위쪽 버튼이 잘리지 않게 (상태 글자 몫은 조금만 남김)
        w.update_idletasks()
        w.minsize(max(px(480), bar.winfo_reqwidth() - self._status.winfo_reqwidth() + px(140)), px(320))

        # 오른쪽 곁 패널 (반입/반출 등) — side(frame) 이 내용을 채움. 전체 화면에서는 숨김
        self._side = None
        if side:
            self._side = tk.Frame(w, bg=self.BG, width=side_width)
            self._side_pad = px(8)
            self._side.pack(side="right", fill="y", padx=(0, px(8)), pady=(0, px(8)))
            self._side.pack_propagate(False)
            side(self._side)
            w.geometry(f"{min(px(1180) + side_width, sw - 80)}x{min(px(760), sh - 120)}")
        area = self._area = tk.Frame(w, bg=self.BG)
        area.pack(fill="both", expand=True)
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        c = self.canvas = tk.Canvas(area, bg="#101315", highlightthickness=0, takefocus=1)
        c.grid(row=0, column=0, sticky="nsew")
        self._vbar = ttk.Scrollbar(area, orient="vertical", command=c.yview)
        self._hbar = ttk.Scrollbar(area, orient="horizontal", command=c.xview)
        c.configure(yscrollcommand=self._vbar.set, xscrollcommand=self._hbar.set)
        self._img_item = c.create_image(0, 0, anchor="nw")
        self._cur_item = c.create_oval(0, 0, 0, 0, outline="#FF5A4F", width=2, state="hidden")
        self._msg_item = c.create_text(0, 0, text="연결 중…", fill=self.FG, font=font)
        c.bind("<Configure>", self._resized)
        c.bind("<Motion>", self._on_motion)
        c.bind("<ButtonPress>", self._on_press)
        c.bind("<ButtonRelease>", self._on_release)
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<KeyPress>", self._on_key_down)
        c.bind("<KeyRelease>", self._on_key_up)
        c.bind("<Enter>", lambda e: self._ctl.get() and c.focus_set())
        w.bind("<FocusOut>", self._on_focus_out)
        self._disable_ime()

        self._frameless()

        threading.Thread(target=self._frame_loop, daemon=True).start()
        threading.Thread(target=self._input_loop, daemon=True).start()
        self._tick()

    # ── 제목 줄 (Windows 제목 표시줄 대신) ──
    TITLE_BG, HOVER_BG, CLOSE_BG = "#15191C", "#2C3439", "#C42B1C"

    def _build_title(self, title: str, font):
        """─ 최소화 · ⛶ 전체 화면 · ✕ 닫기. 최대화 대신 전체 화면만 — 최대화를 전체 화면으로 착각하지 않도록"""
        tk, px = self._tk, self._px
        try:
            from tkinter import font as tkfont
            icons = "Segoe MDL2 Assets" in tkfont.families(self.win)
        except Exception:
            icons = False
        ifont = ("Segoe MDL2 Assets", 8) if icons else font
        # (아이콘 글꼴 글자, 대체 글자)
        self._glyph = {k: (g if icons else t) for k, g, t in (
            ("min", "\uE921", "─"), ("full", "\uE740", "□"), ("back", "\uE73F", "❐"), ("close", "\uE8BB", "✕"))}

        tb = self._title_bar = tk.Frame(self.win, bg=self.TITLE_BG, height=px(32))
        tb.pack(fill="x")
        tb.pack_propagate(False)
        if icons:
            tk.Label(tb, text="\uE7F4", font=("Segoe MDL2 Assets", 10), fg="#4FA3FF",
                     bg=self.TITLE_BG).pack(side="left", padx=(px(12), px(8)))
        name = title.split("—", 1)[-1].strip()
        lbl = tk.Label(tb, text=f"원격  ·  {name}", font=font, fg=self.FG, bg=self.TITLE_BG, anchor="w")
        lbl.pack(side="left", fill="x", expand=True, padx=(0 if icons else px(12), 0))

        def btn(key, cmd, hover, tip):
            b = tk.Label(tb, text=self._glyph[key], font=ifont, fg=self.FG, bg=self.TITLE_BG, width=6,
                         cursor="hand2")
            b.bind("<Enter>", lambda e: b.configure(bg=hover, fg="white"))
            b.bind("<Leave>", lambda e: b.configure(bg=self.TITLE_BG, fg=self.FG))
            b.bind("<ButtonRelease-1>", lambda e: 0 <= e.x < b.winfo_width() and 0 <= e.y < b.winfo_height()
                   and cmd())
            b.pack(side="right", fill="y")
            self._tip(b, tip)
            return b

        btn("close", self.close, self.CLOSE_BG, lambda: "닫기")
        self._full_btn = btn("full", self._toggle_full, self.HOVER_BG,
                             lambda: "창 모드로" if self._full else "전체 화면")
        btn("min", self._minimize, self.HOVER_BG, lambda: "최소화")
        # 끌어서 옮기기 · 두 번 클릭 = 전체 화면 (빈 곳과 제목 글자)
        for wdg in (tb, lbl) + tuple(c for c in tb.winfo_children() if c is not lbl and not c.cget("cursor")):
            wdg.bind("<ButtonPress-1>", self._title_press)
            wdg.bind("<Double-Button-1>", lambda e: self._toggle_full())
            wdg.bind("<Motion>", self._title_motion)

    def _tip(self, widget, text):
        """잠깐 올려 두면 뜨는 작은 설명"""
        tk = self._tk
        state = {"job": None, "win": None}

        def show():
            state["job"] = None
            t = state["win"] = tk.Toplevel(widget)
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            tk.Label(t, text=text(), bg="#2B3135", fg=self.FG, padx=self._px(8), pady=self._px(3),
                     font=("맑은 고딕", 9)).pack()
            t.update_idletasks()
            x = widget.winfo_rootx() + widget.winfo_width() - t.winfo_reqwidth()
            t.geometry(f"+{max(x, widget.winfo_rootx() - t.winfo_reqwidth())}+"
                       f"{widget.winfo_rooty() + widget.winfo_height() + self._px(4)}")

        def hide(_e=None):
            if state["job"]:
                widget.after_cancel(state["job"])
                state["job"] = None
            if state["win"]:
                state["win"].destroy()
                state["win"] = None

        widget.bind("<Enter>", lambda e: (hide(), state.update(job=widget.after(500, show))), add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<ButtonPress>", hide, add="+")

    def _frameless(self):
        """Windows 제목 표시줄을 없앰 — 크기 조절 테두리(왼·오른·아래)·작업 표시줄·최소화는 남기고,
        최대화는 막음 (끌어서 화면 위에 붙여도 최대화되지 않음). 위쪽 테두리는 제목 줄 맨 위에서 대신 처리"""
        try:
            self.win.update_idletasks()
            hwnd = _GetParent(self.win.winfo_id())
            if not hwnd:
                return
            self._hwnd = hwnd

            def proc(h, msg, wp, lp):
                if msg == _WM_NCCALCSIZE and wp:
                    # 기본 계산(테두리 제외)을 쓰되 위쪽만 창 끝까지 → 위쪽 흰 띠 없음
                    rc = ctypes.cast(lp, ctypes.POINTER(wt.RECT))
                    top = rc[0].top
                    _CallWindowProc(old, h, msg, wp, lp)
                    rc[0].top = top
                    return 0
                return _CallWindowProc(old, h, msg, wp, lp)

            cb = _WNDPROC(proc)
            old = _SetWindowLongPtr(hwnd, _GWL_WNDPROC, ctypes.cast(cb, ctypes.c_void_p).value)
            self._wndproc = (cb, old)
            style = _GetWindowLongPtr(hwnd, _GWL_STYLE)
            _SetWindowLongPtr(hwnd, _GWL_STYLE, style & ~_WS_CAPTION & ~_WS_MAXIMIZEBOX)
            _SetWindowPos(hwnd, None, 0, 0, 0, 0, _SWP_FRAMECHANGED_ONLY)
        except Exception:
            self._hwnd = None   # 실패하면 Windows 제목 표시줄을 그대로 씀

    def _unhook(self):
        if self._hwnd and self._wndproc:
            try:
                _SetWindowLongPtr(self._hwnd, _GWL_WNDPROC, self._wndproc[1])
            except Exception:
                pass
        self._wndproc = None

    def _title_edge(self, e) -> bool:
        """제목 줄 맨 위 몇 픽셀 = 창 위쪽 테두리 (크기 조절)"""
        return not self._full and e.y_root - self.win.winfo_rooty() < self._px(5)

    def _title_motion(self, e):
        try:
            e.widget.configure(cursor="size_ns" if self._title_edge(e) else "")
        except Exception:
            pass

    def _title_press(self, e):
        if self._full or not self._hwnd:
            return
        hit = _HTTOP if self._title_edge(e) else _HTCAPTION
        _ReleaseCapture()
        pos = ((e.y_root & 0xFFFF) << 16) | (e.x_root & 0xFFFF)
        _PostMessageW(self._hwnd, _WM_NCLBUTTONDOWN, hit, pos)   # Windows 가 옮기기·크기 조절을 처리 (화면 가장자리 붙이기 포함)

    def _minimize(self):
        self.win.iconify()

    # ── 공통 ─────────────────────────────
    def _req(self, route: str, params: dict, timeout):
        params = dict(params, sid=self.sid, who=self._who)
        return self._request(route, params, timeout)

    @staticmethod
    def _error_text(r) -> str:
        try:
            err = r.json().get("error", "")
        except Exception:
            err = ""
        if r.status_code == 404 or "허용되지 않은 경로" in err or err == "not found":
            return "장비의 클라이언트가 원격을 지원하지 않습니다 (v2.11 이상 필요)"
        return err or f"HTTP {r.status_code}"

    def _sleep(self, sec: float):
        end = time.monotonic() + sec
        while self.alive and time.monotonic() < end:
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    def close(self):
        if not self.alive:
            return
        self.alive = False
        with self._cv:
            self._keys.clear()
            self._btns.clear()
            self._cv.notify_all()
        self._unhook()
        try:
            self.win.destroy()
        except Exception:
            pass
        if self._on_closed:
            self._on_closed(self)

    def focus(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _disable_ime(self):
        """보는 쪽 한글 입력기를 끔 → 한/영 키와 글자 키가 가공되지 않고 장비로 그대로 감"""
        try:
            imm = ctypes.WinDLL("imm32")
            fn = _proto(imm.ImmAssociateContext, ctypes.c_void_p, wt.HWND, ctypes.c_void_p)
            fn(self.canvas.winfo_id(), None)
        except Exception:
            pass

    # ── 화면 스레드 ──────────────────────
    def _frame_loop(self):
        last, prev_key = -1, None
        while self.alive:
            q, fps = self.QUALITY[self._quality]
            key = (self._mon, self._want, q)
            if key != prev_key:
                last, prev_key = -1, key          # 화면·크기·화질이 바뀌면 새 화면을 받음
                self._base = None
            t0 = time.monotonic()
            try:
                r = self._req("/remote/frame", {"mon": self._mon, "w": self._want[0], "h": self._want[1],
                                                "q": q, "last": last, "rough": 1}, timeout=(3, 8))
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
                meta, jpeg = _unpack(r.content)
                ops = None
                if not meta.get("err") and not meta.get("busy"):
                    ops = self._apply(meta, jpeg)      # 압축 풀기·붙이기는 여기(백그라운드)서
            except Exception:
                self._error = "받은 화면을 읽지 못했습니다"
                self._base, last = None, -1
                self._sleep(1.0)
                continue
            if meta.get("err"):
                self._error = meta["err"]
                self._sleep(1.0)
                continue
            if meta.get("busy"):
                self._sleep(0.3)
                continue
            if ops is None:                            # 가진 화면과 조각이 안 맞음 → 통째로 다시 받음
                self._base, last = None, -1
                continue
            self._error = ""
            self._note, self._gdi = meta.get("note", ""), bool(meta.get("gdi"))
            last = meta.get("seq", -1)
            rtt = time.monotonic() - t0
            self._net = f"{max(1, len(jpeg) // 1024)} KB · {int(rtt * 1000)} ms" if jpeg else self._net
            with self._frame_lock:
                if ops and ops[0][0] == "full":
                    self._ops = ops
                else:
                    self._ops.extend(ops)
                if len(self._ops) > 48:                # UI 가 밀림 → 조각 대신 지금 화면 한 장으로
                    self._ops = [("full", self._base.copy())]
                self._meta = meta
                schedule = not self._ui_pending
                self._ui_pending = True
            if schedule:
                try:
                    self.win.after(0, self._show)
                except Exception:
                    return
            # 장비가 알려준 다음 캡처 시각에 맞춰 요청 (옛 장비는 안 알려줌 → 0.15초)
            try:
                nxt = min(max(float(meta.get("next", 0.15)), 0.0), 1.0)
            except (TypeError, ValueError):
                nxt = 0.15
            wait = max(1.0 / fps - (time.monotonic() - t0), nxt) if jpeg else max(nxt, 0.04)
            self._sleep(wait)

    def _apply(self, meta: dict, data: bytes):
        """받은 조각을 가진 화면(_base)에 붙임 → UI 에 넘길 작업 목록. 기준 화면이 안 맞으면 None"""
        parts = meta.get("parts")
        if not data or not parts:
            return []
        size = tuple(meta.get("size") or ())
        base = meta.get("base")
        if base is not None and (self._base is None or self._base_seq != base or self._base.size != size):
            return None
        ops, pos = [], 0
        for x, y, w, h, n in parts:
            im = Image.open(io.BytesIO(data[pos:pos + n]))
            im.load()
            pos += n
            if im.mode != "RGB":
                im = im.convert("RGB")
            if meta.get("rough") and base is None and len(size) == 2 and im.size != size:
                im = im.resize(size, Image.BILINEAR)   # 장비가 작게 보낸 거친 화면 → 여기서 키움
            if base is None:                           # 통째로 — UI 에는 사본을 넘김 (이후 조각은 _base 에 붙음)
                self._base = im
                ops = [("full", im.copy())]
            else:
                self._base.paste(im, (x, y))
                ops.append(("part", im, x, y))
        self._base_seq = meta.get("seq", -1)
        return ops

    def _show(self):
        with self._frame_lock:
            ops, meta = self._ops, self._meta
            self._ops, self._meta = [], None
            self._ui_pending = False
        if meta is None or not self.alive:
            return
        for op in ops:
            if op[0] == "full":
                im = op[1]
                if self._photo is None or (self._photo.width(), self._photo.height()) != im.size:
                    self._photo = self._ImageTk.PhotoImage(im)
                    self.canvas.itemconfigure(self._img_item, image=self._photo)
                else:
                    self._photo.paste(im)
            elif self._photo is not None:              # 바뀐 조각만 화면에 복사
                tmp = self._ImageTk.PhotoImage(op[1])
                self.canvas.tk.call(str(self._photo), "copy", str(tmp), "-to", op[2], op[3])
        if ops and self._photo is not None:
            self._layout((self._photo.width(), self._photo.height()))
            now = time.monotonic()
            self._shown = [t for t in self._shown if now - t < 1.0] + [now]
        self._frame_time = time.monotonic()
        mons = meta.get("mons") or []
        if mons != self._mons:
            self._mons = mons
            names = ["전체"] + [f"화면 {i + 1}" + (" (주)" if m[4] else "") for i, m in enumerate(mons)]
            self._mon_cb.configure(values=names)
            if self._mon >= len(mons):
                self._mon = -1
                self._mon_var.set("전체")
        cur = meta.get("cursor")
        if cur and self._box:
            x0, y0, iw, ih = self._box
            cx, cy, rr = x0 + cur[0] * iw, y0 + cur[1] * ih, 5
            self.canvas.coords(self._cur_item, cx - rr, cy - rr, cx + rr, cy + rr)
            self.canvas.itemconfigure(self._cur_item, state="normal")
        else:
            self.canvas.itemconfigure(self._cur_item, state="hidden")

    def _layout(self, size=None):
        if size is None:
            if not self._box:
                return
            size = self._box[2:]
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        x0, y0 = max(0, (cw - size[0]) // 2), max(0, (ch - size[1]) // 2)
        self.canvas.coords(self._img_item, x0, y0)
        self._box = (x0, y0, size[0], size[1])
        # 화면이 캔버스보다 크면(실제 크기 보기) 스크롤 — 아니면 스크롤 없이 가운데
        sw, sh = max(cw, x0 + size[0]), max(ch, y0 + size[1])
        self.canvas.configure(scrollregion=(0, 0, sw, sh))
        for bar, need, kw in ((self._hbar, self._actual and sw > cw, dict(row=1, column=0, sticky="ew")),
                              (self._vbar, self._actual and sh > ch, dict(row=0, column=1, sticky="ns"))):
            if need and not bar.winfo_ismapped():
                bar.grid(**kw)
            elif not need and bar.winfo_ismapped():
                bar.grid_remove()

    def _tick(self):
        """0.3초마다 상태 표시 · 화면 멈춤 감지"""
        if not self.alive:
            return
        stale = time.monotonic() - self._frame_time > self.FRAME_STALE
        msg = self._error or ("화면 갱신이 멈췄습니다 — 조작 잠김" if stale and self._box else "")
        if not msg and self._note:
            msg = f"{self._note} — 조작 잠김"
        if not self._box and not msg:
            msg = "연결 중…"
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.canvas.coords(self._msg_item, self.canvas.canvasx(cw // 2), self.canvas.canvasy(ch // 2))
        self.canvas.itemconfigure(self._msg_item, text=msg, state="normal" if msg else "hidden")
        self.canvas.tag_raise(self._msg_item)
        if msg:
            self._status.configure(text=msg, fg="#FFB4A9")
        else:
            now = time.monotonic()
            fps = len([t for t in self._shown if now - t < 1.0])
            ctl = ("" if self._ctl.get() else "보기 전용 · ") + ("호환 캡처(느림) · " if self._gdi else "")
            warn = f" · ⚠ {self._input_err}" if self._input_err else ""
            if self._notice[0] and now < self._notice[1]:
                warn = f" · {self._notice[0]}"
            self._status.configure(text=f"{ctl}{fps} fps · {self._net}{warn}",
                                   fg="#FFB4A9" if warn else self.DIM)
        self.win.after(300, self._tick)

    def _resized(self, e):
        self._view = (max(64, e.width), max(64, e.height))
        self._want = self.ACTUAL_BOX if self._actual else self._view
        self._layout()

    def _actual_changed(self):
        self._actual = self._actual_var.get()
        self._want = self.ACTUAL_BOX if self._actual else self._view
        self._box = None
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        self.canvas.focus_set()

    def _toggle_full(self):
        """전체 화면 ↔ 창 모드. 제목 줄·도구 막대는 남기고(창 모드 버튼), 오른쪽 패널만 숨김"""
        full = self._full = not self._full
        if self._hwnd:
            style = _GetWindowLongPtr(self._hwnd, _GWL_STYLE)
            if full:
                r = wt.RECT()
                _GetWindowRect(self._hwnd, ctypes.byref(r))
                self._restore = (r.left, r.top, r.right - r.left, r.bottom - r.top)
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(mi)
                _GetMonitorInfoW(_MonitorFromWindow(self._hwnd, 2), ctypes.byref(mi))   # 창이 있는 모니터
                m = mi.rcMonitor
                _SetWindowLongPtr(self._hwnd, _GWL_STYLE, style & ~_WS_THICKFRAME)
                _SetWindowPos(self._hwnd, None, m.left, m.top, m.right - m.left, m.bottom - m.top, _SWP_PLACE)
            else:
                _SetWindowLongPtr(self._hwnd, _GWL_STYLE, style | _WS_THICKFRAME)
                if self._restore:
                    _SetWindowPos(self._hwnd, None, *self._restore, _SWP_PLACE)
        else:
            self.win.attributes("-fullscreen", full)
        self._full_btn.configure(text=self._glyph["back" if full else "full"])
        if self._side is not None:
            if full:
                self._side.pack_forget()
            else:
                self._side.pack(side="right", fill="y", padx=(0, self._side_pad), pady=(0, self._side_pad),
                                before=self._area)
        self.canvas.focus_set()

    def _say(self, text: str, sec: float = 3.0):
        self._notice = (text, time.monotonic() + sec)

    def _combo(self, vks):
        """특수 키 조합 — 차례로 누르고 거꾸로 뗌 (보는 쪽 PC 에서 가로채지는 키를 장비로)"""
        self.canvas.focus_set()
        if not self._can_control():
            self._say("⚠ 조작이 꺼져 있거나 잠겨 있어 보내지 않았습니다")
            return
        for vk in vks:
            self._push(["k", vk, 1])
        for vk in reversed(vks):
            self._push(["k", vk, 0])

    def _mon_changed(self, _e=None):
        names = list(self._mon_cb.cget("values"))
        idx = names.index(self._mon_var.get()) if self._mon_var.get() in names else 0
        self._release_all()               # 다른 화면으로 바꾸기 전에 눌린 것부터 뗌
        self._mon = idx - 1
        self._box = None
        self.canvas.focus_set()

    def _q_changed(self, _e=None):
        self._quality = self._q_var.get()
        if self._on_quality:
            self._on_quality(self._quality)
        self.canvas.focus_set()

    def _ctl_changed(self):
        if not self._ctl.get():
            self._release_all()

    # ── 입력 (UI 스레드) ─────────────────
    def _can_control(self) -> bool:
        return (self._ctl.get() and self._box is not None and not self._error and not self._note
                and time.monotonic() - self._frame_time < self.FRAME_STALE)

    def _norm(self, e) -> tuple:
        x0, y0, iw, ih = self._box
        nx = min(max((self.canvas.canvasx(e.x) - x0) / max(1, iw - 1), 0.0), 1.0)
        ny = min(max((self.canvas.canvasy(e.y) - y0) / max(1, ih - 1), 0.0), 1.0)
        return round(nx * 65535), round(ny * 65535)

    def _push(self, ev):
        with self._cv:
            if ev[0] == "m" and self._events and self._events[-1][1][0] == "m":
                self._events[-1] = (time.monotonic(), ev)       # 이어진 이동은 마지막 위치만
            else:
                self._events.append((time.monotonic(), ev))
            if ev[0] == "d":
                self._btns.add(ev[1])
            elif ev[0] == "u":
                self._btns.discard(ev[1])
            elif ev[0] == "k":
                (self._keys.add if ev[2] else self._keys.discard)(ev[1])
            self._cv.notify()

    def _release_all(self):
        with self._cv:
            if not (self._keys or self._btns):
                return
        self._push(["r"])
        with self._cv:
            self._keys.clear()
            self._btns.clear()

    def _on_motion(self, e):
        if self._can_control():
            self._push(["m", *self._norm(e)])

    def _on_press(self, e):
        if e.num not in (1, 2, 3):
            return
        self.canvas.focus_set()
        if self._can_control():
            self._push(["d", e.num, *self._norm(e)])

    def _on_release(self, e):
        with self._cv:
            held = e.num in self._btns
        if held and self._box:                      # 뗌은 조작이 잠겨 있어도 보냄 (안전)
            self._push(["u", e.num, *self._norm(e)])

    def _on_wheel(self, e):
        if not self._ctl.get() and self._actual and e.delta:     # 보기 전용 → 휠로 화면 이동
            (self.canvas.xview_scroll if e.state & 0x1 else self.canvas.yview_scroll)(
                -1 if e.delta > 0 else 1, "units")
            return
        if self._can_control() and e.delta:
            self._push(["w", e.delta, *self._norm(e)])

    @staticmethod
    def _vk(e):
        vk = e.keycode
        if vk in (16, 17, 18):     # Shift·Ctrl·Alt 는 왼쪽·오른쪽 구분
            vk = {"Shift_L": 0xA0, "Shift_R": 0xA1, "Control_L": 0xA2, "Control_R": 0xA3,
                  "Alt_L": 0xA4, "Alt_R": 0xA5}.get(e.keysym, vk)
        if not 1 <= vk <= 254 or vk == 229:          # 229 = 입력기 처리 중
            return None
        return vk

    def _on_key_down(self, e):
        vk = self._vk(e)
        if vk and self._can_control():
            ctrl = bool(e.state & 0x4) and not (e.state & 0x20000)     # Ctrl (Alt 함께 누른 것 제외)
            if ctrl and vk == 0x56:
                self._clip_push()                    # 붙여넣기 전에 내 클립보드 글자를 장비로
            elif ctrl and vk in (0x43, 0x58):
                self._push(["g"])                    # 복사 직전 클립보드 번호를 장비가 기억
                self._clip_pull()
            self._push(["k", vk, 1])
        return "break"

    # ── 클립보드 (글자만) ────────────────
    def _clip_push(self):
        try:
            text = self.win.clipboard_get()
        except Exception:                            # 비었거나 글자가 아님
            return
        if not text or text == self._clip_sent:      # 그대로면 장비 클립보드(장비에서 복사한 것)를 씀
            return
        if len(json.dumps(text)) > self.CLIP_SEND:
            self._say("⚠ 클립보드 글자가 너무 길어 보내지 않았습니다")
            return
        self._clip_sent = text
        self._push(["c", text])

    def _clip_pull(self):
        if self._clip_busy:
            return
        self._clip_busy = True
        threading.Thread(target=self._clip_wait, daemon=True).start()

    def _clip_wait(self):
        """장비 클립보드가 바뀔 때까지 잠깐 기다렸다가 글자를 받아 내 클립보드에"""
        try:
            for _ in range(10):                      # 최대 약 2.5초
                self._sleep(0.25)
                if not self.alive:
                    return
                try:
                    r = self._req("/remote/clip", {}, timeout=(2, 4))
                    body = r.json() if r.status_code == 200 else {}
                except Exception:
                    return
                if not body.get("wait"):
                    break
            else:
                return
            text = body.get("text")
            if isinstance(text, str) and text:
                self.win.after(0, self._clip_got, text)
        finally:
            self._clip_busy = False

    def _clip_got(self, text: str):
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
        except Exception:
            return
        self._clip_sent = text                       # 다시 붙여넣을 때 장비로 되돌려 보내지 않음
        self._say(f"📋 장비에서 복사함 ({len(text)}자)")                               # Tab 등이 보는 쪽 창에서 쓰이지 않게

    def _on_key_up(self, e):
        vk = self._vk(e)
        with self._cv:
            held = vk in self._keys
        if held:
            self._push(["k", vk, 0])
        return "break"

    def _on_focus_out(self, _e=None):
        # Alt+Tab 등으로 창을 떠나면 뗌 신호가 이 창에 오지 않으므로 먼저 모두 뗌
        try:
            outside = self.win.focus_get() is None
        except Exception:      # 콤보박스 목록이 열려 있으면 tk 가 예외를 냄 → 창 안에 있는 것
            outside = False
        if outside:
            self._release_all()

    # ── 입력 스레드 ──────────────────────
    def _input_loop(self):
        n, need_sync, last_sent = 0, False, 0.0
        while True:
            with self._cv:
                while self.alive and not self._events:
                    held = bool(self._keys or self._btns)
                    due = need_sync or (held and time.monotonic() - last_sent >= self.HEARTBEAT)
                    if due:
                        break
                    self._cv.wait(0.3)
                if not self.alive:
                    break
                batch, self._events = self._events, []
                keys, btns = sorted(self._keys), sorted(self._btns)
            now = time.monotonic()
            events = [ev for t, ev in batch
                      if now - t <= self.STALE or ev[0] in ("u", "r") or (ev[0] == "k" and not ev[2])]
            dropped = len(batch) - len(events)
            events.append(["s", keys, btns])
            ok, err = True, ""
            for i in range(0, len(events), 150):
                n += 1
                try:
                    r = self._req("/remote/input",
                                  {"mon": self._mon, "n": n,
                                   "ev": json.dumps(events[i:i + 150], separators=(",", ":"))},
                                  timeout=(2, 4))
                    body = r.json() if r.status_code == 200 else {}
                    ok = bool(body.get("ok"))
                    err = "" if ok else (body.get("err") or self._error_text(r))
                except Exception:
                    ok, err = False, "입력 전송 실패"
                if not ok:
                    break                              # 나머지는 버림 — 다음 요청의 동기화로 복구
            last_sent = time.monotonic()
            need_sync = not ok
            self._input_err = err or (f"지연된 입력 {dropped}개를 버렸습니다" if dropped else "")
            wait = (1.0 / self.INPUT_HZ) - (time.monotonic() - now)
            if not ok:
                wait = max(wait, 0.3)
            if wait > 0:
                self._sleep(wait)
        try:
            self._req("/remote/end", {}, timeout=(2, 3))    # 장비가 눌린 것 모두 떼고 세션 종료
        except Exception:
            pass
