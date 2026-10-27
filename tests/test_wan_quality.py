"""화면이 바뀔 때의 화질·데이터 — 진짜 RemoteHost.frame 을 가짜 화면(tests/sim_screen.py)으로 돌려 시험 (Windows 불필요)
- WebP 요청이면 같은 화질에서 JPEG 보다 작고 안 번짐, 요청이 없으면(LAN · 옛 보는 쪽) JPEG 그대로
- 거친(모자이크) 화면은 보는 쪽이 허용할 때, 그리고 계속 바뀌는 동안에만
- 입력을 받으면 바로 다시 캡처"""

import contextlib
import io
import time

import pytest

pytest.importorskip("PIL")
from PIL import Image, ImageChops, ImageStat  # noqa: E402

from sim_screen import Desktop, SimScreen  # noqa: E402


@pytest.fixture
def rig(monkeypatch):
    import winshim
    ar = winshim.load_remote()
    monkeypatch.setattr(ar, "_monitors", lambda: [(0, 0, 1280, 720, 1)])
    monkeypatch.setattr(ar, "_virtual_screen", lambda: (0, 0, 1280, 720))
    monkeypatch.setattr(ar, "_physical_pixels", lambda: contextlib.nullcontext())
    monkeypatch.setattr(ar, "low_priority", lambda level=-1: contextlib.nullcontext())
    monkeypatch.setattr(ar.RemoteHost, "_cursor", staticmethod(lambda rect: None))
    desk = Desktop()
    host = ar.RemoteHost(log=lambda m: None)
    host._screen = SimScreen(ar, desk)

    class Rig:
        pass
    r = Rig()
    r.ar, r.desk, r.host = ar, desk, host

    def ask(**q):
        base = {"sid": "t", "mon": -1, "w": 1280, "h": 720, "q": 60, "last": -1, "wan": 1, "rough": 0, "fmt": "webp"}
        base.update(q)
        host._next_cap = 0.0                      # 시험은 쉬는 시간 없이 바로 캡처
        meta, body = ar._unpack(host.frame({k: str(v) for k, v in base.items()}, "시험"))
        return meta, body
    r.ask = ask
    return r


def decode(meta, body):
    parts, pos, out = meta["parts"], 0, []
    for x, y, w, h, n in parts:
        out.append((x, y, Image.open(io.BytesIO(body[pos:pos + n])).convert("RGB")))
        pos += n
    return out


def mean_error(a, b):
    return ImageStat.Stat(ImageChops.difference(a, b).convert("L")).mean[0]


def test_webp_only_when_asked_and_much_smaller_at_same_quality(rig):
    if not rig.ar.WEBP_OK:
        pytest.skip("이 Pillow 는 WebP 를 못 씀")
    wm, wb = rig.ask(fmt="webp")
    jm, jb = rig.ask(fmt="")                        # LAN · 옛 보는 쪽: 아무것도 안 보냄
    assert wb[:4] == b"RIFF" and jb[:2] == b"\xff\xd8"
    assert len(wb) < 0.8 * len(jb), (len(wb), len(jb))
    truth = rig.desk.snapshot()
    ew = mean_error(decode(wm, wb)[0][2], truth)
    ej = mean_error(decode(jm, jb)[0][2], truth)
    assert ew <= ej + 0.2, (ew, ej)                 # 더 작은데 화질은 같거나 더 좋음


def test_typing_patches_are_small_with_webp(rig):
    if not rig.ar.WEBP_OK:
        pytest.skip("이 Pillow 는 WebP 를 못 씀")
    sizes = {}
    for fmt in ("webp", ""):
        host = rig.ar.RemoteHost(log=lambda m: None)
        host._screen = SimScreen(rig.ar, rig.desk)
        rig.host = host
        first, _ = rig.ask(fmt=fmt)
        total = 0
        for i in range(6):
            from PIL import ImageDraw
            rig.desk._set(lambda im, i=i: ImageDraw.Draw(im).text((40 + 8 * i, 300), "k", fill=(0, 0, 0)))
            meta, body = rig.ask(fmt=fmt, last=first["seq"] if i == 0 else meta["seq"])
            assert meta.get("base") is not None            # 조각 응답
            total += len(body)
        sizes[fmt] = total
    assert sizes["webp"] < 0.6 * sizes[""], sizes          # 글자 몇 개 치는 데이터가 JPEG 의 60% 미만


def test_rough_frame_needs_viewer_consent_and_continuous_change(rig):
    ar, desk = rig.ar, rig.desk
    first, _ = rig.ask(rough=1)
    last = first["seq"]
    screen = rig.host._screen
    # 한 번만 크게 바뀜 → 허용해도 바로 선명
    time.sleep(0.5)
    desk._set(lambda im: im.paste(desk.docs[1]))
    meta, _ = rig.ask(rough=1, last=last)
    assert not meta.get("rough"), meta
    last = meta["seq"]
    # 바로 이어서 또 크게 바뀜(계속 바뀌는 중) → 허용하면 거친 화면
    desk._set(lambda im: im.paste(desk.docs[0]))
    meta, _ = rig.ask(rough=1, last=last)
    assert meta.get("rough"), meta
    assert screen.rough_frames == 1
    # 허용하지 않으면 계속 바뀌어도 선명
    desk._set(lambda im: im.paste(desk.docs[1]))
    meta, _ = rig.ask(rough=0, last=meta["seq"])
    assert not meta.get("rough"), meta
    assert screen.rough_frames == 1


def test_input_makes_host_capture_right_away(rig):
    host = rig.host
    host._next_cap = time.monotonic() + 10
    host.note_input()
    assert host._next_cap == 0.0 and host._act_until > time.monotonic()
