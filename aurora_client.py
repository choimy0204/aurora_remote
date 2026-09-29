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
import winreg
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, date, timedelta
import pystray
from PIL import Image as PILImage

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
    BG       = "#f1f5f9"
    PANEL    = "#ffffff"
    BORDER   = "#e2e8f0"
    TEXT     = "#0f172a"
    TEXT_DIM = "#64748b"
    WHITE    = "#ffffff"
    GREEN    = "#16a34a"
    RED      = "#dc2626"
    YELLOW   = "#d97706"
    BLUE     = "#4f46e5"
    BTN_START    = "#16a34a"
    BTN_START_H  = "#15803d"
    BTN_STOP     = "#dc2626"
    BTN_STOP_H   = "#b91c1c"
    BTN_ALL      = "#7c3aed"
    BTN_ALL_H    = "#6d28d9"
    SELECT_BG    = "#e0e7ff"
    ROW_ALT      = "#e9eef5"

    # ── 폰트 ─────────────────────────────
    FONT             = ("맑은 고딕", 10)
    FONT_BOLD        = ("맑은 고딕", 10, "bold")
    FONT_TITLE       = ("맑은 고딕", 11, "bold")
    FONT_HEAD        = ("맑은 고딕", 13, "bold")
    FONT_SMALL       = ("맑은 고딕", 9)
    FONT_SMALL_BOLD  = ("맑은 고딕", 9, "bold")
    FONT_INPUT       = ("Consolas", 10)
    FONT_MONO        = ("Consolas", 9)
    FONT_MONO_BOLD   = ("Consolas", 9, "bold")

    @classmethod
    def setup_styles(cls):
        """ttk 공통 스타일 (clam 테마)"""
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar",
                        background=cls.BORDER, troughcolor=cls.BG,
                        arrowcolor=cls.TEXT_DIM, bordercolor=cls.BORDER)
        style.configure("TNotebook",
                        background=cls.BG, borderwidth=0, tabmargins=0)
        style.configure("TNotebook.Tab",
                        background=cls.BORDER, foreground=cls.TEXT_DIM,
                        font=cls.FONT_BOLD, padding=[16, 8],
                        borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", cls.PANEL)],
                  foreground=[("selected", cls.BLUE)])
        style.configure("Countdown.Horizontal.TProgressbar",
                        troughcolor=cls.BORDER, background=cls.BLUE,
                        borderwidth=0, thickness=8)

    def __init__(self, root: tk.Tk):
        global app_instance
        app_instance = self

        self.root = root
        self.my_ip = self._get_local_ip()
        self.root.title(f"Aurora Client  |  {self.my_ip}")
        self.root.geometry("660x900")
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
        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", **pack_opts)

    def _card(self, parent):
        """흰 배경 + 얇은 테두리 프레임"""
        return tk.Frame(parent, bg=self.PANEL, highlightthickness=1,
                        highlightbackground=self.BORDER, highlightcolor=self.BORDER)

    def _entry(self, parent, var, width, font=None):
        return tk.Entry(parent, textvariable=var, font=font or self.FONT_INPUT,
                        bg=self.PANEL, fg=self.TEXT, insertbackground=self.TEXT,
                        relief="flat", bd=0, highlightthickness=1,
                        highlightbackground=self.BORDER, highlightcolor=self.BLUE,
                        width=width)

    def _field_label(self, parent, text, width=12):
        return tk.Label(parent, text=text, width=width, anchor="w",
                        font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL)

    def _running_style(self, is_running: bool) -> dict:
        """실행중/중지 표시용 label 옵션"""
        return {"text": "● 실행중" if is_running else "○ 중지",
                "fg": self.GREEN if is_running else self.TEXT_DIM}

    # ── UI ───────────────────────────────
    def _build_ui(self):
        # 타이틀
        tf = tk.Frame(self.root, bg=self.BG)
        tf.pack(fill="x", padx=20, pady=(18, 0))
        tk.Label(tf, text="◈  AURORA CLIENT",
                 font=self.FONT_HEAD, fg=self.BLUE, bg=self.BG).pack(side="left")
        tk.Label(tf, text=self.my_ip,
                 font=self.FONT_INPUT, fg=self.TEXT_DIM, bg=self.BG).pack(side="right")

        self._separator(self.root, padx=20, pady=(10, 0))

        # Notebook
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=20, pady=(10, 16))

        tab_main    = tk.Frame(nb, bg=self.BG)
        tab_process = tk.Frame(nb, bg=self.BG)
        tab_delete  = tk.Frame(nb, bg=self.BG)
        nb.add(tab_main,    text="  설정  ")
        nb.add(tab_process, text="  프로세스 관리  ")
        nb.add(tab_delete,  text="  자동 삭제  ")

        self._build_main_tab(tab_main)
        self._build_process_tab(tab_process)
        self._build_delete_tab(tab_delete)

    def _build_main_tab(self, parent):
        # 설정 패널
        cfg = self._card(parent)
        cfg.pack(fill="x", pady=(10, 0))

        tk.Label(cfg, text="설정",
                 font=self.FONT_TITLE, fg=self.TEXT_DIM,
                 bg=self.PANEL).pack(anchor="w", padx=14, pady=(8, 4))

        # ── 서버 IP 목록 ─────────────────────
        srv_row = tk.Frame(cfg, bg=self.PANEL)
        srv_row.pack(fill="x", padx=14, pady=(4, 0))
        self._field_label(srv_row, "서버 IP 목록").pack(side="left")

        srv_right = tk.Frame(cfg, bg=self.PANEL)
        srv_right.pack(fill="x", padx=14, pady=(2, 6))

        # 리스트박스
        lb_frame = tk.Frame(srv_right, bg=self.PANEL)
        lb_frame.pack(side="left", fill="x", expand=True)
        self.server_lb = tk.Listbox(lb_frame, font=self.FONT_MONO,
                                    bg=self.PANEL, fg=self.TEXT,
                                    selectbackground=self.BLUE, selectforeground=self.WHITE,
                                    relief="flat", bd=0, highlightthickness=1,
                                    highlightbackground=self.BORDER, highlightcolor=self.BLUE,
                                    height=4, activestyle="none")
        self.server_lb.pack(side="left", fill="x", expand=True)
        lb_sb = ttk.Scrollbar(lb_frame, orient="vertical", command=self.server_lb.yview)
        self.server_lb.configure(yscrollcommand=lb_sb.set)
        lb_sb.pack(side="left", fill="y")
        for ip in config.get("server_ips", []):
            self.server_lb.insert("end", ip)

        # 추가/삭제 버튼
        srv_btn = tk.Frame(srv_right, bg=self.PANEL)
        srv_btn.pack(side="left", padx=(8, 0), anchor="n")
        self.server_ip_input = tk.StringVar()
        ip_entry = self._entry(srv_btn, self.server_ip_input, 16, self.FONT_MONO)
        ip_entry.pack(pady=(0, 4), ipady=3)
        ip_entry.bind("<Return>", lambda e: self._add_server_ip())
        self._small_btn(srv_btn, "+ 추가", self.BLUE, self._add_server_ip).pack(fill="x", pady=(0, 2))
        self._small_btn(srv_btn, "− 삭제", self.RED,  self._del_server_ip).pack(fill="x")

        self.server_port_var = tk.StringVar(value=str(config.get("server_port", 9000)))
        self.listen_port_var = tk.StringVar(value=str(config.get("listen_port", 9001)))
        row_p = tk.Frame(cfg, bg=self.PANEL)
        row_p.pack(fill="x", padx=14, pady=4)
        self._field_label(row_p, "서버 포트").pack(side="left")
        self._entry(row_p, self.server_port_var, 8).pack(side="left", ipady=4, padx=(0, 16))
        self._field_label(row_p, "수신 포트", width=10).pack(side="left")
        self._entry(row_p, self.listen_port_var, 8).pack(side="left", ipady=4, padx=(0, 8))
        self._small_btn(row_p, "적용", self.BLUE, self._apply_ports).pack(side="left")

        # ── PC 시작 시 자동 실행 ─────────────
        auto_row = tk.Frame(cfg, bg=self.PANEL)
        auto_row.pack(fill="x", padx=14, pady=(4, 10))
        self._field_label(auto_row, "자동 시작").pack(side="left")
        self._autostart_btn = self._small_btn(
            auto_row,
            "ON  ●" if autostart_enabled() else "OFF ○",
            self.GREEN if autostart_enabled() else self.TEXT_DIM,
            self._toggle_autostart)
        self._autostart_btn.pack(side="left")
        tk.Label(auto_row, text="  PC 시작 시 클라이언트 자동 실행",
                 font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")

        self._separator(parent, pady=(10, 0))

        # 상태 패널
        st = self._card(parent)
        st.pack(fill="x", pady=10)
        for label, attr in [("서버 연결", "server_lbl"),
                             ("Aurora 상태", "aurora_lbl"),
                             ("마지막 명령", "cmd_lbl")]:
            row = tk.Frame(st, bg=self.PANEL)
            row.pack(fill="x", padx=16, pady=5)
            self._field_label(row, label).pack(side="left")
            lbl = tk.Label(row, text="—", anchor="w",
                           font=self.FONT_BOLD, fg=self.TEXT, bg=self.PANEL)
            lbl.pack(side="left")
            setattr(self, attr, lbl)

        self._separator(parent)

        # 핑 모드
        pf = self._card(parent)
        pf.pack(fill="x", pady=10)
        hdr_row = tk.Frame(pf, bg=self.PANEL)
        hdr_row.pack(fill="x", padx=14, pady=(4, 6))
        self._field_label(hdr_row, "핑 모드").pack(side="left")
        self._ping_mode_client = config.get("ping_mode", "manual")
        self.ping_manual_btn_c = self._small_btn(
            hdr_row, "Manual", self.TEXT_DIM, lambda: None)
        self.ping_manual_btn_c.pack(side="left", padx=(0, 4))
        self.ping_auto_btn_c = self._small_btn(
            hdr_row, "Auto", self.TEXT_DIM, lambda: None)
        self.ping_auto_btn_c.pack(side="left")
        self._apply_ping_mode_style()

        self._ping_result_labels = {}
        for ip in config.get("server_ips", []):
            row = tk.Frame(pf, bg=self.PANEL)
            row.pack(fill="x", padx=14, pady=2)
            self._field_label(row, ip, width=18).pack(side="left")
            lbl = tk.Label(row, text="—", anchor="w",
                           font=self.FONT_BOLD, fg=self.TEXT, bg=self.PANEL)
            lbl.pack(side="left")
            self._ping_result_labels[ip] = lbl

        self._separator(parent)

        # 수동 제어
        mf = tk.Frame(parent, bg=self.BG)
        mf.pack(fill="x", pady=10)
        tk.Label(mf, text="수동 제어 (긴급용)",
                 font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.BG).pack(anchor="w", pady=(0, 6))
        br = tk.Frame(mf, bg=self.BG)
        br.pack(fill="x")
        self._btn(br, "▶ 프로그램 실행", self.BTN_START, self.BTN_START_H,
                  self._manual_start).pack(side="left", padx=(0, 8))
        self._btn(br, "■ 프로그램 종료", self.BTN_STOP, self.BTN_STOP_H,
                  self._manual_stop).pack(side="left")

        self._separator(parent, pady=(10, 0))

        # 로그
        lf = tk.Frame(parent, bg=self.BG)
        lf.pack(fill="both", expand=True, pady=(8, 0))
        tk.Label(lf, text="LOG", font=self.FONT_TITLE,
                 fg=self.TEXT_DIM, bg=self.BG).pack(anchor="w", pady=(0, 4))
        li = self._card(lf)
        li.pack(fill="both", expand=True)
        self.log_text = tk.Text(li, font=self.FONT_MONO, bg=self.PANEL,
                 fg=self.TEXT_DIM, bd=0, padx=10, pady=8,
                 state="disabled", wrap="word", highlightthickness=0)
        sb = ttk.Scrollbar(li, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)

    # ── 프로세스 관리 탭 ─────────────────
    def _build_process_tab(self, parent):
        # ── 실행 프로그램 섹션 ────────────────
        run_panel = self._card(parent)
        run_panel.pack(fill="x", pady=(10, 0))

        tk.Label(run_panel, text="실행 프로그램",
                 font=self.FONT_TITLE, fg=self.TEXT_DIM,
                 bg=self.PANEL).pack(anchor="w", padx=14, pady=(8, 4))

        path_row = tk.Frame(run_panel, bg=self.PANEL)
        path_row.pack(fill="x", padx=14, pady=(0, 10))

        self._field_label(path_row, "실행 경로", width=10).pack(side="left")

        self.aurora_path_var = tk.StringVar(
            value=config.get("aurora_path", DEFAULT_CONFIG["aurora_path"]))
        self._entry(path_row, self.aurora_path_var, 30
                    ).pack(side="left", ipady=4, padx=(0, 6))

        def _browse_exe():
            path = filedialog.askopenfilename(
                title="실행 파일 선택", filetypes=[("실행 파일", "*.exe"), ("모든 파일", "*.*")])
            if path:
                self.aurora_path_var.set(path)

        self._small_btn(path_row, "찾아보기", self.TEXT_DIM, _browse_exe).pack(side="left", padx=(0, 6))
        self._small_btn(path_row, "적용", self.BLUE, self._apply_aurora_path).pack(side="left")

        self._separator(parent, pady=(4, 0))

        # ── 프로세스 종료 섹션 ────────────────
        kill_panel = tk.Frame(parent, bg=self.BG)
        kill_panel.pack(fill="both", expand=True, pady=(10, 0))

        hdr = tk.Frame(kill_panel, bg=self.BG)
        hdr.pack(fill="x", padx=4)
        tk.Label(hdr, text="프로세스 종료 목록",
                 font=self.FONT_TITLE, fg=self.TEXT_DIM, bg=self.BG).pack(side="left")

        add_row = tk.Frame(kill_panel, bg=self.BG)
        add_row.pack(fill="x", padx=4, pady=(6, 4))
        self._kill_input_var = tk.StringVar()
        kill_entry = self._entry(add_row, self._kill_input_var, 22)
        kill_entry.pack(side="left", ipady=4, padx=(0, 6))
        kill_entry.bind("<Return>", lambda e: self._add_kill_process())
        tk.Label(add_row, text="예: notepad.exe",
                 font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.BG).pack(side="left", padx=(0, 10))
        self._small_btn(add_row, "+ 추가", self.BLUE, self._add_kill_process, self.BG).pack(side="left", padx=(0, 4))
        self._small_btn(add_row, "− 삭제", self.RED,  self._del_kill_process, self.BG).pack(side="left")

        # 열 헤더
        col_hdr = tk.Frame(kill_panel, bg=self.PANEL)
        col_hdr.pack(fill="x", padx=4)
        for col, w, anchor in [("프로세스 이름", 24, "w"), ("상태", 12, "center")]:
            tk.Label(col_hdr, text=col, width=w, anchor=anchor,
                     font=self.FONT_MONO_BOLD, fg=self.TEXT_DIM,
                     bg=self.PANEL, pady=4).pack(side="left", padx=(8 if anchor == "w" else 0, 0))

        self._separator(kill_panel, padx=4)

        self._kill_rows_frame = tk.Frame(kill_panel, bg=self.BG)
        self._kill_rows_frame.pack(fill="both", expand=True, padx=4)

        # 하단 버튼
        self._separator(kill_panel, padx=4, pady=(4, 0))
        btn_row = tk.Frame(kill_panel, bg=self.BG)
        btn_row.pack(fill="x", padx=4, pady=6)
        self._btn(btn_row, "선택 프로세스 종료", self.BTN_STOP, self.BTN_STOP_H,
                  self._kill_selected).pack(side="left", padx=(0, 8))
        self._btn(btn_row, "전체 종료", self.BTN_ALL, self.BTN_ALL_H,
                  self._kill_all).pack(side="left", padx=(0, 8))
        self._small_btn(btn_row, "새로고침", self.TEXT_DIM,
                        self._refresh_kill_status, self.BG).pack(side="left")

        self._rebuild_kill_rows()

    def _rebuild_kill_rows(self):
        for w in self._kill_rows_frame.winfo_children():
            w.destroy()
        self._kill_check_vars  = {}
        self._kill_status_lbls = {}

        procs = config.get("kill_processes", [])
        if not procs:
            tk.Label(self._kill_rows_frame, text="등록된 프로세스가 없습니다.",
                     font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.BG,
                     pady=12).pack(anchor="w", padx=12)
            return

        running_map = get_running_processes(procs)
        for idx, name in enumerate(procs):
            row_bg = self.BG if idx % 2 == 0 else self.ROW_ALT
            row = tk.Frame(self._kill_rows_frame, bg=row_bg)
            row.pack(fill="x")

            var = tk.BooleanVar(value=False)
            self._kill_check_vars[name] = var
            tk.Checkbutton(row, variable=var, bg=row_bg,
                           activebackground=row_bg, selectcolor=self.PANEL, bd=0
                           ).pack(side="left", padx=(6, 0))

            tk.Label(row, text=name, width=22, anchor="w",
                     font=self.FONT_INPUT, fg=self.TEXT, bg=row_bg, pady=6
                     ).pack(side="left", padx=(4, 0))

            status_lbl = tk.Label(row,
                                  **self._running_style(running_map.get(name, False)),
                                  width=12, anchor="center",
                                  font=self.FONT_MONO_BOLD,
                                  bg=row_bg, pady=6)
            status_lbl.pack(side="left")
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
                    lbl.config(**self._running_style(running_map.get(name, False)))
            self.root.after(0, _upd)
        threading.Thread(target=_do, daemon=True).start()

    # ── 자동 삭제 탭 ─────────────────────
    def _build_delete_tab(self, parent):
        # 헤더
        hdr = tk.Frame(parent, bg=self.BG)
        hdr.pack(fill="x", pady=(10, 6))
        tk.Label(hdr, text="자동 삭제 규칙",
                 font=self.FONT_TITLE, fg=self.TEXT_DIM, bg=self.BG).pack(side="left")
        self._small_btn(hdr, "+ 추가", self.BLUE,
                        self._add_delete_rule, self.BG).pack(side="right")

        # ── 프로그레스바 (다음 실행까지) ─────
        pb_frame = self._card(parent)
        pb_frame.pack(fill="x", pady=(0, 8))

        pb_top = tk.Frame(pb_frame, bg=self.PANEL)
        pb_top.pack(fill="x", padx=10, pady=(6, 4))
        tk.Label(pb_top, text="다음 자동 삭제까지",
                 font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")
        self.delete_countdown_lbl = tk.Label(pb_top, text="—",
                 font=self.FONT_MONO_BOLD, fg=self.BLUE, bg=self.PANEL)
        self.delete_countdown_lbl.pack(side="right")

        self.delete_progress = ttk.Progressbar(pb_frame, orient="horizontal",
                                               length=200, maximum=86400, value=0,
                                               style="Countdown.Horizontal.TProgressbar")
        self.delete_progress.pack(fill="x", padx=10, pady=(0, 8))

        self._separator(parent)

        # 컬럼 헤더
        col = tk.Frame(parent, bg=self.PANEL)
        col.pack(fill="x")
        for text, anchor, w in [("ON", "center", 4), ("폴더 경로", "w", 26),
                                 ("확장자", "center", 8), ("기한(일)", "center", 8),
                                 ("폴더삭제", "center", 7)]:
            tk.Label(col, text=text, width=w, anchor=anchor,
                     font=self.FONT_MONO_BOLD, fg=self.TEXT_DIM,
                     bg=self.PANEL, pady=6).pack(side="left")

        self._separator(parent)

        # 규칙 목록 (스크롤)
        list_outer = self._card(parent)
        list_outer.pack(fill="both", expand=True, pady=(8, 4))

        canvas = tk.Canvas(list_outer, bg=self.PANEL, highlightthickness=0)
        sb2 = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._delete_rules_frame = tk.Frame(canvas, bg=self.PANEL)
        canvas.create_window((0, 0), window=self._delete_rules_frame, anchor="nw")
        self._delete_rules_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._delete_canvas = canvas

        self._separator(parent)

        # 하단: 지금 실행 버튼
        bot = tk.Frame(parent, bg=self.BG)
        bot.pack(fill="x", pady=(6, 0))
        self._small_btn(bot, "지금 실행", self.BLUE,
                        self._run_delete_now, self.BG).pack(side="left")
        self.delete_status_lbl = tk.Label(bot, text="",
                 font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.BG)
        self.delete_status_lbl.pack(side="left", padx=10)

        # 로그
        self._separator(parent, pady=(6, 0))
        dlf = tk.Frame(parent, bg=self.BG)
        dlf.pack(fill="x", pady=(6, 0))
        tk.Label(dlf, text="삭제 LOG", font=self.FONT_TITLE,
                 fg=self.TEXT_DIM, bg=self.BG).pack(anchor="w", pady=(0, 4))
        dli = self._card(dlf)
        dli.pack(fill="x")
        self.delete_log_text = tk.Text(dli, font=self.FONT_MONO, bg=self.PANEL,
                 fg=self.TEXT_DIM, bd=0, padx=8, pady=6,
                 state="disabled", wrap="word", highlightthickness=0, height=5)
        sb3 = ttk.Scrollbar(dli, orient="vertical", command=self.delete_log_text.yview)
        self.delete_log_text.configure(yscrollcommand=sb3.set)
        sb3.pack(side="right", fill="y")
        self.delete_log_text.pack(fill="x")

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
        idx  = len(self._delete_rule_widgets)
        rule = rule or {"path": "", "ext": "", "minutes": 30,
                        "enabled": True, "applied": False, "last_deleted": "",
                        "type": "file"}
        bg   = self.BG if idx % 2 == 0 else self.ROW_ALT

        # ── 행 1: 컨트롤 ─────────────────────
        row1 = tk.Frame(self._delete_rules_frame, bg=bg)
        row1.pack(fill="x")

        ena_var        = tk.BooleanVar(value=rule.get("enabled", True))
        path_var       = tk.StringVar(value=rule.get("path", ""))
        ext_var        = tk.StringVar(value=rule.get("ext",  ""))
        min_var        = tk.StringVar(value=str(rule.get("minutes", 30)))
        folder_del_var = tk.BooleanVar(value=rule.get("type", "file") == "folder")

        tk.Checkbutton(row1, variable=ena_var, bg=bg,
                       activebackground=bg, selectcolor=self.PANEL, bd=0
                       ).pack(side="left", padx=(10, 4))

        self._entry(row1, path_var, 24, self.FONT_MONO
                    ).pack(side="left", ipady=4, padx=(0, 4), pady=6)

        def _browse(_pv=path_var):
            folder = filedialog.askdirectory(parent=self.root)
            if folder:
                _pv.set(folder)
        tk.Button(row1, text="…", font=self.FONT_SMALL, fg=self.TEXT_DIM,
                  bg=self.PANEL, activebackground=self.BORDER,
                  relief="flat", bd=0, cursor="hand2",
                  command=_browse, padx=5, pady=3).pack(side="left", padx=(0, 6))

        ext_entry = self._entry(row1, ext_var, 7, self.FONT_MONO)
        ext_entry.pack(side="left", ipady=4, padx=(0, 6))

        self._entry(row1, min_var, 6, self.FONT_MONO
                    ).pack(side="left", ipady=4, padx=(0, 6))

        tk.Checkbutton(row1, text="폴더삭제", variable=folder_del_var,
                       font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=bg,
                       activebackground=bg, selectcolor=self.PANEL, bd=0
                       ).pack(side="left", padx=(0, 6))

        # ── 적용 버튼 + 상태 표시 ─────────────
        applied_init = rule.get("applied", False)
        status_lbl = tk.Label(row1,
                              text="적용됨" if applied_init else "미적용",
                              font=self.FONT_SMALL_BOLD,
                              fg=self.GREEN if applied_init else self.RED,
                              bg=bg, width=5)
        status_lbl.pack(side="right", padx=(0, 4))

        widget_info = {
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
            _wi["status_lbl"].config(text="적용됨", fg=self.GREEN)
            self._save_delete_rules()

        tk.Button(row1, text="적용", font=self.FONT_SMALL_BOLD,
                  fg=self.BLUE, bg=bg, activeforeground=self.BLUE,
                  activebackground=self.BORDER, relief="flat", bd=0,
                  cursor="hand2", padx=6, pady=2,
                  command=_apply_rule).pack(side="right", padx=(0, 4))

        def _del_rule(_wi=widget_info):
            self._delete_rule_widgets.remove(_wi)
            _wi["row1"].destroy()
            if _wi["last_del_lbl"] and _wi["last_del_lbl"].winfo_exists():
                _wi["last_del_lbl"].master.destroy()
            self._save_delete_rules()
            self._refresh_delete_canvas()

        tk.Button(row1, text="✕", font=self.FONT_SMALL_BOLD,
                  fg=self.RED, bg=bg, activeforeground=self.RED,
                  activebackground=self.BORDER, relief="flat", bd=0, cursor="hand2",
                  command=_del_rule).pack(side="right", padx=(0, 2))

        # ── 행 2: 마지막 삭제 일자 ───────────
        row2 = tk.Frame(self._delete_rules_frame, bg=bg)
        row2.pack(fill="x")
        last_del_text = rule.get("last_deleted", "")
        last_del_lbl = tk.Label(row2,
                                text=f"마지막 삭제: {last_del_text}" if last_del_text else "마지막 삭제: —",
                                font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=bg)
        last_del_lbl.pack(anchor="w", padx=28, pady=(0, 4))
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
                _wi["status_lbl"].config(text="미적용", fg=self.RED)
            self._save_delete_rules()

        # 경로 변경 시 적용 상태 초기화
        def _on_key_change(*_, _wi=widget_info):
            if _wi["applied"]:
                _wi["applied"] = False
                _wi["status_lbl"].config(text="미적용", fg=self.RED)
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
        self.add_log(line)
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
    def _btn(self, parent, text, bg, abg, cmd):
        b = tk.Button(parent, text=text, font=self.FONT_BOLD,
                      fg=self.WHITE, bg=bg, activeforeground=self.WHITE,
                      activebackground=abg, bd=0, cursor="hand2",
                      relief="flat", padx=16, pady=8, command=cmd)
        b.bind("<Enter>", lambda e: b.config(bg=abg))
        b.bind("<Leave>", lambda e: b.config(bg=bg))
        return b

    def _small_btn(self, parent, text, fg, cmd, bg=None):
        bg = bg or self.PANEL
        return tk.Button(parent, text=text, font=self.FONT_SMALL_BOLD,
                         fg=fg, bg=bg, activeforeground=fg,
                         activebackground=self.BORDER, bd=0, cursor="hand2",
                         relief="flat", padx=10, pady=3, command=cmd)

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
            self._autostart_btn.config(
                text="ON  ●" if new_state else "OFF ○",
                fg=self.GREEN if new_state else self.TEXT_DIM)
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
        """현재 핑 모드 기준으로 Manual/Auto 버튼 글자색 표시 (초기화·변경 공용)"""
        is_auto = self._ping_mode_client == "auto"
        manual_fg = self.TEXT_DIM if is_auto else self.BLUE
        auto_fg   = self.BLUE if is_auto else self.TEXT_DIM
        self.ping_manual_btn_c.config(fg=manual_fg, activeforeground=manual_fg)
        self.ping_auto_btn_c.config(fg=auto_fg, activeforeground=auto_fg)

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
