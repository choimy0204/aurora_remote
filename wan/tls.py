"""
암호화 (TLS) — 자체 서명 인증서 + 지문 고정
============================================
인터넷을 지나는 통신(키 · 접근 코드 · 화면 · 파일)이 그대로 보이지 않도록 websocket 을 wss:// 로 감싼다.
  - 인증서는 서버(중계 PC · 현장 PC)가 처음 한 번 스스로 만든다 (인증 기관 · 도메인 · 유료 인증서 필요 없음).
  - 상대가 진짜인지는 인증서의 SHA-256 **지문**으로 확인한다. 지문은 접속 정보(AW1-/AD1-)에 들어 있어 붙여넣기만 하면 고정되고,
    지문 없이 직접 주소를 입력했다면 처음 접속 때 지문을 기억해 두었다가 이후에는 달라지면 거부한다 (처음 한 번만 믿음).
"""

import datetime
import hashlib
import hmac
import os
import ssl
import tempfile
from contextlib import asynccontextmanager

from .protocol import WanDenied

CERT_YEARS = 20

PIN_MISMATCH_TEXT = ("상대 PC 의 인증서가 기억해 둔 것과 다릅니다 — 다른 PC 로 바뀌었거나 누가 중간에서 가로채는 중일 수 있습니다. "
                     "PC 를 새로 설치했다면 목록에서 지우고 다시 추가하세요")


class PinMismatch(WanDenied):
    """상대가 내민 인증서의 지문이 고정해 둔 값과 다름"""


# ── 인증서 만들기 ──
def _generate_pem(common_name: str) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365 * CERT_YEARS))
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM)
            + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))


def ensure_cert(directory: str, name: str = "relay") -> str:
    """<폴더>/<name>_cert.pem (인증서 + 개인 키 한 파일)을 돌려준다.  없으면 만든다.
    두 프로세스가 동시에 만들어도 먼저 놓인 파일 하나만 남는다"""
    path = os.path.join(directory, f"{name}_cert.pem")
    if os.path.isfile(path):
        return path
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f"{name}_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(_generate_pem(f"aurora-{name}"))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        try:
            os.link(tmp, path)            # 이미 있으면 FileExistsError — 먼저 만든 쪽이 이긴다
        except FileExistsError:
            pass
        except OSError:
            if not os.path.isfile(path):
                os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return path


def cert_fingerprint(pem_path: str) -> str:
    """PEM 파일 안의 첫 인증서의 SHA-256 지문 (소문자 16진수).  파일이 없거나 읽을 수 없으면 ''"""
    try:
        with open(pem_path, "r", encoding="ascii") as f:
            text = f.read()
        start = text.index("-----BEGIN CERTIFICATE-----")
        end = text.index("-----END CERTIFICATE-----", start) + len("-----END CERTIFICATE-----")
        return fingerprint_der(ssl.PEM_cert_to_DER_cert(text[start:end]))
    except (OSError, ValueError):
        return ""


def fingerprint_der(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


# ── 연결 ──
def server_context(pem_path: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(pem_path)
    return ctx


def client_context() -> ssl.SSLContext:
    """인증 기관 검사는 하지 않는다 (자체 서명) — 대신 open_ws 가 지문을 확인한다"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def peer_fingerprint(ws) -> str:
    try:
        der = ws.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
        return fingerprint_der(der) if der else ""
    except Exception:
        return ""


def same_fingerprint(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").lower().encode(), (b or "").lower().encode())


def remember_relay_pin(cfg, ws):
    """지문을 모르고 wss:// 로 처음 접속했다면 그 지문을 기억해 이후에는 같은 서버만 믿는다"""
    fp = getattr(ws, "peer_fp", "")
    if fp and not cfg.get("relay_fp"):
        cfg.update(relay_fp=fp)


async def connect_ws(url: str, pin: str = "", **kw):
    """websocket 연결 → ws.  wss:// 이면 TLS 로 감싸고, pin(지문)이 있으면 같은지 확인한다 (다르면 PinMismatch).
    연결된 ws 의 peer_fp 에 상대 지문이 들어 있다 (wss 가 아니면 ''). 닫는 것은 부른 쪽 몫"""
    from websockets.asyncio.client import connect
    secure = str(url).lower().startswith("wss://")
    ws = await connect(url, ssl=client_context() if secure else None, **kw)
    ws.peer_fp = peer_fingerprint(ws) if secure else ""
    if secure and pin and not same_fingerprint(ws.peer_fp, pin):
        await ws.close()
        raise PinMismatch(PIN_MISMATCH_TEXT)
    return ws


@asynccontextmanager
async def open_ws(url: str, pin: str = "", **kw):
    """connect_ws 를 async with 로 쓰는 모양 (블록이 끝나면 닫음)"""
    ws = await connect_ws(url, pin, **kw)
    try:
        yield ws
    finally:
        await ws.close()
