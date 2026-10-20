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
    "enabled":      False,    # 이 PC 를 WAN 원격 대상(현장)으로 쓸지
    "relay_url":    "",       # 안내 서버 주소  ws://주소:포트
    "relay_key":    "",       # 안내 서버 공용 키 (서버에 키를 걸었을 때만 필요)
    "agent_id":     "",       # 이 PC 의 고유 ID — 처음 한 번 만들어 계속 씀
    "name":         "",       # 표시 이름 (안내 서버에서 원격으로 바꿀 수 있음)
    "access_code":  "",       # 접속 코드 — 비어 있으면 모든 접속을 거부
    "max_quality":  "고화질", # 이 PC 가 내보낼 수 있는 최고 화질
    "allow_files":  True,     # 파일 반입/반출 허용
    "config_rev":   0,        # 안내 서버에서 받아 적용한 설정 번호
    "stun":         DEFAULT_STUN,
    "direct_timeout": 6.0,    # 직접 연결을 기다리는 시간(초) — 넘으면 비상 중계로
    "auto_update":  True,
}

# 안내 서버가 원격으로 바꿀 수 있는 항목
REMOTE_KEYS = ("name", "access_code", "max_quality", "allow_files")


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
        self.load()

    # ── 읽기/쓰기 ──
    def load(self):
        with self._lock:
            self._data = dict(DEFAULTS)
            try:
                with open(self.path, "r", encoding="utf-8-sig") as f:
                    saved = json.load(f)
                if not isinstance(saved, dict):
                    raise ValueError("객체가 아님")
                self._data.update({k: v for k, v in saved.items() if k in DEFAULTS})
            except FileNotFoundError:
                pass
            except Exception:
                try:   # 깨진 파일은 보관하고 기본값으로 시작 (LAN 설정에는 영향 없음)
                    os.replace(self.path, self.path + ".bad")
                except OSError:
                    pass

    def save(self):
        with self._lock:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)

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

    def apply_remote(self, cfg: dict, rev: int) -> dict:
        """안내 서버가 보낸 설정 적용 → 실제로 바뀐 항목"""
        clean = sanitize_remote(cfg)
        with self._lock:
            changed = {k: v for k, v in clean.items() if self._data.get(k) != v}
            self._data.update(clean)
            self._data["config_rev"] = int(rev)
            self.save()
            return changed
