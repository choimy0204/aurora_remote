"""
다중화 — 통로 하나에 요청 여러 개를 동시에 싣는다 (화면 · 입력 · 파일이 서로 막지 않게)
================================================================================
- 사용자 쪽은 Mux.request() 를 부르면 requests 와 비슷한 응답(WanResponse)을 받는다.
  RemoteViewer · 반입/반출 코드가 거의 그대로 동작하도록 status_code · ok · json() · content ·
  iter_content() · with 문을 지원한다.
- 우선순위: 입력(0) > 화면·목록(1) > 파일(2). 파일은 보내는 버퍼가 조금만 차도 기다려서
  마우스 입력이 큰 파일 뒤에 줄 서지 않는다.
- 흐름 제어: 본문은 받는 쪽이 읽은 만큼 ACK 를 돌려주고, 보내는 쪽은 WINDOW 이상 앞서가지 않는다.
- 같은 Mux 가 요청을 "보내는" 쪽(사용자)과 "받아서 처리하는" 쪽(현장, Bridge)을 모두 맡는다.
- 모든 상태 변화는 이벤트 루프 스레드에서 일어나고, request() 와 Window 만 다른 스레드에서 부른다.
"""

import asyncio
import itertools
import json
import queue
import struct
import threading
import time
from collections import deque

from . import protocol as P

WINDOW     = 512 * 1024      # 읽기 전에 앞서 보낼 수 있는 본문 양
ACK_EVERY  = 32 * 1024       # 이만큼 읽을 때마다 ACK
LIMIT      = {P.PRIO_NORMAL: 256 * 1024, P.PRIO_BULK: 48 * 1024}   # 이 이상 쌓이면 그 등급은 대기
PING_EVERY = 5.0
DEAD_AFTER = 20.0            # 이 시간 동안 아무것도 못 받으면 끊김으로 봄


# ─────────────────────────────────────────
# 흐름 제어 창 (스레드 사이에서 쓰임)
# ─────────────────────────────────────────
class Window:
    def __init__(self, size: int = WINDOW):
        self.size = size
        self._cv = threading.Condition()
        self._inflight = 0
        self._error = None

    def acquire(self, n: int, timeout: float | None = None):
        end = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            while self._error is None and self._inflight and self._inflight + n > self.size:
                remain = None if end is None else end - time.monotonic()
                if remain is not None and remain <= 0:
                    raise P.WanTimeout("상대가 데이터를 받지 않습니다")
                self._cv.wait(remain)
            if self._error is not None:
                raise self._error
            self._inflight += n

    def release(self, n: int):
        with self._cv:
            self._inflight = max(0, self._inflight - n)
            self._cv.notify_all()

    def abort(self, exc: Exception):
        with self._cv:
            self._error = exc
            self._cv.notify_all()


def _split_timeout(timeout) -> tuple:
    if isinstance(timeout, (tuple, list)):
        return float(timeout[0]), float(timeout[1])
    t = 30.0 if timeout is None else float(timeout)
    return t, t


class _Headers(dict):
    """대소문자를 가리지 않는 헤더 (requests 응답처럼 쓰이도록)"""
    def __init__(self, items=None):
        super().__init__({str(k).lower(): v for k, v in (items or {}).items()})

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())

    def __contains__(self, key):
        return super().__contains__(str(key).lower())


# ─────────────────────────────────────────
# 보내는 쪽(사용자): 응답
# ─────────────────────────────────────────
class _Call:
    def __init__(self, rid: int, read_timeout: float):
        self.rid = rid
        self.read_timeout = read_timeout
        self.status = 0
        self.headers = _Headers()
        self.head = threading.Event()
        self.body = queue.Queue()
        self.error = None
        self.finished = False
        self.req_window = Window()

    def fail(self, exc: Exception):
        if self.error is None:
            self.error = exc
        self.finished = True
        self.head.set()
        self.req_window.abort(exc)
        self.body.put(None)


