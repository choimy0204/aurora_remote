"""진짜 RemoteViewer(+ WAN 파생 클래스)를 진짜 tkinter(Xvfb)에서 돌려, 가짜 현장 화면(tests/sim_host.py)으로 시험
실행:  xvfb-run -a python -m pytest tests/test_wan_viewer_real.py
- 지연(RTT)·지터를 준 상태에서 화면이 끝내 현장과 같아지는지 (겹친 요청이 화면을 깨지 않는지)
- 선요청으로 초당 화면 수가 늘어나는지, 데이터를 얼마나 더 쓰는지"""

import threading
import time

import pytest

tkinter = pytest.importorskip("tkinter")
pytest.importorskip("PIL")
from PIL import ImageChops, ImageStat  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    import winshim
    root = winshim.make_root()
    ar = winshim.load_remote()
    monkeypatch.setattr(ar, "_GetMessageW", lambda *a: 0)       # Windows 키보드 훅 스레드는 바로 끝나게
    from sim_host import SimHost, SimSession
    from wan.viewer import make_viewer_class
    state = {"root": root, "ar": ar, "viewers": [], "stops": []}
    state["Viewer"] = make_viewer_class(ar.RemoteViewer)

    def start(rtt=0.0, jitter=0.0, paint_every=0.04, quality=None, fast=True):
        host = SimHost(ar)
        sess = SimSession(host, rtt=rtt, jitter=jitter)
        stop = threading.Event()
        threading.Thread(target=host.painter, args=(stop, paint_every), daemon=True).start()
        v = state["Viewer"](root, "시험", sess, px=lambda x: x, font=("TkDefaultFont", 9), fast=fast)
        if quality:
            v._quality = quality
            v._q_var.set(quality)
        state["viewers"].append(v)
        state["stops"].append(stop)
        return v, host, sess, stop

    def run(seconds):
        root.after(int(seconds * 1000), root.quit)
        root.mainloop()          # 실제 앱처럼 mainloop (다른 스레드의 after 호출은 mainloop 안에서만 허용됨)

    state["start"], state["run"] = start, run
    yield state
    for s in state["stops"]:
        s.set()
    for v in state["viewers"]:
        try:
            v.close()
            v._photo = None
        except Exception:
            pass
    state["viewers"].clear()
    winshim.destroy_root(root)


def error_vs_host(viewer, host):
    snap = host.snap.get(viewer._base_seq)
    assert snap is not None and viewer._base is not None
    diff = ImageChops.difference(viewer._base.convert("RGB"), snap.convert("RGB"))
    return sum(ImageStat.Stat(diff).mean) / 3


def settle(env, viewer, host, stop, seconds=2.0):
    """현장 화면 변화를 멈추고 잠시 기다리면 보는 쪽이 현장 마지막 화면과 같아져야 함"""
    stop.set()
    env["run"](seconds)
    assert viewer._base_seq == host.seq, (viewer._base_seq, host.seq)
    assert error_vs_host(viewer, host) < 8, "JPEG 손실 이상으로 화면이 다름 — 조각이 잘못 붙었음"


# ── 정확성 ──
@pytest.mark.parametrize("quality", ["절약", "보통", "고화질"])
def test_viewer_converges_to_host_screen_for_every_quality(env, quality):
    v, host, sess, stop = env["start"](rtt=0.15, quality=quality)
    env["run"](3.0)
    settle(env, v, host, stop)
    assert v._error == ""


@pytest.mark.parametrize("rtt,jitter", [(0.2, 0.3), (0.4, 0.6)])
def test_out_of_order_responses_do_not_corrupt_screen(env, rtt, jitter):
    """지터가 커서 응답이 뒤바뀌어 도착해도 (선요청 2장) 화면이 끝내 현장과 같아야 함"""
    v, host, sess, stop = env["start"](rtt=rtt, jitter=jitter, quality="고화질")
    env["run"](4.0)
    settle(env, v, host, stop, seconds=3.0)


