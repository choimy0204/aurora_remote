"""통로 계층(Mux · Bridge) 시험 — 응답 모양 · 차단 · 대용량 · 우선순위 · 끊김"""

import hashlib
import statistics
import threading
import time

import pytest

from wan import protocol as P
from wan.mux import WINDOW
from wan_helpers import Rig, FakeCfg, LOCAL_KEY, pattern


@pytest.fixture
def rig():
    r = Rig()
    yield r
    r.close()


# ── 기본 ──
def test_json_roundtrip_looks_like_requests_response(rig):
    r = rig.user.request("GET", "/info")
    assert r.status_code == 200 and r.ok
    assert r.json() == {"hostname": "FAKE-PC", "api": 2}
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers.get("Content-Type") == r.headers["CONTENT-TYPE"]


def test_context_manager_and_iter_content(rig):
    with rig.user.request("GET", "/remote/frame", {"size": 100000}, stream=True) as r:
        data = b"".join(r.iter_content(1024 * 1024))
    assert data == pattern(100000)


def test_non_ok_status_and_error_json(rig):
    r = rig.user.request("GET", "/fs/list", {"path": "D:\\"})     # 허용된 경로지만 가짜 서버에 없음
    assert r.status_code == 404 and not r.ok
    assert r.json()["error"] == "not found"


def test_lan_key_added_by_bridge_not_by_caller(rig):
    rig.user.request("GET", "/info", headers={"X-Aurora-Key": "attacker"})
    assert rig.local.hits[-1] == ("GET", "/info", LOCAL_KEY)      # 사용자가 보낸 키는 무시, 현장이 붙인 키 사용


def test_fragmented_header_for_large_params(rig):
    text = "a" * 30000                      # 클립보드 붙여넣기(CLIP_SEND=30000) — 머리가 한 조각(16KB)보다 큼
    r = rig.user.request("GET", "/remote/clip", {"text": text})
    assert r.json() == {"ok": True, "len": 30000}


def test_params_are_passed(rig):
    rig.user.request("GET", "/remote/input", {"mon": -1, "n": 3, "ev": '[["m",1,2]]'})
    assert rig.local.last_params == {"mon": "-1", "n": "3", "ev": '[["m",1,2]]'}


# ── 차단 (WAN 으로 LAN 기능을 건드리지 못함) ──
@pytest.mark.parametrize("method,route,params", [
    ("POST", "/command", None),
    ("GET", "/discover", None),
    ("GET", "/info", {"via": "10.0.0.9"}),
    ("GET", "/fs/list", {"path": "D:\\", "via": "10.0.0.9"}),
    ("GET", "/something/else", None),
    ("PUT", "/info", None),
    ("GET", "/fs/upload", None),
    ("PUT", "/remote/frame", None),
])
def test_blocked_requests_never_reach_local_server(rig, method, route, params):
    before = len(rig.local.hits)
    r = rig.user.request(method, route, params)
    assert r.status_code == 403 and "허용되지 않는" in r.json()["error"]
    assert len(rig.local.hits) == before


def test_files_can_be_disabled_remotely():
    rig = Rig(cfg=FakeCfg(allow_files=False))
    try:
        assert rig.user.request("GET", "/fs/list", {"path": "D:\\"}).status_code == 403
        assert rig.user.request("PUT", "/fs/upload", {"path": "x"}, data=b"abc").status_code == 403
        assert rig.user.request("GET", "/remote/frame", {"size": 10}).status_code == 200    # 화면은 계속 가능
    finally:
        rig.close()


@pytest.mark.parametrize("top,asked,expect", [
    ("절약",   {"q": 75, "w": 1920, "h": 1080}, {"q": "40", "w": "1280", "h": "720"}),
    ("절약",   {"q": 30, "w": 800,  "h": 600},  {"q": "30", "w": "800",  "h": "600"}),
    ("보통",   {"q": 75, "w": 1920, "h": 1080}, {"q": "60", "w": "1920", "h": "1080"}),
    ("고화질", {"q": 75, "w": 1920, "h": 1080}, {"q": "75", "w": "1920", "h": "1080"}),
])
def test_quality_cap_applied_by_field(top, asked, expect):
    rig = Rig(cfg=FakeCfg(max_quality=top))
    try:
        rig.user.request("GET", "/remote/frame", dict(asked, size=10))
        assert {k: rig.local.last_params[k] for k in expect} == expect
    finally:
        rig.close()


