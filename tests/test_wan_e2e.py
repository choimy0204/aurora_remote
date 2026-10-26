"""WAN 통합 시험 — 실제 안내 서버 · 현장 에이전트 · 사용자 클라이언트 (aiortc · websockets 모두 실제)"""

import hashlib
import statistics
import threading
import time

import pytest

from wan import protocol as P
from wan import agent as agent_mod
from wan_helpers import World, eventually, pattern, LOCAL_KEY


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


# ── 목록 · 설정 ──
def test_field_pc_appears_in_list_with_id_and_version(world):
    e = world.entry()
    assert e["online"] and e["id"] == world.agent_id and e["ver"] == "2.16.0" and e["name"] == "현장1"


def test_set_config_reaches_field_and_is_acknowledged(world):
    reply = world.client.set_config(world.agent_id, {"name": "CCW-2 현장", "access_code": "9999",
                                                     "max_quality": "보통", "allow_files": False})
    assert reply["online"] is True
    eventually(lambda: world.agent.cfg.get("access_code") == "9999", msg="현장에 설정 적용")
    assert world.agent.cfg.get("name") == "CCW-2 현장" and world.agent.cfg.get("allow_files") is False
    eventually(lambda: world.entry()["name"] == "CCW-2 현장" and world.entry()["applied_rev"] == world.entry()["rev"],
               msg="적용 확인(ack)이 목록에 반영")


def test_config_is_kept_for_offline_field_and_delivered_on_reconnect(world):
    world.agent.stop()
    eventually(lambda: not world.entry()["online"], msg="현장이 꺼진 것으로 표시")
    reply = world.client.set_config(world.agent_id, {"access_code": "newcode", "name": "복귀"})
    assert reply["online"] is False
    from wan_helpers import AgentRunner
    world.agent = AgentRunner(world.agent.cfg.dir, world.relay.url, world.local.port, code="old")
    eventually(lambda: world.agent.cfg.get("access_code") == "newcode", msg="재접속 시 보관된 설정 수신")
    assert world.agent.cfg.get("name") == "복귀"


def test_set_config_rejects_unknown_agent_and_bad_values(world):
    with pytest.raises(P.WanError):
        world.client.set_config("0" * 12, {"name": "x"})
    world.client.set_config(world.agent_id, {"max_quality": "초고화질", "relay_url": "ws://evil:1", "access_code": "ok"})
    eventually(lambda: world.agent.cfg.get("access_code") == "ok")
    assert world.agent.cfg.get("max_quality") == "고화질" and "evil" not in world.agent.cfg.get("relay_url")


# ── 접속 코드 (현장 PC 가 직접 확인) ──
def test_wrong_code_denied(world):
    with pytest.raises(P.WanDenied, match="맞지 않"):
        world.client.open_session(world.agent_id, "0000")


def test_no_code_configured_denies_everything(world):
    world.client.set_config(world.agent_id, {"access_code": ""})
    # 빈 코드는 sanitize 를 통과하지만 현장은 코드가 비어 있으면 모두 거부해야 함
    eventually(lambda: world.agent.cfg.get("access_code") == "")
    with pytest.raises(P.WanDenied, match="접근 코드가 없"):
        world.client.open_session(world.agent_id, "")


def test_shared_key_opens_field_without_access_code(tmp_path):
    """접속 코드를 따로 정하지 않은 현장은 공용 키가 곧 접속 코드 — 더블클릭만으로 연결"""
    w = World(tmp_path, code="", key="shared-key-1")
    try:
        s = w.client.open_session(w.agent_id, "shared-key-1")
        assert s.request("GET", "/info").status_code == 200
        s.close()
        with pytest.raises(P.WanDenied, match="맞지 않"):
            w.client.open_session(w.agent_id, "wrong")
    finally:
        w.close()


def test_explicit_access_code_is_not_opened_by_shared_key(tmp_path):
    w = World(tmp_path, code="4321", key="shared-key-1")
    try:
        with pytest.raises(P.WanDenied, match="맞지 않"):
            w.client.open_session(w.agent_id, "shared-key-1")
        w.client.open_session(w.agent_id, "4321").close()
    finally:
        w.close()