def test_quality_switch_midrun_keeps_screen_correct(env):
    v, host, sess, stop = env["start"](rtt=0.2, jitter=0.2, quality="절약")
    env["run"](1.5)
    for q in ("고화질", "절약", "보통"):
        v._quality = q
        v._q_var.set(q)
        env["run"](1.2)
    settle(env, v, host, stop, seconds=2.5)


def test_window_resize_midrun_keeps_screen_correct(env):
    v, host, sess, stop = env["start"](rtt=0.15, quality="보통")
    env["run"](1.0)
    v._want = (400, 300)                    # 창 크기가 바뀜 → 이전 요청의 응답은 버리고 새로 시작
    env["run"](1.0)
    v._want = (1280, 720)
    env["run"](1.0)
    settle(env, v, host, stop, seconds=2.5)


# ── 선요청 효과 ──
def frame_requests(sess):
    return [p for r, p in sess.requests if r == "/remote/frame"]


def test_prefetch_roughly_doubles_frame_rate_on_slow_links(env, monkeypatch):
    monkeypatch.setattr("wan.viewer.SLOW_RTT", 99)          # 절약의 자동 겹침을 꺼서 한 장씩일 때와 비교
    results = {}
    for quality in ("절약", "보통"):
        v, host, sess, stop = env["start"](rtt=0.4, quality=quality, fast=False)
        env["run"](5.0)
        results[quality] = len(frame_requests(sess))
        stop.set()
        v.close()
    assert results["보통"] >= 1.6 * results["절약"], results      # 슬롯 2개 → 거의 2배
    assert results["절약"] <= 5.0 / 0.4 + 2                           # 절약은 1장씩 (왕복 한 번에 한 장)


def test_saver_never_has_two_requests_in_flight_but_normal_does(env, monkeypatch):
    monkeypatch.setattr("wan.viewer.SLOW_RTT", 99)
    v, host, sess, stop = env["start"](rtt=0.3, quality="절약", fast=False)
    env["run"](3.0)
    assert sess.peak_inflight == 1                      # 절약은 데이터를 아끼려고 한 장씩
    v2, host2, sess2, stop2 = env["start"](rtt=0.3, quality="보통", fast=False)
    env["run"](3.0)
    assert sess2.peak_inflight == 2                     # 보통은 두 장씩 (그 이상은 아님)


def test_prefetch_data_overhead_is_bounded(env):
    """겹친 요청도 같은 변화를 두 번 받지 않는다 (pipe) — 요청당 바이트가 늘지 않음"""
    sent = {}
    for quality in ("절약", "보통"):
        v, host, sess, stop = env["start"](rtt=0.4, quality=quality, paint_every=0.1, fast=False)
        env["run"](5.0)
        stop.set()
        sent[quality] = host.bytes_sent / max(1, len(frame_requests(sess)))      # 요청당 바이트
        v.close()
    assert sent["보통"] < 1.5 * sent["절약"], sent


def test_saver_caps_requested_frame_size_but_normal_does_not(env):
    """창을 실제로 키우면(캔버스 > 1280) 절약은 1280x720 으로 줄여 요청하고, 보통은 창 크기만큼 요청"""
    v, host, sess, stop = env["start"](rtt=0.05, quality="절약")
    if v.win.winfo_screenwidth() < 1400:
        pytest.skip("화면이 작아 1280 보다 큰 캔버스를 만들 수 없음 (xvfb-run -s '-screen 0 1400x1000x24')")
    v.win.geometry("1390x800+0+0")
    env["run"](1.2)
    saver = [p for p in frame_requests(sess) if p["w"] > 0][-5:]
    assert saver and all(p["w"] <= 1280 and p["h"] <= 720 for p in saver), saver
    v._quality = "보통"
    v._q_var.set("보통")
    env["run"](1.2)
    assert max(p["w"] for p in frame_requests(sess)[-5:]) > 1280


def test_fast_mode_is_off_by_default_and_prefetches_two(env):
    v, host, sess, stop = env["start"](rtt=0.3, quality="고화질", fast=False)
    env["run"](2.5)
    assert v._fast is False and sess.peak_inflight == 2          # 기본도 두 장 (겹쳐도 같은 조각은 한 번만)
    settle(env, v, host, stop)


