"""aurora_client.py 에 추가한 WAN 연결 코드 시험 (Windows 전용 부분은 tests/winshim.py 로 대체)
- 이 코드는 운영 중인 LAN 프로그램 안에 들어가므로, 어떤 오류도 LAN 으로 번지지 않는지가 핵심"""

import os
import sys
import threading
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mod(tmp_path, monkeypatch):
    import winshim
    winshim.sandbox(monkeypatch, tmp_path)
    import winshim
    return winshim.load_client()


class FakeApp:
    """AuroraClientApp 메서드를 self 없이 불러 쓰기 위한 최소한의 가짜"""
    def __init__(self, wan=None):
        self._wan = wan
        self.logs = []
        self.root = MagicMock()
        self.quit_app = MagicMock()

    def add_log(self, msg):
        self.logs.append(msg)


def call(mod, name, app, *a, **k):
    return getattr(mod.AuroraClientApp, name)(app, *a, **k)


# ── 모듈 수준 격리 ──
def test_client_module_does_not_import_wan_at_top_level(mod):
    src = open(mod.__file__, encoding="utf-8").read()
    top_level = [l for l in src.splitlines() if l.startswith(("import wan", "from wan"))]
    assert top_level == [], "WAN 모듈은 함수 안에서 늦게 불러와야 LAN 시작에 영향이 없다"


def test_five_tabs_have_matching_hints(mod):
    assert len(mod.AuroraClientApp.TAB_HINTS) == 5


