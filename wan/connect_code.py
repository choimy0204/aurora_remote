"""
접속 정보 — 중계 PC 가 만들고 다른 PC 가 붙여넣는 한 줄 글자
=============================================================
안내 서버 주소(들)와 공용 키를 한 줄로 묶어, 사람이 주소·키를 따로 옮겨 적지 않게 한다.
형식:  AW1-<base64url(JSON)>      JSON = {"u": [주소, ...], "k": 공용키}
주소가 여러 개(외부 주소 · 내부 주소)면 받는 PC 가 실제로 닿는 것을 골라 쓴다.
"""

import base64
import json
import secrets
import socket
from urllib.parse import urlsplit

from .config import DIRECT_PORT

PREFIX = "AW1-"
PREFIX_DIRECT = "AD1-"    # 현장 PC 하나에 주소로 바로 접속하는 정보 — JSON = {"u": [주소, ...], "k": 접근 코드}
KEY_BYTES = 12            # 자동으로 만드는 공용 키 길이 (글자 약 16자)


def new_key() -> str:
    return secrets.token_urlsafe(KEY_BYTES)


def make_url(host: str, port: int) -> str:
    """'공인IP' · '공인IP:포트' · 'ws://...' 어느 모양으로 적어도 ws://호스트:포트 로 만든다"""
    host = (host or "").strip()
    if host.lower().startswith(("ws://", "wss://")):
        return host
    return f"ws://{host}" if ":" in host else f"ws://{host}:{port}"


def parse_address(text: str, default_port: int = DIRECT_PORT) -> str:
    """'192.168.0.5' · '192.168.0.5:9102' · 'ws://host:9102' 어느 모양이든 '주소:포트' 로 정리. 잘못되면 ValueError"""
    t = (text or "").strip()
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.rstrip("/")
    try:
        p = urlsplit("//" + t)
        host, port = p.hostname, p.port
    except ValueError:
        raise ValueError("주소 형식이 올바르지 않습니다 (예: 192.168.0.5 또는 192.168.0.5:9102)") from None
    if not host or any(c.isspace() for c in t):
        raise ValueError("현장 PC 의 주소를 입력하세요 (예: 192.168.0.5 또는 192.168.0.5:9102)")
    if ":" in host:
        raise ValueError("IPv6 주소는 지원하지 않습니다 — IPv4 주소나 도메인을 입력하세요")
    return f"{host}:{port or default_port}"


def encode(urls, key: str, prefix: str = PREFIX) -> str:
    raw = json.dumps({"u": list(urls), "k": key or ""}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return prefix + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def encode_direct(urls, code: str) -> str:
    return encode(urls, code, PREFIX_DIRECT)


def kind_of(text: str) -> str:
    """붙여넣은 글자의 종류 → relay(중계 PC) · direct(현장 PC 주소) · ''(알 수 없음)"""
    t = "".join((text or "").split())
    if PREFIX in t:
        return "relay"
    if PREFIX_DIRECT in t:
        return "direct"
    return ""


def decode_direct(text: str) -> tuple:
    """→ (주소 목록, 접근 코드)"""
    return decode(text, PREFIX_DIRECT)


def decode(text: str, prefix: str = PREFIX) -> tuple:
    """→ (주소 목록, 공용 키 또는 접근 코드).  형식이 맞지 않으면 ValueError (화면에 보여 줄 글자)"""
    t = "".join((text or "").split())                    # 줄바꿈·공백이 끼어 붙여넣어져도 허용
    if prefix in t:
        t = t[t.index(prefix):]
    if not t.startswith(prefix):
        raise ValueError("접속 정보가 아닙니다 — 복사해 둔 접속 정보를 그대로 붙여넣으세요")
    body = t[len(prefix):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
        urls = [u for u in data["u"] if isinstance(u, str) and u.lower().startswith(("ws://", "wss://"))]
        key = data.get("k", "")
        if not urls or not isinstance(key, str):
            raise ValueError
    except Exception:
        raise ValueError("접속 정보가 깨져 있습니다 — 처음부터 다시 복사해 보세요") from None
    return urls, key


def pick_reachable(urls, timeout: float = 2.0) -> str:
    """주소들 중 TCP 로 닿는 첫 번째를 돌려줌 (하나도 안 닿으면 첫 번째 — 연결 상태 글자에 이유가 나온다)"""
    for u in urls:
        p = urlsplit(u)
        port = p.port or (443 if p.scheme == "wss" else 80)
        try:
            with socket.create_connection((p.hostname, port), timeout=timeout):
                return u
        except (OSError, ValueError):
            continue
    return urls[0]


def apply_relay_code(cfg, text: str) -> str:
    """중계 PC 의 접속 정보를 설정에 적용 → 고른 안내 서버 주소 (닿는 주소를 찾느라 몇 초 걸릴 수 있음)"""
    urls, key = decode(text)
    url = pick_reachable(urls)
    cfg.update(relay_url=url, relay_key=key)
    return url


def remember_direct(cfg, text: str, name: str = "") -> str:
    """현장 PC 주소 접속 정보를 '접속했던 현장' 목록에 저장 → '주소:포트'"""
    urls, code = decode_direct(text)
    address = urlsplit(pick_reachable(urls)).netloc
    targets = dict(cfg.get("direct_targets") or {})
    old = targets.get(address, {})
    targets[address] = {"name": name or old.get("name", ""), "code": code, "last": old.get("last", 0)}
    cfg.update(direct_targets=targets)
    return address


def reachable(address: str, timeout: float = 0.8) -> bool:
    """'주소:포트' 에 TCP 로 닿는지 (현장 목록에 켜짐/꺼짐을 보여 주려고)"""
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False
