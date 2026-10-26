"""안내 서버 운영 화면이 쓰는 네트워크 도우미 — 이 PC 의 내부 주소 찾기, Windows 방화벽 포트 열기"""

import os
import re
import secrets
import socket
import struct
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


STUN_MAGIC = 0x2112A442


def stun_server(url: str) -> tuple:
    """'stun:host:port' → (host, port)"""
    t = (url or "").split(":", 1)[-1] if (url or "").startswith(("stun:", "stuns:")) else (url or "")
    host, _, port = t.partition(":")
    return host, int(port or 3478)


def public_address(stun: str, timeout: float = 2.0) -> str | None:
    """인터넷에서 보이는 이 망의 공인 IP (STUN 에 한 번 물어봄).  못 알아내면 None.
    STUN 은 UDP 한 번 주고받는 것이 전부이며 화면 · 코드 같은 정보는 보내지 않는다"""
    if not stun:
        return None
    try:
        host, port = stun_server(stun)
        tid = secrets.token_bytes(12)
        req = struct.pack(">HHI", 0x0001, 0, STUN_MAGIC) + tid
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(req, (host, port))
            data, _ = s.recvfrom(2048)
        return _parse_stun(data, tid)
    except (OSError, ValueError):
        return None


def _parse_stun(data: bytes, tid: bytes) -> str | None:
    if len(data) < 20 or data[8:20] != tid or struct.unpack(">H", data[:2])[0] != 0x0101:
        return None
    pos, end = 20, 20 + struct.unpack(">H", data[2:4])[0]
    while pos + 4 <= min(end, len(data)):
        atype, alen = struct.unpack(">HH", data[pos:pos + 4])
        body = data[pos + 4:pos + 4 + alen]
        if atype in (0x0020, 0x0001) and len(body) >= 8 and body[1] == 1:       # XOR-MAPPED / MAPPED, IPv4
            raw = struct.unpack(">I", body[4:8])[0]
            return socket.inet_ntoa(struct.pack(">I", raw ^ STUN_MAGIC if atype == 0x0020 else raw))
        pos += 4 + alen + (-alen % 4)
    return None


def is_public_ip(text) -> bool:
    """인터넷(공인) 주소인지.  사설망 · 루프백 · 링크 로컬 · 공유 주소(100.64/10) 이거나 알 수 없으면 False"""
    import ipaddress
    try:
        return ipaddress.ip_address(str(text).split("%")[0]).is_global
    except ValueError:
        return False


def firewall_command(port: int) -> str:
    return (f'netsh advfirewall firewall add rule name="{FIREWALL_RULE} {port}" dir=in action=allow '
            f"protocol=TCP localport={port}")


def firewall_commands(port: int) -> list:
    """안내 서버 TCP 포트 + 같은 망 자동 찾기 UDP 포트"""
    from .config import DISCOVER_PORT
    return [firewall_command(port),
            f'netsh advfirewall firewall add rule name="{FIREWALL_RULE} find" dir=in action=allow '
            f"protocol=UDP localport={DISCOVER_PORT}"]


def direct_firewall_command(port: int) -> str:
    """현장 PC 가 주소 접속을 받는 TCP 포트"""
    return (f'netsh advfirewall firewall add rule name="Aurora WAN Direct {port}" dir=in action=allow '
            f"protocol=TCP localport={port}")


def delete_rule_command(add_cmd: str) -> str:
    """add rule 명령에서 같은 이름의 규칙을 지우는 명령을 만든다"""
    name = re.search(r'name="([^"]+)"', add_cmd).group(1)
    return f'netsh advfirewall firewall delete rule name="{name}"'


def _run(cmd: str):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def open_firewall(port: int, direct: bool = False) -> tuple:
    """Windows 방화벽에서 TCP 포트를 연다 → (성공 여부, 안내 글자).  관리자 권한이 없으면 실패하고 직접 할 명령을 알려 준다.
    direct=True 면 현장 PC 의 주소 접속 포트 하나만 연다 (안내 서버용 포트 · 자동 찾기는 건드리지 않음)"""
    if os.name != "nt":
        return False, "Windows 에서만 지원합니다"
    cmds = [direct_firewall_command(port)] if direct else firewall_commands(port)
    for cmd in cmds:
        try:
            _run(delete_rule_command(cmd))                # 같은 이름의 옛 규칙이 있으면 지우고 다시 만든다 (눌러 볼 때마다 규칙이 쌓이지 않게)
            r = _run(cmd)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"실행하지 못했습니다: {e}"
        if r.returncode != 0:
            return False, ("방화벽을 바꾸지 못했습니다 (관리자 권한이 필요합니다).\n"
                           "관리자 권한 명령 프롬프트에서 아래 명령을 실행하세요:\n\n" + "\n".join(cmds))
    return True, (f"방화벽에서 TCP {port} 를 열었습니다" if direct
                  else f"방화벽에서 TCP {port} 와 자동 찾기(UDP)를 열었습니다")


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
