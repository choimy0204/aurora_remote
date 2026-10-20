"""
같은 망에서 중계 PC 자동 찾기 (UDP 방송)
========================================
중계 PC 는 UDP DISCOVER_PORT 로 "찾는다" 신호를 기다리다가 접속 정보(AW1-...)를 답한다.
다른 PC 는 같은 망에 방송으로 물어 첫 답을 받는다 → 접속 정보를 복사해 붙여넣지 않아도 된다.
※ 공용 키가 같은 망의 모든 PC 에 전달된다 (공장 안 망처럼 믿을 수 있는 망에서만 쓰는 편의 기능).
"""

import socket
import time

from .config import DISCOVER_PORT
from .netutil import lan_addresses

MAGIC = b"AURORA-WAN-FIND"


def _targets(port: int) -> list:
    out = [("255.255.255.255", port)]
    for ip in lan_addresses():
        out.append((".".join(ip.split(".")[:3] + ["255"]), port))        # /24 가정의 방송 주소
    return out


def find_relay(timeout: float = 2.0, port: int = DISCOVER_PORT, targets=None) -> str | None:
    """같은 망의 중계 PC 가 보낸 접속 정보 글자를 돌려줌 (없으면 None)"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.3)
        for t in targets or _targets(port):
            try:
                s.sendto(MAGIC, t)
            except OSError:
                pass
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.startswith(b"AW1-"):
                return data.decode("ascii", "ignore")
    finally:
        s.close()
    return None


def serve(code_fn, stop, port: int = DISCOVER_PORT, log=lambda m: None):
    """중계 PC 쪽: 찾는 신호에 접속 정보로 답한다.  stop 은 threading.Event"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
    except OSError as e:
        log(f"자동 찾기 포트 {port} 를 열지 못했습니다 (붙여넣기는 그대로 됩니다): {e}")
        s.close()
        return
    s.settimeout(1.0)
    try:
        while not stop.is_set():
            try:
                data, addr = s.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data == MAGIC:
                try:
                    s.sendto(code_fn().encode("ascii"), addr)
                except Exception as e:
                    log(f"자동 찾기 응답 실패: {e}")
    finally:
        s.close()
