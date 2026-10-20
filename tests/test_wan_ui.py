"""wan/viewer.py · wan/ui.py 시험 — tkinter 없이 (가짜 tkinter 를 sys.modules 에 끼워 넣음)"""

import subprocess
import sys
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wan import ui as wui
from wan import viewer as wv
from wan.config import QUALITY, SAVER_BOX
from wan.protocol import WanDenied, WanError


# ═════════════════════════════════════════
# 공통 가짜들
# ═════════════════════════════════════════
class FakeTclError(Exception):
    pass


class FakeVar:
    created: list = []

    def __init__(self, master=None, value=None, **_kw):
        self.v = value if value is not None else ""
        FakeVar.created.append(self)

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


class FakeLabel:
    def __init__(self, master=None, **kw):
        self.master, self.kw, self.packed = master, dict(kw), None

    def pack(self, **kw):
        self.packed = kw

    def configure(self, **kw):
        self.kw.update(kw)

    @property
    def text(self):
        return self.kw.get("text", "")


class FakeCheck(FakeLabel):
    """tk.Checkbutton 흉내 — 옵션을 기록하고 pack 만 받음"""


class FakeFrame(FakeLabel):
    """tk.Frame 흉내"""


class FakeWin:
    def __init__(self):
        self.jobs = []
        self.fail = False

    def after(self, ms, fn):
        if self.fail:
            raise FakeTclError("destroyed")
        self.jobs.append((ms, fn))
        return len(self.jobs)


class FakeBase:
    """aurora_remote.RemoteViewer 가 쓰는 속성만 흉내 낸 기반 클래스"""
    QUALITY = {"낮음": (45, 8), "보통": (60, 15), "높음": (75, 20)}
    BG, DIM, FG = "#000", "#888", "#fff"

    def __init__(self, root, title, request, px=lambda v: v, quality="보통", on_quality=None,
                 on_closed=None, font=None, side=None, side_width=0):
        self._tk = SimpleNamespace(Label=FakeLabel, Checkbutton=FakeCheck, Frame=FakeFrame, BooleanVar=FakeVar,
                                  TclError=FakeTclError)
        self.canvas = SimpleNamespace(focus_set=lambda: None)
        self._area = object()
        self._request, self._on_quality, self._on_closed = request, on_quality, on_closed
        self._quality = quality if quality in self.QUALITY else "보통"
        self.alive = True
        self.win = FakeWin()
        self._status = FakeLabel(master=SimpleNamespace(name="toolbar"))
        self.init_args = dict(root=root, title=title, px=px, quality=quality, font=font,
                              side=side, side_width=side_width)
        self.sent = []

    def _req(self, route, params, timeout):
        self.sent.append((route, params, timeout))
        return self._request(route, dict(params, sid="s"), timeout)

    def close(self):
        if not self.alive:
            return
        self.alive = False
        if self._on_closed:
            self._on_closed(self)


class FakeSession:
    def __init__(self):
        self.mode, self.state, self.closed = "direct", "연결됨", False
        self.tx = self.rx = 0
        self.close_calls = 0
        self.requests = []

    def viewer_request(self, route, params, timeout):
        self.requests.append((route, params, timeout))
        return "resp"

    def close(self):
        self.close_calls += 1
        self.closed = True


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


# ═════════════════════════════════════════
# SpeedMeter · 순수 함수
# ═════════════════════════════════════════
def test_speedmeter_needs_two_samples():
    m = wv.SpeedMeter(clock=Clock())
    assert m.speed() == 0.0
    assert m.update(1000) == 0.0


def test_speedmeter_constant_rate():
    c = Clock(0)
    m = wv.SpeedMeter(window=3, clock=c)
    m.update(0)
    c.t = 1
    assert m.update(2048) == pytest.approx(2048)
    c.t = 2
    assert m.update(4096) == pytest.approx(2048)


def test_speedmeter_window_forgets_old_burst():
    c = Clock(0)
    m = wv.SpeedMeter(window=3, clock=c)
    m.update(0)
    c.t = 1
    m.update(1_000_000)          # 한꺼번에 많이 받음
    for t in range(2, 10):       # 이후로는 아무것도 안 받음
        c.t = t
        speed = m.update(1_000_000)
    assert speed == 0.0
    assert len(m._samples) <= 5


def test_speedmeter_never_negative_and_same_time():
    c = Clock(5)
    m = wv.SpeedMeter(clock=c)
    m.update(100)
    assert m.update(50) == 0.0        # 같은 시각 → 0
    c.t = 6
    assert m.update(10) == 0.0        # 누적이 줄어도(새 연결) 음수 없음


def test_fmt_speed_and_kind_text():
    assert wv.fmt_speed(2048) == "2 KB/s"
    assert wv.fmt_speed(0) == "0 KB/s"
    assert wv.kind_text("direct") == "직접 연결"
    assert wv.kind_text("relay") == "비상 중계"
    assert wv.kind_text("") == "다시 연결 중…"
    assert wv.kind_text("direct", closed=True) == "연결 닫힘"


def test_info_line():
    line = wv.info_line("relay", 3 * 1024 * 1024 + 104858, 85 * 1024, "절약")
    assert line == "비상 중계 · 이번 연결 3.1 MB · 85 KB/s · 절약"


