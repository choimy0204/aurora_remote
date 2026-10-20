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

PREFIX = "AW1-"
KEY_BYTES = 12            # 자동으로 만드는 공용 키 길이 (글자 약 16자)


def new_key() -> str:
    return secrets.token_urlsafe(KEY_BYTES)


def make_url(host: str, port: int) -> str:
    """'공인IP' · '공인IP:포트' · 'ws://...' 어느 모양으로 적어도 ws://호스트:포트 로 만든다"""
    host = (host or "").strip()
    if host.lower().startswith(("ws://", "wss://")):
        return host
    return f"ws://{host}" if ":" in host else f"ws://{host}:{port}"


def encode(urls, key: str) -> str:
    raw = json.dumps({"u": list(urls), "k": key or ""}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(text: str) -> tuple:
    """→ (주소 목록, 공용 키).  형식이 맞지 않으면 ValueError (화면에 보여 줄 글자)"""
    t = "".join((text or "").split())                    # 줄바꿈·공백이 끼어 붙여넣어져도 허용
    if PREFIX in t:
        t = t[t.index(PREFIX):]
    if not t.startswith(PREFIX):
        raise ValueError("접속 정보가 아닙니다 — 중계 PC 에서 '접속 정보 복사'로 복사한 글자를 붙여넣으세요")
    body = t[len(PREFIX):]
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
