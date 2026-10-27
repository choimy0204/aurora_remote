"""접속 설정 파일(aurora_wan.conn) · 공인 주소(STUN) · 접속 정보 형식 · 런타임 연결"""

import socket
import struct
import threading
import time

import pytest

from wan import connect_code, connfile, netutil
from wan.config import WanConfig
from wan.runtime import WanRuntime
from wan_helpers import eventually, free_port, patch_ui

RELAY_CODE = connect_code.encode(["ws://127.0.0.1:9100"], "k1")
DIRECT_CODE = connect_code.encode_direct(["ws://192.168.0.5:9102"], "c0de")


# ── 접속 정보 형식 ──
def test_kind_of_distinguishes_relay_and_direct():
    assert connect_code.kind_of(RELAY_CODE) == "relay"
    assert connect_code.kind_of("  " + DIRECT_CODE + "\n") == "direct"
    assert connect_code.kind_of("192.168.0.5") == ""


def test_direct_code_roundtrip_and_not_accepted_as_relay():
    assert connect_code.decode_direct(DIRECT_CODE) == (["ws://192.168.0.5:9102"], "c0de")
    with pytest.raises(ValueError):
        connect_code.decode(DIRECT_CODE)


@pytest.mark.parametrize("text,want", [
    ("192.168.0.5", "192.168.0.5:9102"), ("192.168.0.5:7000", "192.168.0.5:7000"),
    ("ws://example.com:9102/", "example.com:9102"), ("  pc.example.org ", "pc.example.org:9102")])
def test_parse_address(text, want):
    assert connect_code.parse_address(text) == want


@pytest.mark.parametrize("text", ["", "   ", "a b", "host:99999", "host:abc", "[::1]:9102"])
def test_parse_address_rejects_garbage(text):
    with pytest.raises(ValueError):
        connect_code.parse_address(text)


# ── 설정 파일 ──
def test_file_write_read_and_fingerprint(tmp_path):
    path = str(tmp_path / connfile.FILE_NAME)
    connfile.write(path, RELAY_CODE)
    text = connfile.read(path)
    assert connect_code.decode(text) == (["ws://127.0.0.1:9100"], "k1")
    assert connfile.fingerprint(text) == connfile.fingerprint(RELAY_CODE)
    assert connfile.fingerprint(text) != connfile.fingerprint(DIRECT_CODE)


def test_file_rejects_non_connection_text(tmp_path):
    path = tmp_path / connfile.FILE_NAME
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError):
        connfile.read(str(path))
    with pytest.raises(ValueError):
        connfile.write(str(path), "hello")
    with pytest.raises(ValueError):
        connfile.read(str(tmp_path / "none.conn"))