def test_cap_frame_params_caps_without_mutating():
    src = {"mon": -1, "w": 3840, "h": 2160, "q": 40}
    out = wv.cap_frame_params(src)
    assert (out["w"], out["h"]) == SAVER_BOX
    assert src["w"] == 3840 and src["h"] == 2160
    assert out is not src and out["q"] == 40


def test_cap_frame_params_keeps_smaller_and_missing():
    assert wv.cap_frame_params({"w": 800, "h": 600}) == {"w": 800, "h": 600}
    assert wv.cap_frame_params({"mon": 0}) == {"mon": 0}
    assert wv.cap_frame_params({"w": 5000, "h": 100})["h"] == 100


# ═════════════════════════════════════════
# WanRemoteViewer (가짜 기반 클래스)
# ═════════════════════════════════════════
@pytest.fixture
def viewer_cls():
    return wv.make_viewer_class(FakeBase)


def make_viewer(cls, **kw):
    s = kw.pop("session", None) or FakeSession()
    return cls("root", "제목", s, px=lambda v: v, font=("f", 9), **kw), s


def test_viewer_quality_table_and_start_value(viewer_cls):
    v, _ = make_viewer(viewer_cls)
    assert viewer_cls.QUALITY == QUALITY and viewer_cls.QUALITY is not QUALITY
    assert list(v.QUALITY) == ["절약", "보통", "고화질"]
    assert v._quality == "절약"
    assert v.init_args["quality"] == "절약"
    assert FakeBase.QUALITY == {"낮음": (45, 8), "보통": (60, 15), "높음": (75, 20)}   # 기반은 그대로


def test_viewer_passes_session_request_and_no_quality_callback(viewer_cls):
    v, s = make_viewer(viewer_cls)
    assert v._request == s.viewer_request
    assert v._on_quality is None
    v._req("/remote/input", {"a": 1}, (3, 8))
    assert s.requests[0][0] == "/remote/input"


def test_viewer_caps_frame_size_when_saver(viewer_cls):
    v, s = make_viewer(viewer_cls)
    params = {"mon": -1, "w": 1920, "h": 1080, "q": 40}
    v._req("/remote/frame", params, (3, 8))
    sent = s.requests[-1][1]
    assert (sent["w"], sent["h"]) == SAVER_BOX
    assert params["w"] == 1920 and params["h"] == 1080      # 호출자 dict 는 그대로


def test_viewer_no_cap_at_other_quality_or_route(viewer_cls):
    v, s = make_viewer(viewer_cls)
    v._quality = "보통"
    v._req("/remote/frame", {"w": 1920, "h": 1080}, 1)
    assert s.requests[-1][1]["w"] == 1920
    v._quality = "절약"
    v._req("/remote/input", {"w": 1920, "h": 1080}, 1)
    assert s.requests[-1][1]["w"] == 1920


def test_viewer_info_row_is_below_toolbar_not_squeezed_into_it(viewer_cls):
    """도구 막대에 끼우면 창이 좁을 때 잘려 보이지 않으므로 별도 줄에 둔다 (실제 Tk 화면에서 확인된 문제)"""
    v, _ = make_viewer(viewer_cls)
    assert v._info.master is v._info_row and v._info_row.master is v.win
    assert v._info_row.master is not v._status.master
    assert v._info.packed["side"] == "right"
    assert v._info.text == "직접 연결 · 이번 연결 0 B · 0 KB/s · 절약"
    assert v.win.jobs and v.win.jobs[0][0] == 1000


def test_viewer_info_refreshes_and_reschedules(viewer_cls):
    v, s = make_viewer(viewer_cls)
    s.tx, s.rx = 1024 * 1024, 512 * 1024
    s.mode = ""
    v._quality = "고화질"
    _ms, fn = v.win.jobs[-1]
    fn()
    assert v._info.text.startswith("다시 연결 중… · 이번 연결 1.5 MB")
    assert v._info.text.endswith("고화질")
    assert len(v.win.jobs) == 2


def test_viewer_info_stops_after_close(viewer_cls):
    v, _ = make_viewer(viewer_cls)
    v.close()
    n = len(v.win.jobs)
    v._refresh_info()
    assert len(v.win.jobs) == n


def test_viewer_info_swallows_tcl_error(viewer_cls):
    v, _ = make_viewer(viewer_cls)
    v.win.fail = True
    v._refresh_info()        # 예외 없음
    v._info.configure = MagicMock(side_effect=FakeTclError("gone"))
    v._refresh_info()


def test_viewer_info_survives_broken_session(viewer_cls):
    class Broken:
        viewer_request = staticmethod(lambda *a: None)
        close = staticmethod(lambda: None)

        @property
        def tx(self):
            raise RuntimeError("boom")
    v, _ = make_viewer(viewer_cls, session=Broken())
    assert v._info.text == ""
    assert len(v.win.jobs) >= 1       # 그래도 계속 갱신 예약


def test_viewer_close_closes_session_once(viewer_cls):
    closed = []
    v, s = make_viewer(viewer_cls, on_closed=closed.append)
    v.close()
    v.close()
    assert s.close_calls == 1 and closed == [v]