# ── 대용량 · 이어받기 · 업로드 ──
def test_large_download_integrity_and_flow_control(rig):
    size = 6 * 1024 * 1024
    h, got = hashlib.sha256(), 0
    with rig.user.request("GET", "/fs/file", {"path": f"size:{size}"}, stream=True, timeout=(3, 30)) as r:
        for chunk in r.iter_content(1024 * 1024):
            h.update(chunk)
            got += len(chunk)
    assert got == size and h.hexdigest() == hashlib.sha256(pattern(size)).hexdigest()


def test_resume_with_range_header(rig):
    size, start = 500_000, 123_456
    with rig.user.request("GET", "/fs/file", {"path": f"size:{size}"}, headers={"Range": f"bytes={start}-"},
                          stream=True) as r:
        assert r.status_code == 206 and r.headers["Content-Range"] == f"bytes {start}-{size - 1}/{size}"
        assert b"".join(r.iter_content(65536)) == pattern(size)[start:]


class _Reader:
    """_BodyReader 와 같은 모양 (길이 + read + 진행 알림)"""
    def __init__(self, data, on_chunk):
        self.data, self.pos, self.on = data, 0, on_chunk

    def __len__(self):
        return len(self.data)

    def read(self, n):
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        self.on(len(chunk))
        return chunk


def test_upload_streams_body_with_progress(rig):
    data, seen = pattern(3 * 1024 * 1024 + 17), []
    r = rig.user.request("PUT", "/fs/upload", {"path": "D:\\a.bin", "mtime": "1"},
                         data=_Reader(data, seen.append), timeout=(3, 30))
    assert r.json()["size"] == len(data)
    assert rig.local.uploads["D:\\a.bin"] == (len(data), hashlib.sha256(data).hexdigest())
    assert sum(seen) == len(data)


def test_upload_empty_and_bytes_body(rig):
    assert rig.user.request("PUT", "/fs/mkdir", {"path": "D:\\x"}, data=b"").ok
    assert rig.user.request("PUT", "/fs/upload", {"path": "D:\\b"}, data=b"hello").json()["size"] == 5


def test_parallel_requests(rig):
    errs, results = [], []
    def work(i):
        try:
            n = 20000 + i
            results.append(len(rig.user.request("GET", "/remote/frame", {"size": n}).content) == n)
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(40)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert not errs and all(results) and len(results) == 40


# ── 우선순위: 큰 파일을 받는 중에도 입력이 막히지 않음 ──
def test_input_stays_responsive_during_bulk_download():
    rig = Rig(rate=1_000_000, latency=0.02)         # 약 8Mbps, 편도 20ms
    try:
        size = 4 * 1024 * 1024
        done = threading.Event()
        def bulk():
            with rig.user.request("GET", "/fs/file", {"path": f"size:{size}"}, stream=True, timeout=(3, 30)) as r:
                for _ in r.iter_content(1024 * 1024):
                    pass
            done.set()
        threading.Thread(target=bulk, daemon=True).start()
        time.sleep(0.5)
        lat = []
        while not done.is_set() and len(lat) < 25:
            t0 = time.monotonic()
            assert rig.user.request("GET", "/remote/input", {"ev": "[]"}, timeout=(3, 5)).ok
            lat.append(time.monotonic() - t0)
            time.sleep(0.05)
        assert not done.is_set() or len(lat) >= 5, "파일 전송이 너무 빨리 끝나 시험이 의미 없음"
        assert statistics.median(lat) < 0.35, f"입력 지연 중앙값 {statistics.median(lat):.3f}s"
        assert max(lat) < 1.0, f"입력 최대 지연 {max(lat):.3f}s"
        # 파일 전송이 끝까지 가는지도 확인
        assert done.wait(30)
    finally:
        rig.close()


def test_bulk_does_not_pile_up_in_link_buffer():
    rig = Rig(rate=2_000_000)
    try:
        with rig.user.request("GET", "/fs/file", {"path": "size:3000000"}, stream=True, timeout=(3, 30)) as r:
            for _ in r.iter_content(1024 * 1024):
                pass
        # 파일(가장 낮은 등급)은 버퍼가 조금만 차도 기다림 → 통로에 쌓이는 양이 작다
        assert rig.link_f.max_buffered < 160 * 1024, rig.link_f.max_buffered
    finally:
        rig.close()


