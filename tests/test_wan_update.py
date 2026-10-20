"""WAN 새 버전 내려받기(wan/update.py) 시험 — 실제 안내 서버(RelayServer)를 띄워서 확인"""

import asyncio
import hashlib
import json
import os
import socket
import threading
import time

import pytest

from wan import update as U
from wan.server import RelayServer
from wan_helpers import RelayRunner, FakeCfg, pattern

SIZE = 5 * 1024 * 1024
EXE = "AuroraClient.exe"


# ─────────────────────────────────────────
# 시험 도구
# ─────────────────────────────────────────
class Cfg(FakeCfg):
    def __init__(self, url, key="", directory=""):
        super().__init__(relay_url=url, relay_key=key)
        self.dir = directory


class BadShaServer(RelayServer):
    """update_info 가 틀린 sha256 을 알려 주는 서버 (end 의 sha 도 같이 틀림)"""

    def update_info(self, kind="client"):
        info = super().update_info(kind)
        if info:
            info["sha256"] = "0" * 64
        return info


class EndMismatchServer(RelayServer):
    """update_info 의 sha 는 맞는데 end 로 다른 sha 를 보내는 서버"""

    async def _update(self, ws, hello):
        info = self.update_info("client")
        await ws.send(json.dumps({"t": "update_info", "available": True, **{k: info[k] for k in ("version", "size", "sha256")}}))
        await ws.recv()
        with open(info["path"], "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                await ws.send(block)
        await ws.send(json.dumps({"t": "end", "sha256": "f" * 64}))


class DropServer(RelayServer):
    """조각을 몇 개 보내다가 연결을 닫아 버리는 서버"""

    async def _update(self, ws, hello):
        info = self.update_info("client")
        await ws.send(json.dumps({"t": "update_info", "available": True, **{k: info[k] for k in ("version", "size", "sha256")}}))
        await ws.recv()
        with open(info["path"], "rb") as f:
            for _ in range(5):
                await ws.send(f.read(65536))
        await ws.close()


class ShortServer(RelayServer):
    """모자란 바이트만 보내고 end 를 보내는 서버 (크기 불일치)"""

    async def _update(self, ws, hello):
        info = self.update_info("client")
        await ws.send(json.dumps({"t": "update_info", "available": True, **{k: info[k] for k in ("version", "size", "sha256")}}))
        await ws.recv()
        with open(info["path"], "rb") as f:
            await ws.send(f.read(65536))
        await ws.send(json.dumps({"t": "end", "sha256": info["sha256"]}))


class StallServer(RelayServer):
    """조각 몇 개 보내고 그대로 멈춰 버리는 서버"""

    async def _update(self, ws, hello):
        info = self.update_info("client")
        await ws.send(json.dumps({"t": "update_info", "available": True, **{k: info[k] for k in ("version", "size", "sha256")}}))
        await ws.recv()
        with open(info["path"], "rb") as f:
            await ws.send(f.read(65536))
        await ws.wait_closed()                              # 클라이언트가 포기하고 닫을 때까지 아무것도 안 보냄


class CustomRelay(RelayRunner):
    """RelayRunner 와 같되 서버 클래스를 바꿔 끼울 수 있다 (start 만 다름)"""

    def __init__(self, server_cls, data_dir, key="", update_dir=None):
        self.server_cls = server_cls
        super().__init__(data_dir, key, update_dir)

    def start(self):
        from websockets.asyncio.server import serve
        self.server = self.server_cls(self.data_dir, self.key, self.update_dir)

        async def go():
            self.ws = await serve(self.server.handle, "127.0.0.1", self.port, max_size=2 ** 20, compression=None,
                                  process_request=self.server.process_request)
            return self.ws.sockets[0].getsockname()[1]
        self.port = self.lt.run(go())


def make_update_dir(root, version="2.17.0", size=SIZE):
    d = root / "relay_update"
    d.mkdir()
    (d / EXE).write_bytes(pattern(size))
    (d / "version.json").write_text(json.dumps({"client": version, "server": version}), encoding="utf-8")
    return str(d)


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def leftovers(dest):
    """dest 폴더에 남은 파일 이름들"""
    return sorted(os.listdir(dest)) if os.path.isdir(dest) else []


@pytest.fixture
def env(tmp_path):
    """(update_dir, dest_dir) — 안내 서버 쪽 폴더와 내려받을 폴더"""
    return make_update_dir(tmp_path), str(tmp_path / "dest")


@pytest.fixture
def relay(tmp_path, env):
    r = RelayRunner(str(tmp_path / "relay"), "", env[0])
    yield r
    r.close()


def run(relay, ver="2.16.0", key="", dest=None, **kw):
    return U.check_and_download(Cfg(relay.url, key, str(dest)), ver, dest_dir=dest, **kw)


# ─────────────────────────────────────────
# 정상 흐름
# ─────────────────────────────────────────
def test_newer_version_downloads_and_verifies(relay, env):
    _, dest = env
    r = run(relay, dest=dest)
    assert r["status"] == "downloaded", r
    assert r["version"] == "2.17.0" and r["size"] == SIZE
    assert r["sha256"] == sha_of(pattern(SIZE))
    assert r["path"] == os.path.join(dest, EXE) and r["msg"]
    with open(r["path"], "rb") as f:
        assert f.read() == pattern(SIZE)
    assert leftovers(dest) == [EXE, "update_ready.json"]          # .part 는 남지 않음

    marker = json.load(open(os.path.join(dest, "update_ready.json"), encoding="utf-8"))
    assert marker["version"] == "2.17.0" and marker["sha256"] == r["sha256"]
    assert marker["size"] == SIZE and marker["path"] == r["path"] and marker["time"] > 0
    p = U.pending_update(dest)
    assert p and p["version"] == "2.17.0" and p["path"] == r["path"]


def test_default_dir_and_dest_default(tmp_path, relay):
    cfg = Cfg(relay.url, "", str(tmp_path / "cfgdir"))
    assert U.default_dir(cfg) == os.path.join(str(tmp_path / "cfgdir"), "wan_update")
    r = U.check_and_download(cfg, "2.16.0")              # dest_dir 생략 → default_dir
    assert r["status"] == "downloaded"
    assert r["path"] == os.path.join(U.default_dir(cfg), EXE)


def test_correct_key_works(tmp_path, env):
    relay = RelayRunner(str(tmp_path / "relay"), "secret-key", env[0])
    try:
        r = run(relay, key="secret-key", dest=env[1])
        assert r["status"] == "downloaded", r
    finally:
        relay.close()


def test_server_kind(tmp_path, env):
    (tmp_path / "relay_update" / "AuroraServer.exe").write_bytes(pattern(1000))
    relay = RelayRunner(str(tmp_path / "relay"), "", env[0])
    try:
        r = run(relay, dest=env[1], kind="server")
        assert r["status"] == "downloaded", r
        assert os.path.basename(r["path"]) == "AuroraServer.exe" and r["size"] == 1000
    finally:
        relay.close()


@pytest.mark.parametrize("ver", ["2.17.0", "2.18.0", "3.0"])
def test_same_or_newer_client_is_latest_and_writes_nothing(relay, env, ver):
    _, dest = env
    r = run(relay, ver=ver, dest=dest)
    assert r["status"] == "latest" and r["msg"], r
    assert leftovers(dest) == []


def test_no_update_dir_is_none(tmp_path):
    relay = RelayRunner(str(tmp_path / "relay"), "", None)
    try:
        dest = str(tmp_path / "dest")
        r = run(relay, dest=dest)
        assert r["status"] == "none" and r["msg"], r
        assert leftovers(dest) == []
    finally:
        relay.close()


def test_no_version_json_is_none(tmp_path):
    d = tmp_path / "relay_update"
    d.mkdir()
    (d / EXE).write_bytes(b"x" * 100)                    # exe 만 있고 version.json 없음
    relay = RelayRunner(str(tmp_path / "relay"), "", str(d))
    try:
        r = run(relay, dest=str(tmp_path / "dest"))
        assert r["status"] == "none", r
    finally:
        relay.close()


# ─────────────────────────────────────────
# 오류
# ─────────────────────────────────────────
def test_wrong_key_is_error(tmp_path, env):
    relay = RelayRunner(str(tmp_path / "relay"), "right-key", env[0])
    try:
        r = run(relay, key="wrong-key", dest=env[1])
        assert r["status"] == "error" and "키" in r["msg"] and "맞지 않" in r["msg"], r
        assert leftovers(env[1]) == []
        r = run(relay, key="", dest=env[1])               # 키를 아예 안 낸 경우도 같음
        assert r["status"] == "error" and "키" in r["msg"]
    finally:
        relay.close()


def test_empty_relay_url_is_error(tmp_path):
    for url in ("", "   ", None):
        r = U.check_and_download(Cfg(url), "2.16.0", dest_dir=str(tmp_path / "d"))
        assert r["status"] == "error" and "주소" in r["msg"], r
    assert not (tmp_path / "d").exists()


def test_unreachable_server_is_error_quickly(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                             # 아무도 듣지 않는 포트
    t0 = time.monotonic()
    r = U.check_and_download(Cfg(f"ws://127.0.0.1:{port}"), "2.16.0", dest_dir=str(tmp_path / "d"))
    assert r["status"] == "error" and "연결하지 못" in r["msg"], r
    assert time.monotonic() - t0 < 5
    assert leftovers(str(tmp_path / "d")) == []


def test_garbage_url_does_not_raise(tmp_path):
    r = U.check_and_download(Cfg("이건 주소가 아님 %%"), "2.16.0", dest_dir=str(tmp_path / "d"))
    assert r["status"] == "error" and r["msg"]


def test_unknown_kind_is_error(relay, env):
    r = run(relay, dest=env[1], kind="bogus")
    assert r["status"] == "error"


@pytest.mark.parametrize("server_cls,word", [
    (BadShaServer, "체크섬"),            # 서버가 알린 sha 와 실제 내용이 다름
    (EndMismatchServer, "체크섬"),       # end 의 sha 가 다름
    (ShortServer, "다 오지"),            # 크기가 모자람
])
def test_verification_failures_leave_nothing(tmp_path, env, server_cls, word):
    relay = CustomRelay(server_cls, str(tmp_path / "relay"), "", env[0])
    try:
        _, dest = env
        r = run(relay, dest=dest)
        assert r["status"] == "error" and word in r["msg"], r
        assert leftovers(dest) == []                       # .part · 최종 파일 · 표식 모두 없음
        assert U.pending_update(dest) is None
    finally:
        relay.close()


def test_connection_dropped_mid_download(tmp_path, env):
    relay = CustomRelay(DropServer, str(tmp_path / "relay"), "", env[0])
    try:
        _, dest = env
        seen = []
        r = run(relay, dest=dest, progress=lambda d, t: seen.append((d, t)))
        assert r["status"] == "error" and "끊어" in r["msg"], r
        assert seen                                         # 일부는 받았음 (그래서 정리가 의미 있다)
        assert leftovers(dest) == []
    finally:
        relay.close()


def test_stalled_server_times_out_and_cleans_up(tmp_path, env):
    relay = CustomRelay(StallServer, str(tmp_path / "relay"), "", env[0])
    try:
        _, dest = env
        t0 = time.monotonic()
        r = asyncio.run(U.download(relay.url, "", "2.16.0", dest, timeout=1.0))
        assert r["status"] == "error" and "중단" in r["msg"], r
        assert time.monotonic() - t0 < 8
        assert leftovers(dest) == []
    finally:
        relay.close()


def test_failed_download_removes_stale_marker(tmp_path, env):
    """이전에 받아 둔 표식이 있어도 새로 받기 시작한 뒤 실패하면 낡은 표식·파일이 남지 않는다"""
    _, dest = env
    os.makedirs(dest)
    open(os.path.join(dest, EXE), "wb").write(b"old")
    json.dump({"version": "2.16.5", "path": os.path.join(dest, EXE), "sha256": sha_of(b"old"), "size": 3, "time": 1},
              open(os.path.join(dest, "update_ready.json"), "w"))
    assert U.pending_update(dest)["version"] == "2.16.5"
    relay = CustomRelay(DropServer, str(tmp_path / "relay"), "", env[0])
    try:
        r = run(relay, dest=dest)
        assert r["status"] == "error"
        assert leftovers(dest) == [] and U.pending_update(dest) is None
    finally:
        relay.close()


def test_unreachable_server_keeps_existing_pending(tmp_path, relay, env):
    """서버에 못 닿았을 뿐이면 이미 받아 둔 새 버전은 그대로 둔다"""
    _, dest = env
    assert run(relay, dest=dest)["status"] == "downloaded"
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = U.check_and_download(Cfg(f"ws://127.0.0.1:{port}"), "2.16.0", dest_dir=dest)
    assert r["status"] == "error"
    assert U.pending_update(dest) is not None


# ─────────────────────────────────────────
# 진행률
# ─────────────────────────────────────────
def test_progress_is_monotonic_and_ends_at_total(relay, env):
    _, dest = env
    seen = []
    r = run(relay, dest=dest, progress=lambda d, t: seen.append((d, t)))
    assert r["status"] == "downloaded"
    assert len(seen) >= 2
    assert all(t == SIZE for _, t in seen)
    dones = [d for d, _ in seen]
    assert dones == sorted(dones) and dones[0] == 0 and dones[-1] == SIZE


def test_progress_callback_exception_does_not_break_download(relay, env):
    def boom(d, t):
        raise RuntimeError("화면 오류")
    r = run(relay, dest=env[1], progress=boom)
    assert r["status"] == "downloaded", r


# ─────────────────────────────────────────
# pending_update / clear_pending
# ─────────────────────────────────────────
def test_pending_none_when_nothing_downloaded(tmp_path):
    assert U.pending_update(str(tmp_path / "없는폴더")) is None
    (tmp_path / "d").mkdir()
    assert U.pending_update(str(tmp_path / "d")) is None


def test_pending_none_after_tamper_same_size(relay, env):
    _, dest = env
    r = run(relay, dest=dest)
    assert U.pending_update(dest)
    with open(r["path"], "r+b") as f:                      # 크기는 그대로 한 바이트만 바꿈
        f.seek(1000)
        b = f.read(1)
        f.seek(1000)
        f.write(bytes([b[0] ^ 0xFF]))
    assert U.pending_update(dest) is None
    assert not os.path.exists(os.path.join(dest, "update_ready.json"))      # 낡은 표식은 지워짐


def test_pending_none_after_truncate_or_delete(relay, env):
    _, dest = env
    r = run(relay, dest=dest)
    with open(r["path"], "r+b") as f:
        f.truncate(SIZE - 1)
    assert U.pending_update(dest) is None
    assert leftovers(dest) == [EXE]                        # 표식만 정리됨

    r = run(relay, dest=dest)                              # 다시 받으면 복구
    assert r["status"] == "downloaded" and U.pending_update(dest)
    os.remove(r["path"])
    assert U.pending_update(dest) is None
    assert leftovers(dest) == []


def test_pending_none_with_corrupt_marker(relay, env):
    _, dest = env
    run(relay, dest=dest)
    with open(os.path.join(dest, "update_ready.json"), "w") as f:
        f.write("{깨진")
    assert U.pending_update(dest) is None


def test_clear_pending(relay, env):
    _, dest = env
    run(relay, dest=dest)
    open(os.path.join(dest, EXE + ".part"), "wb").write(b"half")
    assert U.pending_update(dest)
    U.clear_pending(dest)
    assert leftovers(dest) == []
    assert U.pending_update(dest) is None
    U.clear_pending(dest)                                  # 두 번 불러도 괜찮음
    U.clear_pending(os.path.join(dest, "없음"))


# ─────────────────────────────────────────
# 스레드 / 이벤트 루프
# ─────────────────────────────────────────
def test_worker_thread_while_main_thread_loop_is_running(relay, env):
    """화면 쪽이 asyncio 를 쓰는 중에 작업 스레드에서 불러도 된다"""
    _, dest = env
    out = {}

    def worker():
        out["r"] = run(relay, dest=dest)

    async def main():
        t = threading.Thread(target=worker)
        t.start()
        ticks = 0
        while t.is_alive():
            await asyncio.sleep(0.01)                     # 메인 루프가 막히지 않고 계속 돈다
            ticks += 1
        t.join()
        return ticks

    assert asyncio.run(main()) > 0
    assert out["r"]["status"] == "downloaded", out["r"]


def test_called_directly_inside_running_loop(relay, env):
    """이벤트 루프가 도는 스레드에서 바로 불러도 예외 없이 결과를 준다"""
    async def main():
        return run(relay, dest=env[1])
    assert asyncio.run(main())["status"] == "downloaded"


def test_two_concurrent_threads_different_dirs(relay, env, tmp_path):
    outs = {}

    def w(name):
        outs[name] = run(relay, dest=str(tmp_path / name))

    ts = [threading.Thread(target=w, args=(n,)) for n in ("a", "b")]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert outs["a"]["status"] == outs["b"]["status"] == "downloaded"