def test_viewer_close_session_false(viewer_cls):
    v, s = make_viewer(viewer_cls, close_session=False)
    v.close()
    assert s.close_calls == 0 and not v.alive


def test_viewer_close_session_error_is_swallowed(viewer_cls):
    s = FakeSession()
    s.close = MagicMock(side_effect=RuntimeError("x"))
    v, _ = make_viewer(viewer_cls, session=s)
    v.close()
    assert s.close.called


def test_modules_import_without_tkinter():
    code = "import sys, wan.ui, wan.viewer; assert 'tkinter' not in sys.modules"
    root = __file__.rsplit("tests", 1)[0]
    r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# ═════════════════════════════════════════
# 순수 함수 (ui)
# ═════════════════════════════════════════
NOW = datetime(2026, 10, 20, 15, 0, 0).timestamp()


def ago(**kw):
    from datetime import timedelta
    return (datetime.fromtimestamp(NOW) - timedelta(**kw)).timestamp()


def test_relative_time():
    r = wui.relative_time
    assert r(NOW - 5, NOW) == "방금"
    assert r(NOW + 30, NOW) == "방금"                 # 시계가 조금 어긋나도
    assert r(ago(minutes=3), NOW) == "3분 전"
    assert r(ago(minutes=59), NOW) == "59분 전"
    assert r(ago(hours=2), NOW) == "2시간 전"
    assert r(datetime(2026, 10, 19, 23, 0).timestamp(), NOW) == "어제"
    assert r(datetime(2026, 10, 19, 1, 0).timestamp(), NOW) == "어제"
    assert r(datetime(2026, 10, 1, 9, 0).timestamp(), NOW) == "2026-10-01"
    assert r(0, NOW) == "-" and r(None, NOW) == "-" and r("x", NOW) == "-"


def test_validate_url():
    assert wui.validate_url("  ws://1.2.3.4:8765  ") == ("ws://1.2.3.4:8765", "")
    assert wui.validate_url("wss://relay.example.com")[1] == ""
    assert wui.validate_url("")[1] and wui.validate_url("   ")[1]
    assert wui.validate_url("http://x:1")[1]
    assert wui.validate_url("1.2.3.4:8765")[1]
    assert wui.validate_url("ws://")[1]
    assert wui.validate_url("ws://a b:1")[1]
    assert wui.validate_url(None)[1]


def test_validate_code():
    assert wui.validate_code("  abc ") == ("abc", "")
    assert wui.validate_code("")[1] and wui.validate_code(None)[1]
    assert wui.validate_code("x" * 64)[1] == ""
    assert wui.validate_code("x" * 65)[1]


def test_level_for_state():
    assert wui.level_for_state("연결됨") == "ok"
    assert wui.level_for_state("연결 중") == "warn"
    for s in ("끊김", "키 불일치", "주소 없음", "??"):
        assert wui.level_for_state(s) == "bad"


# ═════════════════════════════════════════
# 컨트롤러
# ═════════════════════════════════════════
class FakeCfg:
    def __init__(self, **kw):
        self.d = {"enabled": False, "relay_url": "", "relay_key": "", "agent_id": "a1b2c3d4e5f6",
                  "name": "", "access_code": ""}
        self.d.update(kw)
        self.loads = 0

    def get(self, k, default=None):
        return self.d.get(k, default)

    def update(self, **kw):
        self.d.update(kw)

    def ensure_id(self):
        return self.d["agent_id"]

    def load(self):
        self.loads += 1


class FakeClient:
    def __init__(self):
        self.state = "연결됨"
        self.agents = []
        self.on_change = None
        self.calls = []
        self.session = SimpleNamespace(mode="direct")
        self.open_error = None
        self.cfg_reply = {"online": True, "rev": 1}
        self.cfg_error = None
        self.threads = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def open_session(self, agent_id, code, name=""):
        self.calls.append(("open", agent_id, code, name))
        self.threads.append(threading.current_thread())
        if self.open_error:
            raise self.open_error
        return self.session

    def set_config(self, agent_id, config):
        self.calls.append(("cfg", agent_id, dict(config)))
        if self.cfg_error:
            raise self.cfg_error
        return self.cfg_reply


class FakeUsage:
    def day_total(self):
        return 5 * 1024 * 1024

    def month_total(self):
        return 2 * 1024 ** 3


class RecUI(wui.NullUI):
    def __init__(self):
        self.calls = []

    def __getattribute__(self, name):
        if name.startswith(("set_", "show_")):
            def rec(*a):
                object.__getattribute__(self, "calls").append((name,) + a)
            return rec
        return object.__getattribute__(self, name)

    def last(self, name):
        return [c for c in self.calls if c[0] == name][-1][1:]

    def all(self, name):
        return [c[1:] for c in self.calls if c[0] == name]


def ag(i, name, online=True, last_seen=0, ver="1.0", rev=0, applied=0):
    return {"id": i, "name": name, "online": online, "last_seen": last_seen, "ver": ver,
            "rev": rev, "applied_rev": applied}


