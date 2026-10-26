"""
WAN 설정 — client_config.json 과 완전히 분리된 별도 파일
=====================================================
LAN 설정 파일은 깨지면 기본값으로 되돌아가므로(load_config), WAN 이 그 파일을 건드리면
LAN 설정이 날아갈 수 있다. 그래서 WAN 은 자기 폴더의 wan_config.json 만 읽고 쓴다.
"""

import os
import json
import secrets
import threading

# ── 화질 (한 곳에 모아 둠 — 측정 후 조정) ──
# 이름 → (JPEG 화질, 최대 fps).  연결할 때마다 항상 "절약" 으로 시작한다.
QUALITY = {"절약": (40, 5), "보통": (60, 15), "고화질": (75, 20)}
QUALITY_DEFAULT = "절약"
SAVER_BOX = (1280, 720)     # "절약" 에서 요청하는 화면 크기 상한

# 화면을 동시에 몇 장 요청할지 (인터넷 지연 대응). 현장 화면 조각은 "보는 쪽이 가진 번호 이후의 누적 변경분" 이라
# 요청이 겹치면 같은 변화가 두 번 올 수 있다 → 데이터를 아끼는 "절약" 은 1장씩, 나머지는 2장.
PREFETCH = {"절약": 1, "보통": 2, "고화질": 2}
MAX_PREFETCH = 3

RELAY_EXIT_PORT_BUSY = 3    # 안내 서버 프로세스가 포트를 못 잡고 끝날 때의 종료 코드 (화면에 이유를 보여 주려고)

DISCOVER_PORT = 9101        # 같은 망에서 중계 PC 를 자동으로 찾는 UDP 포트

DEFAULT_RELAY_KEY = "aurora-wan"    # 공용 키 기본값 — 따로 정하지 않아도 모든 PC 가 같은 키로 시작한다 (믿는 망에서 쓰는 편의 값)

DIRECT_PORT = 9102          # 현장 PC 가 주소로 바로 들어오는 접속을 받는 포트 (안내 서버 없이 쓰는 직접 접속)

DEFAULT_STUN = "stun:stun.l.google.com:19302"


def default_dir() -> str:
    """설정 폴더 — Windows 는 기존과 같은 D:\\RemoteConfig, 그 밖(시험 환경)은 홈 폴더 아래"""
    env = os.environ.get("AURORA_WAN_DIR")
    if env:
        return env
    if os.name == "nt":
        return r"D:\RemoteConfig"
    return os.path.join(os.path.expanduser("~"), ".aurora_wan")


DEFAULTS = {
    "wan_enabled":  None,     # WAN 기능 전체 켜기/끄기. None(처음 · 예전 설정) 이면 예전에 WAN 을 쓰던 PC 만 켜진 것으로 본다
    "enabled":      False,    # 이 PC 를 WAN 원격 대상(현장)으로 쓸지
    "relay_url":    "",       # 안내 서버 주소  ws://주소:포트
    "relay_key":    DEFAULT_RELAY_KEY,   # 안내 서버 공용 키 — 현장 접속 코드를 따로 안 정했으면 이 키가 접속 코드
    "agent_id":     "",       # 이 PC 의 고유 ID — 처음 한 번 만들어 계속 씀
    "name":         "",       # 표시 이름 (안내 서버에서 원격으로 바꿀 수 있음)
    "agent_secret": "",       # 이 현장 PC 만 아는 비밀 값 — 중계 PC 가 키를 바꾼 뒤에도 이 PC 에게만 새 키를 내려 주도록 등록해 둔다
    "access_code":  "",       # 접속 코드 — 비어 있으면 모든 접속을 거부
    "max_quality":  "고화질", # 이 PC 가 내보낼 수 있는 최고 화질
    "allow_files":  True,     # 파일 반입/반출 허용
    "config_rev":   0,        # 안내 서버에서 받아 적용한 설정 번호
    "stun":         DEFAULT_STUN,
    "force_relay":  True,     # 항상 중계 PC 를 거쳐 원격 (직접 연결 시도 안 함). False 면 직접 연결(P2P)을 먼저 시도하고 안 되면 중계
    "direct_timeout": 6.0,    # (force_relay 가 꺼져 있을 때) 직접 연결을 기다리는 시간(초) — 넘으면 중계로
    "auto_update":  True,
    "relay_host":   False,    # 이 PC 에서 안내 서버(중계)도 운영할지 — 공용 키는 relay_key 를 그대로 씀
    "relay_port":   9100,     # 안내 서버가 받을 포트
    "saved_codes":  {},       # 사용자 PC: 현장 PC 별로 한 번 입력해 둔 접속 코드 {agent_id: code} (따로 정한 코드가 있는 현장만)
    "relay_fp":     "",       # 안내 서버 인증서 지문(SHA-256) — 접속 정보에 들어 있거나 처음 접속 때 기억. wss:// 일 때 확인
    "relay_external": "",     # 다른 망에서 이 안내 서버에 닿는 주소(공인 IP · 도메인) — 접속 정보에 넣음
    "direct_listen": True,    # 현장 모드일 때 주소로 바로 들어오는 접속도 받을지 (안내 서버 없이 접근 코드로 접속)
    "direct_port":  DIRECT_PORT,
    "direct_targets": {},     # 사용자 PC: 주소로 접속했던 현장 {"주소:포트": {"name", "code", "last"}}
    "git_source":   "",       # 접속 정보를 주고받는 사내 Git 저장소 주소 (비어 있으면 사용 안 함)
    "git_file":     "aurora_wan.conn",   # 저장소 안의 접속 정보 파일 경로
    "git_branch":   "",       # 비워 두면 저장소의 기본 브랜치
    "conn_hash":    "",       # 마지막으로 적용한 설정 파일(.conn) 내용의 지문 — 같은 파일을 매번 다시 적용하지 않으려고
}

