"""진짜 RemoteHost(현장 쪽 화면 캡처 · 입력 주입)를 Windows API 로 시험 — Windows 또는 Wine 위의 Windows 용 Python 에서만
  AURORA_HOST_TESTS=1   화면 캡처 시험을 켬 (읽기만 함 — 화면 내용을 메모리에 복사해 JPEG 로 압축)
  AURORA_HOST_INPUT=1   입력 주입 시험을 켬 (**실제 마우스 포인터를 움직이고 키를 누름** — 본인 PC 에서는 잠깐 마우스가 움직임)
Wine 시험 환경(tools/wine_lab/run_tests.sh)은 둘 다 켠다."""

import io
import json
import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt" or not os.environ.get("AURORA_HOST_TESTS"),
                                reason="Windows(또는 Wine) 전용, AURORA_HOST_TESTS=1 로 켬")


@pytest.fixture(scope="module")
def win():
    import tkinter
    import winshim
    ar = winshim.load_remote()
    root = winshim.make_root()
    root.geometry("640x420+0+0")
    root.configure(bg="#ff0000")
    root.attributes("-topmost", True)
    label = tkinter.Label(root, text="CAPTURE", bg="#00b000", fg="white", font=("Arial", 40))
    label.pack(expand=True, fill="both", padx=40, pady=40)
    entry = tkinter.Entry(root, font=("Arial", 20))
    entry.pack(fill="x", padx=60, pady=(0, 20))
    root.update()
    time.sleep(0.6)
    root.update()
    host = ar.RemoteHost(log=lambda m: None)
    yield dict(ar=ar, root=root, label=label, entry=entry, host=host)
    winshim.destroy_root(root)


def grab(env, last=-1, w=1400, h=1000):
    ar, host = env["ar"], env["host"]
    data = host.frame({"sid": "t1", "mon": -1, "w": w, "h": h, "q": 80, "last": last}, "시험")
    return ar._unpack(data)


def decode(env, jpeg):
    from PIL import Image
    return Image.open(io.BytesIO(jpeg)).convert("RGB")


def count_color(img, rgb, tol=40):
    px = img.load()
    n = 0
    for y in range(0, img.height, 2):
        for x in range(0, img.width, 2):
            r, g, b = px[x, y]
            if abs(r - rgb[0]) < tol and abs(g - rgb[1]) < tol and abs(b - rgb[2]) < tol:
                n += 1
    return n


def test_capture_returns_valid_frame_with_real_window_content(win):
    meta, jpeg = grab(win)
    assert not meta.get("err"), meta
    assert meta.get("parts") and jpeg, meta
    img = decode(win, jpeg)
    assert img.size == tuple(meta["size"])
    assert count_color(img, (0, 176, 0)) > 200, "화면에 띄운 초록 창 내용이 캡처되지 않음"
    assert count_color(img, (255, 0, 0)) > 100, "빨간 창 배경이 캡처되지 않음"
    assert meta.get("mons"), "모니터 목록이 비어 있음"


def test_capture_reports_which_capture_path_was_used(win):
    meta, _ = grab(win)
    assert "gdi" in meta or "note" in meta or meta.get("seq"), meta          # DXGI 가 안 되면 GDI 로 대체(gdi=True) — 둘 다 허용
    print("캡처 경로:", "GDI(호환)" if meta.get("gdi") else "DXGI", "| note:", meta.get("note"))


def test_diff_frame_after_screen_change_is_smaller_and_references_base(win):
    meta1, jpeg1 = grab(win)
    win["label"].configure(bg="#0000d0", text="CHANGED")
    win["root"].update()
    time.sleep(0.8)
    win["root"].update()
    meta2, jpeg2 = grab(win, last=meta1["seq"])
    assert meta2["seq"] != meta1["seq"], "화면이 바뀌었는데 번호가 그대로"
    if "base" in meta2:                      # 조각 응답이면 전체보다 작아야 함
        assert meta2["base"] == meta1["seq"] and len(jpeg2) < len(jpeg1)
    img = decode(win, jpeg2) if "base" not in meta2 else None
    if img is not None:
        assert count_color(img, (0, 0, 208)) > 100


def test_pipelined_request_skips_changes_already_sent(win):
    """pipe=1: 앞 요청의 응답에 실린 변경은 빼고 그 번호 이후만 — 같은 조각을 두 번 보내지 않음. pipe 가 없으면 예전처럼 누적"""
    ar, host = win["ar"], win["host"]

    def ask(last, pipe):
        q = {"sid": "pipe1", "mon": -1, "w": 1400, "h": 1000, "q": 80, "last": last}
        if pipe:
            q["pipe"] = 1
        return ar._unpack(host.frame(q, "시험"))
    meta0, _ = ask(-1, True)
    for color in ("#0000d0", "#d0d000"):
        win["label"].configure(bg=color)
        win["root"].update()
        time.sleep(0.8)
        win["root"].update()
        first, _ = ask(meta0["seq"], True)
        if first["seq"] != meta0["seq"]:
            break
    assert first["seq"] != meta0["seq"], "화면이 바뀌었는데 번호가 그대로"
    again, body = ask(meta0["seq"], True)            # 같은 last 로 겹쳐 온 요청
    assert again["seq"] == first["seq"] and not again.get("parts") and not body    # 새로 바뀐 게 없으니 빈 응답
    plain, _ = ask(meta0["seq"], False)              # pipe 없이는 누적
    assert plain.get("parts")


def test_unchanged_screen_returns_no_parts(win):
    meta1, _ = grab(win)
    time.sleep(1.0)
    win["root"].update()
    meta2, jpeg2 = grab(win, last=meta1["seq"])
    if meta2["seq"] == meta1["seq"]:
        assert not meta2.get("parts") and not jpeg2


# ── 입력 주입 (실제 마우스·키보드를 움직임) ──
input_only = pytest.mark.skipif(not os.environ.get("AURORA_HOST_INPUT"), reason="AURORA_HOST_INPUT=1 로 켬 (실제 입력을 주입함)")


def send(env, events, n):
    return env["host"].input({"sid": "t1", "mon": -1, "n": n, "ev": json.dumps(events)}, "시험")


@input_only
def test_mouse_move_lands_on_the_requested_point(win):
    import ctypes
    from ctypes import wintypes
    ar = win["ar"]
    r = send(win, [["m", 32768, 32768]], 1)                 # 화면 정중앙
    assert r["ok"], r
    time.sleep(0.3)
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    sw, sh = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    assert abs(pt.x - sw // 2) <= 3 and abs(pt.y - sh // 2) <= 3, (pt.x, pt.y, sw, sh)


@input_only
def test_key_press_types_into_the_focused_entry(win):
    win["root"].lift()
    win["root"].focus_force()
    win["entry"].focus_force()
    win["entry"].delete(0, "end")
    win["root"].update()
    time.sleep(0.3)
    r = send(win, [["k", 0x41, 1], ["k", 0x41, 0], ["k", 0x42, 1], ["k", 0x42, 0]], 2)       # A, B
    assert r["ok"], r
    for _ in range(10):
        win["root"].update()
        time.sleep(0.1)
    assert win["entry"].get().lower() == "ab", repr(win["entry"].get())


@input_only
def test_stale_input_request_number_is_ignored(win):
    r = send(win, [["m", 100, 100]], 1)                      # 이미 처리한 번호(1) → 다시 실행하지 않음
    assert r.get("dup") is True