def make_ctl(**ctx_over):
    ctx = {"cfg": FakeCfg(), "client": FakeClient(), "usage": FakeUsage(),
           "supervisor_state": lambda: {"state": "stopped"},
           "set_field_mode": MagicMock(), "open_remote": MagicMock(), "px": lambda v: v, "log": MagicMock()}
    ctx.update(ctx_over)
    ui = RecUI()
    ctl = wui.WanTabController(ctx, lambda fn: fn(), ui=ui, spawn=lambda fn: fn(), clock=lambda: NOW)
    return ctl, ctx, ui


def test_rows_sorted_online_first_and_formatted():
    ctl, ctx, _ = make_ctl()
    ctx["client"].agents = [
        ag("bbbbbbbbbbbb", "zeta", online=False, last_seen=ago(minutes=3), ver="1.1", rev=2, applied=1),
        ag("aaaaaaaaaaaa", "Beta", online=True, last_seen=NOW, ver="1.0", rev=1, applied=1),
        ag("cccccccccccc", "alpha", online=True, rev=0, applied=0),
        ag("dddddddddddd", "", online=False, last_seen=ago(hours=2), ver=""),
    ]
    rows = ctl.rows()
    assert [r[0] for r in rows] == ["alpha", "Beta", "(이름 없음)", "zeta"]     # 켜짐 먼저, 이름순(대소문자 무시)
    assert rows[1] == ("Beta", "AAAA-AAAA-AAAA", "켜짐", "지금", "1.0", "적용됨")
    assert rows[3] == ("zeta", "BBBB-BBBB-BBBB", "꺼짐", "3분 전", "1.1", "대기 중")
    assert rows[2][3] == "2시간 전" and rows[2][4] == "-"


def test_rows_with_ids_and_agent_lookup():
    ctl, ctx, _ = make_ctl()
    ctx["client"].agents = [ag("x1", "n")]
    assert ctl.rows_with_ids()[0][0] == "x1"
    assert ctl.agent("x1")["name"] == "n" and ctl.agent("nope") is None
    ctx["client"].agents = []
    assert ctl.rows() == []


def test_server_status_text_and_level():
    ctl, ctx, ui = make_ctl()
    ctx["client"].state = "키 불일치"
    assert ctl.server_status() == ("● 키 불일치", "bad")
    ctx["client"].state = "연결 중"
    ctl.refresh_server()
    assert ui.last("set_server_status") == ("● 연결 중", "warn")


def test_apply_server_saves_and_restarts_client():
    ctl, ctx, ui = make_ctl()
    assert ctl.apply_server("  ws://10.0.0.1:8765 ", " secret ") is True
    assert ctx["cfg"].d["relay_url"] == "ws://10.0.0.1:8765"
    assert ctx["cfg"].d["relay_key"] == "secret"
    assert ctx["client"].calls == ["stop", "start"]
    assert ui.all("set_busy") == [("server", True), ("server", False)]
    assert ui.all("set_server_status")                   # 끝나고 상태 갱신
    assert "secret" not in " ".join(str(c) for c in ui.calls)    # 키는 화면 로그에 남기지 않음


def test_apply_server_rejects_bad_url():
    ctl, ctx, ui = make_ctl()
    assert ctl.apply_server("http://x", "k") is False
    assert ctx["cfg"].d["relay_url"] == "" and ctx["client"].calls == []
    assert ui.all("show_error")


def test_apply_server_restart_failure_is_reported_not_raised():
    ctl, ctx, ui = make_ctl()
    ctx["client"].start = MagicMock(side_effect=RuntimeError("boom"))
    assert ctl.apply_server("ws://h:1", "") is True
    assert any("boom" in line for line in ui.last("set_log")[0])
    assert ui.all("set_busy")[-1] == ("server", False)


def test_apply_server_uses_worker_thread_by_default():
    ctx = {"cfg": FakeCfg(), "client": FakeClient(), "usage": FakeUsage()}
    done = threading.Event()
    ctx["client"].start = lambda: done.set()
    ctl = wui.WanTabController(ctx, lambda fn: fn(), ui=RecUI())
    assert ctl.apply_server("ws://h:1", "") is True
    assert done.wait(3)


def test_field_info_texts():
    ctl, ctx, _ = make_ctl()
    assert ctl.field_info() == ("A1B2-C3D4-E5F6", "(이름 없음)", "접속 코드 미설정 — 미설정이면 모두 거부")
    ctx["cfg"].d.update(name=" 사무실 ", access_code="1234")
    assert ctl.field_info()[1:] == ("사무실", "접속 코드 설정됨")


@pytest.mark.parametrize("st,text,level", [
    ({"state": "running", "pid": 77, "restarts": 0, "next_restart_in": 0}, "실행 중 (PID 77)", "ok"),
    ({"state": "running", "pid": None}, "실행 중", "ok"),
    ({"state": "backoff", "next_restart_in": 4.2, "restarts": 2}, "5초 뒤 재시작 · 재시작 2회", "warn"),
    ({"state": "starting"}, "시작 중", "warn"),
    ({"state": "stopped", "pid": None}, "꺼짐", "off"),
    ({}, "꺼짐", "off"),
    (None, "꺼짐", "off"),
])
def test_field_status(st, text, level):
    ctl, _, _ = make_ctl(supervisor_state=lambda: st)
    assert ctl.field_status() == (text, level)
    assert ctl.field_status_text() == text


