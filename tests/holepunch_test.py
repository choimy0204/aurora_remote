"""
WAN 직접 연결(홀 펀칭) 시험 — 개발 지시서 0단계
=============================================
안내 서버 없이 두 PC 가 aiortc 데이터 채널로 직접 연결되는지 확인한다.
연결 정보(SDP)는 화면에 출력되는 "코드" 한 줄을 서로 복사·붙여넣기 해서 주고받는다.
이 파일은 기존 Aurora 코드와 무관한 독립 시험용이다 (import 하지 않음).

사용법 (두 PC 에서 한 번씩)
    pip install aiortc
    A PC :  python holepunch_test.py offer      → 코드 출력 → B 에 전달 → B 의 답 코드를 붙여넣기
    B PC :  python holepunch_test.py answer     → A 의 코드를 붙여넣기 → 답 코드 출력 → A 에 전달
    한 PC 안에서 동작만 점검:  python holepunch_test.py loopback

옵션
    --stun URL    STUN 서버 (기본 stun:stun.l.google.com:19302)
    --no-stun     STUN 없이 (같은 망 시험용)
    --seconds N   전송 속도 측정 시간 (기본 10초)

결과로 나오는 것: 성공 여부 · 쓰인 후보 종류(host/srflx/relay) · 왕복 시간 · 양방향 전송 속도
"""

import sys
import json
import zlib
import time
import base64
import asyncio
import argparse
import statistics

from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription

CHANNEL      = "test"
CHUNK        = 16 * 1024          # SCTP 메시지 크기 제한을 피하려고 16KB 단위로 보냄
HIGH_WATER   = 1024 * 1024        # 보내는 쪽 버퍼가 이만큼 차면 잠깐 쉼
LOW_WATER    = 256 * 1024
PING_COUNT   = 20
OPEN_TIMEOUT = 30                 # 이 시간 안에 채널이 안 열리면 실패


# ─────────────────────────────────────────
# 연결 정보(SDP) ↔ 복사하기 좋은 한 줄 코드
# ─────────────────────────────────────────
def encode_code(desc: RTCSessionDescription) -> str:
    raw = json.dumps({"sdp": desc.sdp, "type": desc.type}).encode()
    return base64.b64encode(zlib.compress(raw, 9)).decode()


def decode_code(code: str) -> RTCSessionDescription:
    d = json.loads(zlib.decompress(base64.b64decode(code.strip())))
    return RTCSessionDescription(sdp=d["sdp"], type=d["type"])


async def ask(prompt: str) -> str:
    """붙여넣기 입력 (입력 대기 중에도 이벤트 루프가 멈추지 않게 스레드에서 받음)"""
    return await asyncio.get_running_loop().run_in_executor(None, input, prompt)


def make_pc(use_stun: bool, stun_url: str) -> RTCPeerConnection:
    servers = [RTCIceServer(urls=stun_url)] if use_stun else []
    return RTCPeerConnection(RTCConfiguration(iceServers=servers))


# ─────────────────────────────────────────
# 실제로 쓰인 후보 쌍 (host / srflx / relay)
# ─────────────────────────────────────────
def selected_pair(pc: RTCPeerConnection) -> dict:
    """연결에 선택된 로컬·원격 후보 정보. aiortc 내부 구조를 쓰므로 실패하면 빈 값"""
    try:
        ice = pc.sctp.transport.transport          # SCTP → DTLS → ICE
        pair = next(iter(ice._connection._nominated.values()))
        loc, rem = pair.local_candidate, pair.remote_candidate
        return {"local": f"{loc.type} {loc.host}:{loc.port}",
                "remote": f"{rem.type} {rem.host}:{rem.port}",
                "local_type": loc.type, "remote_type": rem.type}
    except Exception as e:
        return {"error": repr(e)}