class WanResponse:
    """requests.Response 처럼 쓰는 응답"""

    def __init__(self, mux: "Mux", call: _Call):
        self._mux, self._call = mux, call
        self.status_code = call.status
        self.headers = call.headers
        self._content = None
        self._unacked = 0
        self._ended = False

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def _get(self):
        """다음 조각 (끝이면 None). 중간에 끊겼으면 예외"""
        try:
            item = self._call.body.get(timeout=self._call.read_timeout)
        except queue.Empty:
            self.close()
            raise P.WanTimeout("응답이 중간에 멈췄습니다") from None
        if item is None:
            self._ended = True
            self._mux._forget(self._call.rid)
            if self._call.error is not None:
                raise self._call.error
            return None
        self._unacked += len(item)
        if self._unacked >= ACK_EVERY:
            self._mux._ack(self._call.rid, self._unacked, req_flow=False)
            self._unacked = 0
        return item

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False):
        """받은 조각을 chunk_size 이하로 나눠 차례로 돌려줌. 중간에 끊겼으면 끝에서 예외"""
        chunk_size = max(1, chunk_size or 1)
        while True:
            item = self._get()
            if item is None:
                return
            for i in range(0, len(item), chunk_size):
                yield item[i:i + chunk_size]

    @property
    def content(self) -> bytes:
        if self._content is None:
            self._content = b"".join(self.iter_content(64 * 1024)) if not self._ended else b""
        return self._content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content.decode("utf-8"))

    def close(self):
        if not self._ended and not self._call.finished:
            self._mux.cancel(self._call)
        self._ended = True
        self._mux._forget(self._call.rid)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ─────────────────────────────────────────
# 받는 쪽(현장): 처리 중인 요청
# ─────────────────────────────────────────
class Served:
    """통로로 들어온 요청 하나. Bridge 의 작업 스레드가 쓴다"""

    def __init__(self, mux: "Mux", rid: int, head: dict):
        self.mux, self.rid, self.head = mux, rid, head
        self.method = str(head.get("m", "GET")).upper()
        self.route = str(head.get("r", ""))
        self.params = head.get("p") or {}
        self.headers = head.get("h") or {}
        self.length = int(head.get("l") or 0)
        self.body = queue.Queue()
        self.cancelled = threading.Event()
        self.res_window = Window()
        self._prio = P.priority_for(self.route)     # 머리·본문·끝이 같은 줄을 써야 순서가 안 뒤바뀜
        self._unacked = 0
        self._head_sent = False

    # ── 요청 본문 읽기 (로컬 서버로 흘려 보낼 때) ──
    def read_body(self, n: int, timeout: float = 60.0) -> bytes:
        try:
            item = self.body.get(timeout=timeout)
        except queue.Empty:
            raise P.WanTimeout("요청 본문이 도착하지 않습니다") from None
        if item is None:
            if self.cancelled.is_set():
                raise P.WanError("요청이 취소되었습니다")
            return b""
        self._unacked += len(item)
        if self._unacked >= ACK_EVERY:
            self.mux._ack(self.rid, self._unacked, req_flow=True)
            self._unacked = 0
        return item

    # ── 응답 보내기 ──
    def send_head(self, status: int, headers: dict | None = None):
        self._head_sent = True
        self.mux.post(P.RES, self.rid, P.dumps({"s": status, "h": headers or {}}), self._prio)

    def send_body(self, data: bytes, timeout: float = 60.0):
        for i in range(0, len(data), P.MAX_DATA):
            piece = data[i:i + P.MAX_DATA]
            if self.cancelled.is_set():
                raise P.WanError("요청이 취소되었습니다")
            self.res_window.acquire(len(piece), timeout)
            self.mux.post(P.RES_BODY, self.rid, piece, self._prio)

    def end(self, err: str | None = None):
        self.mux.post(P.RES_END, self.rid, P.dumps({"ok": err is None, "err": err}), self._prio)
        self.mux._served_done(self.rid)

    @property
    def head_sent(self) -> bool:
        return self._head_sent