def test_field_status_supervisor_error():
    def boom():
        raise RuntimeError("x")
    ctl, _, _ = make_ctl(supervisor_state=boom)
    assert ctl.field_status_text() == "꺼짐"


def test_refresh_field_reload_option():
    ctl, ctx, ui = make_ctl()
    ctl.refresh_field()
    assert ctx["cfg"].loads == 0
    ctl.refresh_field(reload=True)
    assert ctx["cfg"].loads == 1
    assert ui.last("set_field_info")[0] == "A1B2-C3D4-E5F6"


def test_field_toggled_on_and_off():
    ctl, ctx, ui = make_ctl()
    ctl.field_toggled(True)
    assert ctx["cfg"].d["enabled"] is True
    ctx["set_field_mode"].assert_called_with(True)
    assert any("접속 코드" in line for line in ui.last("set_log")[0])      # 코드 없음 안내
    ctl.field_toggled(False)
    assert ctx["cfg"].d["enabled"] is False
    ctx["set_field_mode"].assert_called_with(False)
    ctx["log"].assert_called()


def test_field_toggled_failure_reverts():
    ctl, ctx, ui = make_ctl(set_field_mode=MagicMock(side_effect=OSError("실행 실패")))
    ctl.field_toggled(True)
    assert ctx["cfg"].d["enabled"] is False
    assert ui.last("set_field_enabled") == (False,)
    assert "실행 실패" in ui.last("show_error")[1]


def test_on_client_change_dispatch():
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x", "n")]
    ctl.on_client_change("session")
    assert ui.calls == []
    ctl.on_client_change("agents")
    assert [c[0] for c in ui.calls] == ["set_rows"]
    ctl.on_client_change("state")
    assert {c[0] for c in ui.calls} == {"set_rows", "set_server_status"}


def test_schedule_is_used_for_all_ui_updates():
    queued = []
    ctx = {"cfg": FakeCfg(), "client": FakeClient(), "usage": FakeUsage()}
    ui = RecUI()
    ctl = wui.WanTabController(ctx, queued.append, ui=ui, spawn=lambda fn: fn())
    ctl.refresh_all()
    assert ui.calls == [] and queued             # 화면 스레드가 꺼내 실행하기 전에는 아무것도 안 건드림
    for fn in queued:
        fn()
    assert {c[0] for c in ui.calls} >= {"set_rows", "set_usage", "set_field_info", "set_server_status"}


# ── 원격 연결 ──
def test_connect_success_opens_remote_and_remembers_code():
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x1", "현장A")]
    assert ctl.connect("x1", " 1234 ") is True
    assert ctx["client"].calls[-1] == ("open", "x1", "1234", "현장A")
    ctx["open_remote"].assert_called_once()
    session, agent = ctx["open_remote"].call_args[0]
    assert session is ctx["client"].session and agent["id"] == "x1"
    assert ctl.remembered_code("x1") == "1234" and ctl.remembered_code("zz") == ""
    assert ui.all("set_busy") == [("connect", True), ("connect", False)]


@pytest.mark.parametrize("exc", [WanDenied("접속 코드가 맞지 않습니다"), WanError("현장 PC 가 꺼져 있습니다")])
def test_connect_errors_shown_to_user(exc):
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x1", "A")]
    ctx["client"].open_error = exc
    assert ctl.connect("x1", "c") is True
    assert ui.last("show_error") == ("원격 연결 실패", str(exc))
    ctx["open_remote"].assert_not_called()
    assert ui.all("set_busy")[-1] == ("connect", False)


def test_connect_denied_forgets_code_but_error_keeps_it():
    ctl, ctx, _ = make_ctl()
    ctx["client"].agents = [ag("x1", "A")]
    ctx["client"].open_error = WanDenied("틀림")
    ctl.connect("x1", "bad")
    assert ctl.remembered_code("x1") == ""
    ctx["client"].open_error = WanError("꺼짐")
    ctl.connect("x1", "good")
    assert ctl.remembered_code("x1") == "good"


def test_connect_unexpected_exception():
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x1", "A")]
    ctx["client"].open_error = ValueError("이상함")
    ctl.connect("x1", "c")
    assert "이상함" in ui.last("show_error")[1]
    assert ctl._connecting == set()


def test_connect_validation():
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x1", "A")]
    assert ctl.connect("nope", "c") is False
    assert ctl.connect("x1", "") is False
    assert ctl.connect("x1", "x" * 65) is False
    assert len(ui.all("show_error")) == 3
    assert not any(isinstance(c, tuple) and c[0] == "open" for c in ctx["client"].calls)


def test_connect_ignores_second_click_while_connecting():
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x1", "A")]
    ctl._connecting.add("x1")
    assert ctl.connect("x1", "c") is False
    assert not ctx["client"].calls


def test_connect_default_spawn_runs_off_the_calling_thread():
    ctx = {"cfg": FakeCfg(), "client": FakeClient(), "usage": FakeUsage(), "open_remote": MagicMock()}
    ctx["client"].agents = [ag("x1", "A")]
    opened = threading.Event()
    ctx["open_remote"].side_effect = lambda *a: opened.set()
    ctl = wui.WanTabController(ctx, lambda fn: fn(), ui=RecUI())
    assert ctl.connect("x1", "c")
    assert opened.wait(3)
    assert ctx["client"].threads[0] is not threading.current_thread()