def new_agent_id() -> str:
    return secrets.token_hex(6)


def format_id(agent_id: str) -> str:
    """a1b2c3d4e5f6 → A1B2-C3D4-E5F6 (사람이 읽고 불러 주기 쉽게)"""
    s = agent_id.upper()
    return "-".join(s[i:i + 4] for i in range(0, len(s), 4))


def sanitize_remote(cfg: dict) -> dict:
    """안내 서버/사용자가 보낸 설정을 검사해 쓸 수 있는 항목만 남김"""
    out = {}
    if isinstance(cfg.get("name"), str):
        out["name"] = cfg["name"].strip()[:40]
    if isinstance(cfg.get("access_code"), str):
        out["access_code"] = cfg["access_code"].strip()[:64]
    if cfg.get("max_quality") in QUALITY:
        out["max_quality"] = cfg["max_quality"]
    if isinstance(cfg.get("allow_files"), bool):
        out["allow_files"] = cfg["allow_files"]
    return out


class WanConfig:
    """wan_config.json 읽기/쓰기 (스레드 안전, 원자적 저장, 깨진 파일은 .bad 로 치우고 기본값)"""

    def __init__(self, directory: str | None = None):
        self.dir  = directory or default_dir()
        self.path = os.path.join(self.dir, "wan_config.json")
        self._lock = threading.RLock()
        self._data = dict(DEFAULTS)
        self._mtime = None
        self.load()

    # ── 읽기/쓰기 ──
    def _stamp(self):
        try:
            self._mtime = os.path.getmtime(self.path)
        except OSError:
            self._mtime = None

    def reload_if_changed(self) -> bool:
        """다른 프로세스(화면)가 파일을 고쳤으면 다시 읽음 — 에이전트가 접근 코드 · 포트 변경을 바로 따라가게"""
        with self._lock:
            before = self._mtime
            self._stamp()
            if self._mtime == before:
                return False
            self.load()
            return True

    def load(self):
        with self._lock:
            self._stamp()
            try:
                with open(self.path, "r", encoding="utf-8-sig") as f:
                    saved = json.load(f)
                if not isinstance(saved, dict):
                    raise ValueError("객체가 아님")
            except FileNotFoundError:
                saved = {}
            except OSError:
                self._mtime = None     # 다른 프로세스가 쓰는 순간 등 일시적으로 못 읽음 — 있던 값을 그대로 두고 다음에 다시 읽는다
                return
            except Exception:
                saved = {}
                try:   # 깨진 파일은 보관하고 기본값으로 시작 (LAN 설정에는 영향 없음)
                    os.replace(self.path, self.path + ".bad")
                except OSError:
                    pass
            self._data = dict(DEFAULTS)
            self._data.update({k: v for k, v in saved.items() if k in DEFAULTS})

    def save(self):
        with self._lock:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._stamp()

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key) if default is None else default)

    def update(self, **kw):
        with self._lock:
            self._data.update({k: v for k, v in kw.items() if k in DEFAULTS})
            self.save()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    # ── 현장 고유 ID ──
    def ensure_id(self) -> str:
        with self._lock:
            if not self._data["agent_id"]:
                self._data["agent_id"] = new_agent_id()
                self.save()
            return self._data["agent_id"]

    def wan_on(self) -> bool:
        """WAN 전체 스위치.  끄면 접속 정보를 가져오지도, 중계 PC 에 접속하지도, 접속을 받지도 않는다.
        한 번도 정하지 않았다면(예전 버전에서 올라온 설정) WAN 을 쓰던 흔적이 있을 때만 켜진 것으로 본다"""
        with self._lock:
            v = self._data.get("wan_enabled")
            if v is None:
                d = self._data
                return bool(d.get("enabled") or d.get("relay_url") or d.get("relay_host") or d.get("direct_targets"))
            return bool(v)

    def ensure_agent_secret(self) -> str:
        with self._lock:
            if not self._data["agent_secret"]:
                self._data["agent_secret"] = secrets.token_urlsafe(24)
                self.save()
            return self._data["agent_secret"]

    def ensure_access_code(self) -> str:
        """현장 PC 의 접근 코드가 없으면 무작위로 하나 만든다 (중계 PC 가 사용자 PC 에 알려 주므로 사람이 외울 필요 없음)"""
        with self._lock:
            if not self._data["access_code"]:
                self._data["access_code"] = secrets.token_urlsafe(9)
                self.save()
            return self._data["access_code"]

    def apply_remote(self, cfg: dict, rev: int) -> dict:
        """안내 서버가 보낸 설정 적용 → 실제로 바뀐 항목"""
        clean = sanitize_remote(cfg)
        with self._lock:
            changed = {k: v for k, v in clean.items() if self._data.get(k) != v}
            self._data.update(clean)
            self._data["config_rev"] = int(rev)
            self.save()
            return changed