def test_code_check_happens_on_field_and_relay_hands_code_to_users(world):
    """코드를 확인하는 곳은 현장 PC 이고, 중계 PC 는 현장이 알려 준(또는 설정 보내기로 정한) 코드를 인증된 사용자 PC 에 알려 준다"""
    world.client.set_config(world.agent_id, {"access_code": "secret-77"})
    eventually(lambda: world.entry()["code"] == "secret-77", msg="중계 PC 가 공용 키를 가진 사용자 PC 에 현장 코드를 알려 준다")


def test_brute_force_lockout(world, monkeypatch):
    for _ in range(agent_mod.CODE_FAILS):
        with pytest.raises(P.WanDenied):
            world.client.open_session(world.agent_id, "bad")
    with pytest.raises(P.WanDenied, match="막혔"):
        world.client.open_session(world.agent_id, "1234")          # 맞는 코드도 잠시 거부
    world.agent.agent._fails.clear()
    s = world.client.open_session(world.agent_id, "1234")
    s.close()


def test_offline_field_reports_clear_error(world):
    world.agent.stop()
    eventually(lambda: not world.entry()["online"])
    with pytest.raises(P.WanError, match="꺼져"):
        world.client.open_session(world.agent_id, "1234")


# ── 직접 연결 ──
def test_direct_connection_end_to_end(world):
    s = world.client.open_session(world.agent_id, "1234")
    try:
        assert s.mode == "direct" and s.state == "연결됨"
        assert s.request("GET", "/info").json()["hostname"] == "FAKE-PC"
        frame = s.viewer_request("/remote/frame", {"size": 120000, "q": 60}, (3, 8))
        assert frame.status_code == 200 and frame.content == pattern(120000)
        size = 3 * 1024 * 1024
        with s.request("GET", "/fs/file", {"path": f"size:{size}"}, stream=True, timeout=(3, 30)) as r:
            assert hashlib.sha256(b"".join(r.iter_content(1 << 20))).hexdigest() == hashlib.sha256(pattern(size)).hexdigest()
        data = pattern(1_500_000)
        assert s.request("PUT", "/fs/upload", {"path": "D:\\up.bin"}, data=data, timeout=(3, 30)).ok
        assert world.local.uploads["D:\\up.bin"] == (len(data), hashlib.sha256(data).hexdigest())
    finally:
        s.close()


def test_lan_functions_cannot_be_reached_over_wan(world):
    s = world.client.open_session(world.agent_id, "1234")
    try:
        for method, route, params in (("POST", "/command", None), ("GET", "/discover", None),
                                      ("GET", "/info", {"via": "10.1.1.1"})):
            assert s.request(method, route, params).status_code == 403
        assert not [h for h in world.local.hits if h[1] in ("/command", "/discover")]
    finally:
        s.close()


def test_field_quality_cap_applies_end_to_end(world):
    world.client.set_config(world.agent_id, {"max_quality": "절약"})
    eventually(lambda: world.agent.cfg.get("max_quality") == "절약")
    s = world.client.open_session(world.agent_id, "1234")
    try:
        s.request("GET", "/remote/frame", {"size": 10, "q": 75, "w": 1920, "h": 1080})
        assert (world.local.last_params["q"], world.local.last_params["w"], world.local.last_params["h"]) == ("40", "1280", "720")
    finally:
        s.close()


def test_files_off_setting_blocks_transfer_but_not_screen(world):
    world.client.set_config(world.agent_id, {"allow_files": False})
    eventually(lambda: world.agent.cfg.get("allow_files") is False)
    s = world.client.open_session(world.agent_id, "1234")
    try:
        assert s.request("GET", "/fs/list", {"path": "D:\\"}).status_code == 403
        assert s.request("GET", "/remote/frame", {"size": 10}).ok
    finally:
        s.close()