# ── 설정 보내기 ──
def test_send_config_only_filled_fields():
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x1", "A")]
    assert ctl.send_config("x1", name=" 새이름 ", code="", max_quality="보통", allow_files=None)
    assert ctx["client"].calls[-1] == ("cfg", "x1", {"name": "새이름", "max_quality": "보통"})
    assert "적용됨" in ui.last("show_info")[1]


def test_send_config_all_fields_and_allow_false():
    ctl, ctx, _ = make_ctl()
    ctx["client"].agents = [ag("x1", "A")]
    ctl.send_config("x1", "n", "code", "고화질", False)
    assert ctx["client"].calls[-1][2] == {"name": "n", "access_code": "code", "max_quality": "고화질",
                                          "allow_files": False}
    assert ctl.remembered_code("x1") == "code"          # 새 코드는 다음 연결 때 미리 채움


def test_send_config_offline_message():
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x1", "A", online=False)]
    ctx["client"].cfg_reply = {"online": False, "rev": 3}
    ctl.send_config("x1", name="n")
    assert "꺼져 있어 다음 접속 때 적용" in ui.last("show_info")[1]


def test_send_config_validation():
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x1", "A")]
    assert ctl.send_config("x1") is False                                  # 보낼 것이 없음
    assert ctl.send_config("x1", name="n" * 41) is False
    assert ctl.send_config("x1", code="c" * 65) is False
    assert ctl.send_config("x1", max_quality="초고화질") is False
    assert ctl.send_config("x1", name="n" * 40) is True                    # 경계값
    assert ctl.send_config("x1", code="c" * 64) is True
    assert len(ui.all("show_error")) == 4
    assert len([c for c in ctx["client"].calls if c[0] == "cfg"]) == 2


def test_send_config_error_reported():
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x1", "A")]
    ctx["client"].cfg_error = WanError("알 수 없는 현장 PC 입니다")
    assert ctl.send_config("x1", name="n") is True
    assert ui.last("show_error") == ("설정 보내기 실패", "알 수 없는 현장 PC 입니다")
    assert ui.all("set_busy")[-1] == ("config", False)


def test_send_config_does_not_log_access_code():
    ctl, ctx, ui = make_ctl()
    ctx["client"].agents = [ag("x1", "A")]
    ctl.send_config("x1", code="supersecret")
    logs = [l for c in ui.all("set_log") for l in c[0]]
    assert logs and not any("supersecret" in l for l in logs)
    assert "supersecret" not in " ".join(str(c) for c in ctx["log"].call_args_list)


# ── 사용량 · 로그 ──
def test_usage_text():
    ctl, _, ui = make_ctl()
    assert ctl.usage_texts() == ("오늘 5.0 MB", "이번 달 2.0 GB")
    assert ctl.usage_text() == "오늘 5.0 MB · 이번 달 2.0 GB"
    ctl.refresh_usage()
    assert ui.last("set_usage") == ("오늘 5.0 MB", "이번 달 2.0 GB")
    assert "절약" in wui.USAGE_HINT and "고화질" in wui.USAGE_HINT


def test_log_keeps_last_lines_with_time():
    ctl, _, ui = make_ctl()
    for i in range(10):
        ctl.log(f"줄{i}")
    lines = ui.last("set_log")[0]
    assert len(lines) == wui.LOG_LINES
    assert lines[-1].endswith("줄9") and lines[0].endswith("줄4")
    assert lines[-1].startswith(f"{datetime.fromtimestamp(NOW):%H:%M:%S}")


def test_ticks_refresh_expected_parts():
    ctl, _, ui = make_ctl()
    ctl.tick_slow()
    assert {c[0] for c in ui.calls} == {"set_server_status", "set_rows"}
    ui.calls.clear()
    ctl.tick_fast()
    assert {c[0] for c in ui.calls} == {"set_usage", "set_field_info", "set_field_status"}


# ═════════════════════════════════════════
# build_tab — 가짜 tkinter 로 위젯 만들기 경로 실행
# ═════════════════════════════════════════
@pytest.fixture
def fake_tk(monkeypatch):
    FakeVar.created = []
    tk = MagicMock(name="tkinter")
    tk.StringVar = tk.BooleanVar = FakeVar
    tk.TclError = FakeTclError
    tk.ttk, tk.messagebox, tk.simpledialog = (MagicMock(name=n) for n in ("ttk", "messagebox", "simpledialog"))
    # MagicMock 은 호출마다 같은 반환값을 주므로, 위젯마다 따로 만들어 주도록 함
    for fn in (tk.Label, tk.Frame, tk.Text, tk.Checkbutton, tk.Toplevel, tk.ttk.Treeview, tk.ttk.Scrollbar,
               tk.ttk.Combobox):
        fn.side_effect = lambda *a, **k: MagicMock()
    for name, mod in (("tkinter", tk), ("tkinter.ttk", tk.ttk), ("tkinter.messagebox", tk.messagebox),
                      ("tkinter.simpledialog", tk.simpledialog)):
        monkeypatch.setitem(sys.modules, name, mod)
    return tk


