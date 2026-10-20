"""
메시지 형식 — 요청/응답을 16KB 이하 조각(frame)으로 나눠 통로에 싣는다
=================================================================
frame = [종류 1바이트][플래그 1바이트][요청 번호 4바이트] + 데이터        (최대 16KB)

  REQ        요청 머리 (JSON: 방식·주소·매개변수·헤더·본문 길이)
  REQ_BODY   요청 본문 조각          REQ_END   요청 본문 끝
  RES        응답 머리 (JSON: 상태·헤더)
  RES_BODY   응답 본문 조각          RES_END   응답 끝 (JSON: 정상 여부·오류)
  ACK        본문을 읽었다는 알림 (보내는 쪽이 너무 앞서가지 않게 — 흐름 제어)
  CANCEL     요청 취소           PING/PONG   살아 있는지 확인

JSON 머리가 한 조각보다 크면(클립보드 붙여넣기 등) 여러 조각으로 나뉘고 마지막 조각에만 LAST 가 붙는다.
SCTP(데이터 채널)는 메시지 크기 제한이 있어서 조각을 16KB 이하로 유지한다.
"""

import json
import struct

MAX_FRAME = 16 * 1024
HEADER    = struct.Struct(">BBI")
MAX_DATA  = MAX_FRAME - HEADER.size

(REQ, REQ_BODY, REQ_END, RES, RES_BODY, RES_END, ACK, CANCEL, PING, PONG) = range(1, 11)

F_LAST     = 1    # 메시지의 마지막 조각
F_REQ_FLOW = 2    # ACK 가 "요청 본문" 흐름에 대한 것 (없으면 응답 본문)

# 우선순위 — 숫자가 작을수록 먼저 나감. 입력이 큰 파일 전송 뒤에 줄 서지 않게 한다
PRIO_CONTROL, PRIO_NORMAL, PRIO_BULK = 0, 1, 2

CONTROL_ROUTES = ("/remote/input", "/remote/clip", "/remote/end", "/info")
BULK_ROUTES    = ("/fs/file", "/fs/upload")


def priority_for(route: str) -> int:
    if route in CONTROL_ROUTES:
        return PRIO_CONTROL
    if route in BULK_ROUTES:
        return PRIO_BULK
    return PRIO_NORMAL


class WanError(OSError):
    """통로 오류 (연결 끊김 등) — requests 의 ConnectionError 처럼 일반 Exception 으로 잡힌다"""


class WanTimeout(WanError, TimeoutError):
    pass


class WanDenied(WanError):
    """접속 코드가 맞지 않거나 현장이 거부함"""


def encode_frames(kind: int, rid: int, payload: bytes = b"", flags: int = 0) -> list:
    pieces = [payload[i:i + MAX_DATA] for i in range(0, len(payload), MAX_DATA)] or [b""]
    last = len(pieces) - 1
    return [HEADER.pack(kind, flags | (F_LAST if n == last else 0), rid) + p
            for n, p in enumerate(pieces)]


def decode_frame(frame: bytes) -> tuple:
    """→ (종류, 플래그, 요청 번호, 데이터)"""
    if len(frame) < HEADER.size or len(frame) > MAX_FRAME:
        raise ValueError(f"잘못된 frame 크기: {len(frame)}")
    kind, flags, rid = HEADER.unpack_from(frame)
    return kind, flags, rid, frame[HEADER.size:]


class Reassembler:
    """조각을 모아 완성된 메시지로 돌려줌. 서로 다른 요청의 조각은 섞여 들어와도 된다"""

    def __init__(self):
        self._parts: dict = {}

    def feed(self, frame: bytes):
        """→ 완성된 (종류, 플래그, 요청 번호, 데이터) 또는 None"""
        kind, flags, rid, data = decode_frame(frame)
        key = (kind, rid, flags & F_REQ_FLOW)
        if not (flags & F_LAST):
            self._parts.setdefault(key, bytearray()).extend(data)
            return None
        head = self._parts.pop(key, None)
        if head is not None:
            head.extend(data)
            data = bytes(head)
        return kind, flags, rid, data


def dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def loads(data: bytes):
    return json.loads(data.decode("utf-8")) if data else None