# ── 직접 연결이 막힌 망 → 비상 중계 ──
def test_falls_back_to_relay_when_direct_is_blocked(tmp_path):
    w = World(tmp_path, direct_mode="ignore", direct_timeout=1.5)       # 현장이 직접 연결에 응답하지 않음
    try:
        t0 = time.monotonic()
        s = w.client.open_session(w.agent_id, "1234")
        assert s.mode == "relay" and time.monotonic() - t0 < 6
        assert s.request("GET", "/info").json()["hostname"] == "FAKE-PC"
        size = 2 * 1024 * 1024
        with s.request("GET", "/fs/file", {"path": f"size:{size}"}, stream=True, timeout=(3, 30)) as r:
            assert hashlib.sha256(b"".join(r.iter_content(1 << 20))).hexdigest() == hashlib.sha256(pattern(size)).hexdigest()
        data = pattern(900_000)
        assert s.request("PUT", "/fs/upload", {"path": "D:\\r.bin"}, data=data, timeout=(3, 30)).ok
        assert w.local.uploads["D:\\r.bin"][1] == hashlib.sha256(data).hexdigest()
        s.close()
    finally:
        w.close()


def test_falls_back_immediately_when_field_has_no_direct_support(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "direct_available", lambda: False)
    w = World(tmp_path, direct_timeout=30)         # 시간 초과를 기다리지 않고 바로 중계로
    try:
        t0 = time.monotonic()
        s = w.client.open_session(w.agent_id, "1234")
        assert s.mode == "relay" and time.monotonic() - t0 < 5
        s.close()
    finally:
        w.close()


def test_relay_only_when_user_side_has_no_aiortc(world):
    world.client.direct_override = False
    s = world.client.open_session(world.agent_id, "1234")
    try:
        assert s.mode == "relay" and s.request("GET", "/info").ok
    finally:
        s.close()


def test_wrong_code_over_relay_is_denied_too(tmp_path):
    w = World(tmp_path, direct_mode="ignore", direct_timeout=1.0)
    try:
        with pytest.raises(P.WanDenied):
            w.client.open_session(w.agent_id, "wrong")
    finally:
        w.close()


# ── 큰 파일을 보내는 중에도 입력이 막히지 않음 (실제 직접 연결 위에서) ──
@pytest.mark.parametrize("mode", ["direct", "relay"])
def test_input_responsive_during_large_transfer(tmp_path, mode):
    w = World(tmp_path, direct_mode="normal" if mode == "direct" else "ignore", direct_timeout=1.5)
    try:
        s = w.client.open_session(w.agent_id, "1234")
        assert s.mode == mode
        done = threading.Event()

        def bulk():
            with s.request("GET", "/fs/file", {"path": "size:40000000"}, stream=True, timeout=(3, 30)) as r:
                for _ in r.iter_content(1 << 20):
                    pass
            done.set()
        threading.Thread(target=bulk, daemon=True).start()
        time.sleep(0.3)
        lat = []
        while not done.is_set() and len(lat) < 30:
            t0 = time.monotonic()
            assert s.request("GET", "/remote/input", {"ev": "[]"}, timeout=(3, 5)).ok
            lat.append(time.monotonic() - t0)
            time.sleep(0.03)
        assert len(lat) >= 5
        assert statistics.median(lat) < 0.5 and max(lat) < 2.0, (statistics.median(lat), max(lat))
        assert done.wait(60)
        s.close()
    finally:
        w.close()


# ── 끊김 복구 ──
def test_session_reconnects_after_link_drop(world):
    s = world.client.open_session(world.agent_id, "1234")
    try:
        assert s.request("GET", "/info").ok
        first = s.mux
        first.close("시험: 통로 끊김")
        eventually(lambda: s.mux is not first and s.mux.alive and s.state == "연결됨", timeout=15, msg="자동 재연결")
        assert s.request("GET", "/info").ok
    finally:
        s.close()


def test_session_survives_relay_server_restart_for_direct_connections(world):
    s = world.client.open_session(world.agent_id, "1234")
    try:
        assert s.mode == "direct"
        world.relay.restart()
        # 직접 연결은 안내 서버와 무관하게 계속 동작
        assert s.request("GET", "/remote/frame", {"size": 1000}).ok
        eventually(lambda: world.client.state == "연결됨", timeout=20, msg="사용자가 안내 서버에 다시 연결")
        eventually(lambda: world.entry() and world.entry()["online"], timeout=30, msg="현장이 다시 등록")
        assert world.entry()["name"] == "현장1"            # 서버 재시작 후에도 저장된 정보 유지
    finally:
        s.close()


