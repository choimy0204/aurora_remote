"""사내 Git 으로 접속 정보 주고받기 — 진짜 git (로컬 bare 저장소를 사내 서버 대신 씀)"""

import os
import subprocess

import pytest

from wan import connect_code as cc
from wan import connfile, gitsync
from wan.runtime import WanRuntime
from wan_helpers import eventually

pytestmark = pytest.mark.skipif(not gitsync.available(), reason="git 없음")


@pytest.fixture
def remote(tmp_path):
    path = str(tmp_path / "company.git")
    subprocess.run(["git", "init", "--bare", "-q", path], check=True)
    return path


@pytest.fixture(autouse=True)
def git_identity(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def fixed_lan_address(monkeypatch):
    import wan.runtime as rt
    monkeypatch.setattr(rt, "lan_addresses", lambda: ["192.168.0.140"])


CODE1 = cc.encode(["wss://192.168.0.140:9100"], "key-one", fp="ab" * 32)
CODE2 = cc.encode(["wss://203.0.113.5:9100"], "key-two", fp="cd" * 32)


def test_publish_then_fetch_roundtrip_and_unchanged_is_not_pushed_again(remote, tmp_path):
    assert gitsync.publish(remote, str(tmp_path / "a"), CODE1) is True
    assert gitsync.publish(remote, str(tmp_path / "a"), CODE1) is False                  # 같은 내용은 올리지 않음
    text = gitsync.fetch_conn(remote, str(tmp_path / "b"))
    assert cc.decode_full(text) == (["wss://192.168.0.140:9100"], "key-one", "ab" * 32)
    assert gitsync.publish(remote, str(tmp_path / "a"), CODE2) is True                    # 키를 바꿔 다시 올림
    assert cc.decode(gitsync.fetch_conn(remote, str(tmp_path / "b")))[1] == "key-two"     # 받는 쪽이 최신으로 맞춤


def test_second_publisher_clone_stays_in_sync(remote, tmp_path):
    gitsync.publish(remote, str(tmp_path / "a"), CODE1)
    gitsync.publish(remote, str(tmp_path / "other"), CODE2)                               # 다른 PC 에서 올림
    assert cc.decode(gitsync.fetch_conn(remote, str(tmp_path / "a")))[1] == "key-two"


def test_file_in_subfolder_and_missing_file_message(remote, tmp_path):
    gitsync.publish(remote, str(tmp_path / "a"), CODE1, filename="config/aurora_wan.conn")
    assert cc.kind_of(gitsync.fetch_conn(remote, str(tmp_path / "b"), "config/aurora_wan.conn")) == "relay"
    with pytest.raises(gitsync.GitError, match="읽지 못했습니다"):
        gitsync.fetch_conn(remote, str(tmp_path / "c"), "없는파일.conn")


def test_wrong_repository_and_no_git_messages(tmp_path, monkeypatch):
    with pytest.raises(gitsync.GitError, match="찾을 수 없습니다"):
        gitsync.fetch_conn(str(tmp_path / "nope.git"), str(tmp_path / "x"))
    monkeypatch.setattr(gitsync, "available", lambda: False)
    with pytest.raises(gitsync.GitError, match="git 이 설치되어 있지"):
        gitsync.fetch_conn("x", str(tmp_path / "y"))


def test_broken_clone_directory_is_recreated(remote, tmp_path):
    gitsync.publish(remote, str(tmp_path / "a"), CODE1)
    dest = str(tmp_path / "b")
    gitsync.fetch_conn(remote, dest)
    with open(os.path.join(dest, ".git", "HEAD"), "w") as f:
        f.write("깨짐")
    assert cc.kind_of(gitsync.fetch_conn(remote, dest)) == "relay"


def test_publish_rejects_non_connection_text(remote, tmp_path):
    with pytest.raises(gitsync.GitError, match="올릴 접속 정보"):
        gitsync.publish(remote, str(tmp_path / "a"), "hello")


def test_authentication_failure_text(monkeypatch):
    msg = gitsync._explain("fatal: Authentication failed for 'https://git.corp/x.git'", ["clone"])
    assert "로그인" in msg
    assert "닿지" in gitsync._explain("fatal: unable to access: Could not resolve host", ["fetch"])


# ── 런타임 ──
def runtime(tmp_path, name, **cfg):
    r = WanRuntime("2.16.0", lambda: ["x"], None, str(tmp_path / name), lambda m: None)
    r.client.start = lambda: None
    r.client.stop = lambda: None
    r.cfg.update(**cfg)
    return r


def test_runtime_applies_connection_from_git_and_follows_key_changes(remote, tmp_path):
    import socket
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()
    port = srv.getsockname()[1]
    try:
        code = cc.encode([f"wss://127.0.0.1:{port}"], "회사-키-1", fp="ef" * 32)
        gitsync.publish(remote, str(tmp_path / "pub"), code)
        r = runtime(tmp_path, "user", wan_enabled=True, git_source=remote)
        assert r.git_sync_now()["state"] == "ok"
        assert (r.cfg.get("relay_url"), r.cfg.get("relay_key"), r.cfg.get("relay_fp")) == (f"wss://127.0.0.1:{port}", "회사-키-1", "ef" * 32)
        before = r.cfg.get("conn_hash")
        r.git_sync_now()
        assert r.cfg.get("conn_hash") == before                                           # 같은 내용은 다시 적용하지 않음
        gitsync.publish(remote, str(tmp_path / "pub"), cc.encode([f"wss://127.0.0.1:{port}"], "회사-키-2", fp="ef" * 32))
        r.git_sync_now()
        assert r.cfg.get("relay_key") == "회사-키-2"                                      # 키를 바꿔 올리면 따라옴
    finally:
        srv.close()


def test_nothing_happens_when_wan_is_off_or_no_source(remote, tmp_path, monkeypatch):
    monkeypatch.setattr(gitsync, "fetch_conn", lambda *a, **k: pytest.fail("꺼져 있는데 Git 에 접속함"))
    assert runtime(tmp_path, "a", wan_enabled=False, git_source=remote).git_sync_now()["state"] == "off"
    assert runtime(tmp_path, "b", wan_enabled=True, git_source="").git_sync_now()["state"] == "off"


def test_errors_are_reported_in_state_not_raised(tmp_path):
    r = runtime(tmp_path, "u", wan_enabled=True, git_source=str(tmp_path / "missing.git"))
    st = r.git_sync_now()
    assert st["state"] == "error" and "찾을 수 없" in st["msg"]
    assert r.git_state == st


def test_relay_pc_publishes_and_does_not_reapply_its_own_file(remote, tmp_path):
    r = runtime(tmp_path, "relaypc", wan_enabled=True, relay_host=True, relay_key="내키")
    assert r.git_publish(remote, CODE1) is True
    assert r.cfg.get("git_source") == remote and r.cfg.get("conn_hash") == connfile.fingerprint(CODE1)
    r.git_sync_now()
    assert r.cfg.get("relay_url") == "" and r.cfg.get("relay_key") == "내키"              # 자기 자신에게 적용하지 않음
    with pytest.raises(gitsync.GitError, match="주소를 입력"):
        r.git_publish("", CODE1)


def test_other_relay_info_is_ignored_on_a_relay_pc(remote, tmp_path):
    gitsync.publish(remote, str(tmp_path / "pub"), CODE2)
    r = runtime(tmp_path, "relaypc", wan_enabled=True, relay_host=True, relay_key="내키", git_source=remote)
    r.git_sync_now()
    assert r.cfg.get("relay_key") == "내키"


def test_git_loop_checks_periodically_only_while_on(remote, tmp_path, monkeypatch):
    import threading
    import wan.runtime as rt
    monkeypatch.setattr(rt, "GIT_FIRST_DELAY", 0.05)
    monkeypatch.setattr(rt, "GIT_EVERY", 0.1)
    monkeypatch.setattr(rt, "GIT_TICK", 0.02)
    r = runtime(tmp_path, "u", wan_enabled=True, git_source=remote)
    calls = []
    monkeypatch.setattr(r, "git_sync_now", lambda: calls.append(1))
    stop = threading.Event()
    t = threading.Thread(target=r._git_loop, args=(stop,), daemon=True)
    t.start()
    eventually(lambda: len(calls) >= 3, msg="주기적으로 확인")
    stop.set()
    t.join(2)
    assert not t.is_alive()


# ── 접속 코드 새로고침 · 매일 0시 자동 갱신 ──
import datetime


def relay_pc(tmp_path, remote, today=datetime.date(2026, 10, 26), **cfg):
    r = runtime(tmp_path, "relaypc", wan_enabled=True, relay_host=True, relay_key="옛키", relay_port=9100,
                git_source=remote, **cfg)
    r._today = lambda: today
    r.relay_fp = lambda: "ab" * 32
    r.relay_command = lambda: ["x"]
    r.restart_relay = lambda: setattr(r, "restarted", getattr(r, "restarted", 0) + 1)
    return r


def test_refresh_makes_new_random_key_republishes_and_old_code_is_gone(remote, tmp_path):
    r = relay_pc(tmp_path, remote)
    gitsync.publish(remote, str(tmp_path / "seed"), r.relay_code())
    old_key = r.cfg.get("relay_key")
    r.relay_supervisor = object()                                    # 안내 서버가 돌고 있는 상태
    res = r.rotate_relay_key()
    new_key = r.cfg.get("relay_key")
    assert new_key != old_key and len(new_key) >= 16 and res["published"] and res["error"] == ""
    assert r.restarted == 1                                          # 새 키로 안내 서버를 다시 시작
    on_git = cc.decode_full(gitsync.fetch_conn(remote, str(tmp_path / "reader")))
    assert on_git == (["wss://192.168.0.140:9100"], new_key, "ab" * 32)       # 저장소에는 새 접속 정보만 남음
    assert cc.decode(res["code"])[1] == new_key
    assert r.cfg.get("relay_rotated_on") == "2026-10-26"
    assert r.rotate_relay_key()["key"] != new_key                     # 누를 때마다 다른 키


def test_refresh_without_git_still_rotates_and_reports_no_error(tmp_path):
    r = runtime(tmp_path, "pc", wan_enabled=True, relay_host=True, relay_key="옛키")
    r.relay_command = None
    r.relay_fp = lambda: ""
    res = r.rotate_relay_key()
    assert res["published"] is False and res["error"] == "" and r.cfg.get("relay_key") != "옛키"


def test_refresh_when_git_is_down_keeps_new_key_and_retries_later(remote, tmp_path):
    r = relay_pc(tmp_path, remote)
    r.cfg.update(git_source=str(tmp_path / "gone.git"))
    res = r.rotate_relay_key()
    assert res["published"] is False and "찾을 수 없" in res["error"] and r._publish_pending
    assert r.git_state["state"] == "error"
    subprocess.run(["git", "init", "--bare", "-q", str(tmp_path / "gone.git")], check=True)    # 서버가 살아남
    r._maybe_rotate()
    assert r._publish_pending is False
    assert cc.decode(gitsync.fetch_conn(str(tmp_path / "gone.git"), str(tmp_path / "rd")))[1] == r.cfg.get("relay_key")


def test_midnight_rotation_once_per_day(remote, tmp_path):
    day1 = datetime.date(2026, 10, 26)
    r = relay_pc(tmp_path, remote, today=day1)
    r._maybe_rotate()
    assert r.cfg.get("relay_rotated_on") == "2026-10-26" and r.cfg.get("relay_key") == "옛키"   # 처음 켠 날은 건너뜀
    r._today = lambda: day1
    r._maybe_rotate()
    assert r.cfg.get("relay_key") == "옛키"                            # 같은 날에는 다시 만들지 않음
    r._today = lambda: day1 + datetime.timedelta(days=1)             # 자정이 지남
    r._maybe_rotate()
    key2 = r.cfg.get("relay_key")
    assert key2 != "옛키" and r.cfg.get("relay_rotated_on") == "2026-10-27"
    assert cc.decode(gitsync.fetch_conn(remote, str(tmp_path / "rd")))[1] == key2
    r._maybe_rotate()
    assert r.cfg.get("relay_key") == key2                             # 그날 두 번째 확인은 아무 일 없음
    r._today = lambda: day1 + datetime.timedelta(days=5)             # 며칠 꺼져 있다 켜져도 한 번만
    r._maybe_rotate()
    assert r.cfg.get("relay_key") != key2 and r.cfg.get("relay_rotated_on") == "2026-10-31"


@pytest.mark.parametrize("cfg", [dict(wan_enabled=False), dict(relay_rotate=False), dict(git_source=""),
                                 dict(relay_host=False)])
def test_no_automatic_rotation_when_off_or_without_git_or_not_relay(remote, tmp_path, cfg):
    r = relay_pc(tmp_path, remote, relay_rotated_on="2026-10-01")
    r.cfg.update(**cfg)
    r._maybe_rotate()
    assert r.cfg.get("relay_key") == "옛키"


def test_user_pc_polls_faster_when_key_is_rejected(remote, tmp_path, monkeypatch):
    import threading
    import wan.runtime as rt
    monkeypatch.setattr(rt, "GIT_FIRST_DELAY", 0.01)
    monkeypatch.setattr(rt, "GIT_EVERY", 100.0)
    monkeypatch.setattr(rt, "GIT_RETRY", 0.05)
    monkeypatch.setattr(rt, "GIT_TICK", 0.02)
    r = runtime(tmp_path, "u", wan_enabled=True, git_source=remote)
    r.client.state = "연결됨"
    calls = []
    monkeypatch.setattr(r, "git_sync_now", lambda: calls.append(1))
    stop = threading.Event()
    t = threading.Thread(target=r._git_loop, args=(stop,), daemon=True)
    t.start()
    eventually(lambda: len(calls) == 1, msg="처음 한 번")
    import time; time.sleep(0.3)
    assert len(calls) == 1                                            # 정상일 때는 10분 간격이라 더 안 함
    r.client.state = "키 불일치"
    eventually(lambda: len(calls) >= 3, msg="키가 막히면 자주 확인")
    stop.set(); t.join(2)


# ── 끝에서 끝까지: 진짜 안내 서버 프로세스 + 진짜 Git — 새로고침하면 사용자 PC 가 Git 으로 새 키를 받아 다시 붙는다 ──
def test_rotation_end_to_end_user_pc_reconnects_through_git(remote, tmp_path, monkeypatch):
    import sys
    import wan.runtime as rt
    from test_wan_relay_host import free_port, ROOT
    monkeypatch.setenv("AURORA_WAN_DIR", str(tmp_path / "relay_logs"))
    monkeypatch.setenv("PYTHONPATH", ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.setattr(rt, "lan_addresses", lambda: ["127.0.0.1"])
    monkeypatch.setattr(rt, "GIT_FIRST_DELAY", 0.05)
    monkeypatch.setattr(rt, "GIT_EVERY", 0.2)
    monkeypatch.setattr(rt, "GIT_RETRY", 0.1)
    monkeypatch.setattr(rt, "GIT_TICK", 0.05)
    port = free_port()
    relay = WanRuntime("2.16.0", lambda: ["x"], None, str(tmp_path / "relaypc"), lambda m: None,
                       relay_command=lambda: [sys.executable, "-m", "wan.relay_run", "--parent-pid", str(os.getpid()),
                                              "--discover-port", str(free_port())])
    relay.cfg.update(wan_enabled=True, relay_host=True, relay_port=port, relay_key="처음-키-aaaa", git_source=remote)
    user = runtime(tmp_path, "user", wan_enabled=True, git_source=remote)
    user.client = __import__("wan.client", fromlist=["WanClient"]).WanClient(user.cfg, None, "2.16.0", log=lambda m: None)
    try:
        relay.set_relay_host(True)
        eventually(lambda: relay.relay_state()["state"] == "running", 15)
        eventually(lambda: __import__("test_wan_relay_host").health(port), 15)
        relay.git_publish(remote, relay.relay_code())
        user.start()
        eventually(lambda: user.client.state == "연결됨", 20, msg=f"Git 의 접속 정보로 자동 접속: {user.client.state}")
        first_key = user.cfg.get("relay_key")
        res = relay.rotate_relay_key()                                     # 새로고침
        assert res["published"] and relay.cfg.get("relay_key") != first_key
        eventually(lambda: user.cfg.get("relay_key") == relay.cfg.get("relay_key"), 20, msg="Git 에서 새 키를 받음")
        eventually(lambda: user.client.state == "연결됨" and user.cfg.get("relay_key") != first_key, 25,
                   msg=f"새 키로 다시 접속: {user.client.state}")
    finally:
        user.shutdown()
        relay.shutdown()