def test_fast_checkbox_turns_prefetch_on_and_off_midrun(env):
    v, host, sess, stop = env["start"](rtt=0.3, quality="보통", fast=False)
    env["run"](2.0)
    assert sess.peak_inflight == 2
    v._fast_var.set(True)
    v._fast_changed()
    env["run"](2.5)
    assert v._fast is True and sess.peak_inflight == 3
    v._fast_var.set(False)
    v._fast_changed()
    env["run"](1.0)
    settle(env, v, host, stop, seconds=2.5)


def test_starts_at_saver_even_if_nothing_remembered(env):
    v, host, sess, stop = env["start"](rtt=0.05)
    assert v._quality == "절약" and v._q_var.get() == "절약"
    assert list(v.QUALITY) == ["절약", "보통", "고화질"]


# ── 겹친 응답 처리 규칙 (직접 호출) ──
def make_meta(env, host, last):
    """host 에 last 기준 요청을 보낸 응답을 (meta, 데이터) 로"""
    ar = env["ar"]
    meta, body = ar._unpack(host.frame({"last": last, "q": 60}))
    return meta, body


def test_apply_rule_drops_stale_and_accepts_older_base(env):
    v, host, sess, stop = env["start"](rtt=5.0)         # 통신은 사실상 막아 두고 규칙만 직접 시험
    stop.set()
    time.sleep(0.1)
    # 현장 화면을 몇 번 바꿈
    host.paint((10, 10, 100, 100), (255, 0, 0))
    full_meta, full_body = make_meta(env, host, -1)
    v._base = None
    assert v._apply(full_meta, full_body)                # 처음엔 통째로
    s0 = v._base_seq
    host.paint((200, 200, 300, 300), (0, 255, 0))
    host.paint((400, 20, 500, 120), (0, 0, 255))
    a_meta, a_body = make_meta(env, host, s0)            # s0 이후 변화 전부 (누적)
    host.paint((20, 250, 120, 340), (255, 255, 0))
    b_meta, b_body = make_meta(env, host, s0)            # 같은 기준으로 나중에 보낸 요청 (더 많은 변화)
    # b 가 먼저 도착해 적용된 뒤 a 가 늦게 도착 → a 는 버려져야 함
    assert v._apply(b_meta, b_body) != []
    seq_b = v._base_seq
    assert seq_b == b_meta["seq"]
    assert v._apply(a_meta, a_body) == [] and v._base_seq == seq_b
    # 기준 번호가 내 화면보다 오래된 조각도(= 겹쳐 보낸 요청) 더 새 화면 위에 붙을 수 있어야 함
    host.paint((300, 100, 360, 160), (9, 99, 199))
    c_meta, c_body = make_meta(env, host, s0)            # 기준 s0 < 내 번호 seq_b
    assert c_meta["base"] == s0 < v._base_seq
    ops = v._apply(c_meta, c_body)
    assert ops and v._base_seq == c_meta["seq"]
    assert error_vs_host(v, host) < 8


def test_apply_rule_requests_full_frame_when_base_is_missing(env):
    v, host, sess, stop = env["start"](rtt=5.0)
    stop.set()
    time.sleep(0.1)
    host.paint((10, 10, 100, 100), (255, 0, 0))
    meta, body = make_meta(env, host, 1)                  # 조각 응답 (base=1) 인데 내겐 화면이 없음
    assert meta.get("base") == 1
    v._base = None
    assert v._apply(meta, body) is None


# ── 겹친 요청(pipe): 같은 조각을 두 번 안 받고, 순서가 뒤바뀌어도 화면이 맞음 ──
def test_pipelined_requests_do_not_repeat_changes(env):
    v, host, sess, stop = env["start"](rtt=5.0)
    stop.set()
    time.sleep(0.1)
    ar = env["ar"]
    host.paint((10, 10, 100, 100), (255, 0, 0))
    base_seq = host.seq
    host.paint((200, 200, 300, 300), (0, 255, 0))
    first, _ = ar._unpack(host.frame({"last": base_seq, "q": 60, "pipe": 1}))
    host.paint((400, 20, 500, 120), (0, 0, 255))
    second, _ = ar._unpack(host.frame({"last": base_seq, "q": 60, "pipe": 1}))     # 같은 last — 첫 응답이 아직 안 온 상태
    assert second["base"] == first["seq"]                                           # 첫 응답 이후 변경분만
    plain, _ = ar._unpack(host.frame({"last": base_seq, "q": 60}))
    assert plain["base"] == base_seq                                                 # pipe 가 없으면 예전처럼 누적


