"""끝에서 끝까지 — 실제 앱(AuroraClientApp) · 진짜 RemoteViewer · 진짜 CommandHandler · 안내 서버 · 현장 에이전트 · aiortc
Windows 화면 캡처/입력만 가짜(sim_host)로 대체.  실행:  xvfb-run -a python -m pytest tests/test_wan_full_chain.py"""

import os
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

tkinter = pytest.importorskip("tkinter")
from PIL import ImageChops, ImageStat  # noqa: E402

from wan_helpers import AgentRunner, RelayRunner, eventually  # noqa: E402


def make_chain(tmp_path, monkeypatch, real_host=False):
    import winshim
    winshim.sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("AURORA_WAN_DIR", str(tmp_path / "wan"))
    import winshim
    root = winshim.make_root()
    mod = winshim.load_client(real_remote=True)
    ar = __import__("sys").modules["aurora_remote"]
    monkeypatch.setattr(ar, "_GetMessageW", lambda *a: 0)
    monkeypatch.setattr(mod.AuroraClientApp, "_start_threads", lambda self: None)
    mod._init_scale(root)
    mod.AuroraClientApp.setup_styles()
    app = mod.AuroraClientApp(root)

    from sim_host import SimHost
    sim = SimHost(ar)
    events, roots_seen = [], []

    class FakeRemote:                      # Windows 화면 캡처·입력 자리 (현장 PC 의 RemoteHost 대신)
        def frame(self, q, who):
            return sim.frame(q)

        def input(self, q, who):
            events.append(q)
            return {"ok": True}

        def end(self, q, who):
            return {"ok": True}

        def clip(self, q, who):
            return {"ok": True}
    if not real_host:                       # real_host=True 면 Windows 의 진짜 RemoteHost 를 그대로 씀
        monkeypatch.setattr(mod, "_remote", lambda: FakeRemote())
    real_roots = mod.export_roots
    monkeypatch.setattr(mod, "export_roots", lambda: roots_seen.append(1) or real_roots())
    (tmp_path / "Desktop").mkdir()
    (tmp_path / "Desktop" / "현장파일.txt").write_text("안녕", encoding="utf-8")
    mod.config.update(export_key="chain-key", remote_enabled=True)
    # WAN 에이전트는 별도 프로세스라 그 안의 CommandHandler 는 app_instance=None (GUI 로그를 쓰지 않음) — 같은 조건으로
    monkeypatch.setattr(mod, "app_instance", None)

    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.CommandHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay = RelayRunner(str(tmp_path / "relay"))
    field = AgentRunner(str(tmp_path / "field"), relay.url, server.server_address[1], code="1234",
                        key_getter=lambda: mod.config.get("export_key", ""))

    from wan.client import WanClient
    from wan.config import WanConfig
    ucfg = WanConfig(str(tmp_path / "user"))
    ucfg.update(relay_url=relay.url, stun="", direct_timeout=4.0, force_relay=False)
    client = WanClient(ucfg, None, "2.16.0", log=lambda m: None)
    client.start()
    eventually(lambda: client.state == "연결됨")
    eventually(lambda: any(a["online"] for a in client.agents))
    env = dict(root=root, app=app, mod=mod, sim=sim, events=events, roots_seen=roots_seen, client=client,
               field=field, agent=client.agents[0], desktop=tmp_path / "Desktop")
    yield env
    teardown(env, relay, server, root, app, client, field)


def teardown(env, relay, server, root, app, client, field):
    import winshim
    for fn in (client.stop, field.stop, relay.close, server.shutdown):
        try:
            fn()
        except Exception:
            pass
    try:
        app._wan_shutdown()
    except Exception:
        pass
    winshim.destroy_root(root)


@pytest.fixture
def chain(tmp_path, monkeypatch):
    yield from make_chain(tmp_path, monkeypatch)


def run(root, seconds):
    root.after(int(seconds * 1000), root.quit)
    root.mainloop()


def test_remote_window_shows_field_screen_and_sends_input_over_wan(chain):
    root, app, sim, client, agent = chain["root"], chain["app"], chain["sim"], chain["client"], chain["agent"]
    stop = threading.Event()
    threading.Thread(target=sim.painter, args=(stop, 0.05), daemon=True).start()

    session = client.open_session(agent["id"], "1234", agent["name"])
    assert session.mode == "direct"
    app._wan_open_remote(session, agent)                         # 실제 앱의 연결 코드 — 원격 창 + 반입/반출 패널 생성
    viewer = app._wan_views[agent["id"]]
    assert viewer.alive and viewer._quality == "절약"
    run(root, 3.0)

    # 화면: 현장이 바뀌는 동안에도 계속 받고, 멈추면 끝내 같아짐
    assert viewer._error == "" and viewer._base is not None
    stop.set()
    run(root, 2.5)
    assert viewer._base_seq == sim.seq
    diff = ImageChops.difference(viewer._base.convert("RGB"), sim.snap[viewer._base_seq].convert("RGB"))
    assert sum(ImageStat.Stat(diff).mean) / 3 < 8

    # 오른쪽 반입/반출 패널이 실제 CommandHandler 에 장비 폴더 목록을 요청함 (LAN 패널과 같은 코드를 WAN 통로로)
    assert chain["roots_seen"], "반입/반출 패널이 /fs/roots 를 요청하지 않음"
    assert viewer._side is not None and viewer._side.winfo_children()

    # 입력: 조작을 켜고 마우스 이동을 보내면 현장(FakeRemote.input)까지 도착
    viewer._ctl.set(True)
    viewer._ctl_changed()
    viewer._frame_time = time.monotonic()
    viewer._push(["m", 1234, 4321])
    run(root, 1.5)
    assert any('"m"' in str(e.get("ev", "")) and "1234" in str(e.get("ev", "")) for e in chain["events"]), chain["events"][:3]

    # 닫으면 세션도 닫히고 현장의 세션도 정리됨
    viewer.close()
    assert session.closed and agent["id"] not in app._wan_views
    eventually(lambda: not chain["field"].agent.sessions, timeout=10, msg="현장 쪽 세션 정리")


def test_second_open_reuses_window_and_closes_extra_session(chain):
    root, app, client, agent = chain["root"], chain["app"], chain["client"], chain["agent"]
    s1 = client.open_session(agent["id"], "1234")
    app._wan_open_remote(s1, agent)
    first = app._wan_views[agent["id"]]
    s2 = client.open_session(agent["id"], "1234")
    app._wan_open_remote(s2, agent)                                # 같은 장비를 또 열면 기존 창을 앞으로 + 새 세션은 닫음
    assert app._wan_views[agent["id"]] is first and s2.closed and not s1.closed
    first.close()


def test_file_pane_download_through_wan(chain):
    """패널이 쓰는 _export_request 로 실제 파일을 내려받음 (진짜 CommandHandler 의 /fs/file)"""
    client, agent, mod, desktop = chain["client"], chain["agent"], chain["mod"], chain["desktop"]
    s = client.open_session(agent["id"], "1234")
    pane = object.__new__(mod._WanTransferPane)
    pane._session = s
    r = pane._export_request("/fs/file", {"path": str(desktop / "현장파일.txt")}, "ignored", timeout=(3, 10), stream=True)
    assert r.status_code == 200 and b"".join(r.iter_content(1024)).decode("utf-8") == "안녕"
    s.close()
