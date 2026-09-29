"""
Aurora Remote Controller - Client
=====================================
- 서버 IP를 GUI에서 동적 변경 가능
- 설정은 client_config.json 에 저장
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import threading
import subprocess
import re
import requests
import psutil
import time
import socket
import os
import json
import shutil
import sys
import ctypes
import winreg
import ipaddress
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta
import pystray
from PIL import Image as PILImage
from PIL import ImageTk
import aurora_update

APP_VERSION = "2.9.0"   # 올리면 build.bat 이 dist/latest/version.json 에 기록 → 자동 업데이트 기준

# ── 고해상도(DPI) 대응 ────────────────────
def _resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

def _enable_dpi_awareness():
    """고해상도(배율) 화면에서 글자가 흐리지 않도록 DPI 인식 모드로 실행"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

_enable_dpi_awareness()

SCALE = 1.0   # 화면 배율 (100% → 1.0, 150% → 1.5) — tk 루트 생성 직후 _init_scale 로 설정

def _init_scale(root):
    """tk 루트 기준으로 화면 배율 계수 계산"""
    global SCALE
    SCALE = root.winfo_fpixels("1i") / 96.0

def _px(v):
    """100% 기준 픽셀 값을 현재 화면 배율에 맞춰 환산"""
    return int(round(v * SCALE))

# ── 중복 실행 방지 ────────────────────────
_LOCK_SOCK = None
def _acquire_lock(port=19701):
    global _LOCK_SOCK
    try:
        _LOCK_SOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _LOCK_SOCK.bind(("127.0.0.1", port))
    except OSError:
        _r = tk.Tk(); _r.withdraw()
        messagebox.showerror("실행 오류", "Aurora Client 가 이미 실행 중입니다.")
        _r.destroy()
        raise SystemExit

# ─────────────────────────────────────────
# 설정 파일
# ─────────────────────────────────────────
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REMOTE_CONFIG_DIR = r"D:\RemoteConfig"
CONFIG_FILE       = os.path.join(REMOTE_CONFIG_DIR, "client_config.json")

def _init_config_dir():
    os.makedirs(REMOTE_CONFIG_DIR, exist_ok=True)
    # 현재 경로에 기존 config가 있으면 D:\RemoteConfig 로 이동 (추후 제거 예정)
    old_path = os.path.join(BASE_DIR, "client_config.json")
    if os.path.exists(old_path) and not os.path.exists(CONFIG_FILE):
        shutil.move(old_path, CONFIG_FILE)

_init_config_dir()

DEFAULT_CONFIG = {
    "server_ips":        ["192.168.11.7"],
    "server_port":       9000,
    "listen_port":       9001,
    "aurora_path":       r"D:\exe\aurora.exe",
    "kill_processes":    [],
    "auto_delete_rules": [],
    "ping_mode":         "manual",
    # ── 반출(다른 PC 파일 가져오기) ──
    "export_key":        "aurora-export",   # 모든 PC 가 같은 값이어야 서로 요청 가능
    "export_relay":      "192.168.110.152", # 중계 PC (LHS)
    "export_peer_port":  9001,              # 다른 PC 클라이언트의 수신 포트
    "export_save_dir":   os.path.join(os.path.expanduser("~"), "Downloads", "AuroraExport"),
    "export_devices":    {},                # {ip: 표시 이름}
    # 사용자가 직접 자동 시작을 끈 경우에만 True — 기본은 자동 시작 ON
    "autostart_off":     False,
}

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                # 구버전 단일 server_ip → server_ips 마이그레이션
                if "server_ip" in d and "server_ips" not in d:
                    d["server_ips"] = [d["server_ip"]]
                for k, v in DEFAULT_CONFIG.items():
                    d.setdefault(k, v)
                return d
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

config = load_config()

SERVER_PORT     = config.get("server_port", 9000)
LISTEN_PORT     = config.get("listen_port", 9001)
STATUS_INTERVAL = 3

# ─────────────────────────────────────────
# 자동 시작 (레지스트리)
# ─────────────────────────────────────────
_RUN_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_NAME = "AuroraClient"

def _get_exe_path() -> str:
    """실행 중인 경로 반환 — PyInstaller 패키징 여부 자동 감지"""
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])

def autostart_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, _APP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False

def set_autostart(enable: bool):
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE)
    if enable:
        winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, f'"{_get_exe_path()}"')
    else:
        try:
            winreg.DeleteValue(key, _APP_NAME)
        except FileNotFoundError:
            pass
    winreg.CloseKey(key)

# ─────────────────────────────────────────
# Aurora 제어
# ─────────────────────────────────────────
_main_pid: int | None = None

def _main_exe_path() -> str:
    """aurora_path 설정 → 실제 실행 exe 경로 (실행·감지 공용)
    - .exe 지정: 그대로
    - 폴더 지정: 폴더\\폴더이름.exe 가 있으면 그것, 없으면 폴더\\Aurora.exe
      (서버 배포는 D:\\Aurora\\Aurora_EXE_버전 폴더를 보내고, 그 안의 실행 파일은 Aurora.exe)"""
    path = config.get("aurora_path", DEFAULT_CONFIG["aurora_path"]).strip().rstrip("\\/")
    if path.lower().endswith(".exe"):
        return path
    own = os.path.join(path, os.path.basename(path) + ".exe")
    return own if os.path.isfile(own) else os.path.join(path, "Aurora.exe")

def _main_exe_name() -> str:
    """실행/감시 대상 exe 이름"""
    return os.path.basename(_main_exe_path()).lower()

def is_main_running() -> bool:
    """실행 대상 프로그램이 현재 켜져 있는지 확인 (PID 캐싱)"""
    global _main_pid
    exe = _main_exe_name()
    if _main_pid is not None:
        try:
            p = psutil.Process(_main_pid)
            if p.name().lower() == exe and p.status() != psutil.STATUS_ZOMBIE:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        _main_pid = None
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if proc.info["name"].lower() == exe:
                _main_pid = proc.info["pid"]
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False

def start_main() -> str:
    """aurora_path에 지정된 프로그램을 실행"""
    exe = _main_exe_path()
    if is_main_running():
        return "이미 실행중"
    if not os.path.exists(exe):
        return f"파일 없음: {exe}"
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
        return "실행 성공"
    except Exception as e:
        return f"실행 실패: {e}"

def kill_process_by_name(proc_name: str) -> int:
    """이름으로 프로세스를 찾아 종료. 종료된 개수 반환"""
    global _main_pid
    name_lower = proc_name.lower()
    killed = 0
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if proc.info["name"].lower() == name_lower:
                proc.kill()
                killed += 1
                if proc.info["pid"] == _main_pid:
                    _main_pid = None
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return killed

def stop_main() -> str:
    """실행 대상 프로그램 종료"""
    killed = kill_process_by_name(_main_exe_name())
    return f"종료 완료 ({killed}개)" if killed > 0 else "실행중인 프로세스 없음"

def get_running_processes(names: list[str]) -> dict[str, bool]:
    """프로세스 이름 목록에 대해 현재 실행 여부 반환"""
    running = {n: False for n in names}
    for proc in psutil.process_iter(["name"]):
        try:
            pname = proc.info["name"].lower()
            for orig in names:
                if orig.lower() == pname:
                    running[orig] = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return running

# ─────────────────────────────────────────
# 자동 삭제
# ─────────────────────────────────────────

def run_auto_delete(log_fn=None):
    """활성화·적용된 규칙 모두 실행, UI 라벨 갱신"""
    rules = config.get("auto_delete_rules", [])
    for i, rule in enumerate(rules):
        if not rule.get("enabled", True) or not rule.get("applied", False):
            continue
        ts = _apply_delete_rule(rule, log_fn)
        if ts:
            rule["last_deleted"] = ts
            save_config(config)
            # UI 라벨 갱신 (메인 스레드로)
            if app_instance:
                def _upd(idx=i, t=ts, p=rule.get("path", "").strip()):
                    wlist = app_instance._delete_rule_widgets
                    if idx < len(wlist):
                        wlist[idx]["last_del_lbl"].config(text=f"마지막 삭제: {t}")
                        # 행 값도 갱신해 이후 UI 저장이 덮어쓰지 않도록 함 (config 는 위에서 이미 저장됨)
                        wlist[idx]["last_deleted"]      = t
                        wlist[idx]["last_deleted_path"] = p
                app_instance.root.after(0, _upd)

def _parse_folder_date(name: str):
    """폴더 이름에서 날짜 파싱. yyyyMMdd(8자리) → yyMMdd(6자리) 순으로 시도. 실패 시 None."""
    m8 = re.search(r'\d{8}', name)
    if m8:
        try:
            return datetime.strptime(m8.group(), "%Y%m%d")
        except ValueError:
            pass
    m6 = re.search(r'\d{6}', name)
    if m6:
        try:
            return datetime.strptime(m6.group(), "%y%m%d")
        except ValueError:
            pass
    return None