def make_app():
    app = MagicMock(name="app")
    for n in ("BG", "PANEL", "TEXT", "TEXT_DIM", "BLUE", "GREEN", "RED", "YELLOW", "BORDER", "INPUT_BORDER",
              "SELECT_BG", "LOG_BG"):
        setattr(app, n, "#123456")
    app.GREEN, app.YELLOW, app.RED = "#0a0", "#fa0", "#d00"
    app.FONT = app.FONT_BOLD = app.FONT_SMALL = app.FONT_INPUT = app.FONT_HEAD = ("f", 9)
    app.FONT_MONO_SMALL = ("m", 8)
    for fn in (app._button, app._entry, app._card, app._field_label, app._section_title):
        fn.side_effect = lambda *a, **k: MagicMock()
    return app


def make_tab(fake_tk, **ctx_over):
    app = make_app()
    parent = MagicMock(name="parent")
    ctx = {"cfg": FakeCfg(relay_url="ws://h:1", relay_key="k", enabled=True), "client": FakeClient(),
           "usage": FakeUsage(), "supervisor_state": lambda: {"state": "running", "pid": 9},
           "set_field_mode": MagicMock(), "open_remote": MagicMock(), "px": lambda v: v, "log": MagicMock()}
    ctx.update(ctx_over)
    tab = wui.build_tab(app, parent, ctx)
    # 시험을 위해 화면 스레드/작업 스레드를 동기로
    tab.controller.schedule = lambda fn: fn()
    tab.controller._spawn = lambda fn: fn()
    return tab, app, ctx


def button_cmd(app, text):
    for c in app._button.call_args_list:
        if c.args[1] == text:
            return c.args[3]
    raise AssertionError(f"버튼 없음: {text}")


def test_build_tab_runs_and_wires_client(fake_tk):
    prev = MagicMock()
    client = FakeClient()
    client.on_change = prev
    tab, app, ctx = make_tab(fake_tk, client=client)
    assert isinstance(tab, wui.WanTab) and isinstance(tab.controller, wui.WanTabController)
    assert tab.url_var.get() == "ws://h:1" and tab.key_var.get() == "k" and tab.enabled_var.get() is True
    texts = [c.args[1] for c in app._button.call_args_list]
    assert {"적용 후 연결", "원격 연결", "설정 보내기"} <= set(texts)
    # on_change 연결 — 원래 콜백도 계속 불림
    client.on_change("agents")
    prev.assert_called_once_with("agents")
    # 주기 갱신 예약
    delays = [c.args[0] for c in tab.root.after.call_args_list if c.args and isinstance(c.args[0], int)]
    assert wui.SLOW_MS in delays and wui.FAST_MS in delays
    # 첫 화면 채움 (화면 스레드 대신 동기 schedule 로 다시 한 번)
    tab.controller.refresh_all()
    assert "A1B2-C3D4-E5F6" in tab.id_lbl.configure.call_args.kwargs["text"]
    assert tab.day_lbl.configure.call_args.kwargs["text"] == "오늘 5.0 MB"


def test_build_tab_columns_and_treeview(fake_tk):
    tab, _, _ = make_tab(fake_tk)
    ttk = sys.modules["tkinter.ttk"]
    assert ttk.Treeview.call_args.kwargs["columns"] == wui.COLUMNS
    assert wui.COLUMNS == ("이름", "ID", "상태", "마지막 접속", "버전", "설정")
    assert tab.tree.heading.call_count == len(wui.COLUMNS)


def test_tab_apply_server_button(fake_tk):
    tab, app, ctx = make_tab(fake_tk)
    tab.url_var.set("ws://new:2")
    tab.key_var.set("kk")
    button_cmd(app, "적용 후 연결")()
    assert ctx["cfg"].d["relay_url"] == "ws://new:2"
    assert ctx["client"].calls[-2:] == ["stop", "start"]


def test_tab_field_checkbox(fake_tk):
    tab, _, ctx = make_tab(fake_tk)
    tab.enabled_var.set(False)
    tab._field_toggled()
    ctx["set_field_mode"].assert_called_with(False)
    tab.set_field_enabled(True)
    assert tab.enabled_var.get() is True


def test_tab_connect_flow(fake_tk):
    tab, app, ctx = make_tab(fake_tk)
    ctx["client"].agents = [ag("x1", "현장A")]
    tab.tree.selection.return_value = ("x1",)
    sys.modules["tkinter.simpledialog"].askstring.return_value = "9999"
    button_cmd(app, "원격 연결")()
    kw = sys.modules["tkinter.simpledialog"].askstring.call_args.kwargs
    assert kw["show"] == "*" and kw["initialvalue"] == ""
    assert ctx["client"].calls[-1] == ("open", "x1", "9999", "현장A")
    ctx["open_remote"].assert_called_once()
    button_cmd(app, "원격 연결")()                                  # 두 번째 — 기억한 코드가 미리 채워짐
    assert sys.modules["tkinter.simpledialog"].askstring.call_args.kwargs["initialvalue"] == "9999"


