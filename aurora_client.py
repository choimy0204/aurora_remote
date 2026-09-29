"""
Aurora Remote Controller - Client
=====================================
- 서버 IP를 GUI에서 동적 변경 가능
- 설정은 client_config.json 에 저장
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
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
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date, timedelta
import pystray
from PIL import Image as PILImage
from PIL import ImageTk

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

def _main_exe_name() -> str:
    """실행/감시 대상 exe 이름 (aurora_path 설정에서 추출)"""
    path = config.get("aurora_path", DEFAULT_CONFIG["aurora_path"])
    return os.path.basename(path).lower()

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
    path = config.get("aurora_path", DEFAULT_CONFIG["aurora_path"]).strip()
    exe = path if path.lower().endswith(".exe") else os.path.join(path, os.path.basename(path) + ".exe")
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

class CommandHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def _set_headers(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

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
    """AuroraClient.exe 자체를 레지스트리 자동 실행에 등록"""
    if not getattr(sys, "frozen", False):
        return   # 개발 환경(py 직접 실행)에선 등록 안 함
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
    server = HTTPServer(("0.0.0.0", port), CommandHandler)
    server.serve_forever()

# ─────────────────────────────────────────
# GUI
# ─────────────────────────────────────────
class AuroraClientApp:

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

    def _section_title(self, parent, text, bg=None):
        return tk.Label(parent, text=text, font=self.FONT_TITLE,
                        fg=self.TEXT, bg=bg or self.PANEL)

    def _entry(self, parent, var, width, font=None):
        return tk.Entry(parent, textvariable=var, font=font or self.FONT_INPUT,
                        bg=self.PANEL, fg=self.TEXT, insertbackground=self.TEXT,
                        disabledbackground=self.BG, disabledforeground=self.DISABLED_TEXT,
                        relief="flat", bd=_px(6),   # flat 테두리가 안쪽 여백 역할
                        highlightthickness=1,
                        highlightbackground=self.INPUT_BORDER, highlightcolor=self.BLUE,
                        width=width)

    def _field_label(self, parent, text, width=12):
        return tk.Label(parent, text=text, width=width, anchor="w",
                        font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL)

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
        tk.Label(header, text=self.my_ip,
                 font=self.FONT_INPUT, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="right", padx=_px(18))

        # 탭바
        tabbar = tk.Frame(self.root, bg=self.PANEL)
        tabbar.pack(fill="x")
        self._separator(self.root)

        content = tk.Frame(self.root, bg=self.BG)
        content.pack(fill="both", expand=True)

        self._tab_frames = []
        self._tab_labels = []
        self._tab_lines  = []
        for i, title in enumerate(["설정", "프로세스 관리", "자동 삭제"]):
            cell = tk.Frame(tabbar, bg=self.PANEL)
            cell.pack(side="left", padx=(_px(8) if i == 0 else 0, 0))
            lbl = tk.Label(cell, text=title, font=self.FONT_TAB, fg=self.TEXT_DIM,
                           bg=self.PANEL, padx=_px(16), pady=_px(10), cursor="hand2")
            lbl.pack()
            line = tk.Frame(cell, bg=self.PANEL, height=_px(2))
            line.pack(fill="x")
            lbl.bind("<Button-1>", lambda e, n=i: self._select_tab(n))
            self._tab_labels.append(lbl)
            self._tab_lines.append(line)
            self._tab_frames.append(tk.Frame(content, bg=self.BG, padx=_px(18), pady=_px(16)))

        self._build_main_tab(self._tab_frames[0])
        self._build_process_tab(self._tab_frames[1])
        self._build_delete_tab(self._tab_frames[2])
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
        for col, (label, attr) in enumerate([("서버 연결", "server_lbl"),
                                              ("Aurora 상태", "aurora_lbl"),
                                              ("마지막 명령", "cmd_lbl")]):
            tile = tk.Frame(st_in, bg=self.PANEL_ALT, highlightthickness=1,
                            highlightbackground=self.BORDER, highlightcolor=self.BORDER)
            tile.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else _px(8), 0))
            tk.Label(tile, text=label, anchor="w", font=self.FONT,
                     fg=self.TEXT_DIM, bg=self.PANEL_ALT).pack(fill="x", padx=_px(12), pady=(_px(10), 0))
            lbl = tk.Label(tile, text="—", anchor="w",
                           font=self.FONT_VALUE, fg=self.TEXT, bg=self.PANEL_ALT)
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
            tk.Label(row, text=ip, anchor="w", font=self.FONT_MONO,
                     fg=self.TEXT, bg=self.PANEL).pack(side="left")
            lbl = tk.Label(row, text="—", anchor="e",
                           font=self.FONT_MONO_BOLD, fg=self.TEXT, bg=self.PANEL)
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
        auto_row.pack(fill="x", padx=_px(20), pady=(_px(10), _px(16)))
        self._field_label(auto_row, "자동 시작", width=8).pack(side="left", padx=(0, _px(8)))
        self._autostart_btn = self._button(
            auto_row, "OFF ○", "secondary", self._toggle_autostart)
        self._style_autostart_btn(autostart_enabled())
        self._autostart_btn.pack(side="left")
        tk.Label(auto_row, text="  PC 시작 시 클라이언트 자동 실행",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")

        # 수동 제어
        mf = self._card(parent)
        mf.pack(fill="x", pady=(0, _px(14)))
        mf_in = tk.Frame(mf, bg=self.PANEL)
        mf_in.pack(fill="x", padx=_px(20), pady=_px(16))
        self._section_title(mf_in, "수동 제어").pack(side="left")
        tk.Label(mf_in, text=" (긴급용)", font=self.FONT,
                 fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")
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
        tk.Label(add_row, text="예: notepad.exe",
                 font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left", padx=(0, _px(10)))
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
        tk.Label(col_hdr, text="프로세스 이름", anchor="w", font=self.FONT_SMALL,
                 fg=self.TEXT_DIM, bg=self.PANEL_ALT, pady=_px(8)
                 ).grid(row=0, column=1, sticky="w", padx=(0, _px(8)))
        tk.Label(col_hdr, text="상태", anchor="w", font=self.FONT_SMALL,
                 fg=self.TEXT_DIM, bg=self.PANEL_ALT, pady=_px(8)
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
            tk.Label(self._kill_rows_frame, text="등록된 프로세스가 없습니다.",
                     font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.PANEL,
                     pady=_px(12)).pack(anchor="w", padx=_px(12))
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

            tk.Label(row, text=name, anchor="w",
                     font=self.FONT_INPUT, fg=self.TEXT, bg=self.PANEL
                     ).grid(row=0, column=1, sticky="w", padx=(0, _px(8)))

            status_lbl = self._make_badge(row, "", self.TEXT_DIM)
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
        tk.Label(pb_top, text="다음 자동 삭제까지",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")
        self.delete_countdown_lbl = tk.Label(pb_top, text="—",
                 font=self.FONT_COUNTDOWN, fg=self.BLUE, bg=self.PANEL)
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
        self.delete_status_lbl = tk.Label(bot, text="",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL)
        self.delete_status_lbl.pack(side="left", padx=_px(10))

        # 컬럼 헤더 (스크롤바 폭만큼 우측 여백)
        col = tk.Frame(rules_card, bg=self.PANEL_ALT)
        col.pack(fill="x")
        col_in = tk.Frame(col, bg=self.PANEL_ALT)
        col_in.pack(side="left", fill="x", expand=True, padx=_px(14))
        self._configure_delete_grid(col_in)
        for i, text in enumerate(["ON", "폴더 경로", "확장자", "기한(일)", "폴더삭제"]):
            tk.Label(col_in, text=text, anchor="w", font=self.FONT_SMALL,
                     fg=self.TEXT_DIM, bg=self.PANEL_ALT, pady=_px(8)
                     ).grid(row=0, column=i, sticky="w", padx=_px(2))

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
        status_lbl = tk.Label(row1, font=self.FONT_SMALL, padx=_px(6), pady=_px(2))
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
        last_del_lbl = tk.Label(row2,
                                text=f"마지막 삭제: {last_del_text}" if last_del_text else "마지막 삭제: —",
                                font=self.FONT_MONO_SMALL, fg=self.TEXT_DIM, bg=bg)
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
        setup_self_autostart()

        if config.get("ping_mode", "manual") == "auto":
            self._start_auto_ping_client()

        self.add_log("클라이언트 시작됨")
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
