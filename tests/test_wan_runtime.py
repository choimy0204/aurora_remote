"""WanRuntime 시험 — 현장 모드 켜기/끄기 · 시작 조건 · 업데이트 흐름 · 오류가 밖으로 새지 않음"""

import os
import sys
import time

import pytest

import wan.runtime as rt
from wan_helpers import eventually

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


class FakeClient:
    def __init__(self, *a, **k):
        self.started = self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "WanClient", FakeClient)
    made = []

    def _make(**kw):
        logs = []
        r = rt.WanRuntime("2.16.0", lambda: SLEEPER, kw.pop("apply_update", None), str(tmp_path / f"c{len(made)}"),
                          logs.append)
        r.logs = logs
        made.append(r)
        return r
    yield _make
    for r in made:
        r.shutdown()


def test_nothing_runs_by_default(make):
    r = make()
    r.start()
    assert r.supervisor is None and r.client.started == 0
    assert r.supervisor_state()["state"] == "stopped"


def test_user_mode_connects_client_only_when_server_address_is_set(make):
    r = make()
    r.cfg.update(relay_url="ws://x:1")
    r.start()
    assert r.client.started == 1 and r.supervisor is None


def test_field_mode_starts_and_stops_agent_process(make):
    import psutil
    r = make()
    r.set_field_mode(True)
    eventually(lambda: r.supervisor_state()["state"] == "running" and r.supervisor_state()["pid"], msg="에이전트 실행")
    pid = r.supervisor_state()["pid"]
    assert r.cfg.get("enabled") is True and r.cfg.get("agent_id")          # 고유 ID 가 만들어짐
    r.set_field_mode(False)
    eventually(lambda: not psutil.pid_exists(pid), msg="에이전트 종료")
    assert r.supervisor is None and r.cfg.get("enabled") is False


def test_enabled_config_starts_agent_on_launch_and_skips_user_client(make):
    r = make()
    r.cfg.update(enabled=True, relay_url="ws://x:1")
    r.start()
    assert r.supervisor is not None and r.client.started == 0              # 현장 PC 는 탭에서 눌러야 사용자 연결


def test_stop_agent_keeps_enabled_flag(make):
    r = make()
    r.set_field_mode(True)
    r.stop_agent()
    assert r.supervisor is None and r.cfg.get("enabled") is True            # 다음 실행 때 다시 켜짐


def test_shutdown_is_idempotent_and_stops_everything(make):
    import psutil
    r = make()
    r.set_field_mode(True)
    eventually(lambda: r.supervisor_state()["pid"])
    pid = r.supervisor_state()["pid"]
    r.shutdown()
    r.shutdown()
    assert not psutil.pid_exists(pid) and r.client.stopped == 2


def test_errors_never_escape_to_caller(make, monkeypatch):
    r = make()
    r.cfg.update(enabled=True)
    monkeypatch.setattr(r, "agent_command", lambda: (_ for _ in ()).throw(RuntimeError("명령 생성 실패")))
    r.start()                                                               # 예외가 밖으로 나오지 않음
    assert any("실패" in m for m in r.logs)


def test_tab_context_has_everything_the_tab_needs(make):
    r = make()
    ctx = r.tab_context(open_remote=lambda s, a: None)
    assert {"cfg", "client", "usage", "supervisor_state", "set_field_mode", "open_remote", "px", "log"} <= set(ctx)


# ── 업데이트 ──
def test_update_disabled_without_server_or_when_off(make):
    r = make()
    assert r.check_update()["status"] == "off"
    r.cfg.update(relay_url="ws://x:1", auto_update=False)
    assert r.check_update()["status"] == "off"


def test_downloaded_update_is_handed_to_apply(make, monkeypatch):
    got = []
    r = make(apply_update=got.append)
    r.cfg.update(relay_url="ws://x:1")
    monkeypatch.setattr(rt.wan_update, "check_and_download",
                        lambda cfg, ver: {"status": "downloaded", "version": "2.17.0", "path": "p", "sha256": "s"})
    assert r.check_update()["status"] == "downloaded"
    assert got and got[0]["version"] == "2.17.0"


def test_latest_or_error_does_not_apply(make, monkeypatch):
    got = []
    r = make(apply_update=got.append)
    r.cfg.update(relay_url="ws://x:1")
    for status in ("latest", "none", "error"):
        monkeypatch.setattr(rt.wan_update, "check_and_download", lambda cfg, ver, s=status: {"status": s, "msg": "m"})
        r.check_update()
    assert not got


def test_already_downloaded_newer_version_is_used_without_downloading_again(make, monkeypatch):
    got = []
    r = make(apply_update=got.append)
    r.cfg.update(relay_url="ws://x:1")
    monkeypatch.setattr(rt.wan_update, "pending_update", lambda d: {"version": "2.17.0", "path": "p", "sha256": "s"})
    monkeypatch.setattr(rt.wan_update, "check_and_download",
                        lambda *a: pytest.fail("이미 받아 둔 것이 있는데 다시 내려받음"))
    r.check_update()
    assert got and got[0]["status"] == "downloaded"


def test_stale_pending_update_is_cleared(make, monkeypatch):
    cleared = []
    r = make()
    r.cfg.update(relay_url="ws://x:1")
    monkeypatch.setattr(rt.wan_update, "pending_update", lambda d: {"version": "2.16.0", "path": "p", "sha256": "s"})
    monkeypatch.setattr(rt.wan_update, "clear_pending", cleared.append)
    monkeypatch.setattr(rt.wan_update, "check_and_download", lambda *a: {"status": "latest", "msg": ""})
    r.check_update()
    assert cleared


def test_update_thread_waits_then_checks(make, monkeypatch):
    calls = []
    monkeypatch.setattr(rt, "UPDATE_FIRST_DELAY", 0.1)
    r = make()
    monkeypatch.setattr(r, "check_update", lambda: calls.append(1) or {})
    r.start()
    eventually(lambda: calls, msg="첫 확인")
    r.shutdown()