def test_out_of_order_pipelined_responses_still_give_correct_screen(env):
    v, host, sess, stop = env["start"](rtt=5.0)
    stop.set()
    time.sleep(0.1)
    ar = env["ar"]
    host.paint((10, 10, 100, 100), (255, 0, 0))
    full = ar._unpack(host.frame({"last": -1, "q": 60, "pipe": 1}))
    v._base = None
    assert v._apply(*full)
    host.paint((200, 200, 300, 300), (0, 255, 0))
    r1 = ar._unpack(host.frame({"last": v._base_seq, "q": 60, "pipe": 1}))
    host.paint((400, 20, 500, 120), (0, 0, 255))
    r2 = ar._unpack(host.frame({"last": v._base_seq, "q": 60, "pipe": 1}))
    assert r2[0]["base"] == r1[0]["seq"]
    assert v._apply(*r2) == []                           # 앞선 응답(r1)이 아직 — 보관
    ops = v._apply(*r1)                                  # r1 이 오면 r2 까지 이어서 붙음
    assert ops and v._base_seq == r2[0]["seq"] and not v._pending
    assert error_vs_host(v, host) < 8


def test_rough_frame_is_followed_by_sharp_request_without_waiting(env):
    """거친 화면(모자이크)을 받으면 fps 간격을 기다리지 않고 바로 선명한 화면을 요청"""
    v, host, sess, stop = env["start"](rtt=0.0, quality="절약", fast=False)
    stop.set()
    real, times = host.frame, []

    def frame(q):
        times.append(time.monotonic())
        out = real(q)
        meta, body = env["ar"]._unpack(out)
        if len(times) == 2:                               # 두 번째 응답을 "거친 화면" 으로 표시
            meta["rough"] = 1
            return env["ar"]._pack(meta, body)
        return out
    host.frame = frame
    host.paint((10, 10, 100, 100), (255, 0, 0))
    env["run"](1.5)
    assert len(times) >= 4
    assert times[2] - times[1] < 0.5 * (1.0 / v.QUALITY["절약"][1]), times[:4]    # 절약 fps(5) 간격(0.2초)을 안 기다림


def test_saver_overlaps_requests_only_when_the_link_is_slow(env):
    v, host, sess, stop = env["start"](rtt=0.5, quality="절약", fast=False)
    env["run"](4.0)
    assert sess.peak_inflight == 2                      # 왕복이 느리면 절약도 겹쳐 보냄
    settle(env, v, host, stop)


# ── 거친 화면(모자이크) 줄이기 ──
def test_one_off_big_change_is_read_sharp_but_continuous_change_goes_rough(env):
    ar = env["ar"]
    dup = ar._DesktopDup()
    o = {"big": 0.0, "stale": 0.0, "sysmem": False}
    assert dup._rough_wanted(o, True, 10.0) is False       # 처음 크게 바뀜(창 전환 등) → 바로 선명하게
    assert dup._rough_wanted(o, True, 10.2) is True        # 0.4초 안에 또 → 계속 바뀌는 중 → 거칠게
    assert dup._rough_wanted(o, True, 11.0) is False       # 한참 잠잠했다가 다시 → 다시 선명하게 시작
    assert dup._rough_wanted(o, False, 11.1) is False      # 거친 화면을 못 쓰는 경우(여러 모니터 등)
    o["sysmem"] = True
    assert dup._rough_wanted(o, True, 11.2) is False
    o.update(sysmem=False, stale=11.2)
    assert dup._rough_wanted(o, True, 11.3 + dup.ROUGH_MAX) is False   # 너무 오래 거칠었으면 한 번은 선명하게