# ── 에이전트 명령줄 ──
def test_agent_command_for_frozen_and_script(mod, monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    cmd = call(mod, "_wan_agent_command", app)
    assert cmd[0] == sys.executable and cmd[1].endswith("aurora_client.py")
    assert cmd[2:] == ["--wan-agent", "--parent-pid", str(os.getpid())]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd = call(mod, "_wan_agent_command", app)
    assert cmd == [sys.executable, "--wan-agent", "--parent-pid", str(os.getpid())]


# ── 시작/종료는 오류를 삼킨다 ──
def test_start_and_shutdown_without_runtime_are_noops(mod):
    app = FakeApp(wan=None)
    call(mod, "_wan_start", app)
    call(mod, "_wan_shutdown", app)


def test_start_and_shutdown_swallow_runtime_errors(mod):
    wan = MagicMock()
    wan.start.side_effect = RuntimeError("시작 실패")
    wan.shutdown.side_effect = RuntimeError("종료 실패")
    app = FakeApp(wan=wan)
    call(mod, "_wan_start", app)                      # 예외가 밖으로 나오지 않음
    call(mod, "_wan_shutdown", app)
    assert any("시작 실패" in m for m in app.logs)


# ── 새 버전 적용 ──
def info(version="2.17.0"):
    return {"version": version, "path": "x.exe", "sha256": "s"}


def test_apply_update_ignores_same_or_older_version(mod, monkeypatch):
    app = FakeApp(wan=MagicMock())
    apply = MagicMock(return_value=None)
    monkeypatch.setattr(mod.aurora_update, "apply", apply)
    call(mod, "_wan_apply_update", app, info(mod.APP_VERSION))
    call(mod, "_wan_apply_update", app, info("1.0.0"))
    apply.assert_not_called()
    app.root.after.assert_not_called()


def test_apply_update_skips_version_already_tried(mod, monkeypatch):
    app = FakeApp(wan=MagicMock())
    monkeypatch.setattr(mod.aurora_update, "auto_already_tried", lambda kind, v: True)
    apply = MagicMock()
    monkeypatch.setattr(mod.aurora_update, "apply", apply)
    call(mod, "_wan_apply_update", app, info())
    apply.assert_not_called()
    assert any("이미 시도" in m for m in app.logs)


def test_apply_update_success_stops_agent_first_then_quits(mod, monkeypatch):
    order = []
    wan = MagicMock()
    wan.stop_agent.side_effect = lambda: order.append("stop_agent")
    app = FakeApp(wan=wan)
    monkeypatch.setattr(mod.aurora_update, "auto_already_tried", lambda *a: False)
    monkeypatch.setattr(mod.aurora_update, "apply", lambda *a: order.append("apply") or None)
    call(mod, "_wan_apply_update", app, info())
    assert order == ["stop_agent", "apply"]                 # 에이전트가 exe 를 잡고 있으면 교체가 실패함
    app.root.after.assert_called_once_with(0, app.quit_app)


def test_apply_update_failure_restarts_field_agent_and_keeps_running(mod, monkeypatch):
    wan = MagicMock()
    wan.cfg.get.return_value = True                          # enabled
    app = FakeApp(wan=wan)
    monkeypatch.setattr(mod.aurora_update, "auto_already_tried", lambda *a: False)
    monkeypatch.setattr(mod.aurora_update, "apply", lambda *a: "복사 실패")
    call(mod, "_wan_apply_update", app, info())
    wan.set_field_mode.assert_called_once_with(True)
    app.root.after.assert_not_called()
    assert any("복사 실패" in m for m in app.logs)


# ── --wan-agent 진입점 ──
def test_main_dispatches_to_agent_mode_before_taking_the_lan_lock(mod, monkeypatch):
    called = {}
    monkeypatch.setattr(mod, "_run_wan_agent", lambda: called.setdefault("agent", True))
    monkeypatch.setattr(mod, "_acquire_lock", lambda *a: called.setdefault("lock", True))
    monkeypatch.setattr(sys, "argv", ["AuroraClient.exe", "--wan-agent", "--parent-pid", "1"])
    mod.main()
    assert called == {"agent": True}                        # LAN 중복 실행 잠금(19701)을 건드리지 않음


def test_run_wan_agent_uses_loopback_server_and_passes_parent_pid(mod, monkeypatch):
    import wan.agent as wan_agent
    got = {}
    monkeypatch.setattr(wan_agent, "run_agent",
                        lambda cfg_dir, port, key_getter, version, parent_pid: got.update(
                            port=port, key=key_getter(), version=version, parent=parent_pid))
    mod.config["export_key"] = "k-1"
    monkeypatch.setattr(sys, "argv", ["x", "--wan-agent", "--parent-pid", "4321"])
    mod._run_wan_agent()
    assert got["parent"] == 4321 and got["key"] == "k-1" and got["version"] == mod.APP_VERSION
    assert got["port"] != mod.config.get("listen_port", 9001)       # LAN 포트와 별개
    # 그 포트는 127.0.0.1 에서만 열려 있다
    import socket
    s = socket.create_connection(("127.0.0.1", got["port"]), timeout=2)
    s.close()


def test_run_wan_agent_tolerates_missing_parent_pid(mod, monkeypatch):
    import wan.agent as wan_agent
    got = {}
    monkeypatch.setattr(wan_agent, "run_agent", lambda *a: got.update(parent=a[4]))
    monkeypatch.setattr(sys, "argv", ["x", "--wan-agent"])
    mod._run_wan_agent()
    assert got["parent"] is None
    monkeypatch.setattr(sys, "argv", ["x", "--wan-agent", "--parent-pid", "abc"])
    mod._run_wan_agent()
    assert got["parent"] is None


# ── 반입/반출 패널의 WAN 용 요청 ──
def test_wan_transfer_pane_routes_requests_through_the_session(mod):
    pane = object.__new__(mod._WanTransferPane)
    pane._session = MagicMock()
    pane._session.mode = "direct"
    pane._dev = {"label": "현장1"}
    pane._export_request("/fs/list", {"path": "D:\\"}, "ignored-target", timeout=(3, 9))
    pane._session.request.assert_called_once_with("GET", "/fs/list", {"path": "D:\\"}, headers=None, data=None,
                                                  stream=False, timeout=(3, 9))
    pane._export_request("/fs/file", {"path": "x"}, "t", headers={"Range": "bytes=5-"}, stream=True)
    assert pane._session.request.call_args.kwargs["headers"] == {"Range": "bytes=5-"}
    assert pane._session.request.call_args.kwargs["stream"] is True
    pane._export_request("/fs/upload", {"path": "x"}, "t", method="PUT", data=b"abc")
    assert pane._session.request.call_args.args[0] == "PUT" and pane._session.request.call_args.kwargs["data"] == b"abc"
    assert "직접 연결" in pane._export_route_text() and "현장1" in pane._export_route_text()
    assert mod._WanTransferPane._exp_connected is True