# ─────────────────────────────────────────
# 시험 본체 — 양쪽이 같은 코드를 쓰고, offer 쪽이 순서를 진행한다
# ─────────────────────────────────────────
class Tester:
    def __init__(self, name: str, channel, seconds: float):
        self.name, self.ch, self.seconds = name, channel, seconds
        self.result: dict = {}
        self._pong = asyncio.Queue()
        self._remote_report = asyncio.Queue()    # 상대가 알려 준 "내가 보낸 것 중 받은 양"
        self._rx_bytes, self._rx_first = 0, 0.0
        self.last_rx = (0, 1.0)                  # 마지막으로 직접 받아 본 (바이트, 초)
        self._rx_end = asyncio.Event()           # 상대의 보내기가 끝났음
        self._done = asyncio.Event()
        self._low = asyncio.Event()
        self._low.set()
        channel.bufferedAmountLowThreshold = LOW_WATER
        channel.on("bufferedamountlow", self._low.set)
        channel.on("message", self._on_message)

    # ── 받기 ──
    def _on_message(self, msg):
        if isinstance(msg, bytes):                       # 속도 측정용 덩어리
            if not self._rx_first:
                self._rx_first = time.perf_counter()
            self._rx_bytes += len(msg)
            return
        cmd, _, arg = msg.partition(":")
        if cmd == "ping":
            self.ch.send(f"pong:{arg}")
        elif cmd == "pong":
            self._pong.put_nowait(arg)
        elif cmd == "bulk_end":                          # 상대가 보내기를 마침 → 받은 양을 기록하고 알려 줌
            secs = max(time.perf_counter() - self._rx_first, 1e-6)
            self.last_rx = (self._rx_bytes, secs)
            self.ch.send(f"bulk_report:{self._rx_bytes}:{secs:.3f}")
            self._rx_bytes, self._rx_first = 0, 0.0
            self._rx_end.set()
        elif cmd == "bulk_report":
            self._remote_report.put_nowait(arg)
        elif cmd == "send_back":                         # 반대 방향 측정 요청
            asyncio.ensure_future(self._bulk_send(float(arg)))
        elif cmd == "finish":
            self._done.set()

    # ── 보내기 (버퍼를 보며 속도 조절 — 입력 같은 작은 메시지가 막히지 않게) ──
    async def _bulk_send(self, seconds: float) -> tuple:
        block, sent, t0 = bytes(CHUNK), 0, time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            if self.ch.bufferedAmount > HIGH_WATER:
                self._low.clear()
                await self._low.wait()
            self.ch.send(block)
            sent += CHUNK
            await asyncio.sleep(0)
        # 버퍼에 남은 것까지 다 나가야 "끝" 신호를 보냄 (끝 신호가 데이터를 앞지르지 않게 순서 채널 사용)
        self.ch.send("bulk_end:")
        return sent, time.perf_counter() - t0

    # ── 진행 (offer 쪽) ──
    async def drive(self):
        rtts = []
        for i in range(PING_COUNT):
            t0 = time.perf_counter()
            self.ch.send(f"ping:{i}")
            await asyncio.wait_for(self._pong.get(), 5)
            rtts.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0.05)
        self.result["rtt_ms"] = {"min": round(min(rtts), 1), "median": round(statistics.median(rtts), 1),
                                 "max": round(max(rtts), 1)}

        # A → B : 내가 보내고, 상대가 받은 양을 보고받음
        print(f"[{self.name}] 전송 속도 측정 중 ({self.seconds:.0f}초, 보내기)...")
        await self._bulk_send(self.seconds)
        rx, secs = (float(x) for x in (await asyncio.wait_for(self._remote_report.get(), 60)).split(":"))
        self.result["send_mbps"] = round(rx * 8 / secs / 1e6, 2)

        # B → A : 상대에게 보내라고 하고, 내가 받은 양을 잼
        print(f"[{self.name}] 전송 속도 측정 중 ({self.seconds:.0f}초, 받기)...")
        self._rx_end.clear()
        self.ch.send(f"send_back:{self.seconds}")
        await asyncio.wait_for(self._rx_end.wait(), self.seconds + 60)
        rx, secs = self.last_rx
        self.result["recv_mbps"] = round(rx * 8 / secs / 1e6, 2)
        self.ch.send("finish:")
        # 끝 신호가 상대에게 나간 뒤에 연결을 닫도록 버퍼가 빌 때까지 기다림
        while self.ch.bufferedAmount:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)


async def run_pair_session(pc: RTCPeerConnection, ch, name: str, seconds: float, driver: bool) -> dict:
    t = Tester(name, ch, seconds)
    t.result["pair"] = selected_pair(pc)      # 연결 직후에 읽음 (상대가 먼저 닫으면 정보가 사라짐)
    if driver:
        await t.drive()
    else:
        await asyncio.wait_for(t._done.wait(), seconds * 4 + 120)
    return t.result


async def wait_open(ch, timeout: float):
    if ch.readyState == "open":
        return
    opened = asyncio.Event()
    ch.on("open", opened.set)
    await asyncio.wait_for(opened.wait(), timeout)