def test_find_prefers_first_directory(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    (b / connfile.FILE_NAME).write_text(RELAY_CODE)
    assert connfile.find([str(a), str(b)]) == str(b / connfile.FILE_NAME)
    (a / connfile.FILE_NAME).write_text(RELAY_CODE)
    assert connfile.find([str(a), str(b)]) == str(a / connfile.FILE_NAME)
    assert connfile.find([str(tmp_path / "x")]) is None


def _runtime(tmp_path, monkeypatch, file_dir):
    monkeypatch.setattr(connfile, "search_dirs", lambda cfg_dir="": [str(file_dir)])
    rt = WanRuntime("2.16.0", lambda: ["x"], cfg_dir=str(tmp_path / "cfg"), log=lambda m: None)
    started = []
    rt.client.start = lambda: started.append("start")
    rt.client.stop = lambda: None
    return rt, started


def test_runtime_applies_relay_file_once_and_keeps_later_edits(tmp_path, monkeypatch):
    d = tmp_path / "app"
    d.mkdir()
    connfile.write(str(d / connfile.FILE_NAME), connect_code.encode([f"ws://127.0.0.1:{free_port()}"], "file-key"))
    rt, started = _runtime(tmp_path, monkeypatch, d)
    rt.apply_conn_file()
    eventually(lambda: rt.cfg.get("relay_key") == "file-key", msg="파일의 키 적용")
    assert rt.cfg.get("relay_url").startswith("ws://127.0.0.1:") and started
    eventually(lambda: rt.cfg.get("conn_hash"), msg="지문 기록")
    # 사용자가 화면에서 바꾼 뒤에는 같은 파일이 다시 덮어쓰지 않는다
    rt.cfg.update(relay_url="ws://changed:1", relay_key="mine")
    rt.apply_conn_file()
    time.sleep(0.3)
    assert rt.cfg.get("relay_url") == "ws://changed:1" and rt.cfg.get("relay_key") == "mine"
    # 파일 내용이 바뀌면 다시 적용
    connfile.write(str(d / connfile.FILE_NAME), connect_code.encode(["ws://127.0.0.1:1"], "second"))
    rt.apply_conn_file()
    eventually(lambda: rt.cfg.get("relay_key") == "second", msg="바뀐 파일 재적용")


def test_runtime_applies_direct_file_as_saved_target(tmp_path, monkeypatch):
    d = tmp_path / "app"
    d.mkdir()
    port = free_port()
    with socket.socket() as s:                                   # 닿는 주소여야 고른다
        s.bind(("127.0.0.1", port))
        s.listen()
        connfile.write(str(d / connfile.FILE_NAME), connect_code.encode_direct([f"ws://127.0.0.1:{port}"], "cc"))
        rt, _ = _runtime(tmp_path, monkeypatch, d)
        rt.apply_conn_file()
        eventually(lambda: rt.cfg.get("direct_targets"), msg="저장된 현장")
    assert rt.cfg.get("direct_targets")[f"127.0.0.1:{port}"]["code"] == "cc"


def test_runtime_ignores_broken_or_missing_file(tmp_path, monkeypatch):
    d = tmp_path / "app"
    d.mkdir()
    rt, started = _runtime(tmp_path, monkeypatch, d)
    rt.apply_conn_file()                                         # 파일 없음
    (d / connfile.FILE_NAME).write_text("garbage")
    rt.apply_conn_file()
    time.sleep(0.2)
    assert not started and rt.cfg.get("conn_hash") == ""


def test_field_pc_does_not_start_client_from_file(tmp_path, monkeypatch):
    d = tmp_path / "app"
    d.mkdir()
    connfile.write(str(d / connfile.FILE_NAME), connect_code.encode(["ws://127.0.0.1:1"], "k"))
    rt, started = _runtime(tmp_path, monkeypatch, d)
    rt.cfg.update(enabled=True)
    rt.apply_conn_file()
    eventually(lambda: rt.cfg.get("relay_key") == "k")
    assert not started                                          # 현장 PC 는 에이전트가 따로 접속한다


# ── 공인 주소 (STUN) ──
class FakeStun:
    def __init__(self, ip="203.0.113.7", port=40000, mapped=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.ip, self.mport, self.mapped = ip, port, mapped
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        data, addr = self.sock.recvfrom(2048)
        tid = data[8:20]
        packed = struct.unpack(">I", socket.inet_aton(self.ip))[0]
        if self.mapped:
            body = struct.pack(">BBHI", 0, 1, self.mport, packed)
            attr = struct.pack(">HH", 0x0001, 8) + body
        else:
            body = struct.pack(">BBHI", 0, 1, self.mport ^ (netutil.STUN_MAGIC >> 16), packed ^ netutil.STUN_MAGIC)
            attr = struct.pack(">HH", 0x0020, 8) + body
        self.sock.sendto(struct.pack(">HHI", 0x0101, len(attr), netutil.STUN_MAGIC) + tid + attr, addr)

    def close(self):
        self.sock.close()


@pytest.mark.parametrize("mapped", [False, True])
def test_public_address_from_stun(mapped):
    stun = FakeStun(mapped=mapped)
    try:
        assert netutil.public_address(f"stun:127.0.0.1:{stun.port}", timeout=2) == "203.0.113.7"
    finally:
        stun.close()


def test_public_address_none_when_unreachable():
    assert netutil.public_address(f"stun:127.0.0.1:{free_port()}", timeout=0.3) is None
    assert netutil.public_address("", timeout=0.3) is None


# ── 에이전트 상태 파일 ──
def test_agent_info_reads_fresh_alive_file_only(tmp_path):
    rt = WanRuntime("2.16.0", lambda: ["x"], cfg_dir=str(tmp_path), log=lambda m: None)
    assert rt.agent_info() == {}
    path = tmp_path / "wan_agent.alive"
    path.write_text('{"t": 1, "direct": "listening", "online": true}')
    assert rt.agent_info()["direct"] == "listening"
    old = time.time() - 60
    import os
    os.utime(path, (old, old))
    assert rt.agent_info() == {}
    path.write_text("1700000000.5")                              # 옛 형식(숫자만)
    assert rt.agent_info() == {}


# ── 방화벽 (Windows 가 아니어도 명령 흐름은 확인) ──
class FakeRun:
    def __init__(self, fail_add=False):
        self.cmds, self.fail_add = [], fail_add

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        bad = self.fail_add and "add rule" in cmd
        return type("R", (), {"returncode": 1 if bad else 0})()


def test_open_firewall_replaces_old_rule_before_adding(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(netutil.os, "name", "nt")
    monkeypatch.setattr(netutil.subprocess, "run", run)
    ok, msg = netutil.open_firewall(9100)
    assert ok and "9100" in msg
    kinds = ["delete" if "delete rule" in c else "add" for c in run.cmds]
    assert kinds == ["delete", "add", "delete", "add"]                      # 규칙마다 지우고 다시 만듦
    assert 'name="Aurora WAN Relay 9100"' in run.cmds[0] and "localport=9100" in run.cmds[1]
    assert "localport=9101" in run.cmds[3]


def test_open_firewall_direct_port_only(monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(netutil.os, "name", "nt")
    monkeypatch.setattr(netutil.subprocess, "run", run)
    ok, msg = netutil.open_firewall(9102, direct=True)
    assert ok and "9102" in msg and "자동 찾기" not in msg
    assert len(run.cmds) == 2 and 'name="Aurora WAN Direct 9102"' in run.cmds[1] and "localport=9102" in run.cmds[1]


def test_open_firewall_reports_manual_commands_when_not_admin(monkeypatch):
    monkeypatch.setattr(netutil.os, "name", "nt")
    monkeypatch.setattr(netutil.subprocess, "run", FakeRun(fail_add=True))
    ok, msg = netutil.open_firewall(9102, direct=True)
    assert not ok and "관리자" in msg and "netsh advfirewall firewall add rule" in msg


def test_open_firewall_not_windows():
    ok, msg = netutil.open_firewall(9100)
    assert not ok and "Windows" in msg


# ── 설정 파일을 읽는 순간의 일시적 오류 · 중계 PC 자기 적용 방지 ──
def test_config_keeps_values_when_file_is_temporarily_unreadable(tmp_path, monkeypatch):
    cfg = WanConfig(str(tmp_path))
    cfg.update(access_code="내코드", relay_url="ws://h:1")
    real_open = open

    def locked(path, *a, **k):
        if str(path).endswith("wan_config.json"):
            raise PermissionError("다른 프로세스가 쓰는 중")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", locked)
    cfg.load()
    monkeypatch.undo()
    assert cfg.get("access_code") == "내코드" and cfg.get("relay_url") == "ws://h:1"       # 기본값으로 날아가지 않음
    assert not (tmp_path / "wan_config.json.bad").exists()                                  # 멀쩡한 파일을 치워 두지도 않음
    assert cfg.reload_if_changed() is True                                                  # 다음에 다시 읽는다


def test_config_still_quarantines_really_broken_file(tmp_path):
    (tmp_path / "wan_config.json").write_text("{깨짐", encoding="utf-8")
    cfg = WanConfig(str(tmp_path))
    assert cfg.get("access_code") == "" and (tmp_path / "wan_config.json.bad").exists()


def test_reload_if_changed_follows_other_process_edits(tmp_path):
    a, b = WanConfig(str(tmp_path)), WanConfig(str(tmp_path))
    assert a.reload_if_changed() is False
    time.sleep(0.05)
    b.update(access_code="x")
    assert a.reload_if_changed() is True and a.get("access_code") == "x"
    assert a.reload_if_changed() is False


def test_relay_pc_does_not_apply_other_relays_file(tmp_path, monkeypatch):
    d = tmp_path / "app"
    d.mkdir()
    connfile.write(str(d / connfile.FILE_NAME), connect_code.encode(["ws://10.9.9.9:9100"], "다른키"))
    rt, started = _runtime(tmp_path, monkeypatch, d)
    rt.cfg.update(relay_host=True, relay_url="", relay_key="내키")
    rt.apply_conn_file()
    time.sleep(0.3)
    assert rt.cfg.get("relay_url") == "" and rt.cfg.get("relay_key") == "내키" and not started


def test_saving_file_on_this_pc_marks_it_as_already_applied(tmp_path, monkeypatch):
    """중계 PC 가 exe 옆에 저장한 파일을 다음 실행 때 자기 자신에게 다시 적용하지 않는다"""
    import wan.ui as wui
    patch_ui(monkeypatch, "lan_addresses", lambda: ["192.168.0.140"])
    from test_wan_ui import make_ctl, FakeCfg
    ctl, ctx, ui = make_ctl(cfg=FakeCfg(relay_key="k", relay_port=9100))
    path = str(tmp_path / connfile.FILE_NAME)
    assert ctl.save_connection_file(path) is True
    rt, started = _runtime(tmp_path, monkeypatch, tmp_path)
    rt.cfg.update(conn_hash=ctx["cfg"].get("conn_hash"), relay_url="", relay_key="k")
    rt.apply_conn_file()
    time.sleep(0.3)
    assert rt.cfg.get("relay_url") == "" and not started