def test_agent_reconnects_after_server_outage_and_keeps_id(world):
    before = world.agent_id
    world.relay.restart()
    eventually(lambda: world.entry() and world.entry()["online"], timeout=30, msg="현장 재접속")
    assert world.entry()["id"] == before


# ── 인증 키 · 사용량 · LAN 격리 ──
def test_server_key_required_when_configured(tmp_path):
    w = World(tmp_path, key="공용키")
    try:
        assert w.entry()["online"]
        from wan.client import WanClient
        from wan.config import WanConfig
        bad_cfg = WanConfig(str(tmp_path / "bad"))
        bad_cfg.update(relay_url=w.relay.url, relay_key="틀린키", stun="")
        c = WanClient(bad_cfg, None, "x", log=lambda m: None)
        c.start()
        try:
            eventually(lambda: c.state == "키 불일치", msg="틀린 키는 거부")
            assert c.agents == []
        finally:
            c.stop()
    finally:
        w.close()


def test_usage_is_counted_for_both_direct_and_relay(tmp_path):
    for mode, dm in (("direct", "normal"), ("relay", "ignore")):
        w = World(tmp_path / mode, direct_mode=dm, direct_timeout=1.5)
        try:
            s = w.client.open_session(w.agent_id, "1234")
            assert s.mode == mode
            before = w.usage.day_total()
            s.request("GET", "/remote/frame", {"size": 500_000}).content
            time.sleep(0.3)
            grown = w.usage.day_total() - before
            assert 500_000 < grown < 560_000, grown          # 본문 + 조각 머리 정도의 덧붙임
            st = s.stats()
            assert st["rx"] > 500_000 and st["mode"] == mode
            s.close()
        finally:
            w.close()


def test_agent_survives_lan_local_server_failure(world):
    s = world.client.open_session(world.agent_id, "1234")
    try:
        world.local.stop()                                          # LAN 쪽(로컬 서버)이 죽음
        r = s.request("GET", "/info")
        assert r.status_code == 502 and "내부 오류" in r.json()["error"]
        assert world.entry()["online"] and s.mux.alive              # WAN 연결·에이전트는 그대로
    finally:
        s.close()


def test_other_users_cannot_inject_into_someones_relay_session(tmp_path):
    w = World(tmp_path, direct_mode="ignore", direct_timeout=1.0)
    try:
        s = w.client.open_session(w.agent_id, "1234")
        assert s.mode == "relay"
        sid8 = s.mux.link.sid
        import asyncio, json
        from websockets.asyncio.client import connect

        async def attack():
            async with connect(w.relay.url, max_size=2 ** 20) as ws:
                await ws.send(json.dumps({"t": "hello", "role": "user", "key": ""}))
                await ws.recv()
                fake = P.encode_frames(P.REQ, 99, P.dumps({"m": "GET", "r": "/info", "l": 0, "p": {}, "h": {}}))[0]
                await ws.send(sid8 + fake)
                await asyncio.sleep(0.5)
        asyncio.run(attack())
        assert not [h for h in w.local.hits if h[1] == "/info"]      # 남의 세션으로 들어간 요청은 현장에 닿지 않음
        assert s.request("GET", "/info").ok                          # 정상 세션은 영향 없음
        s.close()
    finally:
        w.close()


def test_cloned_pc_with_same_id_gets_a_new_id(world, monkeypatch):
    """디스크를 복제해 만든 현장 PC 는 ID 가 같다 → 서로 밀어내다가 한쪽이 새 ID 를 받아 둘 다 목록에 나타남"""
    import os
    from wan_helpers import AgentRunner
    monkeypatch.setattr(agent_mod, "RECONNECT_BASE", 0.05)
    monkeypatch.setattr(agent_mod, "DUP_LIMIT", 2)
    clone_dir = os.path.join(world.tmp, "clone")
    os.makedirs(clone_dir, exist_ok=True)
    (open(os.path.join(clone_dir, "wan_config.json"), "w", encoding="utf-8")
     .write('{"agent_id": "%s"}' % world.agent_id))
    clone = AgentRunner(clone_dir, world.relay.url, world.local.port, name="복제본")
    try:
        eventually(lambda: len([a for a in world.client.agents if a["online"]]) == 2, timeout=20,
                   msg="ID 가 겹친 두 PC 가 서로 다른 ID 로 정리됨")
        ids = {world.agent.cfg.get("agent_id"), clone.cfg.get("agent_id")}
        assert len(ids) == 2
    finally:
        clone.stop()


