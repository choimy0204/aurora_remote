"""
가짜 화면(DXGI 흉내) + 시나리오 데스크톱 — 진짜 RemoteHost.frame 과 진짜 뷰어로 "화면이 바뀔 때 화질" 을 재는 시험 장치
==========================================================================================================
SimScreen 은 aurora_remote._Screen 과 같은 모양(grab · gdi · note · close)이라 RemoteHost._screen 자리에 끼운다.
바뀐 칸 계산 · 거친 화면 판단은 진짜 코드(_DesktopDup._rough_wanted)를 그대로 쓴다. 장비의 GPU/COM 호출만 없다.
Desktop 은 시간에 맞춰 타이핑 · 창 전환 · 메뉴 번짐 · 스크롤을 일으키는 가짜 바탕화면.
"""

import random
import threading
import time

from PIL import Image, ImageDraw

TILE = 64


class Desktop:
    """시나리오: 입력 → 화면 변화.  markers 에 (이름, 시작, 끝) 이 쌓인다"""

    def __init__(self, w=1280, h=720, seed=3, on_input=None):
        self.w, self.h = w, h
        self.on_input = on_input or (lambda: None)     # 사용자가 키·마우스를 보낸 순간 (장비/뷰어가 서두를 수 있게)
        self.events = []                               # (시각, 종류, 위치, 버전) — 입력 반응 시간을 재는 용도
        self.version = 0                               # 화면이 바뀔 때마다 올라감
        self._rnd = random.Random(seed)
        self._lock = threading.Lock()
        self.docs = [self._make_doc(0), self._make_doc(1)]
        self.img = self.docs[0].copy()
        self.markers = []
        self.t0 = None

    def _make_doc(self, kind):
        im = Image.new("RGB", (self.w, self.h), (250, 250, 250) if kind == 0 else (32, 36, 44))
        d = ImageDraw.Draw(im)
        fg = (20, 20, 20) if kind == 0 else (200, 210, 220)
        d.rectangle((0, 0, self.w, 36), fill=(40, 90, 170) if kind == 0 else (90, 60, 140))
        words = "the quick brown fox jumps over lazy dog remote screen quality data".split()
        for row in range(40, self.h - 20, 18):
            d.text((20, row), " ".join(self._rnd.choice(words) for _ in range(16)), fill=fg)
        return im

    def snapshot(self):
        with self._lock:
            return self.img.copy()

    def snapshot_v(self):
        with self._lock:
            return self.img.copy(), self.version

    def _set(self, fn):
        with self._lock:
            fn(self.img)
            self.version += 1

    # ── 시나리오 ──
    def run(self, stop: threading.Event):
        self.t0 = time.monotonic()
        steps = [("idle", 1.0, None), ("typing", 2.4, self._typing), ("idle", 1.0, None),
                 ("switch", 1.5, self._switch), ("menu", 1.5, self._menu), ("scroll", 1.8, self._scroll),
                 ("idle", 1.5, None)]
        for name, dur, fn in steps:
            if stop.is_set():
                return
            start = time.monotonic() - self.t0
            if fn:
                fn(stop)
            rest = start + dur - (time.monotonic() - self.t0)
            if rest > 0:
                stop.wait(rest)
            self.markers.append((name, start, start + dur))

    def _typing(self, stop):
        x = 40
        for i in range(24):                                  # 100ms 마다 글자 하나
            if stop.is_set():
                return
            self.on_input()
            self._set(lambda im, x=x: ImageDraw.Draw(im).text((x, 300), self._rnd.choice("abcdefghij"), fill=(0, 0, 0)))
            self.events.append((time.monotonic() - self.t0, "key", (x, 300), self.version))
            x += 8
            time.sleep(0.1)

    def _switch(self, stop):                                 # 다른 창으로 전환 — 한 번만 화면 전체가 바뀜
        self.on_input()
        self.events.append((time.monotonic() - self.t0, "switch", None, self.version + 1))
        self._set(lambda im: im.paste(self.docs[1]))

    def _menu(self, stop):                                   # 메뉴 번짐: 넓은 영역이 80ms 간격으로 몇 번 바뀜
        self.on_input()
        self.events.append((time.monotonic() - self.t0, "menu", None, self.version + 1))
        for a in (60, 120, 180, 240):
            self._set(lambda im, a=a: im.paste(Image.new("RGB", (int(self.w * .85), int(self.h * .85)), (a, a, a + 10)), (20, 20)))
            time.sleep(0.08)
        self._set(lambda im: im.paste(self.docs[1]))

    def _scroll(self, stop):                                 # 계속 스크롤: 50ms 마다 전체가 위로 밀림
        for _ in range(32):
            if stop.is_set():
                return
            def shift(im):
                strip = im.crop((0, 60, self.w, self.h))
                im.paste(strip, (0, 40))
                ImageDraw.Draw(im).text((20, self.h - 30), " ".join(self._rnd.choice(["data", "scroll", "text"]) for _ in range(14)), fill=(200, 210, 220))
            self._set(shift)
            time.sleep(0.05)


