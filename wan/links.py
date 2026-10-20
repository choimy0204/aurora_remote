"""
통로(Link) — frame 을 실어 나르는 길. 두 종류가 같은 모양이라 위쪽(mux)은 어느 길인지 모른다.

  DirectLink   직접 연결  (aiortc 데이터 채널, 홀 펀칭)
  RelayLink    비상 중계  (안내 서버 websocket 을 거쳐 전달)
"""

import asyncio

from . import protocol as P


class Link:
    kind = "?"
    LOW_WATER = 32 * 1024     # 보내는 버퍼가 이만큼 줄면 "비었다" 알림

    def __init__(self):
        self.closed = False
        self._receiver = self._on_close = self._on_drain = None

    def set_handlers(self, receiver, on_close, on_drain):
        self._receiver, self._on_close, self._on_drain = receiver, on_close, on_drain

    async def send(self, frame: bytes):
        raise NotImplementedError

    @property
    def buffered(self) -> int:
        """아직 상대에게 나가지 못하고 쌓여 있는 바이트"""
        return 0

    def close(self):
        self._mark_closed()

    # 하위 클래스가 부르는 쪽
    def deliver(self, frame: bytes):
        if self._receiver and not self.closed:
            self._receiver(frame)

    def drained(self):
        if self._on_drain:
            self._on_drain()

    def _mark_closed(self):
        if self.closed:
            return
        self.closed = True
        if self._on_close:
            self._on_close()


class DirectLink(Link):
    kind = "direct"

    def __init__(self, channel, pc=None):
        super().__init__()
        self.channel, self.pc = channel, pc
        channel.bufferedAmountLowThreshold = self.LOW_WATER
        channel.on("message", self._on_message)
        channel.on("close", self._mark_closed)
        channel.on("bufferedamountlow", self.drained)

    def _on_message(self, msg):
        if isinstance(msg, (bytes, bytearray)):
            self.deliver(bytes(msg))

    async def send(self, frame: bytes):
        if self.closed or self.channel.readyState != "open":
            raise P.WanError("직접 연결이 닫혔습니다")
        self.channel.send(frame)

    @property
    def buffered(self) -> int:
        return self.channel.bufferedAmount

    def close(self):
        try:
            self.channel.close()
        except Exception:
            pass
        if self.pc is not None:
            asyncio.ensure_future(self.pc.close())
        self._mark_closed()


class RelayLink(Link):
    """안내 서버를 거치는 길. 한 websocket 에 여러 세션이 섞이므로 앞에 8바이트 세션 표시를 붙인다"""
    kind = "relay"

    def __init__(self, ws, sid: bytes, on_bye=None):
        super().__init__()
        self.ws, self.sid, self._on_bye = ws, sid, on_bye

    async def send(self, frame: bytes):
        if self.closed:
            raise P.WanError("중계 연결이 닫혔습니다")
        try:
            await self.ws.send(self.sid + frame)
        except Exception as e:
            raise P.WanError(f"중계 전송 실패: {e}") from e

    @property
    def buffered(self) -> int:
        try:
            return self.ws.transport.get_write_buffer_size()
        except Exception:
            return 0

    def close(self):
        if not self.closed and self._on_bye:
            try:
                self._on_bye(self.sid)
            except Exception:
                pass
        self._mark_closed()


# ─────────────────────────────────────────
# aiortc 는 필요할 때만 불러온다 — 못 불러와도(설치 안 됨·DLL 문제) 비상 중계는 쓸 수 있게
# ─────────────────────────────────────────
def direct_available() -> bool:
    try:
        import aiortc  # noqa: F401
        return True
    except Exception:
        return False


def make_peer_connection(stun: str):
    from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer
    servers = [RTCIceServer(urls=stun)] if stun else []
    return RTCPeerConnection(RTCConfiguration(iceServers=servers))


def session_description(data: dict):
    from aiortc import RTCSessionDescription
    return RTCSessionDescription(sdp=data["sdp"], type=data["type"])


async def wait_open(channel, timeout: float):
    if channel.readyState == "open":
        return
    opened = asyncio.Event()
    channel.on("open", opened.set)
    try:
        await asyncio.wait_for(opened.wait(), timeout)
    except asyncio.TimeoutError:
        raise P.WanTimeout("직접 연결이 시간 안에 맺어지지 않았습니다") from None