def test_wan_viewer_marks_requests_as_wan_and_asks_for_webp(env):
    v, host, sess, stop = env["start"](rtt=0.0, quality="보통", fast=False)
    env["run"](0.6)
    stop.set()
    reqs = frame_requests(sess)
    assert reqs and all(p.get("wan") == 1 for p in reqs)
    assert all(p.get("fmt") == "webp" for p in reqs) == env["ar"].WEBP_OK


def test_rough_frames_only_when_saver_or_link_is_slow(env):
    v, host, sess, stop = env["start"](rtt=5.0, quality="보통", fast=False)
    stop.set()
    assert v._rough_allowed() is False                     # 보통 + 회선 정보 없음 → 바로 선명하게
    v._quality = "절약"
    assert v._rough_allowed() is True                      # 절약은 데이터 우선
    v._quality = "보통"
    v._note_transfer(100, 0.05)                            # 변화 없는 작은 응답 → 왕복 바닥값 50ms
    v._note_transfer(60000, 0.25)                          # 60KB 가 0.2초(+왕복) → 300KB/s → 화면 한 장 0.2초: 빠름
    assert v._rough_allowed() is False
    for _ in range(6):
        v._note_transfer(40000, 1.05)                      # 40KB 가 1초 → 40KB/s → 화면 한 장 1.5초: 느림
    assert v._rough_allowed() is True
    for _ in range(12):
        v._note_transfer(60000, 0.2)                       # 다시 빨라지면 되돌아옴 (히스테리시스)
    assert v._rough_allowed() is False


def test_input_wakes_frame_requests_and_polls_faster_for_a_while(env):
    v, host, sess, stop = env["start"](rtt=0.0, quality="보통", fast=False)
    stop.set()
    time.sleep(0.2)
    t = time.monotonic()
    done = []
    threading.Thread(target=lambda: (v._nap(5.0), done.append(time.monotonic() - t)), daemon=True).start()
    time.sleep(0.1)
    v._after_input(True)
    time.sleep(0.2)
    assert done and done[0] < 1.0                         # 5초를 쉬려던 요청이 입력이 나가자 바로 깨어남
    v._act_until = time.monotonic() + 2.0
    t = time.monotonic()
    v._nap(5.0)
    assert time.monotonic() - t < 0.2                     # 조작 중에는 쉬는 시간이 ACT_POLL 로 짧아짐


def test_small_window_size_changes_do_not_restart_the_screen_stream(env):
    v, host, sess, stop = env["start"](rtt=0.0, quality="보통", fast=False)
    env["run"](0.8)
    first = v._want
    assert first[0] % 16 == 0 and first[1] % 16 == 0          # 16 단위로 맞춤
    keys = []
    base_gen = v._pf_gen
    for dh in (1, 2, 3, 1):                                   # 도구 막대 글자가 바뀌어 캔버스가 몇 픽셀씩 흔들리는 상황
        e = type("E", (), {"width": v._view[0], "height": v._view[1] + dh})()
        v._resized(e)
        env["run"](0.05)
    env["run"](0.6)
    assert v._want == first and v._pf_gen == base_gen         # 같은 16 칸 안이라 처음부터 다시 받지 않음
    e = type("E", (), {"width": v._view[0] + 200, "height": v._view[1]})()
    v._resized(e)
    assert v._want == first                                   # 끌고 있는 동안은 아직 반영 안 함
    env["run"](0.7)
    assert v._want[0] > first[0]                              # 멈추면 반영
    settle(env, v, host, stop, seconds=2.0)


def test_idle_screen_is_polled_less_often_but_input_or_change_restores_speed(env):
    v, host, sess, stop = env["start"](rtt=0.0, quality="절약", fast=False)
    stop.set()
    time.sleep(0.2)
    v._changed_at = time.monotonic()
    assert v._idle_wait(0.3) == 0.3                           # 방금 바뀜 → 그대로
    v._changed_at = time.monotonic() - 10
    assert v._idle_wait(0.3) == 1.0                           # 절약: 한참 그대로 → 1초 간격
    v._quality = "보통"
    assert v._idle_wait(0.3) == 0.6
    v._act_until = time.monotonic() + 1.0
    assert v._idle_wait(0.3) == 0.3                           # 조작 중에는 바로 원래대로