def test_client_can_be_restarted_with_a_new_server_address(world):
    """탭의 "적용 후 연결" 은 client.stop() → client.start() 를 부른다"""
    world.client.stop()
    assert world.client.state in ("끊김", "연결됨")          # 정지 직후 상태는 어느 쪽이든 상관없음
    world.client.start()
    eventually(lambda: world.client.state == "연결됨", timeout=15, msg="재시작 후 다시 연결")
    eventually(lambda: world.entry() and world.entry()["online"], msg="목록을 다시 받음")
    s = world.client.open_session(world.agent_id, "1234")
    assert s.request("GET", "/info").ok
    s.close()


# ── 현장이 등록한 접근 코드를 중계 PC 가 사용자 PC 에 알려 준다 (더블클릭 접속) ──
def test_field_registers_its_code_on_connect_and_user_sees_it(world):
    assert world.entry()["code"] == "1234"                           # 현장이 접속하며 알린 코드


def test_code_changed_on_field_screen_reaches_users(world):
    from wan import agent as agent_mod
    from wan.config import WanConfig
    old = agent_mod.HEARTBEAT
    agent_mod.HEARTBEAT = 0.3
    try:
        world.agent.stop()
        from wan_helpers import AgentRunner
        world.agent = AgentRunner(world.agent.cfg.dir, world.relay.url, world.local.port, code="1234")
        eventually(lambda: world.entry() and world.entry()["online"] and world.entry()["code"] == "1234")
        WanConfig(world.agent.cfg.dir).update(access_code="바뀐-코드-5")
        eventually(lambda: world.entry()["code"] == "바뀐-코드-5", 10, msg="화면에서 바꾼 코드가 사용자 목록에 반영")
        s = world.client.open_session(world.agent_id, world.entry()["code"])
        s.close()
    finally:
        agent_mod.HEARTBEAT = old


def test_set_config_code_is_visible_even_while_field_is_offline(world):
    world.agent.stop()
    eventually(lambda: not world.entry()["online"])
    world.client.set_config(world.agent_id, {"access_code": "예약-코드"})
    eventually(lambda: world.entry()["code"] == "예약-코드")


def test_agents_without_code_show_empty_code(tmp_path):
    w = World(tmp_path, code="", key="shared")
    try:
        assert w.entry()["code"] == ""
    finally:
        w.close()


# ── 현장 삭제 ──
def test_offline_agent_can_be_forgotten_and_online_one_cannot(world):
    with pytest.raises(P.WanError, match="켜져 있는"):
        world.client.forget_agent(world.agent_id)
    world.agent.stop()
    eventually(lambda: not world.entry()["online"])
    world.client.forget_agent(world.agent_id)
    eventually(lambda: world.entry() is None, msg="목록에서 사라짐")
    with pytest.raises(P.WanError, match="이미 없는"):
        world.client.forget_agent(world.agent_id)


def test_forget_against_old_relay_fails_fast_with_clear_message(world):
    world.agent.stop()
    eventually(lambda: not world.entry()["online"])
    world.client.relay_caps = set()                                  # 옛 안내 서버(기능 목록을 안 보냄) 흉내
    with pytest.raises(P.WanError, match="새 버전으로 업데이트"):
        world.client.forget_agent(world.agent_id)


def test_forget_times_out_with_clear_message_when_relay_never_answers(world, monkeypatch):
    world.agent.stop()
    eventually(lambda: not world.entry()["online"])

    async def silent(self, ws, data):
        return None
    monkeypatch.setattr(type(world.relay.server), "_forget", silent)
    world.client.relay_caps = {"forget"}
    with pytest.raises(P.WanError, match="응답하지 않습니다"):
        world.client.forget_agent(world.agent_id, timeout=0.5)
    assert world.client._replies == {}                               # 대기 항목이 남지 않음
