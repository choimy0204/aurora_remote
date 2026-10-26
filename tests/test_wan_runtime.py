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
    r.cfg.update(wan_enabled=True)
    monkeypatch.setattr(r, "check_update", lambda: calls.append(1) or {})
    r.start()
    eventually(lambda: calls, msg="첫 확인")
    r.shutdown()


# ── WAN 켜기/끄기 ──
def test_new_install_has_wan_off_and_does_nothing_on_start(make, monkeypatch):
    r = make()
    assert r.wan_on() is False
    started = []
    monkeypatch.setattr(r, "apply_conn_file", lambda: started.append("conn"))
    monkeypatch.setattr(r, "_start_client", lambda: started.append("client"))
    r.start()
    assert started == [] and r._update_thread is None and r.supervisor is None and r.relay_supervisor is None


@pytest.mark.parametrize("field", [{"enabled": True}, {"relay_url": "ws://h:1"}, {"relay_host": True},
                                   {"direct_targets": {"1.2.3.4:9102": {}}}])
def test_existing_wan_users_stay_on_after_upgrade(make, field):
    r = make()
    r.cfg.update(**field)
    assert r.wan_on() is True                                         # 예전에 쓰던 흔적이 있으면 켜진 것으로 본다
    r.cfg.update(wan_enabled=False)
    assert r.wan_on() is False                                        # 사용자가 끈 뒤에는 흔적과 상관없이 꺼짐


def test_turning_on_starts_everything_saved_and_off_stops_it(make, monkeypatch):
    r = make()
    monkeypatch.setattr(r, "apply_conn_file", lambda: None)
    r.cfg.update(enabled=True, relay_url="ws://h:1", wan_enabled=False)
    r.set_wan_enabled(True)
    assert r.cfg.get("wan_enabled") is True and r.supervisor is not None and r._update_thread.is_alive()
    r.set_wan_enabled(True)                                           # 한 번 더 눌러도 중복 시작 없음
    sup = r.supervisor
    r.set_wan_enabled(False)
    assert r.supervisor is None and not r._running
    assert r.cfg.get("enabled") is True and r.cfg.get("relay_url") == "ws://h:1"   # 개별 설정은 그대로 — 다시 켜면 돌아옴
    r.set_wan_enabled(True)
    assert r.supervisor is not None and r.supervisor is not sup
    r.set_wan_enabled(False)


def test_update_check_is_skipped_while_wan_is_off(make, monkeypatch):
    r = make()
    r.cfg.update(relay_url="ws://h:1", wan_enabled=False)
    monkeypatch.setattr(rt.wan_update, "check_and_download", lambda *a: pytest.fail("꺼져 있는데 서버에 물어봄"))
    assert r.check_update() == {"status": "off"}