# ─────────────────────────────────────────
# Mux
# ─────────────────────────────────────────
class Mux:
    def __init__(self, link, loop: asyncio.AbstractEventLoop, *, name: str = "", bridge=None,
                 on_bytes=None, on_dead=None, ping_every: float = PING_EVERY, dead_after: float = DEAD_AFTER):
        self.link, self.loop, self.name = link, loop, name
        self.bridge, self.on_bytes, self.on_dead = bridge, on_bytes, on_dead
        self.alive = True
        self.dead_reason = ""
        self.tx = self.rx = 0
        self.rtt_ms = None
        self.last_rx = time.monotonic()
        self._ping_every, self._dead_after = ping_every, dead_after
        self._q = {p: deque() for p in (P.PRIO_CONTROL, P.PRIO_NORMAL, P.PRIO_BULK)}
        self._wake = asyncio.Event()
        self._reasm = P.Reassembler()
        self._lock = threading.Lock()
        self._rids = itertools.count(1)
        self._calls: dict = {}
        self._served: dict = {}
        link.set_handlers(self._on_frame, self._link_closed, self._wake.set)
        self._tasks = [loop.create_task(self._writer()), loop.create_task(self._pinger())]

    # ── 보내기 ──
    def post(self, kind: int, rid: int, payload: bytes = b"", prio: int = P.PRIO_NORMAL, flags: int = 0):
        """어느 스레드에서든 호출 가능 — frame 으로 나눠 우선순위 줄에 넣는다"""
        frames = P.encode_frames(kind, rid, payload, flags)
        try:
            self.loop.call_soon_threadsafe(self._enqueue, frames, prio)
        except RuntimeError:
            raise P.WanError("통로가 닫혔습니다") from None

    def _enqueue(self, frames: list, prio: int):
        if self.alive:
            self._q[prio].extend(frames)
            self._wake.set()

    def _pick(self, pop: bool = True):
        for prio in (P.PRIO_CONTROL, P.PRIO_NORMAL, P.PRIO_BULK):
            q = self._q[prio]
            if q and (prio == P.PRIO_CONTROL or self.link.buffered < LIMIT[prio]):
                return q.popleft() if pop else q[0]
        return None

    async def _writer(self):
        try:
            while self.alive:
                frame = self._pick()
                if frame is None:
                    self._wake.clear()
                    if self._pick(pop=False) is not None:
                        continue
                    queued = any(self._q.values())
                    try:   # 줄 선 것이 있는데 못 보내는 중이면 버퍼가 비는 것을 짧게 확인하며 기다림
                        await asyncio.wait_for(self._wake.wait(), 0.05 if queued else None)
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self.link.send(frame)
                self.tx += len(frame)
                if self.on_bytes:
                    self.on_bytes(len(frame), 0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._die(f"전송 실패: {e}")

    async def _pinger(self):
        try:
            while self.alive:
                await asyncio.sleep(self._ping_every)
                if time.monotonic() - self.last_rx > self._dead_after:
                    self._die("상대 응답 없음")
                    return
                self._enqueue(P.encode_frames(P.PING, 0, struct.pack(">d", time.monotonic())), P.PRIO_CONTROL)
        except asyncio.CancelledError:
            raise

    # ── 받기 (이벤트 루프 스레드) ──
    def _on_frame(self, frame: bytes):
        self.rx += len(frame)
        self.last_rx = time.monotonic()
        if self.on_bytes:
            self.on_bytes(0, len(frame))
        try:
            msg = self._reasm.feed(frame)
        except ValueError:
            return
        if msg is None:
            return
        kind, flags, rid, data = msg
        try:
            self._dispatch(kind, flags, rid, data)
        except Exception as e:     # 잘못된 메시지 하나가 통로 전체를 죽이지 않게
            self._served_error(rid, f"처리 오류: {e}")

    def _dispatch(self, kind, flags, rid, data):
        if kind == P.PING:
            self._enqueue(P.encode_frames(P.PONG, 0, data), P.PRIO_CONTROL)
        elif kind == P.PONG:
            if len(data) == 8:
                self.rtt_ms = (time.monotonic() - struct.unpack(">d", data)[0]) * 1000
        elif kind == P.REQ:
            self._on_req(rid, data)
        elif kind == P.REQ_BODY:
            s = self._served.get(rid)
            if s:
                s.body.put(data)
        elif kind == P.REQ_END:
            s = self._served.get(rid)
            if s:
                s.body.put(None)
        elif kind == P.CANCEL:
            s = self._served.pop(rid, None)
            if s:
                s.cancelled.set()
                s.body.put(None)
                s.res_window.abort(P.WanError("요청이 취소되었습니다"))
        elif kind == P.ACK:
            n = int.from_bytes(data[:4], "big")
            if flags & P.F_REQ_FLOW:
                c = self._calls.get(rid)
                if c:
                    c.req_window.release(n)
            else:
                s = self._served.get(rid)
                if s:
                    s.res_window.release(n)
        elif kind == P.RES:
            c = self._calls.get(rid)
            if c:
                head = P.loads(data) or {}
                c.status, c.headers = int(head.get("s", 502)), _Headers(head.get("h"))
                c.head.set()
        elif kind == P.RES_BODY:
            c = self._calls.get(rid)
            if c:
                c.body.put(data)
        elif kind == P.RES_END:
            c = self._calls.get(rid)
            if c:
                end = P.loads(data) or {}
                if not end.get("ok", True):
                    c.error = P.WanError(end.get("err") or "현장에서 오류가 났습니다")
                c.finished = True
                c.head.set()
                c.body.put(None)

    def _on_req(self, rid: int, data: bytes):
        if self.bridge is None:
            self._enqueue(P.encode_frames(P.RES, rid, P.dumps({"s": 501, "h": {}})), P.PRIO_NORMAL)
            self._enqueue(P.encode_frames(P.RES_END, rid, P.dumps({"ok": False, "err": "요청을 처리하지 않는 쪽입니다"})),
                          P.PRIO_NORMAL)
            return
        served = Served(self, rid, P.loads(data) or {})
        self._served[rid] = served
        self.bridge.submit(served)

    def _served_error(self, rid: int, msg: str):
        s = self._served.pop(rid, None)
        if s:
            s.cancelled.set()
            s.body.put(None)

    def _served_done(self, rid: int):
        self._served.pop(rid, None)

    # ── ACK / 취소 ──
    def _ack(self, rid: int, n: int, req_flow: bool):
        try:
            self.post(P.ACK, rid, n.to_bytes(4, "big"), P.PRIO_CONTROL, P.F_REQ_FLOW if req_flow else 0)
        except P.WanError:
            pass

    def cancel(self, call: _Call):
        try:
            self.post(P.CANCEL, call.rid, b"", P.PRIO_CONTROL)
        except P.WanError:
            pass

    def _forget(self, rid: int):
        with self._lock:
            self._calls.pop(rid, None)

    # ── 요청 (사용자 쪽, 아무 스레드) ──
    def request(self, method: str, route: str, params: dict | None = None, headers: dict | None = None,
                data=None, stream: bool = False, timeout=(3, 30)) -> WanResponse:
        connect_t, read_t = _split_timeout(timeout)
        if not self.alive:
            raise P.WanError(self.dead_reason or "통로가 끊어졌습니다")
        rid = next(self._rids)
        call = _Call(rid, read_t)
        with self._lock:
            self._calls[rid] = call
        prio = P.priority_for(route)     # 이 요청의 머리·본문·끝은 모두 같은 우선순위 줄
        length, source = _body_source(data)
        head = {"m": method.upper(), "r": route, "h": dict(headers or {}), "l": length,
                "p": {str(k): str(v) for k, v in (params or {}).items()}}
        try:
            self.post(P.REQ, rid, P.dumps(head), prio)
            self._send_body(call, source, length, prio, read_t)
            self.post(P.REQ_END, rid, b"", prio)
            if not call.head.wait(connect_t + read_t):
                raise P.WanTimeout("현장이 응답하지 않습니다")
            if call.error is not None and not call.status:
                raise call.error
        except BaseException:
            self.cancel(call)
            self._forget(rid)
            raise
        resp = WanResponse(self, call)
        if not stream:
            try:
                resp.content   # 본문을 모두 받아 둠
            except BaseException:
                self._forget(rid)
                raise
        return resp

    def _send_body(self, call: _Call, source, length: int, prio: int, read_t: float):
        sent = 0
        while sent < length:
            chunk = source.read(min(P.MAX_DATA, length - sent))
            if not chunk:
                raise P.WanError("보내던 본문이 중간에 끊겼습니다")
            call.req_window.acquire(len(chunk), read_t)
            self.post(P.REQ_BODY, call.rid, chunk, prio)
            sent += len(chunk)

    # ── 끝내기 ──
    def _link_closed(self):
        self.close("연결이 닫혔습니다")

    def close(self, reason: str = "닫음"):
        try:
            self.loop.call_soon_threadsafe(self._die, reason)
        except RuntimeError:
            pass

    def _die(self, reason: str):
        if not self.alive:
            return
        self.alive = False
        self.dead_reason = reason
        err = P.WanError(reason)
        for t in self._tasks:
            if t is not asyncio.current_task():     # 자기 자신(_writer 가 부른 경우)은 취소하지 않음
                t.cancel()
        with self._lock:
            calls = list(self._calls.values())
            self._calls.clear()
        for c in calls:
            c.fail(err)
        for s in list(self._served.values()):
            s.cancelled.set()
            s.body.put(None)
            s.res_window.abort(err)
        self._served.clear()
        try:
            self.link.close()
        except Exception:
            pass
        if self.on_dead:
            try:
                self.on_dead(reason)
            except Exception:
                pass


class _BytesSource:
    def __init__(self, data):
        self._mv, self._pos = memoryview(data), 0

    def read(self, n):
        out = bytes(self._mv[self._pos:self._pos + n])
        self._pos += len(out)
        return out


class _IterSource:
    def __init__(self, it):
        self._it, self._buf = iter(it), b""

    def read(self, n):
        while len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


def _body_source(data) -> tuple:
    """요청 본문 → (길이, read(n) 을 가진 객체)"""
    if data is None or data == b"":
        return 0, None
    if isinstance(data, (bytes, bytearray, memoryview)):
        return len(data), _BytesSource(data)
    try:
        length = len(data)
    except TypeError:
        raise P.WanError("본문 길이를 알 수 없는 요청은 보낼 수 없습니다") from None
    return length, (data if hasattr(data, "read") else _IterSource(data))