def _apply_delete_rule(rule: dict, log_fn=None) -> str | None:
    """삭제 실행. 삭제된 항목이 있으면 타임스탬프 반환, 없으면 None"""
    rule_type = rule.get("type", "file")
    folder    = rule.get("path", "").strip()
    days      = int(rule.get("minutes", 30))

    if not folder or days <= 0:
        return None
    if not os.path.isdir(folder):
        if log_fn:
            log_fn(f"[자동삭제] 폴더 없음: {folder}")
        return None

    now       = datetime.now()
    threshold = days * 86400

    # ── 폴더 삭제 모드 ─────────────────────
    if rule_type == "folder":
        current_aurora = os.path.normpath(config.get("aurora_path", "")).lower()
        deleted = 0
        for name in os.listdir(folder):
            dpath = os.path.join(folder, name)
            if not os.path.isdir(dpath):
                continue
            # 현재 Aurora 경로가 이 폴더 안에 있으면 보호
            if current_aurora.startswith(os.path.normpath(dpath).lower()):
                if log_fn:
                    log_fn(f"[자동삭제] 스킵 (사용중): {name}")
                continue
            folder_date = _parse_folder_date(name)
            if folder_date is None:
                continue
            age_sec = (now - folder_date).total_seconds()
            if age_sec >= threshold:
                try:
                    shutil.rmtree(dpath)
                    deleted += 1
                    age_days = int(age_sec // 86400)
                    if log_fn:
                        log_fn(f"[자동삭제] 폴더 삭제: {name} ({age_days}일 경과)")
                except Exception as e:
                    if log_fn:
                        log_fn(f"[자동삭제] 폴더 삭제 실패: {name} — {e}")
        if log_fn and deleted == 0:
            log_fn(f"[자동삭제] {folder} — 삭제 대상 폴더 없음")
        return now.strftime("%Y-%m-%d %H:%M") if deleted > 0 else None

    # ── 파일 삭제 모드 (기존) ──────────────
    ext = rule.get("ext", "").strip().lstrip(".")
    if not ext:
        return None
    deleted = 0
    for fname in os.listdir(folder):
        if not fname.lower().endswith(f".{ext.lower()}"):
            continue
        fpath = os.path.join(folder, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
            age_sec = (now - mtime).total_seconds()
            if age_sec >= threshold:
                os.remove(fpath)
                deleted += 1
                age_days = int(age_sec // 86400)
                if log_fn:
                    log_fn(f"[자동삭제] {fname} 삭제 (최종수정 {age_days}일 전)")
        except Exception as e:
            if log_fn:
                log_fn(f"[자동삭제] 오류: {fname} — {e}")
    if log_fn and deleted == 0:
        log_fn(f"[자동삭제] {folder} *.{ext} — 삭제 대상 없음")
    return now.strftime("%Y-%m-%d %H:%M") if deleted > 0 else None

def auto_delete_watchdog(log_fn=None):
    """날짜가 바뀔 때마다 자동 삭제 실행"""
    last_date = date.today()
    while True:
        time.sleep(60)
        today = date.today()
        if today != last_date:
            last_date = today
            if config.get("auto_delete_rules"):
                run_auto_delete(log_fn)
            # UI 카운트다운 리셋
            if app_instance:
                app_instance.root.after(0, app_instance._reset_countdown)

# ─────────────────────────────────────────
# 명령 수신 HTTP 핸들러
# ─────────────────────────────────────────
app_instance = None

# ─────────────────────────────────────────
# 반출: 허용 경로(D 드라이브·바탕화면) 파일 제공 + 중계
# ─────────────────────────────────────────
EXPORT_KEY_HEADER = "X-Aurora-Key"
EXPORT_CHUNK      = 1024 * 1024   # 1MB 단위 스트리밍
EXPORT_API_VER    = 1

def _desktop_dir() -> str:
    """현재 사용자 바탕화면 경로 (OneDrive 리디렉션 포함)"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        val, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        path = os.path.expandvars(val)
        if os.path.isdir(path):
            return path
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")

def export_roots() -> list[dict]:
    """반출 허용 루트 — 이 밖의 경로는 모두 거부"""
    roots = []
    if os.path.isdir("D:\\"):
        roots.append({"name": "D 드라이브", "path": "D:\\"})
    desk = _desktop_dir()
    if os.path.isdir(desk):
        roots.append({"name": "바탕화면", "path": desk})
    # 설치 프로그램이 만드는 바로가기는 대부분 공용 바탕화면에 있음 (탐색기 바탕화면은 둘을 합쳐 보여 줌)
    pub = os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop")
    if os.path.isdir(pub):
        roots.append({"name": "공용 바탕화면", "path": pub})
    return roots

def export_resolve(path: str) -> str | None:
    """요청 경로를 실제 경로로 정규화 후 허용 루트 안이면 반환, 아니면 None
    (.. / 심볼릭 링크 / 정션으로 루트 밖을 가리키는 경우도 차단)"""
    if not path:
        return None
    try:
        real = os.path.normcase(os.path.realpath(path))
    except Exception:
        return None
    for root in export_roots():
        r = os.path.normcase(os.path.realpath(root["path"]))
        try:
            if os.path.commonpath([real, r]) == r:
                return os.path.realpath(path)
        except ValueError:   # 드라이브가 다름
            continue
    return None

def _export_entry(full: str, name: str) -> dict | None:
    try:
        st = os.stat(full)
    except OSError:
        return None
    is_dir = os.path.isdir(full)
    return {"name": name, "path": full, "dir": is_dir,
            "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime}

def export_list(path: str) -> list[dict]:
    items = []
    with os.scandir(path) as it:
        for e in it:
            ent = _export_entry(e.path, e.name)
            if ent:
                items.append(ent)
    items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return items

def export_walk(path: str) -> dict:
    """폴더 하위 전체 — 파일은 상대경로/크기/수정시각, 빈 폴더도 포함"""
    files, dirs = [], []
    for cur, dnames, fnames in os.walk(path):
        rel_dir = os.path.relpath(cur, path)
        rel_dir = "" if rel_dir == "." else rel_dir
        if rel_dir:
            dirs.append(rel_dir)
        for fn in fnames:
            full = os.path.join(cur, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            files.append({"rel": os.path.join(rel_dir, fn) if rel_dir else fn,
                          "size": st.st_size, "mtime": st.st_mtime})
    return {"files": files, "dirs": dirs}

def _export_peer(ip: str) -> str | None:
    """중계 대상은 사설망 IPv4 만 허용"""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if addr.version != 4 or not addr.is_private:
        return None
    return f"{addr}:{config.get('export_peer_port', 9001)}"

_discover_cache = {"time": 0.0, "result": []}
_discover_lock  = threading.Lock()

def export_discover(force: bool = False) -> list[dict]:
    """자기 NIC 대역(/24)을 스캔해 반출 API 가 켜진 클라이언트 목록 반환 (60초 캐시)"""
    with _discover_lock:
        if not force and time.time() - _discover_cache["time"] < 60:
            return _discover_cache["result"]

        my_ips, hosts = set(), set()
        for iface in psutil.net_if_addrs().values():
            for snic in iface:
                if snic.family != socket.AF_INET or snic.address.startswith(("127.", "169.254.")):
                    continue
                my_ips.add(snic.address)
                try:
                    net = ipaddress.ip_network(f"{snic.address}/24", strict=False)
                except ValueError:
                    continue
                hosts.update(str(h) for h in net.hosts())
        hosts -= my_ips
        port = config.get("export_peer_port", 9001)
        key  = config.get("export_key", "")

        found, found_lock = [], threading.Lock()
        def _probe(ip):
            try:
                with socket.create_connection((ip, port), timeout=0.4):
                    pass
                r = requests.get(f"http://{ip}:{port}/info",
                                 headers={EXPORT_KEY_HEADER: key}, timeout=2)
                if r.ok:
                    info = r.json()
                    with found_lock:
                        found.append({"ip": ip, "hostname": info.get("hostname", "")})
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=128) as ex:
            list(ex.map(_probe, sorted(hosts)))

        found.sort(key=lambda d: [int(x) for x in d["ip"].split(".")])
        _discover_cache.update(time=time.time(), result=found)
        return found


class _BodyReader:
    """스트림에서 정해진 길이만큼만 읽는 파일 객체 — 반입 업로드·중계용 (requests 가 Content-Length 를 유지)
    on_chunk(n) 로 진행량 통지, on_chunk 가 예외를 던지면 전송 중단"""
    def __init__(self, src, length: int, on_chunk=None):
        self._src, self._len, self._left, self._on = src, length, length, on_chunk

    def __len__(self):
        return self._len

    def __iter__(self):
        while True:
            chunk = self.read(EXPORT_CHUNK)
            if not chunk:
                return
            yield chunk

    def read(self, n=-1):
        if self._left <= 0:
            return b""
        n = self._left if n is None or n < 0 else min(n, self._left)
        chunk = self._src.read(n)
        if not chunk:   # 약속한 길이보다 짧음 → 받는 쪽이 무한 대기하지 않도록 끊음
            raise IOError("전송이 중간에 끊겼습니다")
        self._left -= len(chunk)
        if self._on:
            self._on(len(chunk))
        return chunk


class CommandHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def _set_headers(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── 반출 API (GET) ───────────────────
    def do_GET(self):
        import hmac
        key = config.get("export_key", "")
        if not key or not hmac.compare_digest(self.headers.get(EXPORT_KEY_HEADER, ""), key):
            return self._send_json({"error": "인증 실패 (반출 키 불일치)"}, 403)

        parsed = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if q.get("via"):
                return self._relay(parsed, q)
            route = parsed.path
            if route == "/info":
                return self._send_json({"hostname": socket.gethostname(),
                                        "api": EXPORT_API_VER})
            if route == "/discover":
                return self._send_json(export_discover(force=q.get("refresh") == "1"))
            if route == "/fs/roots":
                return self._send_json(export_roots())

            path = export_resolve(q.get("path", ""))
            if path is None:
                return self._send_json({"error": "허용되지 않은 경로 (D 드라이브·바탕화면만 가능)"}, 403)
            if route == "/fs/list":
                return self._send_json(export_list(path))
            if route == "/fs/walk":
                return self._send_json(export_walk(path))
            if route == "/fs/file":
                return self._send_file(path)
            self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._send_json({"error": str(e)}, 500)
            except Exception:
                pass

    def _send_file(self, path: str):
        if not os.path.isfile(path):
            return self._send_json({"error": "파일이 아닙니다"}, 400)
        size  = os.path.getsize(path)
        start = 0
        rng   = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d+)-$", rng)
        if m and int(m.group(1)) < size:
            start = int(m.group(1))
        with open(path, "rb") as f:
            f.seek(start)
            self.send_response(206 if start else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size - start))
            if start:
                self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            self.end_headers()
            if app_instance and not start:
                app_instance.add_log(f"📤 반출: {path} → {self.client_address[0]}")
            while True:
                chunk = f.read(EXPORT_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _relay(self, parsed, q: dict):
        """via=IP1,IP2… 의 첫 IP 로 요청을 그대로 넘기고 응답을 스트리밍 (디스크 저장 없음)"""
        hops = [h for h in q.pop("via").split(",") if h.strip()]
        peer = _export_peer(hops[0])
        if peer is None:
            return self._send_json({"error": f"중계 대상 IP 오류: {hops[0]}"}, 400)
        if hops[1:]:
            q["via"] = ",".join(hops[1:])
        url = f"http://{peer}{parsed.path}?{urlencode(q)}"
        headers = {EXPORT_KEY_HEADER: self.headers.get(EXPORT_KEY_HEADER, "")}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            r = requests.get(url, headers=headers, stream=True, timeout=(3, 120))
        except Exception as e:
            return self._send_json({"error": f"{hops[0]} 연결 실패: {e}"}, 502)
        with r:
            self.send_response(r.status_code)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if h in r.headers:
                    self.send_header(h, r.headers[h])
            self.end_headers()
            for chunk in r.raw.stream(EXPORT_CHUNK, decode_content=False):
                self.wfile.write(chunk)

    # ── 반입 API (PUT) — 반출과 같은 키·허용 경로(D 드라이브·바탕화면)만 ──
    def do_PUT(self):
        import hmac
        key = config.get("export_key", "")
        if not key or not hmac.compare_digest(self.headers.get(EXPORT_KEY_HEADER, ""), key):
            return self._send_json({"error": "인증 실패 (반출 키 불일치)"}, 403)

        parsed = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if q.get("via"):
                return self._relay_put(parsed, q)
            path = export_resolve(q.get("path", ""))
            if path is None:
                return self._send_json({"error": "허용되지 않은 경로 (D 드라이브·바탕화면만 가능)"}, 403)
            if parsed.path == "/fs/mkdir":
                os.makedirs(path, exist_ok=True)
                return self._send_json({"ok": True})
            if parsed.path == "/fs/upload":
                return self._recv_file(path, q)
            self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            try:
                self._send_json({"error": str(e)}, 500)
            except Exception:
                pass

    def _recv_file(self, path: str, q: dict):
        """받은 본문을 .part 에 쓴 뒤 완료되면 교체 (중간에 끊기면 .part 삭제 → 기존 파일 유지)"""
        length = int(self.headers.get("Content-Length", "-1") or -1)
        if length < 0:
            return self._send_json({"error": "Content-Length 없음"}, 411)
        if os.path.isdir(path):
            return self._send_json({"error": "같은 이름의 폴더가 있습니다"}, 400)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        part = path + ".part"
        try:
            with open(part, "wb") as f:
                for chunk in _BodyReader(self.rfile, length):
                    f.write(chunk)
            os.replace(part, path)
        except Exception:
            try:
                os.remove(part)
            except OSError:
                pass
            raise
        try:
            mtime = float(q.get("mtime", ""))
            os.utime(path, (mtime, mtime))
        except (ValueError, OSError):
            pass
        if app_instance:
            app_instance.add_log(f"📥 반입: {self.client_address[0]} → {path}")
        self._send_json({"ok": True, "size": length})

    def _relay_put(self, parsed, q: dict):
        """via 의 첫 IP 로 업로드 본문을 그대로 흘려 보냄 (디스크 저장 없음)"""
        hops = [h for h in q.pop("via").split(",") if h.strip()]
        peer = _export_peer(hops[0])
        if peer is None:
            return self._send_json({"error": f"중계 대상 IP 오류: {hops[0]}"}, 400)
        if hops[1:]:
            q["via"] = ",".join(hops[1:])
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            r = requests.put(f"http://{peer}{parsed.path}?{urlencode(q)}",
                             data=_BodyReader(self.rfile, length) if length else b"",
                             headers={EXPORT_KEY_HEADER: self.headers.get(EXPORT_KEY_HEADER, "")},
                             timeout=(3, 120))
        except Exception as e:
            return self._send_json({"error": f"{hops[0]} 연결 실패: {e}"}, 502)
        self.send_response(r.status_code)
        self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(r.content)))
        self.end_headers()
        self.wfile.write(r.content)

    def do_POST(self):
        if self.path == "/command":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                cmd = data.get("command", "")
                self._set_headers()
                self.wfile.write(b'{"ok": true}')
                threading.Thread(target=handle_command, args=(cmd,), daemon=True).start()
            except Exception:
                self._set_headers(400)
                self.wfile.write(b'{"error": "bad request"}')
        else:
            self._set_headers(404)
            self.wfile.write(b'{"error": "not found"}')

def ping_ip_client(ip: str) -> int | None:
    try:
        result = subprocess.run(
            ["ping", "-n", "1", "-w", "1000", ip],
            capture_output=True, text=True,
            encoding="cp949", errors="replace", timeout=3
        )
        m = re.search(r"평균\s*=\s*(\d+)ms|Average\s*=\s*(\d+)ms", result.stdout)
        if m:
            return int(m.group(1) or m.group(2))
    except Exception:
        pass
    return None

def ping_all_servers():
    """등록된 서버 IP 전체 핑 후 UI 업데이트"""
    ips = config.get("server_ips", [])
    results = {}
    def _do(ip):
        results[ip] = ping_ip_client(ip)
    threads = [threading.Thread(target=_do, args=(ip,), daemon=True) for ip in ips]
    for t in threads: t.start()
    for t in threads: t.join()
    if app_instance:
        app_instance.root.after(0, lambda r=dict(results): app_instance._update_ping_labels(r))

def handle_command(cmd: str):
    if app_instance is None:
        return

    if cmd == "start":
        result = start_main()
        app_instance.add_log(f"▶ 명령 수신: START → {result}")
        app_instance.root.after(0, lambda: app_instance.cmd_lbl.config(
            text="START ▶", fg=app_instance.GREEN))

    elif cmd == "stop":
        result = stop_main()
        app_instance.add_log(f"■ 명령 수신: STOP → {result}")
        app_instance.root.after(0, lambda: app_instance.cmd_lbl.config(
            text="STOP ■", fg=app_instance.RED))

    elif cmd.startswith("setpath:"):
        new_path = cmd[len("setpath:"):]
        config["aurora_path"] = new_path
        save_config(config)
        app_instance.add_log(f"📁 Aurora 경로 업데이트: {new_path}")
        app_instance.root.after(0, lambda: app_instance.aurora_path_var.set(new_path))

    elif cmd.startswith("setpingmode:"):
        mode = cmd[len("setpingmode:"):]
        config["ping_mode"] = mode
        save_config(config)
        app_instance.add_log(f"핑 모드 변경: {mode}")
        app_instance.root.after(0, lambda m=mode: app_instance._on_ping_mode_changed(m))

    elif cmd == "ping":
        threading.Thread(target=ping_all_servers, daemon=True).start()

# ─────────────────────────────────────────
# 부팅 시 자동 실행 (클라이언트 자신)
# ─────────────────────────────────────────
def setup_self_autostart():
    """AuroraClient.exe 자체를 레지스트리 자동 실행에 등록 (기본 ON)
    사용자가 '자동 시작' 을 직접 OFF 로 바꾼 경우(autostart_off)에만 등록하지 않는다"""
    if not getattr(sys, "frozen", False):
        return   # 개발 환경(py 직접 실행)에선 등록 안 함
    if config.get("autostart_off"):
        return
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "AuroraClient", 0, winreg.REG_SZ,
                          f'"{sys.executable}"')
        winreg.CloseKey(key)
    except Exception:
        pass

def run_command_server():
    port = config.get("listen_port", 9001)
    # 파일 전송 중에도 명령·상태 요청을 받을 수 있도록 요청마다 스레드로 처리
    server = ThreadingHTTPServer(("0.0.0.0", port), CommandHandler)
    server.daemon_threads = True
    server.serve_forever()

# ─────────────────────────────────────────
# GUI
# ─────────────────────────────────────────
class _Tooltip:
    """위젯에 마우스를 올리고 잠시 있으면 설명(hint)을 띄우는 툴팁. text 는 문자열 또는 callable"""
    DELAY_MS = 500

    def __init__(self, widget, text):
        self.widget = widget
        self.text   = text
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>",       self._schedule, add="+")
        widget.bind("<Leave>",       self._hide,     add="+")
        widget.bind("<ButtonPress>", self._hide,     add="+")
        widget.bind("<Destroy>",     self._hide,     add="+")

    def _schedule(self, event=None):
        self._cancel()
        self._after_id = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self):
        self._after_id = None
        if self._tip is not None:
            return
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        try:
            x, y = self.widget.winfo_pointerxy()
            x += _px(12)
            y += _px(18)
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.attributes("-topmost", True)
            tk.Label(tip, text=text, justify="left", wraplength=_px(320),
                     font=("맑은 고딕", 9), fg="#FFFFFF", bg="#1D2327",
                     padx=_px(10), pady=_px(6), bd=0).pack()
            tip.update_idletasks()
            # 화면 오른쪽/아래를 넘으면 안쪽으로 당김
            x = max(0, min(x, tip.winfo_screenwidth()  - tip.winfo_reqwidth()))
            y = max(0, min(y, tip.winfo_screenheight() - tip.winfo_reqheight()))
            tip.wm_geometry(f"+{x}+{y}")
            self._tip = tip
        except tk.TclError:
            self._tip = None

    def _hide(self, event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


class AuroraClientApp:

    # ── 라벨 설명(hint) ──────────────────
    # 섹션 제목(_section_title) / 필드 이름(_field_label) 텍스트 기준으로 자동 부착
    LABEL_HINTS = {
        "서버 IP 목록": "상태를 보고할 서버 IP 목록입니다. 추가·삭제하면 바로 설정 파일에 저장되고 다음 보고부터 반영됩니다. 핑 결과 표시 줄은 프로그램을 다시 시작해야 갱신됩니다.",
        "핑 모드": "등록된 서버 IP별 핑 응답 시간을 보여 줍니다. Manual/Auto 모드는 서버가 변경하며 이 화면에서는 바꿀 수 없습니다. Auto 는 5초마다 측정하고, Manual 은 서버가 ping 명령을 보낼 때만 측정합니다.",
        "서버 포트": "서버로 상태를 보고할 때 쓰는 서버 쪽 포트입니다(기본 9000). 1024~65535 범위이며 '적용' 후 프로그램을 다시 시작해야 반영됩니다.",
        "수신 포트": "서버 명령(start/stop/setpath/ping/setpingmode)과 반출 요청을 받는 이 PC의 포트입니다(기본 9001). 1024~65535 범위이며 '적용' 후 프로그램을 다시 시작해야 반영됩니다.",
        "자동 시작": "PC에 로그인할 때 이 클라이언트를 자동 실행하도록 현재 사용자(HKCU)의 Run 레지스트리에 등록하거나 해제합니다. 기본값은 ON 이며, 직접 OFF 로 바꾼 경우에만 꺼진 상태가 유지됩니다.",
        "버전": "현재 실행 중인 클라이언트 버전입니다. D:\\RemoteConfig\\Update 폴더에 새 버전(AuroraClient.exe + version.json)을 넣어두면 켜질 때 자동으로 업데이트 후 재시작합니다. '최신 버전 확인' 을 누르면 바로 확인해 새 버전이면 업데이트 후 재시작합니다.",
        "수동 제어": "서버 명령과 별개로 이 PC에서 Aurora 를 직접 실행·종료하는 긴급용 영역입니다. 실행은 이미 켜져 있으면 하지 않고, 종료는 실행 경로의 exe 와 이름이 같은 프로세스를 모두 강제 종료합니다.",
        "LOG": "명령 수신, 서버 추가·삭제, 포트·경로 변경 등 클라이언트 동작 기록입니다. 최근 40줄만 표시합니다.",
        "실행 프로그램": "서버 명령이나 수동 제어로 실행·종료할 Aurora 프로그램을 지정하는 영역입니다.",
        "실행 경로": "Aurora 실행 파일 경로입니다. .exe 로 끝나면 그대로, 폴더만 입력하면 '폴더\\폴더이름.exe', 없으면 '폴더\\Aurora.exe' 를 실행합니다 (서버 배포 폴더 Aurora_EXE_버전 도 이 규칙). 이 exe 이름으로 실행 여부를 감시하고 종료도 합니다. '적용'으로 저장하며, 배포 후에는 서버가 setpath 명령으로 갱신합니다.",
        "프로세스 종료 목록": "강제 종료할 프로세스의 exe 이름을 등록하는 목록입니다. 이름 뒤에 .exe 가 없으면 자동으로 붙습니다. 체크한 항목을 아래 '선택 프로세스 종료', 목록 전체를 '전체 종료'로 종료합니다.",
        "자동 삭제 규칙": "폴더 안의 오래된 파일 또는 날짜 폴더를 자동 삭제하는 규칙 목록입니다. ON 체크와 '적용'이 모두 된 규칙만 날짜가 바뀔 때(자정) 실행되며, '지금 실행'으로 즉시 실행할 수도 있습니다.",
        "삭제 LOG": "자동 삭제 실행 내역(삭제한 파일·폴더, 건너뜀, 오류)을 시각과 함께 기록합니다.",
        "반입/반출 LOG": "중계 PC 연결 상태, 장비 검색, 파일 반입·반출 진행·완료·실패·취소 내역을 기록합니다.",
        "저장 폴더": "반출한 파일을 받을 내 PC의 폴더입니다. 실제로는 '저장 폴더\\장비이름 (IP)\\' 아래에 저장됩니다. 파일은 4개까지 동시에 받고, 취소하면 받던 파일이 .part 로 남아 다음에 이어받습니다.",
        "중계 PC": "직접 연결되지 않는 장비의 파일을 가져올 때 거치는 중계 PC(LHS)의 IP 입니다. '적용'으로 저장하며, 5초마다 연결을 확인하고 연결되면 장비를 자동으로 검색합니다. 모든 PC 의 export_key 가 같아야 연결됩니다.",
        "접속 경로": "반출 요청이 실제로 거치는 PC 순서입니다. 내 PC 에서 가져오면 내 PC → 중계 PC(LHS) → 대상 장비(CCW 등) 3대, 중계 PC(LHS)에서 직접 가져오면 LHS → 대상 장비 2대로 표시됩니다. 화살표가 초록이면 연결됨, 빨강이면 연결 안됨입니다.",
        "보낼 항목": "반입 모드에서 내 PC 에서 장비로 보낼 파일·폴더 목록입니다. '파일 추가'/'폴더 추가'로 넣고, 폴더는 하위 전체를 보냅니다. 파일은 4개까지 동시에 보내고, 같은 이름의 파일은 확인 후 덮어씁니다.",
        "반입 위치": "반입 파일이 들어갈 장비의 폴더입니다. 아래 파일 탐색에서 D 드라이브·바탕화면 안의 폴더로 이동하면 그 폴더가 반입 위치가 됩니다.",
        "장비": "중계 PC 가 자기 네트워크 대역(/24)을 스캔해 찾은 클라이언트 목록입니다. [중계]는 중계 PC 자신입니다. 장비를 고르면 허용 경로(D 드라이브·바탕화면)만 탐색할 수 있으며, '이름' 버튼으로 표시 이름을 바꿀 수 있습니다.",
    }

    # 탭 이름별 설명 (탭 순서: 설정 / 프로세스 관리 / 자동 삭제 / 반입/반출)
    TAB_HINTS = [
        "서버 연결과 Aurora 상태 확인, 서버 IP·포트 설정, 자동 시작, 수동 실행/종료를 하는 탭입니다. 설정은 D:\\RemoteConfig\\client_config.json 에 저장됩니다.",
        "Aurora 실행 경로를 지정하고, 등록한 exe 이름의 프로세스를 강제 종료하는 탭입니다.",
        "규칙(폴더·확장자·기한·ON)에 따라 오래된 파일이나 날짜 폴더를 날짜가 바뀔 때(자정) 자동 삭제하는 탭입니다.",
        "중계 PC(LHS)를 거쳐 직접 연결되지 않는 장비와 파일을 주고받는 탭입니다. 반출 모드는 장비 → 내 PC, 반입 모드는 내 PC → 장비이며, D 드라이브와 바탕화면만 접근할 수 있습니다.",
    ]

    # ── 색상 ─────────────────────────────
    BG       = "#F3F5F7"
    PANEL    = "#FFFFFF"
    PANEL_ALT    = "#F7F8FA"
    BORDER   = "#E1E5E8"
    INPUT_BORDER = "#CDD3D7"
    ROW_LINE = "#EEF1F3"
    TEXT     = "#111418"
    TEXT_DIM = "#5F686B"
    DISABLED_TEXT = "#9AA2A5"
    WHITE    = "#FFFFFF"
    BRAND    = "#2F98F9"
    BLUE     = "#1A6FC9"
    BLUE_H   = "#155CA8"
    NAV_ON_BG = "#E8F2FE"
    GREEN    = "#138A43"
    RED      = "#C9372C"
    YELLOW   = "#9A5700"
    OK_BG    = "#E7F6EC"
    ERR_BG   = "#FDECEA"
    WARN_BG  = "#FFF4E0"
    IDLE_BG  = "#EEF1F3"
    LOG_BG   = "#1D2327"
    LOG_FG   = "#D5DADD"
    BTN_START    = "#138A43"
    BTN_START_H  = "#0F7036"
    BTN_STOP     = "#C9372C"
    BTN_STOP_H   = "#A82C23"
    DANGER_BORDER = "#E8A9A3"
    OK_BORDER    = "#9FD5B3"
    OK_HOVER     = "#D6EEDD"
    SELECT_BG    = "#E8F2FE"

    # ── 폰트 ─────────────────────────────
    FONT             = ("맑은 고딕", 9)
    FONT_BOLD        = ("맑은 고딕", 9, "bold")
    FONT_TITLE       = ("맑은 고딕", 10, "bold")
    FONT_HEAD        = ("맑은 고딕", 11, "bold")
    FONT_VALUE       = ("맑은 고딕", 10, "bold")
    FONT_TAB         = ("맑은 고딕", 10)
    FONT_TAB_ON      = ("맑은 고딕", 10, "bold")
    FONT_SMALL       = ("맑은 고딕", 8, "bold")
    FONT_SMALL_BOLD  = ("맑은 고딕", 8, "bold")
    # 데이터(입력·IP·로그)도 같은 글꼴로 통일 — 고정폭 글꼴은 한글이 없어 대체 글꼴이 섞임
    FONT_INPUT       = ("맑은 고딕", 9)
    FONT_MONO        = ("맑은 고딕", 9)
    FONT_MONO_SMALL  = ("맑은 고딕", 8)
    FONT_MONO_BOLD   = ("맑은 고딕", 9, "bold")
    FONT_COUNTDOWN   = ("맑은 고딕", 13, "bold")

    @classmethod
    def setup_styles(cls):
        """ttk 공통 스타일 (clam 테마)"""
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar",
                        background=cls.BORDER, troughcolor=cls.BG,
                        arrowcolor=cls.TEXT_DIM, bordercolor=cls.BG,
                        lightcolor=cls.BORDER, darkcolor=cls.BORDER,
                        width=_px(10), arrowsize=_px(10))
        style.configure("Countdown.Horizontal.TProgressbar",
                        troughcolor=cls.BORDER, background=cls.BRAND,
                        bordercolor=cls.BORDER, lightcolor=cls.BRAND, darkcolor=cls.BRAND,
                        borderwidth=0, thickness=_px(8))
        style.configure("Export.Treeview", font=cls.FONT_INPUT, rowheight=_px(24),
                        background=cls.PANEL, fieldbackground=cls.PANEL,
                        foreground=cls.TEXT, bordercolor=cls.BORDER, borderwidth=0)
        style.map("Export.Treeview", background=[("selected", cls.SELECT_BG)],
                  foreground=[("selected", cls.TEXT)])
        style.configure("Export.Treeview.Heading", font=cls.FONT_SMALL,
                        background=cls.PANEL_ALT, foreground=cls.TEXT_DIM,
                        relief="flat", borderwidth=0, padding=(_px(6), _px(6)))
        style.map("Export.Treeview.Heading", background=[("active", cls.PANEL_ALT)])
        style.configure("Export.TCombobox", padding=_px(4))

    def __init__(self, root: tk.Tk):
        global app_instance
        app_instance = self

        self.root = root
        self.my_ip = self._get_local_ip()
        self.root.title(f"Aurora Client  |  {self.my_ip}")
        self.root.geometry(f"{_px(660)}x{_px(900)}")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

        self.log_entries = []
        self._log_shown  = 0
        self.running     = True
        self.quit_app    = root.destroy   # main() 에서 트레이 종료까지 포함한 함수로 교체
        self._delete_rule_widgets = []   # [{frame, path_var, ext_var, days_var, ena_var}, ...]
        self._cached_all_ips = self._get_local_ips()
        self._cached_ips_time = time.time()

        self._build_ui()
        self._start_threads()

    def _get_local_ip(self):
        try:
            first = config.get("server_ips", ["192.168.11.7"])[0]
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((first, SERVER_PORT))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "알 수 없음"

    # ── 위젯 헬퍼 ────────────────────────
    def _separator(self, parent, **pack_opts):
        tk.Frame(parent, bg=self.BORDER, height=max(1, _px(1))).pack(fill="x", **pack_opts)

    def _card(self, parent):
        """흰 배경 + 얇은 테두리 프레임"""
        return tk.Frame(parent, bg=self.PANEL, highlightthickness=1,
                        highlightbackground=self.BORDER, highlightcolor=self.BORDER)

    def _tip(self, widget, text):
        """위젯에 마우스 오버 설명(hint) 부착. widget 을 그대로 반환"""
        _Tooltip(widget, text)
        return widget

    def _section_title(self, parent, text, bg=None):
        lbl = tk.Label(parent, text=text, font=self.FONT_TITLE,
                       fg=self.TEXT, bg=bg or self.PANEL)
        if text in self.LABEL_HINTS:
            self._tip(lbl, self.LABEL_HINTS[text])
        return lbl

    def _entry(self, parent, var, width, font=None):
        return tk.Entry(parent, textvariable=var, font=font or self.FONT_INPUT,
                        bg=self.PANEL, fg=self.TEXT, insertbackground=self.TEXT,
                        disabledbackground=self.BG, disabledforeground=self.DISABLED_TEXT,
                        relief="flat", bd=_px(6),   # flat 테두리가 안쪽 여백 역할
                        highlightthickness=1,
                        highlightbackground=self.INPUT_BORDER, highlightcolor=self.BLUE,
                        width=width)

    def _field_label(self, parent, text, width=12):
        lbl = tk.Label(parent, text=text, width=width, anchor="w",
                       font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL)
        if text in self.LABEL_HINTS:
            self._tip(lbl, self.LABEL_HINTS[text])
        return lbl

    def _running_style(self, is_running: bool) -> dict:
        """실행중/중지 표시용 label 옵션"""
        return {"text": "● 실행중" if is_running else "○ 중지",
                "fg": self.GREEN if is_running else self.TEXT_DIM}

    def _badge_bg(self, fg: str) -> str:
        """글자색으로 배지 배경색 결정"""
        if fg == self.GREEN:
            return self.OK_BG
        if fg == self.RED:
            return self.ERR_BG
        if fg == self.YELLOW:
            return self.WARN_BG
        return self.IDLE_BG

    def _set_badge(self, lbl, text: str, fg: str):
        """상태 라벨을 배지(글자색 + 옅은 배경)로 표시"""
        lbl.config(text=text, fg=fg, bg=self._badge_bg(fg))

    def _make_badge(self, parent, text: str, fg: str):
        lbl = tk.Label(parent, font=self.FONT_SMALL, padx=_px(8), pady=_px(2))
        self._set_badge(lbl, text, fg)
        return lbl

    def _load_logo(self, height: int):
        """assets/ubisam_logo.png 를 높이에 맞춰 로드 (실패 시 None)"""
        try:
            img = PILImage.open(_resource_path(os.path.join("assets", "ubisam_logo.png"))).convert("RGBA")
            width = max(1, round(img.width * height / img.height))
            img = img.resize((width, height), PILImage.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _log_panel(self, parent, title: str, height: int = None):
        """다크 로그 카드. Text 위젯 반환"""
        card = self._card(parent)
        if height:
            card.configure(height=_px(height))
            card.pack_propagate(False)
            card.pack(fill="x", side="bottom", pady=(_px(14), 0))
        else:
            card.pack(fill="both", expand=True)
        self._section_title(card, title).pack(anchor="w", padx=_px(20), pady=(_px(16), _px(10)))
        dark = tk.Frame(card, bg=self.LOG_BG)
        dark.pack(fill="both", expand=True, padx=_px(20), pady=(0, _px(16)))
        text = tk.Text(dark, font=self.FONT_MONO, bg=self.LOG_BG, fg=self.LOG_FG,
                       insertbackground=self.LOG_FG, relief="flat", bd=0,
                       padx=_px(12), pady=_px(10), spacing1=_px(2), state="disabled", wrap="word",
                       highlightthickness=0)
        sb = ttk.Scrollbar(dark, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        return text

    # ── UI ───────────────────────────────
    def _build_ui(self):
        # 헤더
        header = tk.Frame(self.root, bg=self.PANEL, height=_px(52))
        header.pack(fill="x")
        header.pack_propagate(False)
        self._logo_img = self._load_logo(_px(18))
        name_padx = (_px(18), 0)
        if self._logo_img:
            tk.Label(header, image=self._logo_img, bg=self.PANEL).pack(side="left", padx=(_px(18), _px(12)))
            tk.Frame(header, bg=self.BORDER, width=max(1, _px(1)), height=_px(18)).pack(side="left", padx=(0, _px(12)))
            name_padx = (0, 0)
        tk.Label(header, text="Aurora Client",
                 font=self.FONT_HEAD, fg=self.TEXT, bg=self.PANEL).pack(side="left", padx=name_padx)
        self._tip(tk.Label(header, text=f"v{APP_VERSION}", font=self.FONT_SMALL,
                           fg=self.TEXT_DIM, bg=self.IDLE_BG, padx=_px(6), pady=_px(1)),
                  self.LABEL_HINTS["버전"]).pack(side="left", padx=(_px(8), 0))
        self._tip(tk.Label(header, text=self.my_ip,
                           font=self.FONT_INPUT, fg=self.TEXT_DIM, bg=self.PANEL),
                  "이 PC의 IP 입니다. 첫 번째 등록 서버로 연결할 때 사용되는 로컬 주소이며, 프로그램 시작 시 한 번 확인합니다. 확인하지 못하면 '알 수 없음'으로 표시됩니다."
                  ).pack(side="right", padx=_px(18))

        # 탭바
        tabbar = tk.Frame(self.root, bg=self.PANEL)
        tabbar.pack(fill="x")
        self._separator(self.root)

        content = tk.Frame(self.root, bg=self.BG)
        content.pack(fill="both", expand=True)

        self._tab_frames = []
        self._tab_labels = []
        self._tab_lines  = []
        for i, title in enumerate(["설정", "프로세스 관리", "자동 삭제", "반입/반출"]):
            cell = tk.Frame(tabbar, bg=self.PANEL)
            cell.pack(side="left", padx=(_px(8) if i == 0 else 0, 0))
            lbl = tk.Label(cell, text=title, font=self.FONT_TAB, fg=self.TEXT_DIM,
                           bg=self.PANEL, padx=_px(16), pady=_px(10), cursor="hand2")
            lbl.pack()
            line = tk.Frame(cell, bg=self.PANEL, height=_px(2))
            line.pack(fill="x")
            lbl.bind("<Button-1>", lambda e, n=i: self._select_tab(n))
            self._tip(lbl, self.TAB_HINTS[i])
            self._tab_labels.append(lbl)
            self._tab_lines.append(line)
            self._tab_frames.append(tk.Frame(content, bg=self.BG, padx=_px(18), pady=_px(16)))

        self._build_main_tab(self._tab_frames[0])
        self._build_process_tab(self._tab_frames[1])
        self._build_delete_tab(self._tab_frames[2])
        self._build_export_tab(self._tab_frames[3])
        self._select_tab(0)

    def _select_tab(self, index: int):
        for i, frame in enumerate(self._tab_frames):
            selected = (i == index)
            if selected:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
            self._tab_labels[i].config(
                fg=self.BLUE if selected else self.TEXT_DIM,
                font=self.FONT_TAB_ON if selected else self.FONT_TAB)
            self._tab_lines[i].config(bg=self.BRAND if selected else self.PANEL)

    def _build_main_tab(self, parent):
        # 상태 카드
        st = self._card(parent)
        st.pack(fill="x", pady=(0, _px(14)))
        st_in = tk.Frame(st, bg=self.PANEL)
        st_in.pack(fill="x", padx=_px(20), pady=_px(16))
        for c in range(3):
            st_in.columnconfigure(c, weight=1, uniform="stat")
        stat_hints = {
            "server_lbl": "이 PC가 등록된 모든 서버 IP 의 서버 포트(기본 9000)로 3초마다 상태를 보고한 결과입니다. 전부 성공하면 '전체 연결됨', 일부만 성공하면 '성공 수/전체 연결됨', 하나도 성공하지 못하면 '연결 안됨'으로 표시합니다.",
            "aurora_lbl": "실행 경로에 지정한 exe 와 이름이 같은 프로세스가 켜져 있는지 3초마다 확인한 결과입니다. 이 값이 서버로 보고됩니다.",
            "cmd_lbl": "서버에서 마지막으로 받은 START/STOP 명령입니다. 명령은 수신 포트(기본 9001)로 들어오며, 프로그램을 시작한 뒤 받은 명령이 없으면 '—' 입니다.",
        }
        for col, (label, attr) in enumerate([("서버 연결", "server_lbl"),
                                              ("Aurora 상태", "aurora_lbl"),
                                              ("마지막 명령", "cmd_lbl")]):
            tile = tk.Frame(st_in, bg=self.PANEL_ALT, highlightthickness=1,
                            highlightbackground=self.BORDER, highlightcolor=self.BORDER)
            tile.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else _px(8), 0))
            self._tip(tk.Label(tile, text=label, anchor="w", font=self.FONT,
                               fg=self.TEXT_DIM, bg=self.PANEL_ALT),
                      stat_hints[attr]).pack(fill="x", padx=_px(12), pady=(_px(10), 0))
            lbl = self._tip(tk.Label(tile, text="—", anchor="w",
                                     font=self.FONT_VALUE, fg=self.TEXT, bg=self.PANEL_ALT),
                            stat_hints[attr])
            lbl.pack(fill="x", padx=_px(12), pady=(_px(4), _px(10)))
            setattr(self, attr, lbl)

        # 서버 카드 (좌: 서버 IP 목록 / 우: 핑 모드)
        srv = self._card(parent)
        srv.pack(fill="x", pady=(0, _px(14)))
        srv_in = tk.Frame(srv, bg=self.PANEL)
        srv_in.pack(fill="x", padx=_px(20), pady=_px(16))

        srv_left = tk.Frame(srv_in, bg=self.PANEL)
        srv_left.pack(side="left", fill="both", expand=True)
        self._section_title(srv_left, "서버 IP 목록").pack(anchor="w", pady=(0, _px(10)))

        # 리스트박스
        lb_frame = tk.Frame(srv_left, bg=self.PANEL, padx=_px(8), pady=_px(6),
                            highlightthickness=1, highlightbackground=self.INPUT_BORDER,
                            highlightcolor=self.BLUE)
        lb_frame.pack(fill="x")
        self.server_lb = tk.Listbox(lb_frame, font=self.FONT_MONO,
                                    bg=self.PANEL, fg=self.TEXT,
                                    selectbackground=self.BLUE, selectforeground=self.WHITE,
                                    relief="flat", bd=0, highlightthickness=0,
                                    height=4, activestyle="none")
        self.server_lb.pack(side="left", fill="x", expand=True)
        lb_sb = ttk.Scrollbar(lb_frame, orient="vertical", command=self.server_lb.yview)
        self.server_lb.configure(yscrollcommand=lb_sb.set)
        lb_sb.pack(side="left", fill="y")
        for ip in config.get("server_ips", []):
            self.server_lb.insert("end", ip)

        # 추가/삭제 버튼
        srv_btn = tk.Frame(srv_left, bg=self.PANEL)
        srv_btn.pack(fill="x", pady=(_px(10), 0))
        self.server_ip_input = tk.StringVar()
        ip_entry = self._entry(srv_btn, self.server_ip_input, 15, self.FONT_MONO)
        ip_entry.pack(side="left", fill="x", expand=True, ipady=_px(1), padx=(0, _px(8)))
        ip_entry.bind("<Return>", lambda e: self._add_server_ip())
        self._button(srv_btn, "+ 추가", "primary", self._add_server_ip).pack(side="left", padx=(0, _px(8)))
        self._button(srv_btn, "− 삭제", "danger",  self._del_server_ip).pack(side="left")

        tk.Frame(srv_in, bg=self.ROW_LINE, width=max(1, _px(1))).pack(side="left", fill="y", padx=_px(20))

        pf = tk.Frame(srv_in, bg=self.PANEL)
        pf.pack(side="left", fill="y")
        tk.Frame(pf, bg=self.PANEL, width=_px(220), height=max(1, _px(1))).pack()
        hdr_row = tk.Frame(pf, bg=self.PANEL)
        hdr_row.pack(fill="x", pady=(0, _px(8)))
        self._section_title(hdr_row, "핑 모드").pack(side="left")
        seg = tk.Frame(hdr_row, bg=self.PANEL)
        seg.pack(side="right")
        self._ping_mode_client = config.get("ping_mode", "manual")
        self.ping_manual_btn_c = self._button(seg, "Manual", "secondary", lambda: None, small=True)
        self.ping_manual_btn_c.pack(side="left")
        self.ping_auto_btn_c = self._button(seg, "Auto", "secondary", lambda: None, small=True)
        self.ping_auto_btn_c.pack(side="left")
        self._apply_ping_mode_style()

        self._ping_result_labels = {}
        for i, ip in enumerate(config.get("server_ips", [])):
            if i > 0:
                tk.Frame(pf, bg=self.ROW_LINE, height=max(1, _px(1))).pack(fill="x")
            row = tk.Frame(pf, bg=self.PANEL)
            row.pack(fill="x", pady=_px(6))
            self._tip(tk.Label(row, text=ip, anchor="w", font=self.FONT_MONO,
                               fg=self.TEXT, bg=self.PANEL),
                      "핑 응답 시간을 측정하는 서버 IP 입니다."
                      ).pack(side="left")
            lbl = self._tip(tk.Label(row, text="—", anchor="e",
                                     font=self.FONT_MONO_BOLD, fg=self.TEXT, bg=self.PANEL),
                            "서버까지의 핑 응답 시간입니다. 10ms 미만은 초록, 50ms 미만은 노랑, 그 이상은 빨강이며 응답이 없으면 '시간초과'(빨강)입니다. 측정 전에는 '—' 입니다.")
            lbl.pack(side="right")
            self._ping_result_labels[ip] = lbl

        # 설정 카드
        cfg = self._card(parent)
        cfg.pack(fill="x", pady=(0, _px(14)))

        self.server_port_var = tk.StringVar(value=str(config.get("server_port", 9000)))
        self.listen_port_var = tk.StringVar(value=str(config.get("listen_port", 9001)))
        row_p = tk.Frame(cfg, bg=self.PANEL)
        row_p.pack(fill="x", padx=_px(20), pady=(_px(16), 0))
        self._field_label(row_p, "서버 포트", width=8).pack(side="left", padx=(0, _px(8)))
        self._entry(row_p, self.server_port_var, 10).pack(side="left", ipady=_px(1), padx=(0, _px(28)))
        self._field_label(row_p, "수신 포트", width=8).pack(side="left", padx=(0, _px(8)))
        self._entry(row_p, self.listen_port_var, 10).pack(side="left", ipady=_px(1), padx=(0, _px(8)))
        self._button(row_p, "적용", "primary", self._apply_ports).pack(side="left")

        # ── PC 시작 시 자동 실행 ─────────────
        auto_row = tk.Frame(cfg, bg=self.PANEL)
        auto_row.pack(fill="x", padx=_px(20), pady=(_px(10), 0))
        self._field_label(auto_row, "자동 시작", width=8).pack(side="left", padx=(0, _px(8)))
        self._autostart_btn = self._button(
            auto_row, "OFF ○", "secondary", self._toggle_autostart)
        self._style_autostart_btn(autostart_enabled())
        self._autostart_btn.pack(side="left")
        self._tip(tk.Label(auto_row, text="  PC 시작 시 클라이언트 자동 실행",
                           font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL),
                  self.LABEL_HINTS["자동 시작"]).pack(side="left")

        # ── 버전 / 업데이트 ─────────────────
        upd_row = tk.Frame(cfg, bg=self.PANEL)
        upd_row.pack(fill="x", padx=_px(20), pady=(_px(10), _px(16)))
        self._field_label(upd_row, "버전", width=8).pack(side="left", padx=(0, _px(8)))
        tk.Label(upd_row, text=f"v{APP_VERSION}", font=self.FONT_BOLD,
                 fg=self.TEXT, bg=self.PANEL).pack(side="left", padx=(0, _px(12)))
        self._update_btn = self._button(upd_row, "최신 버전 확인", "secondary",
                                        lambda: self._check_update(manual=True))
        self._update_btn.pack(side="left")
        self._update_lbl = self._tip(tk.Label(upd_row, text="", anchor="w", font=self.FONT,
                                              fg=self.TEXT_DIM, bg=self.PANEL),
                                     self.LABEL_HINTS["버전"])
        self._update_lbl.pack(side="left", fill="x", expand=True, padx=(_px(10), 0))

        # 수동 제어
        mf = self._card(parent)
        mf.pack(fill="x", pady=(0, _px(14)))
        mf_in = tk.Frame(mf, bg=self.PANEL)
        mf_in.pack(fill="x", padx=_px(20), pady=_px(16))
        self._section_title(mf_in, "수동 제어").pack(side="left")
        self._tip(tk.Label(mf_in, text=" (긴급용)", font=self.FONT,
                           fg=self.TEXT_DIM, bg=self.PANEL),
                  self.LABEL_HINTS["수동 제어"]).pack(side="left")
        self._button(mf_in, "■  프로그램 종료", "stop",
                     self._manual_stop).pack(side="right")
        self._button(mf_in, "▶  프로그램 실행", "start",
                     self._manual_start).pack(side="right", padx=(0, _px(8)))

        # 로그
        self.log_text = self._log_panel(parent, "LOG")

    # ── 프로세스 관리 탭 ─────────────────
    def _build_process_tab(self, parent):
        # ── 실행 프로그램 섹션 ────────────────
        run_panel = self._card(parent)
        run_panel.pack(fill="x", pady=(0, _px(14)))

        self._section_title(run_panel, "실행 프로그램").pack(anchor="w", padx=_px(20), pady=(_px(16), _px(10)))

        path_row = tk.Frame(run_panel, bg=self.PANEL)
        path_row.pack(fill="x", padx=_px(20), pady=(0, _px(16)))

        self._field_label(path_row, "실행 경로", width=8).pack(side="left", padx=(0, _px(8)))

        self.aurora_path_var = tk.StringVar(
            value=config.get("aurora_path", DEFAULT_CONFIG["aurora_path"]))

        self._button(path_row, "적용", "primary", self._apply_aurora_path).pack(side="right")

        def _browse_exe():
            path = filedialog.askopenfilename(
                title="실행 파일 선택", filetypes=[("실행 파일", "*.exe"), ("모든 파일", "*.*")])
            if path:
                self.aurora_path_var.set(path)

        self._button(path_row, "찾아보기", "secondary", _browse_exe).pack(side="right", padx=(0, _px(8)))
        self._entry(path_row, self.aurora_path_var, 30
                    ).pack(side="left", fill="x", expand=True, ipady=_px(1), padx=(0, _px(8)))

        # ── 프로세스 종료 섹션 ────────────────
        kill_panel = self._card(parent)
        kill_panel.pack(fill="both", expand=True)

        top = tk.Frame(kill_panel, bg=self.PANEL)
        top.pack(fill="x", padx=_px(20), pady=(_px(16), _px(12)))
        self._section_title(top, "프로세스 종료 목록").pack(anchor="w", pady=(0, _px(10)))

        add_row = tk.Frame(top, bg=self.PANEL)
        add_row.pack(fill="x")
        self._kill_input_var = tk.StringVar()
        kill_entry = self._entry(add_row, self._kill_input_var, 28)
        kill_entry.pack(side="left", ipady=_px(1), padx=(0, _px(8)))
        kill_entry.bind("<Return>", lambda e: self._add_kill_process())
        self._tip(tk.Label(add_row, text="예: notepad.exe",
                           font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.PANEL),
                  "입력 예시입니다. 종료할 프로세스의 exe 이름을 입력하고 '+ 추가'(또는 Enter)를 누르면 목록에 저장됩니다. .exe 를 생략하면 자동으로 붙습니다."
                  ).pack(side="left", padx=(0, _px(10)))
        self._button(add_row, "− 삭제", "danger", self._del_kill_process).pack(side="right")
        self._button(add_row, "+ 추가", "primary", self._add_kill_process).pack(side="right", padx=(0, _px(8)))

        # 하단 버튼
        btn_bar = tk.Frame(kill_panel, bg=self.PANEL)
        btn_bar.pack(side="bottom", fill="x")
        self._separator(btn_bar)
        btn_row = tk.Frame(btn_bar, bg=self.PANEL)
        btn_row.pack(fill="x", padx=_px(20), pady=_px(16))
        self._button(btn_row, "선택 프로세스 종료", "stop",
                     self._kill_selected).pack(side="left", padx=(0, _px(8)))
        self._button(btn_row, "전체 종료", "danger",
                     self._kill_all).pack(side="left")
        self._button(btn_row, "새로고침", "secondary",
                     self._refresh_kill_status).pack(side="right")

        # 열 헤더
        col_hdr = tk.Frame(kill_panel, bg=self.PANEL_ALT)
        col_hdr.pack(fill="x")
        self._configure_kill_grid(col_hdr)
        self._tip(tk.Label(col_hdr, text="프로세스 이름", anchor="w", font=self.FONT_SMALL,
                           fg=self.TEXT_DIM, bg=self.PANEL_ALT, pady=_px(8)),
                  "종료 대상으로 등록한 프로세스의 exe 이름입니다. 왼쪽 체크박스로 선택한 항목이 '선택 프로세스 종료'와 '− 삭제'의 대상입니다."
                  ).grid(row=0, column=1, sticky="w", padx=(0, _px(8)))
        self._tip(tk.Label(col_hdr, text="상태", anchor="w", font=self.FONT_SMALL,
                           fg=self.TEXT_DIM, bg=self.PANEL_ALT, pady=_px(8)),
                  "해당 이름의 프로세스가 지금 실행 중인지 보여 줍니다(● 실행중 / ○ 중지). 자동으로 갱신되지 않으며 목록을 다시 만들 때, 종료 직후, '새로고침'을 누를 때 갱신됩니다."
                  ).grid(row=0, column=2, sticky="w")

        self._kill_rows_frame = tk.Frame(kill_panel, bg=self.PANEL)
        self._kill_rows_frame.pack(fill="both", expand=True)

        self._rebuild_kill_rows()

    def _configure_kill_grid(self, frame):
        """프로세스 표 열 규격 (헤더·행 공통)"""
        frame.columnconfigure(0, minsize=_px(44))
        frame.rowconfigure(0, minsize=_px(38))
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(2, minsize=_px(110))

    def _rebuild_kill_rows(self):
        for w in self._kill_rows_frame.winfo_children():
            w.destroy()
        self._kill_check_vars  = {}
        self._kill_status_lbls = {}

        procs = config.get("kill_processes", [])
        if not procs:
            self._tip(tk.Label(self._kill_rows_frame, text="등록된 프로세스가 없습니다.",
                               font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.PANEL,
                               pady=_px(12)),
                      "종료할 프로세스가 아직 없습니다. 위 입력란에 exe 이름을 넣고 '+ 추가'를 누르세요."
                      ).pack(anchor="w", padx=_px(12))
            return

        running_map = get_running_processes(procs)
        for name in procs:
            tk.Frame(self._kill_rows_frame, bg=self.ROW_LINE, height=max(1, _px(1))).pack(fill="x")
            row = tk.Frame(self._kill_rows_frame, bg=self.PANEL)
            row.pack(fill="x")
            self._configure_kill_grid(row)

            var = tk.BooleanVar(value=False)
            self._kill_check_vars[name] = var
            tk.Checkbutton(row, variable=var, bg=self.PANEL,
                           activebackground=self.PANEL, selectcolor=self.PANEL, bd=0
                           ).grid(row=0, column=0, padx=(_px(14), 0))

            self._tip(tk.Label(row, text=name, anchor="w",
                               font=self.FONT_INPUT, fg=self.TEXT, bg=self.PANEL),
                      "종료 대상으로 등록된 프로세스 이름입니다. 종료하면 이 이름의 프로세스가 모두 강제 종료됩니다."
                      ).grid(row=0, column=1, sticky="w", padx=(0, _px(8)))

            status_lbl = self._make_badge(row, "", self.TEXT_DIM)
            self._tip(status_lbl, "이 프로세스의 실행 여부입니다(● 실행중 / ○ 중지). '새로고침'을 누르거나 종료한 직후에 갱신됩니다.")
            self._set_badge(status_lbl, **self._running_style(running_map.get(name, False)))
            status_lbl.grid(row=0, column=2, sticky="w")
            self._kill_status_lbls[name] = status_lbl

    def _add_kill_process(self):
        name = self._kill_input_var.get().strip()
        if not name:
            return
        if not name.lower().endswith(".exe"):
            name += ".exe"
        procs = config.setdefault("kill_processes", [])
        if name in procs:
            messagebox.showwarning("중복", f"{name} 은 이미 등록되어 있습니다.")
            return
        procs.append(name)
        save_config(config)
        self._kill_input_var.set("")
        self._rebuild_kill_rows()

    def _del_kill_process(self):
        selected = [n for n, v in self._kill_check_vars.items() if v.get()]
        if not selected:
            messagebox.showinfo("안내", "삭제할 프로세스를 체크하세요.")
            return
        procs = config.get("kill_processes", [])
        for name in selected:
            if name in procs:
                procs.remove(name)
        save_config(config)
        self._rebuild_kill_rows()

    def _kill_selected(self):
        selected = [n for n, v in self._kill_check_vars.items() if v.get()]
        if not selected:
            messagebox.showinfo("안내", "종료할 프로세스를 체크하세요.")
            return
        def _do():
            results = []
            for name in selected:
                killed = kill_process_by_name(name)
                results.append(f"{name}: {killed}개 종료" if killed else f"{name}: 실행중 아님")
            self.add_log("[프로세스 종료] " + " / ".join(results))
            self.root.after(0, self._refresh_kill_status)
        threading.Thread(target=_do, daemon=True).start()

    def _kill_all(self):
        procs = config.get("kill_processes", [])
        if not procs:
            return
        def _do():
            results = []
            for name in procs:
                killed = kill_process_by_name(name)
                results.append(f"{name}: {killed}개" if killed else f"{name}: 없음")
            self.add_log("[전체 종료] " + " / ".join(results))
            self.root.after(0, self._refresh_kill_status)
        threading.Thread(target=_do, daemon=True).start()

    def _refresh_kill_status(self):
        procs = config.get("kill_processes", [])
        if not procs:
            return
        def _do():
            running_map = get_running_processes(procs)
            def _upd():
                for name, lbl in self._kill_status_lbls.items():
                    self._set_badge(lbl, **self._running_style(running_map.get(name, False)))
            self.root.after(0, _upd)
        threading.Thread(target=_do, daemon=True).start()

    # ── 자동 삭제 탭 ─────────────────────
    # 규칙 표 열 규격 (최소폭, 가중치) — 헤더·행 공통
    _DELETE_COLS = [(30, 0), (0, 1), (62, 0), (50, 0), (86, 0), (50, 0), (49, 0), (37, 0)]

    def _configure_delete_grid(self, frame):
        for i, (minsize, weight) in enumerate(self._DELETE_COLS):
            frame.columnconfigure(i, minsize=_px(minsize), weight=weight)

    def _build_delete_tab(self, parent):
        # ── 프로그레스바 (다음 실행까지) ─────
        pb_frame = self._card(parent)
        pb_frame.pack(fill="x", pady=(0, _px(14)))

        pb_top = tk.Frame(pb_frame, bg=self.PANEL)
        pb_top.pack(fill="x", padx=_px(20), pady=_px(16))
        countdown_hint = ("자동 삭제가 실행될 때까지 남은 시간(시:분:초)입니다. 자동 삭제는 날짜가 바뀔 때(자정) 실행됩니다. "
                          "'지금 실행'을 누르면 표시만 24시간으로 다시 시작하며 실제 자정 실행 시각은 바뀌지 않습니다.")
        self._tip(tk.Label(pb_top, text="다음 자동 삭제까지",
                           font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL),
                  countdown_hint).pack(side="left")
        self.delete_countdown_lbl = self._tip(tk.Label(pb_top, text="—",
                 font=self.FONT_COUNTDOWN, fg=self.BLUE, bg=self.PANEL), countdown_hint)
        self.delete_countdown_lbl.pack(side="left", padx=_px(12))

        self.delete_progress = ttk.Progressbar(pb_top, orient="horizontal",
                                               length=_px(200), maximum=86400, value=0,
                                               style="Countdown.Horizontal.TProgressbar")
        self.delete_progress.pack(side="left", fill="x", expand=True)

        # 로그 (하단 고정 — 규칙 카드보다 먼저 pack)
        self.delete_log_text = self._log_panel(parent, "삭제 LOG", height=170)

        # ── 규칙 카드 ────────────────────────
        rules_card = self._card(parent)
        rules_card.pack(fill="both", expand=True)

        tool = tk.Frame(rules_card, bg=self.PANEL)
        tool.pack(fill="x", padx=_px(20), pady=_px(16))
        self._section_title(tool, "자동 삭제 규칙").pack(side="left")
        self._button(tool, "+ 추가", "primary", self._add_delete_rule).pack(side="right")

        # 하단: 지금 실행 버튼
        bot_bar = tk.Frame(rules_card, bg=self.PANEL)
        bot_bar.pack(side="bottom", fill="x")
        self._separator(bot_bar)
        bot = tk.Frame(bot_bar, bg=self.PANEL)
        bot.pack(fill="x", padx=_px(20), pady=_px(16))
        self._button(bot, "지금 실행", "primary", self._run_delete_now).pack(side="left")
        self.delete_status_lbl = self._tip(tk.Label(bot, text="",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL),
                 "'지금 실행'의 진행 상태입니다. 실행 중에는 '실행 중...', 끝나면 '✓ 완료'로 표시됩니다. 삭제 내역은 '삭제 LOG'에서 확인하세요.")
        self.delete_status_lbl.pack(side="left", padx=_px(10))

        # 컬럼 헤더 (스크롤바 폭만큼 우측 여백)
        col = tk.Frame(rules_card, bg=self.PANEL_ALT)
        col.pack(fill="x")
        col_in = tk.Frame(col, bg=self.PANEL_ALT)
        col_in.pack(side="left", fill="x", expand=True, padx=_px(14))
        self._configure_delete_grid(col_in)
        delete_col_hints = {
            "ON": "규칙 사용 여부입니다. 체크를 해제하면 '적용됨'이어도 자동 삭제와 '지금 실행'에서 이 규칙은 건너뜁니다.",
            "폴더 경로": "삭제 대상을 찾을 폴더입니다. 이 폴더 바로 아래 항목만 검사하며 하위 폴더 안의 파일은 보지 않습니다. 폴더가 없으면 로그에 '폴더 없음'만 남깁니다.",
            "확장자": "파일 삭제 모드에서 삭제할 파일의 확장자입니다(예: log, 앞의 점은 생략 가능). '폴더삭제'를 체크하면 사용하지 않아 입력이 비활성화됩니다.",
            "기한(일)": "이 일수 이상 지난 항목을 삭제합니다. 파일은 최종 수정일, 폴더는 폴더 이름에 든 날짜를 기준으로 하며, 0 이하이면 삭제하지 않습니다.",
            "폴더삭제": "체크하면 폴더 경로 바로 아래의 하위 폴더를 통째로 삭제합니다. 폴더 이름에 yyyyMMdd(8자리) 또는 yyMMdd(6자리) 날짜가 있어야 하며, 날짜가 없는 폴더와 현재 Aurora 경로가 들어 있는 폴더는 건드리지 않습니다.",
        }
        for i, text in enumerate(["ON", "폴더 경로", "확장자", "기한(일)", "폴더삭제"]):
            self._tip(tk.Label(col_in, text=text, anchor="w", font=self.FONT_SMALL,
                               fg=self.TEXT_DIM, bg=self.PANEL_ALT, pady=_px(8)),
                      delete_col_hints[text]).grid(row=0, column=i, sticky="w", padx=_px(2))

        # 규칙 목록 (스크롤)
        list_outer = tk.Frame(rules_card, bg=self.PANEL)
        list_outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(list_outer, bg=self.PANEL, highlightthickness=0)
        sb2 = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        tk.Frame(col, bg=self.PANEL_ALT, width=sb2.winfo_reqwidth(), height=max(1, _px(1))).pack(side="left")

        self._delete_rules_frame = tk.Frame(canvas, bg=self.PANEL)
        rules_win = canvas.create_window((0, 0), window=self._delete_rules_frame, anchor="nw")
        self._delete_rules_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
            lambda e: canvas.itemconfigure(rules_win, width=e.width))
        self._delete_canvas = canvas

        # 저장된 규칙 복원
        for rule in config.get("auto_delete_rules", []):
            self._add_delete_rule(rule)

        # 카운트다운 시작 (자정까지 남은 초로 초기화)
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        next_midnight = midnight + timedelta(days=1)
        self._countdown_val = int((next_midnight - now).total_seconds())
        self._tick_countdown()

    def _add_delete_rule(self, rule: dict = None):
        rule = rule or {"path": "", "ext": "", "minutes": 30,
                        "enabled": True, "applied": False, "last_deleted": "",
                        "type": "file"}
        bg   = self.PANEL

        # 규칙 1개 = item(구분선 + 행 1 + 행 2)
        item = tk.Frame(self._delete_rules_frame, bg=bg)
        item.pack(fill="x")
        tk.Frame(item, bg=self.ROW_LINE, height=max(1, _px(1))).pack(fill="x")

        # ── 행 1: 컨트롤 ─────────────────────
        row1 = tk.Frame(item, bg=bg)
        row1.pack(fill="x", padx=_px(14), pady=(_px(10), 0))
        self._configure_delete_grid(row1)

        ena_var        = tk.BooleanVar(value=rule.get("enabled", True))
        path_var       = tk.StringVar(value=rule.get("path", ""))
        ext_var        = tk.StringVar(value=rule.get("ext",  ""))
        min_var        = tk.StringVar(value=str(rule.get("minutes", 30)))
        folder_del_var = tk.BooleanVar(value=rule.get("type", "file") == "folder")

        tk.Checkbutton(row1, variable=ena_var, bg=bg,
                       activebackground=bg, selectcolor=self.PANEL, bd=0
                       ).grid(row=0, column=0, padx=_px(2))

        path_cell = tk.Frame(row1, bg=bg)
        path_cell.grid(row=0, column=1, sticky="ew", padx=_px(2))
        self._entry(path_cell, path_var, 1, self.FONT_MONO
                    ).pack(side="left", fill="x", expand=True, ipady=_px(1))

        def _browse(_pv=path_var):
            folder = filedialog.askdirectory(parent=self.root)
            if folder:
                _pv.set(folder)
        self._button(path_cell, "…", "secondary", _browse, small=True).pack(side="left")

        ext_entry = self._entry(row1, ext_var, 1, self.FONT_MONO)
        ext_entry.grid(row=0, column=2, sticky="ew", padx=_px(2), ipady=_px(1))

        self._entry(row1, min_var, 1, self.FONT_MONO
                    ).grid(row=0, column=3, sticky="ew", padx=_px(2), ipady=_px(1))

        tk.Checkbutton(row1, text="폴더삭제", variable=folder_del_var,
                       font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=bg,
                       activebackground=bg, selectcolor=self.PANEL, bd=0, padx=_px(6)
                       ).grid(row=0, column=4, sticky="w", padx=_px(2))

        # ── 적용 버튼 + 상태 표시 ─────────────
        applied_init = rule.get("applied", False)
        status_lbl = self._tip(tk.Label(row1, font=self.FONT_SMALL, padx=_px(6), pady=_px(2)),
                               "'적용됨' 규칙만 자동 삭제가 실행됩니다. 폴더 경로·확장자(파일 모드)·폴더삭제를 바꾸면 '미적용'으로 돌아가므로 '적용'을 다시 눌러야 합니다.")
        self._set_badge(status_lbl,
                        "적용됨" if applied_init else "미적용",
                        self.GREEN if applied_init else self.RED)
        status_lbl.grid(row=0, column=5, sticky="w", padx=_px(2))

        widget_info = {
            "item": item,
            "row1": row1, "path": path_var, "ext": ext_var,
            "minutes": min_var, "enabled": ena_var,
            "folder_del": folder_del_var, "ext_entry": ext_entry,
            "applied": applied_init,
            "last_deleted": rule.get("last_deleted", ""),
            "last_deleted_path": rule.get("path", "").strip(),
            "status_lbl": status_lbl,
            "last_del_lbl": None,
        }

        def _apply_rule(_wi=widget_info):
            path      = _wi["path"].get().strip()
            is_folder = _wi["folder_del"].get()
            ext       = _wi["ext"].get().strip()
            if not path:
                messagebox.showwarning("알림", "폴더 경로를 입력하세요.", parent=self.root)
                return
            if not is_folder and not ext:
                messagebox.showwarning("알림", "확장자를 입력하거나 폴더삭제를 체크하세요.", parent=self.root)
                return
            _wi["applied"] = True
            self._set_badge(_wi["status_lbl"], "적용됨", self.GREEN)
            self._save_delete_rules()

        self._button(row1, "적용", "primary", _apply_rule, small=True
                     ).grid(row=0, column=6, sticky="ew", padx=_px(2))

        def _del_rule(_wi=widget_info):
            self._delete_rule_widgets.remove(_wi)
            _wi["row1"].destroy()
            if _wi["last_del_lbl"] and _wi["last_del_lbl"].winfo_exists():
                _wi["last_del_lbl"].master.destroy()
            _wi["item"].destroy()
            self._save_delete_rules()
            self._refresh_delete_canvas()

        self._button(row1, "✕", "danger", _del_rule, small=True
                     ).grid(row=0, column=7, sticky="ew", padx=_px(2))

        # ── 행 2: 마지막 삭제 일자 ───────────
        row2 = tk.Frame(item, bg=bg)
        row2.pack(fill="x")
        last_del_text = rule.get("last_deleted", "")
        last_del_lbl = self._tip(tk.Label(row2,
                                text=f"마지막 삭제: {last_del_text}" if last_del_text else "마지막 삭제: —",
                                font=self.FONT_MONO_SMALL, fg=self.TEXT_DIM, bg=bg),
                                "이 규칙이 마지막으로 항목을 삭제한 시각입니다. 삭제된 항목이 없던 실행은 기록하지 않으며, 폴더 경로를 바꾸면 초기화됩니다.")
        last_del_lbl.pack(anchor="w", padx=(_px(48), _px(14)), pady=(_px(4), _px(10)))
        widget_info["last_del_lbl"] = last_del_lbl
        last_del_lbl.master = row2

        # 폴더삭제 체크 → ext 활성/비활성 연동
        def _toggle_folder_del(*_, _wi=widget_info):
            is_folder = _wi["folder_del"].get()
            _wi["ext_entry"].config(
                state="disabled" if is_folder else "normal",
                fg=self.TEXT_DIM if is_folder else self.TEXT
            )
            if _wi["applied"]:
                _wi["applied"] = False
                self._set_badge(_wi["status_lbl"], "미적용", self.RED)
            self._save_delete_rules()

        # 경로 변경 시 적용 상태 초기화
        def _on_key_change(*_, _wi=widget_info):
            if _wi["applied"]:
                _wi["applied"] = False
                self._set_badge(_wi["status_lbl"], "미적용", self.RED)
                self._save_delete_rules()

        path_var.trace_add("write", _on_key_change)
        # 파일 모드일 때만 ext 변경이 재적용 필요 (폴더 모드에선 ext 비활성)
        def _on_ext_change(*_, _wi=widget_info):
            if not _wi["folder_del"].get():
                _on_key_change()
        ext_var.trace_add("write", _on_ext_change)

        min_var.trace_add("write",        lambda *_: self._save_delete_rules())
        ena_var.trace_add("write",        lambda *_: self._save_delete_rules())
        folder_del_var.trace_add("write", _toggle_folder_del)

        # 초기 상태 반영 (폴더삭제 모드로 로드 시 ext 비활성)
        if folder_del_var.get():
            ext_entry.config(state="disabled", fg=self.TEXT_DIM)

        self._delete_rule_widgets.append(widget_info)
        self._refresh_delete_canvas()

    def _refresh_delete_canvas(self):
        self._delete_canvas.update_idletasks()
        self._delete_canvas.configure(
            scrollregion=self._delete_canvas.bbox("all"))

    def _save_delete_rules(self):
        rules = []
        for w in self._delete_rule_widgets:
            try:
                minutes = int(w["minutes"].get())
            except ValueError:
                minutes = 0
            # 마지막 삭제 일자는 행 자신의 값을 보존 (경로가 바뀌면 초기화)
            path = w["path"].get().strip()
            last_deleted = w["last_deleted"] if path == w["last_deleted_path"] else ""
            w["last_deleted"]      = last_deleted
            w["last_deleted_path"] = path
            rules.append({
                "path":         path,
                "ext":          w["ext"].get().strip().lstrip("."),
                "minutes":      minutes,
                "enabled":      w["enabled"].get(),
                "applied":      w["applied"],
                "last_deleted": last_deleted,
                "type":         "folder" if w.get("folder_del") and w["folder_del"].get() else "file",
            })
        config["auto_delete_rules"] = rules
        save_config(config)

    def _delete_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.add_log(msg)   # add_log 가 시각을 붙이므로 원문 전달
        self.delete_log_text.config(state="normal")
        self.delete_log_text.insert("end", line + "\n")
        self.delete_log_text.see("end")
        self.delete_log_text.config(state="disabled")

    def _tick_countdown(self):
        if not self.running:
            return
        v = self._countdown_val
        if hasattr(self, "delete_progress"):
            elapsed = 86400 - v
            self.delete_progress["value"] = elapsed
            # 남은 시간을 시:분:초로 표시
            h, rem = divmod(v, 3600)
            m, s   = divmod(rem, 60)
            self.delete_countdown_lbl.config(text=f"{h:02d}:{m:02d}:{s:02d}")
        self._countdown_val -= 1
        if self._countdown_val < 0:
            self._countdown_val = 86400
        self.root.after(1000, self._tick_countdown)

    def _reset_countdown(self):
        """자동 삭제 실행 직후 카운트다운 리셋"""
        self._countdown_val = 86400

    def _run_delete_now(self):
        self.delete_status_lbl.config(text="실행 중...", fg=self.YELLOW)
        def run():
            run_auto_delete(self._delete_log)
            self.root.after(0, self._reset_countdown)
            self.root.after(0, lambda: self.delete_status_lbl.config(
                text="✓ 완료", fg=self.GREEN))
        threading.Thread(target=run, daemon=True).start()

    # ── 버튼 헬퍼 ────────────────────────
    # ── 반출 탭 ──────────────────────────
    def _build_export_tab(self, parent):
        self._exp_connected = False
        self._exp_devices   = []      # [{ip, label}] — 콤보박스 순서와 동일
        self._exp_items     = {}      # {treeview iid: entry}
        self._exp_path      = ""      # "" = 루트(허용 경로 목록)
        self._exp_roots     = []
        self._exp_cancel    = threading.Event()
        self._exp_busy      = False
        self._exp_relay_host = ""     # 중계 PC 호스트명 (/info 응답)

        # 로그 (하단 고정 — 먼저 pack)
        self.export_log_text = self._log_panel(parent, "반입/반출 LOG", height=140)

        self._exp_mode      = "export"  # "export"(장비 → 내 PC) / "import"(내 PC → 장비)
        self._imp_sources   = []      # 반입할 내 PC 파일·폴더 경로

        # ── 저장/실행 카드 (하단) ──
        act = self._card(parent)
        act.pack(side="bottom", fill="x", pady=(_px(14), 0))
        # 반출 모드: 저장 폴더
        row = self._exp_save_row = tk.Frame(act, bg=self.PANEL)
        row.pack(fill="x", padx=_px(20), pady=(_px(16), _px(10)))
        self._field_label(row, "저장 폴더", width=8).pack(side="left", padx=(0, _px(8)))
        self._exp_save_var = tk.StringVar(value=config.get("export_save_dir"))
        self._button(row, "열기", "secondary", self._export_open_save_dir).pack(side="right")
        self._button(row, "찾아보기", "secondary",
                     self._export_browse_save_dir).pack(side="right", padx=(0, _px(8)))
        self._entry(row, self._exp_save_var, 30).pack(
            side="left", fill="x", expand=True, ipady=_px(1), padx=(0, _px(8)))

        # 반입 모드: 보낼 항목 + 반입 위치 (모드 전환 시 저장 폴더 줄과 교체)
        imp = self._imp_frame = tk.Frame(act, bg=self.PANEL)
        ir1 = tk.Frame(imp, bg=self.PANEL)
        ir1.pack(fill="x", pady=(0, _px(6)))
        self._field_label(ir1, "보낼 항목", width=8).pack(side="left", anchor="n", padx=(0, _px(8)))
        lb_wrap = tk.Frame(ir1, bg=self.PANEL, highlightthickness=1, highlightbackground=self.INPUT_BORDER)
        lb_wrap.pack(side="left", fill="x", expand=True)
        self._imp_list = tk.Listbox(lb_wrap, height=2, font=self.FONT_INPUT, relief="flat", bd=0,
                                    selectmode="extended", fg=self.TEXT, bg=self.PANEL,
                                    selectbackground=self.SELECT_BG, selectforeground=self.TEXT,
                                    highlightthickness=0, activestyle="none")
        lsb = ttk.Scrollbar(lb_wrap, orient="vertical", command=self._imp_list.yview)
        self._imp_list.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self._imp_list.pack(side="left", fill="both", expand=True, padx=_px(4), pady=_px(2))
        ir2 = tk.Frame(imp, bg=self.PANEL)
        ir2.pack(fill="x")
        self._field_label(ir2, "반입 위치", width=8).pack(side="left", padx=(0, _px(8)))
        self._button(ir2, "빼기", "secondary", self._import_remove, small=True).pack(side="right")
        self._button(ir2, "폴더 추가", "secondary", self._import_add_dir, small=True).pack(side="right", padx=(0, _px(6)))
        self._button(ir2, "파일 추가", "secondary", self._import_add_files, small=True).pack(side="right", padx=(0, _px(6)))
        self._imp_dest_lbl = tk.Label(ir2, text="", anchor="w", font=self.FONT_INPUT,
                                      fg=self.TEXT_DIM, bg=self.PANEL)
        self._imp_dest_lbl.pack(side="left", fill="x", expand=True)

        row2 = self._exp_act_row = tk.Frame(act, bg=self.PANEL)
        row2.pack(fill="x", padx=_px(20), pady=(0, _px(16)))
        self._exp_go_btn = self._button(row2, "⬇ 반출", "primary", self._transfer_start)
        self._exp_go_btn.pack(side="right")
        self._exp_cancel_btn = self._button(row2, "취소", "danger", self._export_cancel)
        self._exp_cancel_btn.pack(side="right", padx=(0, _px(8)))
        self._exp_cancel_btn.config(state="disabled")
        self._exp_progress = ttk.Progressbar(row2, orient="horizontal", maximum=1, value=0,
                                             style="Countdown.Horizontal.TProgressbar")
        self._exp_progress.pack(side="top", fill="x", padx=(0, _px(12)), pady=(_px(4), _px(4)))
        self._exp_prog_lbl = self._tip(tk.Label(row2, text="대기", anchor="w",
                                      font=self.FONT_MONO_SMALL, fg=self.TEXT_DIM, bg=self.PANEL),
                                      "반입·반출 진행 상황입니다. 처리한 파일 수/전체 파일 수, 처리한 용량/전체 용량, 전송 속도, 실패한 파일 수(있을 때)를 보여 줍니다. 파일은 4개까지 동시에 주고받습니다.")
        self._exp_prog_lbl.pack(side="top", fill="x")

        # ── 모드 전환 (최상단) ──
        mode = tk.Frame(parent, bg=self.BG)
        mode.pack(fill="x", pady=(0, _px(10)))
        self._exp_mode_btns = {
            "export": self._button(mode, "⬇ 반출 모드", "secondary", lambda: self._set_transfer_mode("export")),
            "import": self._button(mode, "⬆ 반입 모드", "secondary", lambda: self._set_transfer_mode("import")),
        }
        self._exp_mode_btns["export"].pack(side="left")
        self._exp_mode_btns["import"].pack(side="left", padx=(_px(6), 0))
        self._tip(self._exp_mode_btns["export"], "반출 모드: 선택한 장비의 파일·폴더를 내 PC 저장 폴더로 가져옵니다.")
        self._tip(self._exp_mode_btns["import"], "반입 모드: 내 PC 의 파일·폴더를 선택한 장비의 현재 폴더로 보냅니다. 같은 이름의 파일은 확인 후 덮어씁니다.")
        self._exp_mode_lbl = tk.Label(mode, text="", font=self.FONT, fg=self.TEXT_DIM, bg=self.BG)
        self._exp_mode_lbl.pack(side="left", padx=(_px(12), 0))

        # ── 연결 카드 (상단) ──
        conn = self._card(parent)
        conn.pack(fill="x", pady=(0, _px(14)))
        r1 = tk.Frame(conn, bg=self.PANEL)
        r1.pack(fill="x", padx=_px(20), pady=(_px(16), _px(10)))
        self._field_label(r1, "중계 PC", width=8).pack(side="left", padx=(0, _px(8)))
        self._exp_relay_var = tk.StringVar(value=config.get("export_relay"))
        self._entry(r1, self._exp_relay_var, 18).pack(side="left", ipady=_px(1), padx=(0, _px(8)))
        self._button(r1, "적용", "primary", self._export_apply_relay).pack(side="left")
        self._exp_conn_lbl = self._tip(self._make_badge(r1, "○ 확인 중", self.TEXT_DIM),
                                       "중계 PC 연결 상태입니다. 5초마다 확인하며 연결되면 '● 연결됨 (호스트 이름)'과 함께 장비를 자동 검색합니다. '키 불일치'는 PC마다 export_key 설정이 달라 거부된 경우입니다. 연결되지 않으면 장비 검색·반출이 비활성화됩니다.")
        self._exp_conn_lbl.pack(side="right")

        r2 = tk.Frame(conn, bg=self.PANEL)
        r2.pack(fill="x", padx=_px(20), pady=(0, _px(10)))
        self._field_label(r2, "장비", width=8).pack(side="left", padx=(0, _px(8)))
        self._exp_dev_var = tk.StringVar()
        self._exp_dev_combo = ttk.Combobox(r2, textvariable=self._exp_dev_var, state="readonly",
                                           font=self.FONT_INPUT, style="Export.TCombobox")
        self._exp_dev_combo.pack(side="left", fill="x", expand=True, padx=(0, _px(8)))
        self._exp_dev_combo.bind("<<ComboboxSelected>>",
                                 lambda e: (self._export_update_route(), self._export_nav(""),
                                            self._export_fill("", [])))
        self._button(r2, "이름", "secondary", self._export_rename_device).pack(side="right")
        self._exp_scan_btn = self._button(r2, "장비 검색", "secondary",
                                          lambda: self._export_discover(force=True))
        self._exp_scan_btn.pack(side="right", padx=(0, _px(8)))

        # 접속 경로: 내 PC → 중계 PC → 대상 장비 (중계 PC 에서 실행 중이면 2대)
        r3 = tk.Frame(conn, bg=self.PANEL)
        r3.pack(fill="x", padx=_px(20), pady=(0, _px(16)))
        self._field_label(r3, "접속 경로", width=8).pack(side="left", padx=(0, _px(8)))
        self._exp_route_frame = tk.Frame(r3, bg=self.PANEL)
        self._exp_route_frame.pack(side="left", fill="x", expand=True)

        # ── 파일 탐색 카드 ──
        browse = self._card(parent)
        browse.pack(fill="both", expand=True)
        nav = tk.Frame(browse, bg=self.PANEL)
        nav.pack(fill="x", padx=_px(20), pady=(_px(16), _px(10)))
        self._button(nav, "↑ 상위", "secondary", self._export_up, small=True).pack(side="left", padx=(0, _px(8)))
        self._button(nav, "새로고침", "secondary",
                     lambda: self._export_nav(self._exp_path), small=True).pack(side="right")
        self._exp_path_lbl = self._tip(tk.Label(nav, text="장비를 선택하세요", anchor="w",
                                      font=self.FONT_INPUT, fg=self.TEXT_DIM, bg=self.PANEL),
                                      "선택한 장비에서 현재 보고 있는 경로입니다. 처음에는 접근 가능한 허용 경로(D 드라이브·바탕화면)가 나오고, 그 밖의 경로는 열 수 없습니다. 폴더를 더블클릭하면 안으로 들어갑니다.")
        self._exp_path_lbl.pack(side="left", fill="x", expand=True)

        tree_wrap = tk.Frame(browse, bg=self.PANEL)
        tree_wrap.pack(fill="both", expand=True, padx=_px(20), pady=(0, _px(16)))
        self._exp_tree = ttk.Treeview(tree_wrap, columns=("size", "mtime"),
                                      style="Export.Treeview", selectmode="extended")
        self._exp_tree.heading("#0", text="이름", anchor="w")
        self._exp_tree.heading("size", text="크기", anchor="e")
        self._exp_tree.heading("mtime", text="수정일", anchor="w")
        self._exp_tree.column("#0", width=_px(300), anchor="w")
        self._exp_tree.column("size", width=_px(80), anchor="e", stretch=False)
        self._exp_tree.column("mtime", width=_px(130), anchor="w", stretch=False)
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self._exp_tree.yview)
        self._exp_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._exp_tree.pack(fill="both", expand=True)
        self._exp_tree.bind("<Double-1>", self._export_on_double)

        self._export_set_connected(False)
        self._set_transfer_mode("export")

    # 반입/반출 모드 전환
    def _set_transfer_mode(self, mode: str):
        if self._exp_busy:
            return
        self._exp_mode = mode
        for m, b in self._exp_mode_btns.items():
            self._style_segment(b, m == mode)
        is_imp = mode == "import"
        self._exp_mode_lbl.config(text="내 PC → 장비 (선택한 장비의 현재 폴더로 보내기)" if is_imp
                                  else "장비 → 내 PC (선택한 파일·폴더를 저장 폴더로 가져오기)")
        self._exp_go_btn.config(text="⬆ 반입" if is_imp else "⬇ 반출")
        self._exp_tree.config(selectmode="browse" if is_imp else "extended")
        if is_imp:
            self._exp_save_row.pack_forget()
            self._imp_frame.pack(fill="x", padx=_px(20), pady=(_px(16), _px(10)), before=self._exp_act_row)
        else:
            self._imp_frame.pack_forget()
            self._exp_save_row.pack(fill="x", padx=_px(20), pady=(_px(16), _px(10)), before=self._exp_act_row)
        self._import_update_dest()

    def _transfer_start(self):
        (self._import_start if self._exp_mode == "import" else self._export_start)()

    # 접속 경로 표시
    def _export_relay_is_self(self) -> bool:
        relay = config.get("export_relay", "").strip()
        return relay in ("127.0.0.1", "localhost") or relay in self._cached_all_ips

    def _export_route_nodes(self) -> list:
        """[(역할, 이름, IP)] — 실제 요청이 거치는 PC 순서"""
        relay  = config.get("export_relay", "").strip()
        me     = (socket.gethostname(), self.my_ip)
        target = None
        idx = self._exp_dev_combo.current()
        if 0 <= idx < len(self._exp_devices):
            d = self._exp_devices[idx]
            name = config.get("export_devices", {}).get(d["ip"]) or d["host"] or d["ip"]
            target = (name, d["ip"])
        if self._export_relay_is_self():
            # 이 PC 가 곧 중계 PC (예: LHS 에서 CCW 반출) → 2대
            nodes = [("내 PC · 중계", me[0], relay)]
        else:
            nodes = [("내 PC", *me),
                     ("중계 PC", self._exp_relay_host or "—", relay or "미설정")]
        if target and target[1] != relay:
            nodes.append(("대상 장비", *target))
        return nodes

    def _export_route_text(self) -> str:
        return " → ".join(f"{n} ({ip})" for _, n, ip in self._export_route_nodes())

    def _export_update_route(self):
        f = self._exp_route_frame
        for w in f.winfo_children():
            w.destroy()
        nodes = self._export_route_nodes()
        tip = ("반출 요청이 실제로 거치는 PC 순서입니다. 내 PC → 중계 PC(LHS) → 대상 장비(CCW 등) 순으로 연결되며, "
               "중계 PC 에서 직접 실행하면 중계 PC → 대상 장비 2대만 표시됩니다. 대상으로 중계 PC 자신을 고르면 중계 PC 까지만 표시됩니다. "
               "화살표 색: 초록=연결됨, 빨강=연결 안됨, 회색=장비 미선택.")
        for i, (role, name, ip) in enumerate(nodes):
            if i:
                # 첫 구간(내 PC→중계)은 연결 감시 결과, 이후 구간은 중계 PC 가 찾은 장비이므로 연결 상태를 따름
                self._tip(tk.Label(f, text="→", font=self.FONT_HEAD, bg=self.PANEL,
                                   fg=self.GREEN if self._exp_connected else self.RED),
                          tip).pack(side="left", padx=_px(6))
            fg = self.BLUE if role == "대상 장비" else (self.GREEN if self._exp_connected else self.TEXT_DIM)
            chip = tk.Frame(f, bg=self._badge_bg(fg) if fg != self.BLUE else self.SELECT_BG,
                            highlightthickness=1, highlightbackground=self.BORDER)
            chip.pack(side="left")
            name = name if len(name) <= 16 else name[:15] + "…"
            for n, (text, font, color) in enumerate(((role, self.FONT_SMALL, self.TEXT_DIM),
                                                      (name, self.FONT_BOLD, fg),
                                                      (ip, self.FONT_MONO_SMALL, self.TEXT_DIM))):
                self._tip(tk.Label(chip, text=text, font=font, fg=color, bg=chip.cget("bg"),
                                   anchor="w"), tip).pack(fill="x", padx=_px(8),
                                                          pady=(_px(3) if n == 0 else 0, _px(3) if n == 2 else 0))
        if nodes[-1][0] != "대상 장비" and not (self._export_target()):
            self._tip(tk.Label(f, text="  → 장비 선택", font=self.FONT, fg=self.DISABLED_TEXT,
                               bg=self.PANEL), tip).pack(side="left")
        self._tip(tk.Label(f, text=f"총 {len(nodes)}대", font=self.FONT_SMALL, fg=self.TEXT_DIM,
                           bg=self.PANEL), tip).pack(side="right")

    # 로그 / 상태 표시
    def _export_log(self, msg: str):
        def _do():
            ts = datetime.now().strftime("%H:%M:%S")
            self.export_log_text.config(state="normal")
            self.export_log_text.insert("end", f"[{ts}] {msg}\n")
            self.export_log_text.see("end")
            self.export_log_text.config(state="disabled")
        self.root.after(0, _do)

    def _export_set_connected(self, ok: bool, hostname: str = ""):
        self._exp_connected = ok
        if ok:
            self._set_badge(self._exp_conn_lbl, f"● 연결됨  {hostname}", self.GREEN)
        else:   # hostname 자리에 실패 사유(예: 키 불일치)가 올 수 있음
            self._set_badge(self._exp_conn_lbl, f"○ {hostname or '연결 안됨'}", self.RED)
        state = "normal" if ok else "disabled"
        self._exp_scan_btn.config(state=state)
        self._exp_go_btn.config(state=state if not self._exp_busy else "disabled")
        self._exp_dev_combo.config(state="readonly" if ok else "disabled")
        if ok:
            self._exp_relay_host = hostname
        self._export_update_route()

    # 중계 PC 요청
    def _export_request(self, route: str, params: dict = None, target: str = None,
                        timeout=(3, 30), method: str = "GET", **kw):
        """중계 PC 경유 요청 (반입은 PUT). target 이 중계 PC 자신이면 via 없이 직접 처리"""
        relay  = config.get("export_relay", "").strip()
        port   = config.get("export_peer_port", 9001)
        params = dict(params or {})
        if target and target != relay:
            params["via"] = target
        headers = {EXPORT_KEY_HEADER: config.get("export_key", "")}
        headers.update(kw.pop("headers", {}))
        return requests.request(method, f"http://{relay}:{port}{route}", params=params,
                                headers=headers, timeout=timeout, **kw)

    @staticmethod
    def _export_error(r) -> str:
        try:
            return r.json().get("error", f"HTTP {r.status_code}")
        except Exception:
            return f"HTTP {r.status_code}"

    def _export_monitor_loop(self):
        """중계 PC 연결 감시 — 연결되는 순간 장비 자동 검색"""
        while self.running:
            ok, host = False, ""
            if config.get("export_relay", "").strip():
                try:
                    r = self._export_request("/info", timeout=(2, 3))
                    if r.ok:
                        ok, host = True, r.json().get("hostname", "")
                    elif r.status_code == 403:
                        host = "키 불일치"
                except Exception:
                    pass
            was = self._exp_connected
            # 상태가 바뀔 때만 갱신 (경로 칩을 5초마다 다시 그리지 않도록)
            if (ok, host) != getattr(self, "_exp_last_state", None):
                self._exp_last_state = (ok, host)
                self.root.after(0, lambda o=ok, h=host: self._export_set_connected(o, h))
            if ok and not was:
                self._export_log(f"중계 PC 연결됨 ({host})")
                self._export_discover(force=False, relay_host=host)
            elif was and not ok:
                self._export_log("중계 PC 연결 끊김")
            time.sleep(5)

    def _export_apply_relay(self):
        config["export_relay"] = self._exp_relay_var.get().strip()
        save_config(config)
        self._exp_connected = False
        self._exp_relay_host = ""
        self._exp_last_state = None
        self._export_update_route()
        self._export_log(f"중계 PC 변경: {config['export_relay']}")

    def _export_device_label(self, ip: str, hostname: str) -> str:
        name = config.get("export_devices", {}).get(ip) or hostname or ip
        return f"{name}  ({ip})"

    def _export_discover(self, force: bool, relay_host: str = ""):
        def _do():
            self._export_log("장비 검색 중…")
            try:
                r = self._export_request("/discover", {"refresh": "1" if force else "0"},
                                         timeout=(3, 90))
                if not r.ok:
                    self._export_log(f"장비 검색 실패: {self._export_error(r)}")
                    return
                found = r.json()
            except Exception as e:
                self._export_log(f"장비 검색 실패: {e}")
                return
            relay = config.get("export_relay", "").strip()
            devices = [{"ip": relay, "host": relay_host, "relay": True}]
            devices += [{"ip": d["ip"], "host": d["hostname"], "relay": False}
                        for d in found if d["ip"] != relay]
            self._export_log(f"장비 {len(devices)}대 발견")
            self.root.after(0, lambda: self._export_set_devices(devices))
        threading.Thread(target=_do, daemon=True).start()

    def _export_set_devices(self, devices: list):
        cur_ip = self._export_target()
        self._exp_devices = devices
        for d in devices:
            d["label"] = self._export_device_label(d["ip"], d["host"]) + ("  [중계]" if d["relay"] else "")
        self._exp_dev_combo.config(values=[d["label"] for d in devices])
        for i, d in enumerate(devices):
            if d["ip"] == cur_ip:
                self._exp_dev_combo.current(i)
                break
        self._export_update_route()

    def _export_target(self) -> str | None:
        idx = self._exp_dev_combo.current()
        if 0 <= idx < len(self._exp_devices):
            return self._exp_devices[idx]["ip"]
        return None

    def _export_rename_device(self):
        ip = self._export_target()
        if not ip:
            messagebox.showinfo("안내", "장비를 먼저 선택하세요.")
            return
        names = config.setdefault("export_devices", {})
        name = simpledialog.askstring("장비 이름", f"{ip} 의 표시 이름 (비우면 호스트명)",
                                      initialvalue=names.get(ip, ""), parent=self.root)
        if name is None:
            return
        if name.strip():
            names[ip] = name.strip()
        else:
            names.pop(ip, None)
        save_config(config)
        self._export_set_devices(self._exp_devices)

    # 탐색
    @staticmethod
    def _fmt_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024

    def _export_nav(self, path: str):
        target = self._export_target()
        if not target:
            return
        def _do():
            try:
                if path:
                    r = self._export_request("/fs/list", {"path": path}, target)
                else:
                    r = self._export_request("/fs/roots", target=target)
                if not r.ok:
                    self._export_log(f"목록 조회 실패: {self._export_error(r)}")
                    return
                items = r.json()
            except Exception as e:
                self._export_log(f"목록 조회 실패: {e}")
                return
            if not path:
                for it in items:
                    it.update(dir=True, size=0, mtime=None)
            self.root.after(0, lambda: self._export_fill(path, items))
        threading.Thread(target=_do, daemon=True).start()

    def _export_fill(self, path: str, items: list):
        self._exp_path = path
        if not path:
            self._exp_roots = [it["path"] for it in items]
        self._exp_path_lbl.config(text=path or "허용 경로 (D 드라이브 · 바탕화면 · 공용 바탕화면)",
                                  fg=self.TEXT if path else self.TEXT_DIM)
        self._exp_tree.delete(*self._exp_tree.get_children())
        self._exp_items = {}
        for it in items:
            mtime = datetime.fromtimestamp(it["mtime"]).strftime("%Y-%m-%d %H:%M") if it.get("mtime") else ""
            iid = self._exp_tree.insert("", "end",
                text=("📁 " if it["dir"] else "📄 ") + it["name"],
                values=("" if it["dir"] else self._fmt_size(it["size"]), mtime))
            self._exp_items[iid] = it
        self._import_update_dest()

    def _export_on_double(self, event):
        iid = self._exp_tree.identify_row(event.y)
        it = self._exp_items.get(iid)
        if it and it["dir"]:
            self._export_nav(it["path"])

    def _export_up(self):
        if not self._exp_path:
            return
        norm = os.path.normcase(os.path.normpath(self._exp_path))
        if any(norm == os.path.normcase(os.path.normpath(r)) for r in self._exp_roots):
            self._export_nav("")
        else:
            self._export_nav(os.path.dirname(self._exp_path.rstrip("\\")) or "")

    # 저장 폴더
    def _export_browse_save_dir(self):
        path = filedialog.askdirectory(title="저장 폴더 선택")
        if path:
            self._exp_save_var.set(os.path.normpath(path))

    def _export_open_save_dir(self):
        path = self._exp_save_var.get().strip()
        os.makedirs(path, exist_ok=True)
        os.startfile(path)

    # 반출 실행
    def _export_start(self):
        target = self._export_target()
        sel = [self._exp_items[i] for i in self._exp_tree.selection() if i in self._exp_items]
        if not target or not sel:
            messagebox.showinfo("안내", "장비와 반출할 파일/폴더를 선택하세요.")
            return
        save_dir = self._exp_save_var.get().strip()
        if not save_dir:
            messagebox.showinfo("안내", "저장 폴더를 입력하세요.")
            return
        config["export_save_dir"] = save_dir
        save_config(config)

        label = self._exp_dev_combo.get().replace("  [중계]", "")
        dest_root = os.path.join(save_dir, re.sub(r'[\\/:*?"<>|]+', "_", label).strip())
        self._export_log(f"경로: {self._export_route_text()}")
        self._exp_cancel.clear()
        self._exp_busy = True
        self._exp_go_btn.config(state="disabled")
        self._exp_cancel_btn.config(state="normal")
        threading.Thread(target=self._export_worker, args=(target, sel, dest_root),
                         daemon=True).start()

    def _export_cancel(self):
        self._exp_cancel.set()
        self._export_log("취소 요청됨")

    def _export_worker(self, target: str, sel: list, dest_root: str):
        # 1) 작업 목록 만들기 — 폴더는 하위 전체 조회
        jobs = []   # (원격 경로, 로컬 경로, 크기, 수정시각)
        try:
            for it in sel:
                if it["dir"]:
                    r = self._export_request("/fs/walk", {"path": it["path"]}, target,
                                             timeout=(3, 120))
                    if not r.ok:
                        raise RuntimeError(f"{it['name']}: {self._export_error(r)}")
                    tree = r.json()
                    base = os.path.join(dest_root, it["name"].rstrip(":\\") or "root")
                    os.makedirs(base, exist_ok=True)
                    for d in tree["dirs"]:
                        os.makedirs(os.path.join(base, d), exist_ok=True)
                    for f in tree["files"]:
                        jobs.append((os.path.join(it["path"], f["rel"]),
                                     os.path.join(base, f["rel"]), f["size"], f["mtime"]))
                else:
                    jobs.append((it["path"], os.path.join(dest_root, it["name"]),
                                 it["size"], it["mtime"]))
        except Exception as e:
            self._export_log(f"❌ 목록 준비 실패: {e}")
            return self.root.after(0, self._export_done)

        total = sum(j[2] for j in jobs) or 1
        prog  = {"done": 0, "files": 0, "fail": 0}
        lock  = threading.Lock()
        t0    = time.time()
        self._export_log(f"반출 시작: {len(jobs)}개 파일, {self._fmt_size(total)} → {dest_root}")

        def _one(job):
            remote, local, size, mtime = job
            if self._exp_cancel.is_set():
                return
            part = local + ".part"
            try:
                os.makedirs(os.path.dirname(local), exist_ok=True)
                start = os.path.getsize(part) if os.path.exists(part) else 0
                hdr = {"Range": f"bytes={start}-"} if 0 < start < size else {}
                with self._export_request("/fs/file", {"path": remote}, target,
                                          timeout=(3, 120), stream=True, headers=hdr) as r:
                    if r.status_code not in (200, 206):
                        raise RuntimeError(self._export_error(r))
                    if r.status_code == 200:
                        start = 0   # 서버가 이어받기를 안 한 경우 처음부터
                    with lock:
                        prog["done"] += start
                    with open(part, "ab" if start else "wb") as f:
                        for chunk in r.iter_content(EXPORT_CHUNK):
                            if self._exp_cancel.is_set():
                                return
                            f.write(chunk)
                            with lock:
                                prog["done"] += len(chunk)
                os.replace(part, local)
                if mtime:
                    os.utime(local, (mtime, mtime))
                with lock:
                    prog["files"] += 1
            except Exception as e:
                with lock:
                    prog["fail"] += 1
                self._export_log(f"❌ {remote}: {e}")

        # 2) 병렬 다운로드 + 진행률 갱신
        self._transfer_run(jobs, total, _one, prog, lock, t0)

        elapsed = time.time() - t0
        if self._exp_cancel.is_set():
            self._export_log("⏹ 반출 취소됨 (받던 파일은 .part 로 남아 다음에 이어받기)")
        else:
            self._export_log(f"✅ 반출 완료: {prog['files']}/{len(jobs)}개, "
                             f"{elapsed:.1f}초" + (f", 실패 {prog['fail']}" if prog["fail"] else ""))
        self.root.after(0, self._export_done)

    def _export_done(self):
        self._exp_busy = False
        self._exp_cancel_btn.config(state="disabled")
        self._exp_go_btn.config(state="normal" if self._exp_connected else "disabled")
        if self._exp_mode == "import" and self._exp_path:
            self._export_nav(self._exp_path)   # 반입한 파일이 보이도록 새로고침

    def _transfer_run(self, jobs: list, total: int, one, prog: dict, lock, t0: float):
        """반입·반출 공용: jobs 를 4개씩 병렬 처리하면서 0.3초마다 진행률 표시
        one(job) 이 prog["done"/"files"/"fail"] 을 lock 안에서 갱신"""
        def _ui():
            with lock:
                done, nf, nfail = prog["done"], prog["files"], prog["fail"]
            speed = done / max(time.time() - t0, 0.1)
            self._exp_progress.config(maximum=total, value=done)
            self._exp_prog_lbl.config(
                text=f"{nf}/{len(jobs)}개  ·  {self._fmt_size(done)} / {self._fmt_size(total)}"
                     f"  ·  {self._fmt_size(speed)}/s" + (f"  ·  실패 {nfail}" if nfail else ""))

        stop_ui = threading.Event()
        def _ui_loop():
            while not stop_ui.is_set():
                self.root.after(0, _ui)
                time.sleep(0.3)
        threading.Thread(target=_ui_loop, daemon=True).start()
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(one, jobs))
        stop_ui.set()
        self.root.after(0, _ui)

    # ── 반입 (내 PC → 장비) ──────────────
    def _import_update_dest(self):
        if not hasattr(self, "_imp_dest_lbl"):
            return
        if not self._export_target():
            text, fg = "장비를 선택하세요", self.TEXT_DIM
        elif not self._exp_path:
            text, fg = "아래에서 D 드라이브·바탕화면 안의 폴더로 이동하세요", self.TEXT_DIM
        else:
            name = self._exp_dev_combo.get().replace("  [중계]", "")
            text, fg = f"{name}  ›  {self._exp_path}", self.TEXT
        self._imp_dest_lbl.config(text=text, fg=fg)

    def _import_refresh_list(self):
        self._imp_list.delete(0, "end")
        for p in self._imp_sources:
            name = os.path.basename(p.rstrip("\\/")) or p
            self._imp_list.insert("end", ("📁 " if os.path.isdir(p) else "📄 ") + f"{name}   ({os.path.dirname(p.rstrip(chr(92) + '/'))})")

    def _import_add(self, paths):
        for p in paths:
            p = os.path.normpath(p)
            if p not in self._imp_sources:
                self._imp_sources.append(p)
        self._import_refresh_list()

    def _import_add_files(self):
        self._import_add(filedialog.askopenfilenames(title="반입할 파일 선택") or [])

    def _import_add_dir(self):
        path = filedialog.askdirectory(title="반입할 폴더 선택")
        if path:
            self._import_add([path])

    def _import_remove(self):
        sel = set(self._imp_list.curselection())
        if not sel:   # 선택이 없으면 전체 비우기
            sel = set(range(len(self._imp_sources)))
        self._imp_sources = [p for i, p in enumerate(self._imp_sources) if i not in sel]
        self._import_refresh_list()

    def _import_jobs(self, dest: str):
        """(파일 작업 [(로컬, 원격, 크기, 수정시각)], 만들 원격 폴더 [경로]) — 폴더는 하위 전체"""
        jobs, dirs = [], []
        for src in self._imp_sources:
            if os.path.isdir(src):
                name = os.path.basename(src.rstrip("\\/")) or (src[0] + "_drive")
                base = os.path.join(dest, name)
                dirs.append(base)
                for cur, _, fnames in os.walk(src):
                    rel = os.path.relpath(cur, src)
                    rbase = base if rel == "." else os.path.join(base, rel)
                    if rel != ".":
                        dirs.append(rbase)
                    for fn in fnames:
                        full = os.path.join(cur, fn)
                        try:
                            st = os.stat(full)
                        except OSError:
                            continue
                        jobs.append((full, os.path.join(rbase, fn), st.st_size, st.st_mtime))
            elif os.path.isfile(src):
                st = os.stat(src)
                jobs.append((src, os.path.join(dest, os.path.basename(src)), st.st_size, st.st_mtime))
        return jobs, dirs

    def _import_start(self):
        target = self._export_target()
        if not target:
            messagebox.showinfo("안내", "장비를 먼저 선택하세요.")
            return
        dest = self._exp_path
        if not dest:
            messagebox.showinfo("안내", "반입할 위치로 이동하세요.\n아래 파일 탐색에서 D 드라이브 또는 바탕화면 안의 폴더를 더블클릭하면 그 폴더가 반입 위치가 됩니다.")
            return
        if not self._imp_sources:
            messagebox.showinfo("안내", "'파일 추가' 또는 '폴더 추가'로 보낼 항목을 넣으세요.")
            return
        jobs, dirs = self._import_jobs(dest)
        if not jobs and not dirs:
            messagebox.showinfo("안내", "보낼 파일이 없습니다 (추가한 경로가 없어졌을 수 있습니다).")
            return
        total = sum(j[2] for j in jobs)
        name = self._exp_dev_combo.get().replace("  [중계]", "")
        if not messagebox.askyesno("반입 확인",
                f"{name}\n{dest}\n\n위 위치로 파일 {len(jobs)}개 ({self._fmt_size(total)}) 를 보냅니다.\n"
                "같은 이름의 파일이 있으면 덮어씁니다. 진행할까요?"):
            return
        self._export_log(f"경로: {self._export_route_text()}")
        self._exp_cancel.clear()
        self._exp_busy = True
        self._exp_go_btn.config(state="disabled")
        self._exp_cancel_btn.config(state="normal")
        threading.Thread(target=self._import_worker, args=(target, jobs, dirs, dest),
                         daemon=True).start()

    def _import_worker(self, target: str, jobs: list, dirs: list, dest: str):
        # 1) 폴더 먼저 (빈 폴더 포함)
        try:
            for d in dirs:
                r = self._export_request("/fs/mkdir", {"path": d}, target, method="PUT")
                if not r.ok:
                    raise RuntimeError(f"{d}: {self._export_error(r)}")
        except Exception as e:
            self._export_log(f"❌ 폴더 만들기 실패: {e}")
            return self.root.after(0, self._export_done)

        total = sum(j[2] for j in jobs) or 1
        prog  = {"done": 0, "files": 0, "fail": 0}
        lock  = threading.Lock()
        t0    = time.time()
        self._export_log(f"반입 시작: {len(jobs)}개 파일, {self._fmt_size(total)} → {dest}")

        def _one(job):
            local, remote, size, mtime = job
            if self._exp_cancel.is_set():
                return
            sent = [0]
            def _on_chunk(n):
                if self._exp_cancel.is_set():
                    raise IOError("취소됨")
                sent[0] += n
                with lock:
                    prog["done"] += n
            try:
                with open(local, "rb") as f:
                    r = self._export_request("/fs/upload", {"path": remote, "mtime": str(mtime)},
                                             target, timeout=(3, 120), method="PUT",
                                             data=_BodyReader(f, size, _on_chunk))
                if not r.ok:
                    raise RuntimeError(self._export_error(r))
                with lock:
                    prog["files"] += 1
            except Exception as e:
                with lock:
                    prog["done"] -= sent[0]   # 실패한 파일은 진행량에서 뺌
                if self._exp_cancel.is_set():
                    return
                with lock:
                    prog["fail"] += 1
                self._export_log(f"❌ {local}: {e}")

        # 2) 병렬 업로드 + 진행률 갱신
        self._transfer_run(jobs, total, _one, prog, lock, t0)

        elapsed = time.time() - t0
        if self._exp_cancel.is_set():
            self._export_log(f"⏹ 반입 취소됨 ({prog['files']}/{len(jobs)}개 완료, 보내던 파일은 장비에 남지 않음)")
        else:
            self._export_log(f"✅ 반입 완료: {prog['files']}/{len(jobs)}개, "
                             f"{elapsed:.1f}초" + (f", 실패 {prog['fail']}" if prog["fail"] else ""))
        self.root.after(0, self._export_done)

    def _button(self, parent, text, kind, cmd, small=False):
        """kind: primary / start / stop / secondary / danger
        base_bg / hover_bg 속성으로 hover 색을 관리 (상태 변경 시 함께 갱신)"""
        styles = {
            #             bg           hover        fg           border
            "primary":   (self.BLUE,      self.BLUE_H,      self.WHITE, self.BLUE),
            "start":     (self.BTN_START, self.BTN_START_H, self.WHITE, self.BTN_START),
            "stop":      (self.BTN_STOP,  self.BTN_STOP_H,  self.WHITE, self.BTN_STOP),
            "secondary": (self.PANEL,     self.BG,          self.TEXT,  self.INPUT_BORDER),
            "danger":    (self.PANEL,     self.BG,          self.RED,   self.DANGER_BORDER),
        }
        bg, hover, fg, border = styles[kind]
        # tk.Button 은 highlight 테두리가 그려지지 않으므로 1px 테두리용 Frame 으로 감싼다
        # (pack/grid/place 는 테두리 Frame 에 위임)
        border_frame = tk.Frame(parent, bg=border)
        b = tk.Button(border_frame, text=text,
                      font=self.FONT_SMALL if small else self.FONT_BOLD,
                      fg=fg, bg=bg, activeforeground=fg, activebackground=hover,
                      disabledforeground=self.DISABLED_TEXT,
                      bd=0, cursor="hand2", relief="flat", highlightthickness=0,
                      padx=_px(10) if small else _px(14), pady=_px(6) if small else _px(7),
                      command=cmd)
        b.pack(fill="both", expand=True, padx=1, pady=1)
        b.border_frame = border_frame
        b.pack   = border_frame.pack
        b.grid   = border_frame.grid
        b.place  = border_frame.place
        b.base_bg  = bg
        b.hover_bg = hover
        b.bind("<Enter>", lambda e: b.config(bg=b.hover_bg))
        b.bind("<Leave>", lambda e: b.config(bg=b.base_bg))
        return b

    def _style_button(self, btn, text, fg, bg, hover, border):
        """토글/세그먼트 버튼 표시 갱신 (hover 색도 함께)"""
        btn.base_bg  = bg
        btn.hover_bg = hover
        btn.border_frame.config(bg=border)
        btn.config(text=text, fg=fg, bg=bg, activeforeground=fg, activebackground=hover)

    def _style_segment(self, btn, selected: bool):
        """Manual/Auto 세그먼트: 선택=BLUE 배경·흰 글자, 비선택=흰 배경·TEXT_DIM"""
        if selected:
            self._style_button(btn, btn.cget("text"), self.WHITE, self.BLUE, self.BLUE_H, self.BLUE)
        else:
            self._style_button(btn, btn.cget("text"), self.TEXT_DIM, self.PANEL, self.BG, self.INPUT_BORDER)

    def _style_autostart_btn(self, enabled: bool):
        """자동 시작 토글: ON=초록 배지 느낌, OFF=보조 버튼"""
        if enabled:
            self._style_button(self._autostart_btn, "ON  ●", self.GREEN, self.OK_BG,
                               self.OK_HOVER, self.OK_BORDER)
        else:
            self._style_button(self._autostart_btn, "OFF ○", self.TEXT_DIM, self.PANEL,
                               self.BG, self.INPUT_BORDER)

    # ── 서버 IP 목록 관리 ────────────────
    def _add_server_ip(self):
        ip = self.server_ip_input.get().strip()
        if not ip:
            return
        existing = list(self.server_lb.get(0, "end"))
        if ip in existing:
            messagebox.showwarning("알림", f"{ip} 는 이미 등록되어 있습니다.", parent=self.root)
            return
        self.server_lb.insert("end", ip)
        self.server_ip_input.set("")
        config["server_ips"] = list(self.server_lb.get(0, "end"))
        save_config(config)
        self.add_log(f"서버 추가: {ip}")

    def _del_server_ip(self):
        sel = self.server_lb.curselection()
        if not sel:
            return
        ip = self.server_lb.get(sel[0])
        self.server_lb.delete(sel[0])
        config["server_ips"] = list(self.server_lb.get(0, "end"))
        save_config(config)
        self.add_log(f"서버 삭제: {ip}")

    def _apply_ports(self):
        try:
            sp = int(self.server_port_var.get().strip())
            lp = int(self.listen_port_var.get().strip())
            if not (1024 <= sp <= 65535 and 1024 <= lp <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("오류", "포트는 1024~65535 사이의 숫자여야 합니다.")
            return
        config["server_port"] = sp
        config["listen_port"] = lp
        save_config(config)
        self.add_log(f"포트 변경 — 서버:{sp} / 수신:{lp} (재시작 필요)")

    def _toggle_autostart(self):
        try:
            new_state = not autostart_enabled()
            set_autostart(new_state)
            # 사용자가 직접 끈 경우만 기억 → 다음 실행 때 다시 켜지지 않음
            config["autostart_off"] = not new_state
            save_config(config)
            self._style_autostart_btn(new_state)
            self.add_log(f"자동 시작 {'활성화' if new_state else '비활성화'}")
        except Exception as e:
            messagebox.showerror("오류", f"레지스트리 쓰기 실패:\n{e}")

    def _apply_aurora_path(self):
        path = self.aurora_path_var.get().strip()
        if not path: return
        config["aurora_path"] = path
        save_config(config)
        self.add_log(f"실행 경로 변경: {path}")

    # ── 로그 ─────────────────────────────
    def add_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_entries.append(f"[{ts}] {msg}")
        if len(self.log_entries) > 100:
            self.log_entries = self.log_entries[-100:]
        self.root.after(0, self._flush_log)

    def _flush_log(self):
        visible = self.log_entries[-40:]
        if len(visible) < self._log_shown:
            self._log_shown = 0
            self.log_text.config(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.config(state="disabled")
        new_lines = visible[self._log_shown:]
        if new_lines:
            self.log_text.config(state="normal")
            for line in new_lines:
                self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
            self._log_shown = len(visible)

    # ── 상태 보고 ────────────────────────
    @staticmethod
    def _get_local_ips():
        ips = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                addr = info[4][0]
                if not addr.startswith("127.") and ":" not in addr:
                    ips.add(addr)
        except Exception:
            pass
        try:
            for iface in psutil.net_if_addrs().values():
                for snic in iface:
                    if snic.family == 2 and not snic.address.startswith("127."):
                        ips.add(snic.address)
        except Exception:
            pass
        return list(ips)

    def _status_report_loop(self):
        while self.running:
            running = is_main_running()
            self.root.after(0, lambda r=running: self.aurora_lbl.config(
                **self._running_style(r)))

            ips = config.get("server_ips", [])
            ok_count = 0
            ok_lock = threading.Lock()
            now_t = time.time()
            if now_t - self._cached_ips_time > 30:
                self._cached_all_ips = self._get_local_ips()
                self._cached_ips_time = now_t
            all_ips = self._cached_all_ips
            aurora_path = config.get("aurora_path", "")

            def _report(ip, r=running):
                nonlocal ok_count
                try:
                    requests.post(
                        f"http://{ip}:{SERVER_PORT}/status",
                        json={"aurora_running": r, "all_ips": all_ips,
                              "aurora_path": aurora_path}, timeout=3)
                    with ok_lock:
                        ok_count += 1
                except Exception:
                    pass

            threads = [threading.Thread(target=_report, args=(ip,), daemon=True)
                       for ip in ips]
            for t in threads: t.start()
            # join에 timeout을 걸어 서버 무응답 시 루프가 영구 블로킹되지 않도록
            for t in threads: t.join(timeout=4)

            total = len(ips)
            if ok_count == 0:
                self.root.after(0, lambda: self.server_lbl.config(
                    text="○ 연결 안됨", fg=self.RED))
            elif ok_count < total:
                self.root.after(0, lambda o=ok_count, n=total: self.server_lbl.config(
                    text=f"● {o}/{n} 연결됨", fg=self.YELLOW))
            else:
                self.root.after(0, lambda n=total: self.server_lbl.config(
                    text=f"● 전체 연결됨 ({n})", fg=self.GREEN))

            time.sleep(STATUS_INTERVAL)

    # ── 자동 업데이트 (RemoteConfig\Update) ──
    def _check_update(self, manual: bool):
        """manual=False: 시작 시 자동 확인 (같은 버전 자동 시도는 1회만)
        manual=True : 버튼 — 결과를 팝업으로 알림"""
        def _do():
            res = aurora_update.check("client", APP_VERSION)
            self.root.after(0, lambda: self._update_lbl.config(
                text=res["msg"], fg=self.BLUE if res["status"] == "newer" else self.TEXT_DIM))
            if res["status"] != "newer":
                if manual:
                    self.root.after(0, lambda: messagebox.showinfo("업데이트", res["msg"], parent=self.root))
                return
            if not manual and aurora_update.auto_already_tried("client", res["version"]):
                self.add_log(f"⚠ v{res['version']} 자동 업데이트를 이미 시도했지만 버전이 그대로입니다 — Update 폴더의 exe 확인 필요")
                return
            self.add_log(f"⬆ {res['msg']} — 업데이트 후 재시작합니다")
            err = aurora_update.apply("client", res["exe"], res["version"])
            if err:
                self.add_log(f"업데이트 실패: {err}")
                if manual:
                    self.root.after(0, lambda: messagebox.showerror("업데이트 실패", err, parent=self.root))
                return
            self.root.after(0, self.quit_app)
        if manual:
            self._update_btn.config(state="disabled")
            self.root.after(2000, lambda: self._update_btn.config(state="normal"))
        threading.Thread(target=_do, daemon=True).start()

    def _manual_start(self):
        result = start_main()
        self.add_log(f"[수동] 실행 → {result}")

    def _manual_stop(self):
        result = stop_main()
        self.add_log(f"[수동] 종료 → {result}")

    def _update_ping_labels(self, results: dict):
        for ip, ms in results.items():
            lbl = self._ping_result_labels.get(ip)
            if lbl is None:
                continue
            if ms is None:
                lbl.config(text="시간초과", fg=self.RED)
            else:
                fg = self.GREEN if ms < 10 else self.YELLOW if ms < 50 else self.RED
                lbl.config(text=f"{ms}ms", fg=fg)

    def _apply_ping_mode_style(self):
        """현재 핑 모드 기준으로 Manual/Auto 세그먼트 표시 (초기화·변경 공용)"""
        is_auto = self._ping_mode_client == "auto"
        self._style_segment(self.ping_manual_btn_c, not is_auto)
        self._style_segment(self.ping_auto_btn_c, is_auto)

    def _on_ping_mode_changed(self, mode: str):
        self._ping_mode_client = mode
        self._apply_ping_mode_style()
        if mode == "auto":
            self._start_auto_ping_client()

    def _start_auto_ping_client(self):
        if getattr(self, "_auto_ping_client_running", False):
            return
        self._auto_ping_client_running = True
        def _loop():
            while self._ping_mode_client == "auto":
                ping_all_servers()
                time.sleep(5)
            self._auto_ping_client_running = False
        threading.Thread(target=_loop, daemon=True).start()

    def _start_threads(self):
        threading.Thread(target=run_command_server,       daemon=True).start()
        threading.Thread(target=self._status_report_loop, daemon=True).start()
        threading.Thread(target=auto_delete_watchdog,
                         args=(self._delete_log,),        daemon=True).start()
        threading.Thread(target=self._export_monitor_loop, daemon=True).start()
        setup_self_autostart()

        if config.get("ping_mode", "manual") == "auto":
            self._start_auto_ping_client()

        self.add_log(f"클라이언트 시작됨 (v{APP_VERSION})")
        # 켜질 때 업데이트 폴더 자동 확인 (화면이 뜬 뒤)
        self.root.after(3000, lambda: self._check_update(manual=False))
        server_addrs = ", ".join(f"{ip}:{SERVER_PORT}" for ip in config.get("server_ips", []))
        self.add_log(f"서버 IP: {server_addrs or '없음'}")
        self.add_log(f"명령 수신 포트: {LISTEN_PORT}")


# ─────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────
def load_tray_icon(ico_name):
    for base in [getattr(sys, "_MEIPASS", None), BASE_DIR]:
        if base is None:
            continue
        ico_path = os.path.join(base, ico_name)
        if os.path.exists(ico_path):
            return PILImage.open(ico_path).convert("RGBA").resize((64, 64))
    return PILImage.new("RGBA", (64, 64), (26, 82, 160, 255))


def main():
    _acquire_lock(19701)
    root = tk.Tk()

    # 창 아이콘 설정 (타이틀바 + 작업표시줄)
    _ico_search = [
        os.path.join(getattr(sys, "_MEIPASS", ""), "aurora_client.ico"),
        os.path.join(BASE_DIR, "aurora_client.ico"),
    ]
    for ico_path in _ico_search:
        if os.path.exists(ico_path):
            root.iconbitmap(ico_path)
            break

    _init_scale(root)
    AuroraClientApp.setup_styles()

    app = AuroraClientApp(root)

    tray_icon_img = load_tray_icon("aurora_client.ico")
    tray = None

    def show_window(icon=None, item=None):
        root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))

    def quit_app(icon=None, item=None):
        app.running = False
        if tray: tray.stop()
        root.after(0, root.destroy)

    def on_close():
        root.withdraw()
        app.add_log("트레이로 최소화됨 — 클라이언트는 계속 실행 중")

    app.quit_app = lambda: quit_app()

    tray_menu = pystray.Menu(
        pystray.MenuItem("Aurora Client 열기", show_window, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", quit_app),
    )
    tray = pystray.Icon("AuroraClient", tray_icon_img, "Aurora Client", tray_menu)
    threading.Thread(target=tray.run, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    if tray:
        tray.stop()


if __name__ == "__main__":
    main()
