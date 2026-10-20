"""WAN 기본 모듈 단위 테스트 — 설정 · 사용량 · 메시지 조각"""

import json
import os
import threading
from datetime import date

import pytest

from wan import config as wcfg
from wan import protocol as P
from wan.usage import UsageStore, fmt_bytes


# ── 설정 ──
def test_config_defaults_and_id_persist(tmp_path):
    c = wcfg.WanConfig(str(tmp_path))
    assert c.get("enabled") is False and c.get("access_code") == ""
    agent_id = c.ensure_id()
    assert len(agent_id) == 12
    assert wcfg.WanConfig(str(tmp_path)).ensure_id() == agent_id      # 다시 열어도 같은 ID


def test_config_corrupt_file_is_set_aside_not_crash(tmp_path):
    (tmp_path / "wan_config.json").write_text("{깨진 json", encoding="utf-8")
    c = wcfg.WanConfig(str(tmp_path))
    assert c.get("relay_url") == ""
    assert (tmp_path / "wan_config.json.bad").exists()


def test_config_ignores_unknown_keys(tmp_path):
    (tmp_path / "wan_config.json").write_text(json.dumps({"relay_url": "ws://x:1", "evil": 1}), encoding="utf-8")
    c = wcfg.WanConfig(str(tmp_path))
    assert c.get("relay_url") == "ws://x:1" and "evil" not in c.snapshot()


def test_config_never_touches_lan_config(tmp_path):
    lan = tmp_path / "client_config.json"
    lan.write_text('{"server_ips": ["1.2.3.4"]}', encoding="utf-8")
    c = wcfg.WanConfig(str(tmp_path))
    c.update(relay_url="ws://a:1")
    c.ensure_id()
    assert lan.read_text(encoding="utf-8") == '{"server_ips": ["1.2.3.4"]}'


def test_sanitize_remote_filters_bad_values():
    out = wcfg.sanitize_remote({"name": "  현장1  ", "access_code": 123, "max_quality": "초고화질",
                                "allow_files": "yes", "relay_url": "ws://evil"})
    assert out == {"name": "현장1"}
    out = wcfg.sanitize_remote({"access_code": "abc", "max_quality": "절약", "allow_files": False})
    assert out == {"access_code": "abc", "max_quality": "절약", "allow_files": False}


def test_apply_remote_reports_changes_and_rev(tmp_path):
    c = wcfg.WanConfig(str(tmp_path))
    assert c.apply_remote({"name": "A", "access_code": "1"}, 3) == {"name": "A", "access_code": "1"}
    assert c.get("config_rev") == 3
    assert c.apply_remote({"name": "A"}, 4) == {}


def test_format_id():
    assert wcfg.format_id("a1b2c3d4e5f6") == "A1B2-C3D4-E5F6"


def test_quality_table_is_single_source():
    assert wcfg.QUALITY["절약"][1] < wcfg.QUALITY["보통"][1] < wcfg.QUALITY["고화질"][1]
    assert wcfg.QUALITY_DEFAULT == "절약"


# ── 사용량 ──
def test_usage_accumulates_per_day_and_month(tmp_path):
    day = [date(2026, 10, 20)]
    u = UsageStore(str(tmp_path / "u.json"), today=lambda: day[0])
    u.add(tx=100, rx=900)
    day[0] = date(2026, 10, 21)
    u.add(tx=50, rx=50)
    day[0] = date(2026, 11, 1)
    u.add(tx=1, rx=1)
    assert u.day_total("2026-10-20") == 1000
    assert u.month_total("2026-10") == 1100
    assert u.month_total("2026-11") == 2
    assert u.day_total() == 2


def test_usage_persists_after_flush(tmp_path):
    p = str(tmp_path / "u.json")
    u = UsageStore(p, today=lambda: date(2026, 10, 20))
    u.add(tx=5, rx=5)
    u.flush()
    assert UsageStore(p, today=lambda: date(2026, 10, 20)).day_total() == 10


def test_usage_batches_writes(tmp_path):
    t = [0.0]
    p = tmp_path / "u.json"
    u = UsageStore(str(p), clock=lambda: t[0], today=lambda: date(2026, 10, 20))
    for _ in range(100):
        u.add(rx=10)
    assert not p.exists()            # 10초가 지나기 전에는 파일을 쓰지 않음
    t[0] = 11.0
    u.add(rx=10)
    assert p.exists()


def test_usage_thread_safe(tmp_path):
    u = UsageStore(str(tmp_path / "u.json"), today=lambda: date(2026, 10, 20))
    def work():
        for _ in range(1000):
            u.add(tx=1, rx=2)
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert u.day_total() == 8 * 1000 * 3


def test_usage_corrupt_file_ignored(tmp_path):
    p = tmp_path / "u.json"
    p.write_text("not json")
    assert UsageStore(str(p)).day_total() == 0


def test_fmt_bytes():
    assert fmt_bytes(512) == "512 B" and fmt_bytes(2048) == "2.0 KB" and fmt_bytes(5 * 1024**2) == "5.0 MB"


# ── 프로토콜 ──
def test_small_message_roundtrip():
    frames = P.encode_frames(P.REQ, 7, b'{"a":1}')
    assert len(frames) == 1 and len(frames[0]) <= P.MAX_FRAME
    kind, flags, rid, data = P.Reassembler().feed(frames[0])
    assert (kind, rid, data) == (P.REQ, 7, b'{"a":1}') and flags & P.F_LAST


def test_large_message_is_split_under_limit_and_reassembled():
    payload = os.urandom(P.MAX_DATA * 3 + 123)
    frames = P.encode_frames(P.REQ, 9, payload)
    assert len(frames) == 4 and all(len(f) <= P.MAX_FRAME for f in frames)
    r = P.Reassembler()
    outs = [r.feed(f) for f in frames]
    assert outs[:-1] == [None, None, None]
    assert outs[-1][3] == payload


def test_interleaved_messages_do_not_mix():
    a, b = os.urandom(P.MAX_DATA * 2), os.urandom(P.MAX_DATA * 2 + 5)
    fa, fb = P.encode_frames(P.REQ, 1, a), P.encode_frames(P.REQ, 2, b)
    r, done = P.Reassembler(), {}
    order = [fa[0], fb[0], fb[1], fa[1], fa[2] if len(fa) > 2 else None, fb[2]]
    for f in [x for x in order if x is not None] + fa[3:] + fb[3:]:
        out = r.feed(f)
        if out:
            done[out[2]] = out[3]
    assert done == {1: a, 2: b}


def test_empty_payload_is_one_frame():
    frames = P.encode_frames(P.REQ_END, 5)
    assert len(frames) == 1
    assert P.Reassembler().feed(frames[0])[3] == b""


def test_decode_rejects_bad_sizes():
    with pytest.raises(ValueError):
        P.decode_frame(b"\x01")
    with pytest.raises(ValueError):
        P.decode_frame(bytes(P.MAX_FRAME + 1))


def test_priority_input_before_bulk():
    assert P.priority_for("/remote/input") < P.priority_for("/remote/frame") < P.priority_for("/fs/file")
    assert P.priority_for("/fs/upload") == P.PRIO_BULK


def test_json_helpers_korean():
    obj = {"가": "나", "n": 1}
    assert P.loads(P.dumps(obj)) == obj
    assert P.loads(b"") is None
