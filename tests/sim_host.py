"""
RemoteHost.frame 흉내 — 진짜 aurora_remote 의 응답 형식(_pack · _jpeg)과 "누적 변경분" 규칙을 그대로 따르는 가짜 현장 화면
===========================================================================================================
요청의 last 번호 이후 바뀐 칸(타일)을 모두 JPEG 조각으로 돌려준다 (직전에 보낸 것이 아니라 last 기준 누적).
last 가 기록 범위 밖이거나 너무 많이 바뀌었으면 화면 전체. 응답에는 seq · size · parts · base(조각일 때) · next 가 있다.
"""

import json
import random
import threading
import time

from PIL import Image, ImageDraw


class SimHost:
    TILE = 64

    def __init__(self, aurora_remote, w=640, h=360):
        self.ar = aurora_remote
        self.w, self.h = w, h
        self.screen = Image.new("RGB", (w, h), (40, 40, 40))
        self.seq = 1
        self.hist = []                 # [(seq, 사각형)]
        self.hist_base = 1
        self.snap = {1: self.screen.copy()}
        self._lock = threading.Lock()
        self.bytes_sent = 0
        self.full_frames = self.diff_frames = 0

    # ── 화면이 바뀜 ──
    def paint(self, rect, color):
        with self._lock:
            ImageDraw.Draw(self.screen).rectangle(rect, fill=color)
            self.seq += 1
            self.hist.append((self.seq, rect))
            self.snap[self.seq] = self.screen.copy()
            if len(self.snap) > 400:
                self.snap.pop(min(self.snap))

    def painter(self, stop: threading.Event, every=0.04, seed=1):
        rnd = random.Random(seed)
        while not stop.is_set():
            x, y = rnd.randrange(0, self.w - 80), rnd.randrange(0, self.h - 60)
            self.paint((x, y, x + rnd.randrange(20, 80), y + rnd.randrange(20, 60)),
                       (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
            time.sleep(every)

    # ── 요청 처리 ──
    def frame(self, q: dict) -> bytes:
        last = int(q.get("last", -1))
        qual = int(q.get("q", 60))
        with self._lock:
            seq, img = self.seq, self.screen.copy()
            meta = {"seq": seq, "mons": [], "next": 0.02}
            if last == seq:
                return self.ar._pack(meta)
            rects = self._tiles_since(last) if self.hist_base <= last < seq else None
            if rects is not None:
                parts, chunks = [], []
                for (x0, y0, x1, y1) in rects:
                    j = self.ar._jpeg(img.crop((x0, y0, x1, y1)), qual)
                    parts.append([x0, y0, x1 - x0, y1 - y0, len(j)])
                    chunks.append(j)
                meta.update(size=[self.w, self.h], parts=parts, base=last)
                body = b"".join(chunks)
                self.diff_frames += 1
            else:
                j = self.ar._jpeg(img, qual)
                meta.update(size=[self.w, self.h], parts=[[0, 0, self.w, self.h, len(j)]])
                body = j
                self.full_frames += 1
            out = self.ar._pack(meta, body)
            self.bytes_sent += len(out)
            return out

    def _tiles_since(self, last):
        t, tiles = self.TILE, set()
        for s, (x0, y0, x1, y1) in self.hist:
            if s > last:
                for ty in range(y0 // t, (y1 - 1) // t + 1):
                    for tx in range(x0 // t, (x1 - 1) // t + 1):
                        tiles.add((tx, ty))
        if len(tiles) > 0.6 * (-(-self.w // t)) * (-(-self.h // t)):
            return None
        return [(tx * t, ty * t, min(self.w, tx * t + t), min(self.h, ty * t + t)) for tx, ty in sorted(tiles)]


class _Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return json.loads(self.content.decode("utf-8"))


class SimSession:
    """Session 흉내 — 요청마다 왕복 지연(rtt ± jitter)을 주고 SimHost 가 응답"""
    mode = "direct"
    closed = False

    def __init__(self, host: SimHost, rtt=0.2, jitter=0.0, seed=7):
        self.host, self.rtt, self.jitter = host, rtt, jitter
        self.tx = self.rx = 0
        self.requests = []
        self.inflight = self.peak_inflight = 0      # 화면 요청이 동시에 몇 개까지 날아갔는지
        self._rnd = random.Random(seed)
        self._lock = threading.Lock()

    def _delay(self):
        with self._lock:
            return max(0.0, self.rtt / 2 + self._rnd.uniform(-self.jitter, self.jitter) / 2)

    def viewer_request(self, route, params, timeout):
        with self._lock:
            self.requests.append((route, dict(params)))
            if route == "/remote/frame":
                self.inflight += 1
                self.peak_inflight = max(self.peak_inflight, self.inflight)
        time.sleep(self._delay())
        if route == "/remote/frame":
            body = self.host.frame(params)
        else:
            body = json.dumps({"ok": True}).encode()
        time.sleep(self._delay())
        with self._lock:
            if route == "/remote/frame":
                self.inflight -= 1
            self.tx += 200
            self.rx += len(body) + 100
        return _Resp(body)

    def close(self):
        self.closed = True