def test_tab_connect_cancel_and_no_selection(fake_tk):
    tab, app, ctx = make_tab(fake_tk)
    ctx["client"].agents = [ag("x1", "A")]
    tab.tree.selection.return_value = ()
    button_cmd(app, "원격 연결")()
    assert sys.modules["tkinter.messagebox"].showinfo.called
    tab.tree.selection.return_value = ("x1",)
    sys.modules["tkinter.simpledialog"].askstring.return_value = None      # 취소
    button_cmd(app, "원격 연결")()
    assert not any(isinstance(c, tuple) for c in ctx["client"].calls)


def test_tab_config_dialog_sends_only_filled(fake_tk):
    tab, app, ctx = make_tab(fake_tk)
    ctx["client"].agents = [ag("x1", "A")]
    tab.tree.selection.return_value = ("x1",)
    before = len(FakeVar.created)
    app._button.reset_mock()
    tab._config_dialog()
    name_v, code_v, q_v, f_v = FakeVar.created[before:before + 4]
    assert q_v.get() == wui.KEEP and f_v.get() == wui.KEEP
    name_v.set("새 이름")
    q_v.set("보통")
    f_v.set("차단")
    button_cmd(app, "보내기")()
    assert ctx["client"].calls[-1] == ("cfg", "x1", {"name": "새 이름", "max_quality": "보통", "allow_files": False})


def test_tab_config_dialog_keep_everything_shows_error(fake_tk):
    tab, app, ctx = make_tab(fake_tk)
    ctx["client"].agents = [ag("x1", "A")]
    tab.tree.selection.return_value = ("x1",)
    app._button.reset_mock()
    tab._config_dialog()
    button_cmd(app, "보내기")()
    assert sys.modules["tkinter.messagebox"].showerror.called
    assert not any(isinstance(c, tuple) for c in ctx["client"].calls)


def test_tab_set_rows_rebuild_then_update_in_place(fake_tk):
    tab, _, _ = make_tab(fake_tk)
    rows = [("a1", ("A", "ID", "켜짐", "지금", "1", "적용됨")), ("b2", ("B", "ID", "꺼짐", "-", "1", "대기 중"))]
    tab.tree.get_children.return_value = ()
    tab.tree.selection.return_value = ()
    tab.set_rows(rows)
    assert tab.tree.insert.call_count == 2
    assert tab.tree.insert.call_args_list[1].kwargs["tags"] == ("off",)
    tab.tree.insert.reset_mock()
    tab.tree.get_children.return_value = ("a1", "b2")
    tab.set_rows(rows)
    assert tab.tree.insert.call_count == 0 and tab.tree.item.call_count == 2
    tab.tree.get_children.return_value = ("a1", "zz")
    tab.tree.selection.return_value = ("a1",)
    tab.set_rows(rows)
    tab.tree.selection_set.assert_called_with(["a1"])


def test_tab_simple_setters(fake_tk):
    tab, app, _ = make_tab(fake_tk)
    tab.set_server_status("● 연결됨", "ok")
    assert tab.server_lbl.configure.call_args.kwargs == {"text": "● 연결됨", "fg": app.GREEN}
    tab.set_server_status("● 끊김", "bad")
    assert tab.server_lbl.configure.call_args.kwargs["fg"] == app.RED
    tab.set_field_status("5초 뒤 재시작", "warn")
    assert tab.field_lbl.configure.call_args.kwargs["fg"] == app.YELLOW
    tab.set_usage("오늘 1 B", "이번 달 2 B")
    tab.set_log(["a", "b"])
    tab.log_text.insert.assert_called_with("end", "a\nb")
    tab.set_busy("connect", True)
    assert tab.connect_btn.configure.call_args.kwargs == {"state": "disabled"}
    tab.set_busy("nothing", True)
    tab.show_error("t", "m")
    tab.show_info("t", "m")
    assert sys.modules["tkinter.messagebox"].showerror.call_args.args == ("t", "m")


def test_tab_schedule_marshals_to_root_after_and_guards(fake_tk):
    tab, _, _ = make_tab(fake_tk)
    ran = []
    tab._schedule(lambda: ran.append(1))
    ms, run = tab.root.after.call_args.args
    assert ms == 0 and ran == []
    run()
    assert ran == [1]

    def bad():
        raise FakeTclError("gone")
    tab._schedule(bad)
    tab.root.after.call_args.args[1]()            # TclError 는 삼킴
    tab._schedule(lambda: 1 / 0)
    tab.root.after.call_args.args[1]()            # 그 밖의 오류는 로그로
    tab.stop()
    tab._schedule(lambda: ran.append(2))
    tab.root.after.call_args.args[1]()
    assert ran == [1]
    tab.root.after.side_effect = RuntimeError("main thread is not in main loop")
    tab._schedule(lambda: None)                   # 예외 없음


def test_tab_loops_stop_after_stop(fake_tk):
    tab, _, _ = make_tab(fake_tk)
    tab.root.after.reset_mock()
    tab._loop_slow()
    assert tab.root.after.call_args.args[0] == wui.SLOW_MS
    tab.root.after.reset_mock()
    tab.stop()
    tab._loop_slow()
    tab._loop_fast()
    assert not tab.root.after.called


def test_tab_log_method_feeds_log_widget(fake_tk):
    tab, _, _ = make_tab(fake_tk)
    tab.log("안녕")
    assert "안녕" in tab.log_text.insert.call_args.args[1]