def print_report(role: str, res: dict):
    pair = res.get("pair", {})
    print("\n" + "=" * 50)
    print(f"결과 ({role})")
    print("=" * 50)
    print("연결 성공: 예")
    print(f"로컬 후보 : {pair.get('local', pair.get('error', '?'))}")
    print(f"상대 후보 : {pair.get('remote', '?')}")
    kinds = {pair.get("local_type"), pair.get("remote_type")}
    if "relay" in kinds:
        how = "relay (TURN 중계)"
    elif "srflx" in kinds or "prflx" in kinds:
        how = "srflx/prflx (홀 펀칭으로 직접 연결)"
    else:
        how = "host (같은 망 직접 연결 — 홀 펀칭 시험으로는 부족, 다른 망에서 다시 시험)"
    print(f"연결 방식 : {how}")
    if "rtt_ms" in res:
        r = res["rtt_ms"]
        print(f"왕복 시간 : 최소 {r['min']} / 중앙 {r['median']} / 최대 {r['max']} ms")
        print(f"전송 속도 : 보내기 {res['send_mbps']} Mbps / 받기 {res['recv_mbps']} Mbps")
    print("-" * 50)
    print("결과 한 줄 (복사해서 전달):")
    print(json.dumps(res, ensure_ascii=False))


# ─────────────────────────────────────────
# 역할별 진행
# ─────────────────────────────────────────
async def role_offer(args) -> dict:
    pc = make_pc(not args.no_stun, args.stun)
    ch = pc.createDataChannel(CHANNEL)
    await pc.setLocalDescription(await pc.createOffer())     # 후보 수집이 끝난 뒤 돌아옴
    print("\n[1] 아래 코드 한 줄을 B PC 에 전달하세요 (B 에서 answer 실행):\n")
    print(encode_code(pc.localDescription))
    code = await ask("\n[2] B PC 가 출력한 답 코드를 붙여넣고 Enter: ")
    await pc.setRemoteDescription(decode_code(code))
    print("연결 중...")
    await wait_open(ch, OPEN_TIMEOUT)
    print("연결됨!")
    res = await run_pair_session(pc, ch, "offer", args.seconds, driver=True)
    await pc.close()
    return res


async def role_answer(args) -> dict:
    pc = make_pc(not args.no_stun, args.stun)
    got = asyncio.get_running_loop().create_future()
    pc.on("datachannel", lambda c: got.set_result(c) if not got.done() else None)
    code = await ask("\n[1] A PC 가 출력한 코드를 붙여넣고 Enter: ")
    await pc.setRemoteDescription(decode_code(code))
    await pc.setLocalDescription(await pc.createAnswer())
    print("\n[2] 아래 답 코드 한 줄을 A PC 에 전달하세요:\n")
    print(encode_code(pc.localDescription))
    print("\n연결 대기 중...")
    ch = await asyncio.wait_for(got, OPEN_TIMEOUT * 4)
    await wait_open(ch, OPEN_TIMEOUT)
    print("연결됨!")
    res = await run_pair_session(pc, ch, "answer", args.seconds, driver=False)
    await asyncio.sleep(0.5)
    await pc.close()
    return res


async def role_loopback(args) -> dict:
    """한 프로세스 안에서 두 연결을 맺어 시험 코드 자체가 동작하는지 점검 (STUN 없음)"""
    a, b = make_pc(False, ""), make_pc(False, "")
    got = asyncio.get_running_loop().create_future()
    b.on("datachannel", lambda c: got.set_result(c) if not got.done() else None)
    ch_a = a.createDataChannel(CHANNEL)
    await a.setLocalDescription(await a.createOffer())
    await b.setRemoteDescription(decode_code(encode_code(a.localDescription)))
    await b.setLocalDescription(await b.createAnswer())
    await a.setRemoteDescription(decode_code(encode_code(b.localDescription)))
    ch_b = await asyncio.wait_for(got, OPEN_TIMEOUT)
    await asyncio.gather(wait_open(ch_a, OPEN_TIMEOUT), wait_open(ch_b, OPEN_TIMEOUT))
    ra, _ = await asyncio.gather(run_pair_session(a, ch_a, "offer", args.seconds, True),
                                 run_pair_session(b, ch_b, "answer", args.seconds, False))
    await asyncio.gather(a.close(), b.close())
    return ra


def main():
    ap = argparse.ArgumentParser(description="WAN 직접 연결(홀 펀칭) 시험")
    ap.add_argument("role", choices=["offer", "answer", "loopback"])
    ap.add_argument("--stun", default="stun:stun.l.google.com:19302")
    ap.add_argument("--no-stun", action="store_true")
    ap.add_argument("--seconds", type=float, default=10)
    args = ap.parse_args()
    role = {"offer": role_offer, "answer": role_answer, "loopback": role_loopback}[args.role]
    try:
        res = asyncio.run(role(args))
    except asyncio.TimeoutError:
        print("\n실패: 시간 안에 끝나지 않았습니다 (연결이 안 맺어졌거나, 측정 도중 끊겼거나, 코드 전달 오류)")
        sys.exit(1)
    print_report(args.role, res)


if __name__ == "__main__":
    main()
