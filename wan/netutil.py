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


def firewall_commands(port: int) -> list:
    """안내 서버 TCP 포트 + 같은 망 자동 찾기 UDP 포트"""
    from .config import DISCOVER_PORT
    return [firewall_command(port),
            f'netsh advfirewall firewall add rule name="{FIREWALL_RULE} find" dir=in action=allow '
            f"protocol=UDP localport={DISCOVER_PORT}"]


def open_firewall(port: int) -> tuple:
    """Windows 방화벽에서 TCP 포트를 연다 → (성공 여부, 안내 글자).  관리자 권한이 없으면 실패하고 직접 할 명령을 알려 준다"""
    if os.name != "nt":
        return False, "Windows 에서만 지원합니다"
    cmds = firewall_commands(port)
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"실행하지 못했습니다: {e}"
        if r.returncode != 0:
            return False, ("방화벽을 바꾸지 못했습니다 (관리자 권한이 필요합니다).\n"
                           "관리자 권한 명령 프롬프트에서 아래 명령을 실행하세요:\n\n" + "\n".join(cmds))
    return True, f"방화벽에서 TCP {port} 와 자동 찾기(UDP)를 열었습니다"


def is_admin() -> bool:
    """관리자 권한으로 실행 중인지. Windows 가 아니면 권한 올리기가 필요 없으니 True"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(exe: str, args: list) -> tuple:
    """같은 프로그램을 관리자 권한(UAC 확인 창)으로 새로 띄운다 → (성공 여부, 안내 글자).
    성공하면 부르는 쪽이 자기 자신을 끝내야 한다.  사용자가 UAC 창에서 '아니요'를 누르면 실패로 돌려준다"""
    if os.name != "nt":
        return False, "Windows 에서만 지원합니다"
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(args), None, 1)
    except Exception as e:
        return False, f"다시 시작하지 못했습니다: {e}"
    if rc > 32:
        return True, "관리자 권한으로 다시 시작합니다"
    return False, "관리자 권한 요청이 취소되었거나 실패했습니다 (UAC 창에서 '예'를 눌러야 합니다)"