def test_slow_reader_limits_sender_ahead():
    """받는 쪽이 느리면 보내는 쪽이 WINDOW 이상 앞서 보내지 않는다 (메모리 폭증 방지)"""
    rig = Rig()
    try:
        r = rig.user.request("GET", "/fs/file", {"path": "size:8000000"}, stream=True, timeout=(3, 30))
        time.sleep(1.0)                              # 읽지 않고 기다림
        queued = r._call.body.qsize() * 16 * 1024
        assert queued <= WINDOW + 64 * 1024, queued
        assert len(b"".join(r.iter_content(1 << 20))) == 8_000_000
    finally:
        rig.close()


# ── 오류 처리 ──
def test_truncated_response_raises_instead_of_silently_short(rig):
    with pytest.raises(P.WanError):
        rig.user.request("GET", "/remote/frame", {"trunc": 1}, timeout=(3, 10))
    # 통로는 계속 쓸 수 있음
    assert rig.user.request("GET", "/info").ok


def test_truncated_stream_raises_at_end(rig):
    r = rig.user.request("GET", "/remote/frame", {"trunc": 1}, stream=True, timeout=(3, 10))
    with pytest.raises(P.WanError):
        b"".join(r.iter_content(1024))


def test_read_timeout_does_not_break_channel(rig):
    t0 = time.monotonic()
    with pytest.raises(P.WanTimeout):
        rig.user.request("GET", "/remote/frame", {"t": 3}, timeout=(0.2, 0.5))
    assert time.monotonic() - t0 < 2
    assert rig.user.request("GET", "/info").ok


def test_cancel_stops_download_and_frees_state(rig):
    r = rig.user.request("GET", "/fs/file", {"path": "size:50000000"}, stream=True, timeout=(3, 30))
    next(r.iter_content(65536))
    r.close()
    deadline = time.time() + 3
    while rig.field._served and time.time() < deadline:
        time.sleep(0.05)
    assert not rig.field._served
    assert rig.user.request("GET", "/info").ok


def test_link_death_fails_pending_and_reports():
    rig = Rig()
    try:
        errors = []
        def slow():
            try:
                rig.user.request("GET", "/remote/frame", {"t": 5}, timeout=(3, 20))
            except Exception as e:
                errors.append(e)
        t = threading.Thread(target=slow)
        t.start()
        time.sleep(0.3)
        t0 = time.monotonic()
        rig.link_u.close()
        t.join(5)
        assert errors and isinstance(errors[0], P.WanError) and time.monotonic() - t0 < 2
        assert not rig.user.alive and rig.dead["user"]
        with pytest.raises(P.WanError):
            rig.user.request("GET", "/info")
    finally:
        rig.close()


def test_silent_peer_is_detected_as_dead():
    rig = Rig(ping_every=0.1, dead_after=0.5)
    try:
        assert rig.user.request("GET", "/info").ok
        rig.link_u.drop = rig.link_f.drop = True            # 서로 아무것도 못 받음
        deadline = time.time() + 3
        while rig.user.alive and time.time() < deadline:
            time.sleep(0.05)
        # 양쪽이 거의 동시에 감지 → 먼저 감지한 쪽은 "응답 없음", 다른 쪽은 그 때문에 "연결이 닫혔습니다"
        assert not rig.user.alive and not rig.field.alive
        assert any("응답 없음" in r for rs in rig.dead.values() for r in rs)
    finally:
        rig.close()


def test_ping_measures_rtt():
    rig = Rig(latency=0.03, ping_every=0.1)
    try:
        time.sleep(0.6)
        assert rig.user.rtt_ms is not None and 40 < rig.user.rtt_ms < 400
    finally:
        rig.close()


def test_usage_counts_match_between_both_sides(rig):
    rig.user.request("GET", "/remote/frame", {"size": 200000}).content
    time.sleep(0.2)
    u_tx, u_rx = rig.bytes["user"]
    f_tx, f_rx = rig.bytes["field"]
    assert u_rx > 200000 and f_tx > 200000
    assert abs(u_rx - f_tx) < 2048 and abs(u_tx - f_rx) < 2048       # 양쪽이 센 값이 (거의) 같다
    assert rig.user.tx == u_tx and rig.user.rx == u_rx


def test_bridge_reports_local_server_down_as_502():
    rig = Rig()
    try:
        rig.local.stop()
        r = rig.user.request("GET", "/info")
        assert r.status_code == 502 and "내부 오류" in r.json()["error"]
    finally:
        rig.user.close(); rig.field.close(); rig.bridge.shutdown(); rig.lt.stop()
