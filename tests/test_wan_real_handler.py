"""진짜 aurora_client.CommandHandler(운영 중인 파일 API)를 WAN 통로로 시험
(Windows 전용 부분은 tests/winshim.py 로 대체 — 파일 API 는 Linux 에서도 그대로 동작)"""

import hashlib
import os
import threading
from http.server import ThreadingHTTPServer

import pytest

from wan import protocol as P
from wan_helpers import Rig, pattern

LAN_KEY = "lan-export-key"


@pytest.fixture
def real(tmp_path, monkeypatch):
    # aurora_client 를 불러올 때 D:\RemoteConfig 폴더를 만들려 하므로 임시 폴더에서 실행
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "a.txt").write_text("안녕하세요", encoding="utf-8")
    (desktop / "big.bin").write_bytes(pattern(3 * 1024 * 1024 + 5))
    import winshim
    mod = winshim.load_client()
    mod.config["export_key"] = LAN_KEY
    mod.config["remote_enabled"] = True
    discover_calls = []
    monkeypatch.setattr(mod, "export_discover", lambda *a, **k: discover_calls.append(a) or [])
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.CommandHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    rig = Rig(local_port=server.server_address[1], key_getter=lambda: mod.config.get("export_key", ""))
    rig.mod, rig.desktop, rig.discover_calls = mod, desktop, discover_calls
    yield rig
    rig.close()
    server.shutdown()
    server.server_close()


def test_info_and_roots_work_through_wan(real):
    assert real.user.request("GET", "/info").json()["api"] == real.mod.EXPORT_API_VER
    roots = real.user.request("GET", "/fs/roots").json()
    assert str(real.desktop) in [r["path"] for r in roots]


def test_directory_listing_with_korean_names(real):
    items = real.user.request("GET", "/fs/list", {"path": str(real.desktop)}).json()
    assert {i["name"] for i in items} == {"a.txt", "big.bin"}


def test_download_and_resume_match_original(real):
    path, size = str(real.desktop / "big.bin"), 3 * 1024 * 1024 + 5
    with real.user.request("GET", "/fs/file", {"path": path}, stream=True, timeout=(3, 30)) as r:
        assert r.status_code == 200 and int(r.headers["Content-Length"]) == size
        assert hashlib.sha256(b"".join(r.iter_content(1 << 20))).hexdigest() == hashlib.sha256(pattern(size)).hexdigest()
    with real.user.request("GET", "/fs/file", {"path": path}, headers={"Range": "bytes=1000000-"}, stream=True) as r:
        assert r.status_code == 206 and r.headers["Content-Range"].startswith("bytes 1000000-")
        assert b"".join(r.iter_content(1 << 20)) == pattern(size)[1000000:]


def test_upload_writes_file_with_mtime(real):
    data = pattern(2 * 1024 * 1024 + 3)
    dest = str(real.desktop / "sub" / "uploaded.bin")
    r = real.user.request("PUT", "/fs/upload", {"path": dest, "mtime": "1700000000"}, data=data, timeout=(3, 30))
    assert r.ok and r.json()["size"] == len(data)
    assert open(dest, "rb").read() == data and int(os.path.getmtime(dest)) == 1700000000
    assert not os.path.exists(dest + ".part")


def test_mkdir_and_rename(real):
    new = str(real.desktop / "newdir")
    assert real.user.request("PUT", "/fs/mkdir", {"path": new}).ok and os.path.isdir(new)
    r = real.user.request("PUT", "/fs/rename", {"path": new, "name": "renamed"})
    assert r.ok and os.path.isdir(str(real.desktop / "renamed"))


def test_real_handler_still_restricts_paths_outside_allowed_roots(real):
    r = real.user.request("GET", "/fs/list", {"path": "/etc"})
    assert r.status_code == 403 and "허용되지 않은 경로" in r.json()["error"]
    r = real.user.request("GET", "/fs/file", {"path": str(real.desktop / ".." / ".." / "etc" / "passwd")})
    assert r.status_code == 403


def test_lan_only_features_are_unreachable_over_wan(real):
    for method, route, params in (("POST", "/command", None), ("GET", "/discover", None),
                                  ("GET", "/fs/list", {"path": str(real.desktop), "via": "10.0.0.2"})):
        assert real.user.request(method, route, params).status_code == 403
    assert real.discover_calls == []             # 공정망 스캔은 실행되지 않음


def test_wan_user_does_not_need_the_lan_key_but_field_must_have_it(real):
    """접속 코드를 통과한 사용자는 LAN 키를 몰라도 되고, 현장의 LAN 키가 바뀌면 WAN 도 그 키를 따라간다"""
    assert real.user.request("GET", "/info").ok
    real.mod.config["export_key"] = "changed-key"
    assert real.user.request("GET", "/info").ok                       # bridge 가 키를 매번 읽음
    real.bridge.key_getter = lambda: "wrong"
    r = real.user.request("GET", "/info")
    assert r.status_code == 403 and "인증 실패" in r.json()["error"]  # 키가 틀리면 진짜 핸들러가 거부


def test_remote_screen_disabled_flag_respected(real):
    real.mod.config["remote_enabled"] = False
    r = real.user.request("GET", "/remote/frame", {"w": 640, "h": 480})
    assert r.status_code == 403 and "꺼져" in r.json()["error"]


def test_wan_agent_entry_point_exists_and_uses_loopback(real):
    """--wan-agent 로 실행되는 진입점이 있고 LAN 포트 설정과 무관하게 127.0.0.1 임의 포트를 쓴다"""
    import inspect
    src = inspect.getsource(real.mod._run_wan_agent)
    assert '("127.0.0.1", 0)' in src and "run_agent" in src
    assert "listen_port" not in src and "LISTEN_PORT" not in src          # LAN 포트와 분리
