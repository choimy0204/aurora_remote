"""안내 서버 운영 화면이 쓰는 네트워크 도우미 — 이 PC 의 내부 주소 찾기, Windows 방화벽 포트 열기"""

import os
import socket
import subprocess

FIREWALL_RULE = "Aurora WAN Relay"


def lan_addresses() -> list:
    """이 PC 의 내부(사설) IPv4 주소들. 인터넷에는 아무것도 보내지 않는다 (UDP connect 는 패킷을 보내지 않음)"""
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in found:
        if ip and not ip.startswith(("127.", "169.254.", "0.")) and ip not in out:
            out.append(ip)
    return out


def firewall_command(port: int) -> str:
    return (f'netsh advfirewall firewall add rule name="{FIREWALL_RULE} {port}" dir=in action=allow '
            f"protocol=TCP localport={port}")


def open_firewall(port: int) -> tuple:
    """Windows 방화벽에서 TCP 포트를 연다 → (성공 여부, 안내 글자).  관리자 권한이 없으면 실패하고 직접 할 명령을 알려 준다"""
    if os.name != "nt":
        return False, "Windows 에서만 지원합니다"
    try:
        r = subprocess.run(firewall_command(port), capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"실행하지 못했습니다: {e}"
    if r.returncode == 0:
        return True, f"방화벽에서 TCP {port} 를 열었습니다"
    return False, ("방화벽을 바꾸지 못했습니다 (관리자 권한이 필요합니다).\n"
                   "관리자 권한 명령 프롬프트에서 아래 명령을 실행하세요:\n\n" + firewall_command(port))