class SimScreen:
    """_Screen 흉내 — 바뀐 곳은 64 격자로 모아 DIRTY_FULL 넘으면 통째로, 크게 바뀌면 거친 화면(진짜 판단 코드)"""
    gdi = False
    note = ""
    GDI_INTERVAL = 0.5

    def __init__(self, ar, desktop: Desktop):
        self.ar, self.desk = ar, desktop
        self.tile = getattr(ar, "DIRTY_TILE", TILE)       # 장비의 바뀐 칸 격자와 같게
        self.dup = ar._DesktopDup()
        self.o = {"big": 0.0, "stale": 0.0, "sysmem": False}
        self.img = None            # 장비가 가진 화면 (제자리에서 고침)
        self.truth = None          # 마지막으로 읽은 실제 화면
        self.rough_frames = 0
        self.version = 0           # 마지막으로 읽은 바탕화면 버전
        self.sharp_big = 0

    def close(self):
        pass

    def grab(self, rect, sig, rough_ok=True):
        w, h = self.desk.w, self.desk.h
        now = time.monotonic()
        cur, self.version = self.desk.snapshot_v()
        if self.img is None:
            self.img, self.truth = cur.copy(), cur
            return self.img, [(0, 0, w, h)], None
        tiles = self._dirty_tiles(self.truth, cur, self.tile)
        o = self.o
        if not tiles:
            if o["stale"]:                                   # 잠잠해짐 → 선명하게 다시 읽음
                o["stale"] = 0.0
                self.img.paste(cur)
                self.truth = cur
                return self.img, [(0, 0, w, h)], None
            return self.img, [], None
        big = len(tiles) > (-(-w // self.tile)) * (-(-h // self.tile)) * self.ar.DIRTY_FULL
        if big:
            wanted = (self.dup._rough_wanted(o, rough_ok, now) if hasattr(self.dup, "_rough_wanted")
                      else bool(rough_ok and (not o["stale"] or now - o["stale"] < self.dup.ROUGH_MAX)))
            if wanted:
                o["stale"] = o["stale"] or now
                self.truth = cur
                self.rough_frames += 1
                k = max(2, min(4, w // 640))
                rough = cur.resize((w, h // k), Image.NEAREST).reduce((k, 1))   # 줄 건너뛰기 + 가로 평균 (진짜 _read_rough 와 같은 모양)
                return self.img, [(0, 0, w, h)], rough
            o["stale"] = 0.0
            self.sharp_big += 1
            self.img.paste(cur)
            self.truth = cur
            return self.img, [(0, 0, w, h)], None
        if o["stale"]:
            o["stale"] = 0.0
            self.img.paste(cur)
            self.truth = cur
            return self.img, [(0, 0, w, h)], None
        for x0, y0, x1, y1 in tiles:
            self.img.paste(cur.crop((x0, y0, x1, y1)), (x0, y0))
        self.truth = cur
        return self.img, tiles, None

    @staticmethod
    def _dirty_tiles(a, b, TILE=TILE):
        """바뀐 칸 [(x0, y0, x1, y1)] — 차이 그림을 칸 크기로 줄여(밝은 쪽 우선) 한 번에 찾음"""
        from PIL import ImageChops
        diff = ImageChops.difference(a, b).convert("L").point(lambda p: 255 if p else 0)
        if diff.getbbox() is None:
            return []
        w, h = a.size
        cols, rows = -(-w // TILE), -(-h // TILE)
        grid = diff.resize((cols, rows), Image.BOX)          # 칸 안에 바뀐 점이 하나라도 있으면 0 보다 큼
        px = grid.load()
        return [(x * TILE, y * TILE, min(w, x * TILE + TILE), min(h, y * TILE + TILE))
                for y in range(rows) for x in range(cols) if px[x, y] > 0]
