"""
Aurora Remote Controller - Server
=====================================
- 클라이언트 IP 목록 GUI에서 동적 추가/삭제
- 설정은 server_config.json 에 저장
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import threading
import requests
import json
import time
import os
import winreg
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import pystray
from PIL import Image as PILImage, ImageTk
import socket as _socket
import shutil
import sys
import ctypes
import aurora_update

APP_VERSION = "2.13.0"   # 올리면 build.bat 이 dist/latest/version.json 에 기록 → 자동 업데이트 기준

# ── 번들 리소스(이미지) ──────────────
def _resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

# ── 고해상도(DPI) 대응 ───────────────────
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

SCALE = 1.0   # tk 루트 생성 후 main() 에서 실제 화면 배율로 설정 (100% → 1.0, 150% → 1.5)

def _px(v):
    """100% 기준 픽셀 값을 현재 화면 배율에 맞춰 환산"""
    return int(round(v * SCALE))

# ── 중복 실행 방지 ────────────────────────
_LOCK_SOCK = None
def _acquire_lock(port=19700):
    global _LOCK_SOCK
    try:
        _LOCK_SOCK = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _LOCK_SOCK.bind(("127.0.0.1", port))
    except OSError:
        _r = tk.Tk(); _r.withdraw()
        messagebox.showerror("실행 오류", "Aurora Server 가 이미 실행 중입니다.")
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
CONFIG_FILE       = os.path.join(REMOTE_CONFIG_DIR, "server_config.json")

def _init_config_dir():
    os.makedirs(REMOTE_CONFIG_DIR, exist_ok=True)
    # 현재 경로에 기존 config가 있으면 D:\RemoteConfig 로 이동 (추후 제거 예정)
    old_path = os.path.join(BASE_DIR, "server_config.json")
    if os.path.exists(old_path) and not os.path.exists(CONFIG_FILE):
        shutil.move(old_path, CONFIG_FILE)

_init_config_dir()

DEFAULT_CONFIG = {
    "client_ips": [f"192.168.11.{i}" for i in range(1, 7)],
    "current_process_ips": [],
    "client_names": {},
    "server_port": 9000,
    "client_port": 9001,
    "deploy_src": "",
    "deploy_version": "",
    "deploy_client_folders": {},
    "deploy_user": "user",
    "deploy_pw": "1",
    "ping_mode": "manual",
}

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ─────────────────────────────────────────
# 공유 상태
# ─────────────────────────────────────────
SERVER_HOST = "0.0.0.0"

config = load_config()

SERVER_PORT = config.get("server_port", 9000)
CLIENT_PORT = config.get("client_port", 9001)

shared_state = {
    "clients": {},   # { ip: {online, aurora_running, last_seen, ping_ms, aurora_path} }
    "log": [],
    "log_seq": 0,    # 지금까지 추가된 로그 누적 개수 (앞부분 잘림과 무관)
}
state_lock = threading.Lock()

def init_clients(ip_list: list):
    with state_lock:
        for ip in ip_list:
            if ip not in shared_state["clients"]:
                shared_state["clients"][ip] = {
                    "online": False,
                    "aurora_running": False,
                    "last_seen": None,
                    "ping_ms": None,
                    "aurora_path": "",
                }
        for ip in list(shared_state["clients"].keys()):
            if ip not in ip_list:
                del shared_state["clients"][ip]

init_clients(config["client_ips"])

# ─────────────────────────────────────────
# HTTP 핸들러
# ─────────────────────────────────────────
class AuroraHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def _set_headers(self, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def do_POST(self):
        if self.path == "/status":
            ip = self.client_address[0]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                aurora_running = data.get("aurora_running", False)
                aurora_path    = data.get("aurora_path", "")
                all_ips = data.get("all_ips", [ip])
                if ip not in all_ips:
                    all_ips = [ip] + all_ips
                with state_lock:
                    for target_ip in all_ips:
                        if target_ip in shared_state["clients"]:
                            shared_state["clients"][target_ip]["online"] = True
                            shared_state["clients"][target_ip]["aurora_running"] = aurora_running
                            shared_state["clients"][target_ip]["last_seen"] = time.time()
                            if aurora_path:
                                shared_state["clients"][target_ip]["aurora_path"] = aurora_path
            except Exception:
                pass
            # 등록된 클라이언트 목록(IP·이름)을 응답에 실어 보냄 → 클라이언트 반입/반출 장비 목록으로 사용
            names = config.get("client_names", {})
            clients = [{"ip": c, "name": names.get(c, f"CLIENT {i+1:02d}")}
                       for i, c in enumerate(config.get("client_ips", []))]
            self._set_headers()
            self.wfile.write(json.dumps({"ok": True, "clients": clients}, ensure_ascii=False).encode("utf-8"))
        else:
            self._set_headers(404)
            self.wfile.write(b'{"error": "not found"}')

# ─────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────
def _ip_sort_key(ip: str) -> list:
    return [int(x) for x in ip.split(".")]

def add_log(message: str):
    ts = datetime.now().strftime("%H:%M:%S")
    with state_lock:
        shared_state["log"].append(f"[{ts}] {message}")
        shared_state["log_seq"] += 1
        if len(shared_state["log"]) > 200:
            shared_state["log"] = shared_state["log"][-200:]

def push_command(ip: str, command: str, retries: int = 1, retry_delay: float = 0):
    """retries: 총 시도 횟수 (기본 1 = 재시도 없음), retry_delay: 재시도 전 대기(초)"""
    last_exc = None
    for attempt in range(retries):
        if attempt > 0 and retry_delay:
            time.sleep(retry_delay)
        try:
            resp = requests.post(
                f"http://{ip}:{CLIENT_PORT}/command",
                json={"command": command}, timeout=5
            )
            if resp.status_code == 200:
                add_log(f"[{ip}] ✓ {command[:40].upper()}")
                if command.lower().startswith("setpath:"):
                    new_path = command[len("setpath:"):]
                    with state_lock:
                        if ip in shared_state["clients"]:
                            shared_state["clients"][ip]["aurora_path"] = new_path
                return
            add_log(f"[{ip}] ✗ {command[:40].upper()} (HTTP {resp.status_code})")
            return
        except Exception as e:
            last_exc = e
    add_log(f"[{ip}] ✗ 전달 실패 ({retries}회 시도): {last_exc}")

def send_to_online(command: str, ip_filter: list = None):
    """ip_filter가 None이면 전체 공정, 리스트면 해당 IP만"""
    with state_lock:
        online_ips = [ip for ip, v in shared_state["clients"].items() if v["online"]]
    if ip_filter is not None:
        online_ips = [ip for ip in online_ips if ip in ip_filter]
        # 전체 공정 목록에 없는 현재 공정 IP는 온라인 여부 무관하게 시도
        extras = [ip for ip in ip_filter if ip not in online_ips]
        online_ips += extras
    if not online_ips:
        add_log("⚠ 온라인 클라이언트 없음")
        return
    add_log(f"→ 전송 대상: {len(online_ips)}대")
    for ip in online_ips:
        threading.Thread(target=push_command, args=(ip, command), daemon=True).start()

# ─────────────────────────────────────────
# 핑
# ─────────────────────────────────────────
def ping_ip(ip: str) -> int | None:
    """ICMP 핑. 응답 시 ms 반환, 타임아웃 시 None"""
    import subprocess, re
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

def ping_all_clients():
    """등록된 모든 클라이언트 IP에 핑을 보내고 shared_state에 결과 저장.
    동시 스레드를 16개로 제한해 28대에서도 시스템 자원 폭발을 방지."""
    from concurrent.futures import ThreadPoolExecutor
    with state_lock:
        ips = list(shared_state["clients"].keys())
    current_ips = config.get("current_process_ips", [])
    all_ips = list(set(ips + current_ips))

    def _do(ip):
        ms = ping_ip(ip)
        with state_lock:
            if ip in shared_state["clients"]:
                shared_state["clients"][ip]["ping_ms"] = ms

    with ThreadPoolExecutor(max_workers=min(len(all_ips), 16)) as ex:
        list(ex.map(_do, all_ips))

# ─────────────────────────────────────────
# 파일 배포 (UNC 공유 복사)
# ─────────────────────────────────────────
def _connect_share(ip: str, user: str, pw: str) -> bool:
    import subprocess
    unc = f"\\\\{ip}\\D"
    subprocess.run(["net", "use", unc, "/delete", "/yes"], capture_output=True)
    result = subprocess.run(
        ["net", "use", unc, f"/user:{user}", pw, "/persistent:no"],
        capture_output=True, text=True, encoding="cp949", errors="replace"
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        add_log(f"[{ip}] net use 오류: {err}")
    return result.returncode == 0

def _disconnect_share(ip: str):
    import subprocess
    unc = f"\\\\{ip}\\D"
    subprocess.run(["net", "use", unc, "/delete", "/yes"], capture_output=True)

def _copy_file_cancellable(src_file: str, dst_file: str,
                           cancel_event=None, chunk_size: int = 1024 * 1024) -> bool:
    """1MB 청크 단위 복사 — 청크마다 cancel_event 확인. 취소 시 부분 파일 삭제 후 False 반환."""
    try:
        with open(src_file, "rb") as fsrc, open(dst_file, "wb") as fdst:
            while True:
                if cancel_event and cancel_event.is_set():
                    fdst.close()
                    try:
                        os.remove(dst_file)
                    except OSError:
                        pass
                    return False
                chunk = fsrc.read(chunk_size)
                if not chunk:
                    break
                fdst.write(chunk)
        shutil.copystat(src_file, dst_file)
        return True
    except Exception:
        try:
            os.remove(dst_file)
        except OSError:
            pass
        raise

def _deploy_to_client(ip: str, src: str, folder_name: str, user: str, pw: str,
                      progress_fn=None, file_progress_fn=None, cancel_event=None) -> bool:
    """복사 완료 여부 반환"""
    unc_root = f"\\\\{ip}\\D"
    dst = f"{unc_root}\\{folder_name}"
    connected = False
    try:
        add_log(f"[{ip}] 복사 대상: {dst}")
        if progress_fn:
            progress_fn(f"[{ip}] 연결 중...")
        if not os.path.exists(unc_root):
            connected = _connect_share(ip, user, pw)
            if not connected:
                add_log(f"[{ip}] ✗ 공유 연결 실패")
                return False
        if cancel_event and cancel_event.is_set():
            return False
        if progress_fn:
            progress_fn(f"[{ip}] 복사 중...")
        if os.path.exists(dst):
            shutil.rmtree(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)

        # 파일 목록 수집 후 한 파일씩 복사 (1MB 청크 — 취소 즉시 반응)
        all_files = []
        for dirpath, dirnames, filenames in os.walk(src):
            for fname in filenames:
                all_files.append((dirpath, fname))
        total = len(all_files)
        for i, (dirpath, fname) in enumerate(all_files):
            if cancel_event and cancel_event.is_set():
                add_log(f"[{ip}] ✗ 배포 취소됨")
                return False
            rel = os.path.relpath(dirpath, src)
            dst_dir = os.path.join(dst, rel) if rel != "." else dst
            os.makedirs(dst_dir, exist_ok=True)
            ok = _copy_file_cancellable(
                os.path.join(dirpath, fname),
                os.path.join(dst_dir, fname),
                cancel_event
            )
            if not ok:
                add_log(f"[{ip}] ✗ 배포 취소됨 ({fname})")
                return False
            if file_progress_fn:
                file_progress_fn(ip, i + 1, total)

        add_log(f"[{ip}] ✓ 복사 완료 → {dst} ({total}개 파일)")
        return True
    except Exception as e:
        add_log(f"[{ip}] ✗ 복사 실패: {e}")
        return False
    finally:
        if connected:
            _disconnect_share(ip)

def deploy_selected(targets: list, deploy_src: str, user: str, pw: str,
                    dst_folder: str = "", progress_fn=None, done_fn=None,
                    file_progress_fn=None, cancel_event=None, inner_path: str = "") -> int:
    """
    targets: [(ip, src_folder_name, version_name), ...]
    순차 배포. 반환값: 실패 클라이언트 수
    """
    if not targets:
        add_log("⚠ 배포 대상이 없습니다")
        return 0

    results = {}
    folder_map = {}

    for ip, src_folder, version in targets:
        if cancel_event and cancel_event.is_set():
            add_log("◆ 배포 중단됨")
            break
        src = os.path.join(deploy_src, src_folder, inner_path) if inner_path else os.path.join(deploy_src, src_folder)
        exe_folder = f"Aurora_EXE_{version}"
        folder_name = f"{dst_folder}\\{exe_folder}" if dst_folder else exe_folder
        if not os.path.isdir(src):
            add_log(f"[{ip}] ⚠ 소스 폴더 없음: {src}")
            results[ip] = False
            if done_fn:
                done_fn(False)
            continue
        add_log(f"[{ip}] → {folder_name} 배포 시작")
        folder_map[ip] = folder_name
        ok = _deploy_to_client(ip, src, folder_name, user, pw,
                               progress_fn, file_progress_fn, cancel_event)
        results[ip] = ok
        if done_fn:
            done_fn(ok)

    fail_count = sum(1 for ok in results.values() if not ok)
    if not (cancel_event and cancel_event.is_set()):
        add_log("◆ 배포 작업 완료")

    # Aurora 경로 업데이트 (성공한 클라이언트만) — 파일 전송 직후라 네트워크 안정화 대기 후 재시도
    for ip, ok in results.items():
        if ok:
            new_path = f"D:\\{folder_map[ip]}"
            threading.Thread(target=push_command,
                             args=(ip, f"setpath:{new_path}"),
                             kwargs={"retries": 3, "retry_delay": 1.5},
                             daemon=True).start()
    if any(results.values()):
        add_log("→ Aurora 경로 업데이트 명령 전송 중 (최대 3회 재시도)")

    return fail_count


def _single_deploy_all(src_folder: str, files: list, targets: dict, log_fn,
                       done_fn=None) -> int:
    """
    단일 파일 배포. 반환값: 실패한 클라이언트 수
    targets: {ip: (user, pw)}
    done_fn(ip, success): PC 한 대 완료될 때마다 호출 (병렬 스레드에서 호출됨)
    """
    import subprocess

    normalized = os.path.normpath(src_folder)
    failed = []

    def _derive_dst_folder(src: str, ip: str) -> str:
        drive, rel = os.path.splitdrive(src)
        letter = drive.rstrip(":").upper()
        return f"\\\\{ip}\\{letter}" + rel

    def _deploy_single_to_client(ip: str, user: str, pw: str):
        dst_folder = _derive_dst_folder(normalized, ip)
        unc_root   = f"\\\\{ip}\\{os.path.splitdrive(normalized)[0].rstrip(':').upper()}"
        connected  = False
        success    = False
        try:
            log_fn(f"[{ip}] 복사 대상: {dst_folder}")

            if not os.path.exists(unc_root):
                subprocess.run(["net", "use", unc_root, "/delete", "/yes"],
                               capture_output=True)
                result = subprocess.run(
                    ["net", "use", unc_root, f"/user:{user}", pw, "/persistent:no"],
                    capture_output=True, text=True, encoding="cp949", errors="replace"
                )
                if result.returncode != 0:
                    err = (result.stderr or result.stdout or "").strip()
                    log_fn(f"[{ip}] ✗ 공유 연결 실패: {err}")
                    failed.append(ip)
                    return
                connected = True

            os.makedirs(dst_folder, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")

            for fname in files:
                src_file = os.path.join(normalized, fname)
                dst_file = os.path.join(dst_folder, fname)

                if os.path.exists(dst_file):
                    name, ext = os.path.splitext(fname)
                    backup_name = f"{name}_{ts}{ext}"
                    backup_path = os.path.join(dst_folder, backup_name)
                    os.rename(dst_file, backup_path)
                    log_fn(f"[{ip}] 백업: {fname} → {backup_name}")

                shutil.copy2(src_file, dst_file)
                log_fn(f"[{ip}] ✓ 복사: {fname}")

            log_fn(f"[{ip}] ◆ 완료 → {dst_folder}")
            success = True
        except Exception as e:
            log_fn(f"[{ip}] ✗ 오류: {e}")
            failed.append(ip)
        finally:
            if connected:
                subprocess.run(["net", "use", unc_root, "/delete", "/yes"],
                               capture_output=True)
        if done_fn:
            done_fn(ip, success)

    threads = [
        threading.Thread(target=_deploy_single_to_client, args=(ip, user, pw), daemon=True)
        for ip, (user, pw) in targets.items()
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log_fn("◆ 단일 배포 전체 완료")
    return len(failed)


def run_http_server():
    server = HTTPServer((SERVER_HOST, SERVER_PORT), AuroraHandler)
    server.serve_forever()

def check_offline():
    while True:
        now = time.time()
        ts  = datetime.now().strftime("%H:%M:%S")
        with state_lock:
            for ip, info in shared_state["clients"].items():
                last = info["last_seen"]
                if last and (now - last) > 10 and info["online"]:
                    info["online"] = False
                    info["aurora_running"] = False
                    shared_state["log"].append(f"[{ts}] [{ip}] 오프라인")
                    shared_state["log_seq"] += 1
            if len(shared_state["log"]) > 300:
                del shared_state["log"][:-300]
        time.sleep(3)

# ─────────────────────────────────────────
# GUI
# ─────────────────────────────────────────
_DISABLED_BG = "#EEF1F3"


class _StyledButton(tk.Button):
    """테두리(1px)를 가진 버튼. Windows 의 tk.Button 은 highlightthickness 테두리가 그려지지 않으므로
    테두리색 Frame 으로 감싸고, pack/pack_forget 은 그 Frame 에 적용한다.
    state 변경 시 disabled 스타일(회색 배경)을 자동 적용/복원한다."""

    def __init__(self, parent, border, **kw):
        self._border_frame = tk.Frame(parent, bg=border)
        super().__init__(self._border_frame, **kw)
        tk.Button.pack(self, padx=1, pady=1)

    def pack(self, cnf=None, **kw):
        self._border_frame.pack(cnf or {}, **kw)

    def pack_forget(self):
        self._border_frame.pack_forget()

    def set_border(self, color):
        self.base_border = color
        if str(self.cget("state")) != "disabled":
            self._border_frame.configure(bg=color)

    def configure(self, cnf=None, **kw):
        result = super().configure(cnf, **kw)
        if "state" in kw:
            self._apply_state()
        return result

    config = configure

    def _apply_state(self):
        if str(self.cget("state")) == "disabled":
            super().configure(bg=_DISABLED_BG)
            self._border_frame.configure(bg=_DISABLED_BG)
        else:
            super().configure(bg=self.base_bg)
            self._border_frame.configure(bg=self.base_border)


class _Tooltip:
    """위젯에 마우스를 올리고 잠시(500ms) 있으면 설명(hint)을 띄우는 툴팁.
    text 는 문자열 또는 문자열을 반환하는 callable(동적 설명)."""

    DELAY_MS = 500

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self._after_id = None
        self._tip_win = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self):
        if self._after_id is None:
            return
        try:
            self.widget.after_cancel(self._after_id)
        except tk.TclError:
            pass
        self._after_id = None

    def _show(self):
        self._after_id = None
        if self._tip_win is not None:
            return
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        try:
            x = self.widget.winfo_pointerx() + _px(12)
            y = self.widget.winfo_pointery() + _px(18)
            win = tk.Toplevel(self.widget)
            win.wm_overrideredirect(True)
            win.attributes("-topmost", True)
            tk.Label(win, text=text, justify="left", bg="#1D2327", fg="#FFFFFF",
                     font=("맑은 고딕", 9), padx=_px(10), pady=_px(6),
                     wraplength=_px(320), bd=0).pack()
            win.update_idletasks()
            # 화면 오른쪽/아래를 넘으면 안쪽으로 당김
            x = max(0, min(x, self.widget.winfo_screenwidth() - win.winfo_reqwidth()))
            y = max(0, min(y, self.widget.winfo_screenheight() - win.winfo_reqheight()))
            win.wm_geometry(f"+{x}+{y}")
            self._tip_win = win
        except tk.TclError:
            self._tip_win = None

    def _hide(self, _event=None):
        self._cancel()
        if self._tip_win is None:
            return
        try:
            self._tip_win.destroy()
        except tk.TclError:
            pass
        self._tip_win = None


class AuroraServerApp:

    # ── 색상 팔레트 ──
    BG            = "#F3F5F7"
    PANEL         = "#FFFFFF"
    PANEL_ALT     = "#F7F8FA"   # 표 헤더 배경
    BORDER        = "#E1E5E8"
    INPUT_BORDER  = "#CDD3D7"
    ROW_LINE      = "#EEF1F3"   # 표 행 구분선
    TEXT          = "#111418"
    TEXT_DIM      = "#5F686B"
    DISABLED_TEXT = "#9AA2A5"
    BRAND         = "#2F98F9"
    BLUE          = "#1A6FC9"
    BLUE_H        = "#155CA8"
    NAV_ON_BG     = "#E8F2FE"
    GREEN         = "#138A43"
    GREEN_H       = "#0F7036"
    RED           = "#C9372C"
    RED_H         = "#A82C23"
    YELLOW        = "#9A5700"
    OK_BG         = "#E7F6EC"
    OK_BG_H       = "#D4EDDC"
    OK_BORDER     = "#9FD5B3"
    ERR_BG        = "#FDECEA"
    WARN_BG       = "#FFF4E0"
    IDLE_BG       = "#EEF1F3"
    DANGER_BORDER = "#E8A9A3"
    LOG_BG        = "#1D2327"
    LOG_FG        = "#D5DADD"
    BTN_START     = GREEN
    BTN_START_H   = GREEN_H
    BTN_STOP      = RED
    BTN_STOP_H    = RED_H
    BTN_DEPLOY    = BLUE
    BTN_DEPLOY_H  = BLUE_H
    WHITE         = "#FFFFFF"
    SELECT_BG     = NAV_ON_BG

    # ── 폰트 ──
    FONT            = ("맑은 고딕", 9)
    FONT_DESC       = ("맑은 고딕", 9)
    FONT_BOLD       = ("맑은 고딕", 9, "bold")
    FONT_TITLE      = ("맑은 고딕", 10, "bold")
    FONT_APP_TITLE  = ("맑은 고딕", 11, "bold")
    FONT_NAV        = ("맑은 고딕", 10)
    FONT_NAV_ON     = ("맑은 고딕", 10, "bold")
    FONT_SMALL      = ("맑은 고딕", 8, "bold")
    FONT_SMALL_BOLD = ("맑은 고딕", 8, "bold")
    FONT_MONO       = ("맑은 고딕", 9)
    FONT_MONO_BOLD  = ("맑은 고딕", 9, "bold")
    FONT_MONO_L     = ("맑은 고딕", 9)

    ROW_H = 38      # 표 데이터 행 높이
    HDR_H = 32      # 표 헤더 행 높이

    # ── 라벨 설명(툴팁) — 라벨 텍스트(또는 지정 키) → 설명. _field_label / _table_header / _table_card 등이 조회 ──
    LABEL_HINTS = {
        # 헤더 / 사이드바
        "SERVER": "이 프로그램이 서버(관리 PC) 역할로 실행 중임을 나타내는 표시입니다.",
        "버전": "현재 실행 중인 서버 버전입니다. D:\\RemoteConfig\\Update 폴더에 새 버전(AuroraServer.exe + version.json)을 넣어두면 켜질 때 자동으로 업데이트 후 재시작합니다. '최신 버전 확인' 을 누르면 바로 확인해 새 버전이면 업데이트 후 재시작합니다.",
        "온라인 카운트": "온라인 클라이언트 수 / 등록된 전체 클라이언트 수입니다. "
                        "클라이언트는 3초마다 상태를 보고하며, 10초 이상 보고가 없으면 오프라인으로 바뀝니다. "
                        "화면은 2초마다 갱신됩니다.",
        "서버 상태": "클라이언트의 상태 보고(POST /status)를 받는 HTTP 서버의 상태입니다. "
                    "':숫자'는 수신 포트이며, 포트를 바꾸려면 저장 후 프로그램을 재시작해야 합니다.",
        "서버 뷰": "전체/현재 공정 실행·종료, 핑 모드, 포트·자동 시작·버전명 설정, 클라이언트 IP 관리를 하는 화면입니다.",
        "배포 뷰": "소스 폴더의 하위 폴더를 클라이언트별로 골라 각 PC 의 D: 드라이브(Aurora_EXE_버전명 폴더)로 복사하는 화면입니다.",
        "단일 배포 뷰": "선택한 파일만 클라이언트의 같은 경로로 복사하는 화면입니다. 기존 파일은 백업 후 교체합니다.",
        # 서버 뷰 — 제어
        "전체 공정": "아래 [실행]/[종료] 는 온라인인 모든 클라이언트에 start/stop 명령을 1회 전송합니다. "
                    "(클라이언트 포트로 POST /command) 오프라인 클라이언트에는 보내지 않습니다.",
        "현재 공정": "아래 [실행]/[종료] 는 '현재 공정 IP 관리' 목록의 IP 에만 명령을 전송합니다. "
                    "온라인 여부와 관계없이 목록의 모든 IP 로 전송을 시도하며, 목록이 비어 있으면 경고가 표시됩니다.",
        "핑 모드": "Manual: [전체 핑] 버튼을 누를 때만 핑을 확인합니다. Auto: 5초마다 자동으로 확인합니다. "
                  "선택한 모드는 온라인 클라이언트에도 전달됩니다. 핑 결과 색상은 10ms 미만 녹색, 50ms 미만 노랑, 그 이상·시간초과 빨강입니다.",
        # 서버 뷰 — 설정
        "포트 설정": "서버/클라이언트 통신 포트를 정합니다. [적용] 은 설정 파일(D:\\RemoteConfig\\server_config.json)에 "
                    "저장만 하며, 프로그램을 재시작해야 반영됩니다. 1024~65535 사이의 숫자만 가능합니다.",
        "서버 포트": "클라이언트가 3초마다 상태를 보고하는 이 서버의 수신 포트입니다. (기본 9000) "
                    "변경 후 저장하고 프로그램을 재시작해야 합니다.",
        "클라이언트 포트": "서버가 클라이언트에 명령(start/stop/setpath/ping/setpingmode)을 보내는 포트입니다. (기본 9001) "
                          "클라이언트가 수신하는 포트와 같아야 하며, 변경 후 재시작이 필요합니다.",
        "자동 시작": "Windows 로그인 시 이 서버 프로그램을 자동 실행할지 설정합니다. "
                    "버튼을 누르면 현재 사용자 시작 프로그램(레지스트리)에 등록/해제됩니다.",
        "배포 버전명": "배포 뷰와 공유되는 버전명입니다. [적용] 을 눌러야 확정되며, 배포 시 클라이언트에 "
                      "Aurora_EXE_<버전명> 폴더로 복사됩니다. [전체 경로 적용] 은 온라인 클라이언트의 Aurora 경로를 이 폴더로 바꿉니다.",
        # 표 제목 / 로그 제목
        "전체 공정 IP 관리": "등록된 모든 클라이언트 PC 목록과 상태입니다. 추가/삭제하면 배포 뷰·단일 배포 뷰 목록에도 반영됩니다. "
                            "이름은 더블클릭해서 바꿀 수 있습니다.",
        "현재 공정 IP 관리": "'현재 공정' 실행/종료 명령의 대상만 모아둔 IP 목록입니다. "
                            "전체 공정 목록과 별도로 저장되며, 핑은 이 목록의 IP 도 확인합니다.",
        "배포 LOG": "배포 관련(배포·복사·공유 등) 로그만 모아 보여줍니다. 최근 50줄만 유지됩니다.",
        "배포 대상 클라이언트": "배포할 클라이언트 목록입니다. 체크된 클라이언트만 배포하며, "
                              "클라이언트를 순차적으로 하나씩 복사합니다.",
        "단일 배포 LOG": "단일 배포의 진행 로그(연결·백업·복사·완료/오류)를 보여줍니다.",
        "파일 목록": "[적용]한 소스 폴더 바로 아래의 파일 목록입니다. (하위 폴더 제외) 체크된 파일만 배포됩니다.",
        "배포 대상": "파일을 복사할 클라이언트 목록입니다. 체크된 클라이언트에만 동시에(병렬) 배포합니다. "
                    "접속 계정은 서버 뷰 표의 ID/PW 를 사용합니다.",
        "전체 공정 개수": "등록된 클라이언트 수입니다.",
        "현재 공정 개수": "'현재 공정' 목록에 등록된 IP 수입니다.",
        # 표 열 헤더 / 행 상태
        "CLIENT": "클라이언트 PC 의 이름입니다. (전체 공정 IP 관리 표에서는 이름을 더블클릭해서 바꿀 수 있습니다.)",
        "IP": "클라이언트 PC 의 IP 주소입니다. 명령 전송, 핑, 공유 폴더 접속에 이 주소를 사용합니다.",
        "연결 상태": "클라이언트의 상태 보고 수신 여부입니다. 3초마다 보고를 받으며, 10초 이상 없으면 오프라인이 됩니다.",
        "Aurora": "온라인 클라이언트에서 Aurora 프로그램이 실행 중인지(실행중/중지) 보여줍니다. 오프라인이면 —로 표시됩니다.",
        "마지막 응답": "마지막 상태 보고를 받은 뒤 경과한 시간입니다. 5초 미만 녹색, 10초 미만 노랑, 그 이상은 회색입니다.",
        "핑": "서버에서 클라이언트로 보낸 핑 응답 시간입니다. 10ms 미만 녹색, 50ms 미만 노랑, 그 이상·시간초과 빨강입니다. "
             "갱신은 핑 모드(Manual/Auto)를 따릅니다.",
        "ID": "단일 배포 시 이 PC 의 공유 폴더(\\\\IP\\드라이브)에 접속할 Windows 계정 ID 입니다. 입력 즉시 설정 파일에 저장됩니다.",
        "PW": "단일 배포 시 이 PC 공유 폴더에 접속할 계정의 비밀번호입니다. 입력 즉시 설정 파일에 저장됩니다.",
        "경로": "클라이언트가 보고한 Aurora 실행 경로입니다. 배포 완료 후 setpath 로 갱신되며, 아직 없으면 —로 표시됩니다.",
        "선택": "체크한 클라이언트만 배포 대상이 됩니다.",
        "소스 폴더 선택": "이 클라이언트에 복사할 소스 폴더입니다. 목록은 배포 소스 폴더의 하위 폴더이며, "
                        "선택값은 [적용] 을 눌러야 저장됩니다.",
        "진행률": "이 클라이언트로 복사한 파일 수 / 전체 파일 수입니다.",
        "저장 경로": "클라이언트 D: 드라이브 기준의 복사 위치입니다. (추가 저장 경로\\Aurora_EXE_버전명)",
        "배포 상태": "클라이언트별 배포 결과입니다. 배포 예정 → 배포 중... → 배포 완료 또는 실패로 바뀝니다.",
        "이름 변경": "이름을 더블클릭하면 새 이름을 입력해 바꿀 수 있습니다.",
        # 배포 뷰 — 입력 라벨
        "배포 소스 폴더": "배포할 버전 폴더들이 들어 있는 상위 폴더입니다. [적용] 하면 이 폴더의 하위 폴더 목록이 "
                        "클라이언트별 소스 폴더 선택 목록에 채워지고 설정에 저장됩니다.",
        "내부 경로": "선택한 하위 폴더 안에서 실제로 복사할 폴더입니다. (예: x64-Debug) 비우면 하위 폴더 전체를 복사합니다. "
                    "[적용] 을 눌러 저장합니다.",
        "추가 저장 경로": "클라이언트 D: 드라이브 아래에서 Aurora_EXE_버전명 폴더를 만들 상위 폴더입니다. "
                        "비우면 D: 바로 아래에 만듭니다.",
        "저장 경로 미리보기": "클라이언트에 복사될 최종 경로 미리보기입니다. 버전명이 비어 있으면 Aurora_EXE_버전명 으로 표시됩니다.",
        "전체 진행률": "이번 배포에서 완료된 클라이언트 수 / 대상 수입니다. 성공한 클라이언트에는 완료 후 "
                      "Aurora 경로(setpath)가 자동 전송됩니다.",
        "배포 상태 메시지": "배포·적용 작업의 진행 상태와 결과(성공 녹색, 진행 중 노랑, 실패·중단 빨강)를 보여줍니다.",
        "전체 진행률 수치": "완료한 클라이언트 수 / 배포 대상 클라이언트 수입니다.",
        # 단일 배포 뷰
        "단일 배포 소스 폴더": "배포할 파일이 들어 있는 폴더입니다. 클라이언트에도 같은 드라이브·같은 경로의 폴더로 복사됩니다. "
                              "기존 파일은 이름_YYYYMMDD_HHmmss.확장자 로 백업한 뒤 교체합니다.",
        "단일 배포 상태 메시지": "[적용]/배포 결과를 보여줍니다. (파일 로드 수, 배포 중, 완료·실패 대수)",
        "전체 배포": "이번 단일 배포에서 완료된 클라이언트 수 / 배포 대상 수입니다.",
        "단일 현재 공정": "배포 대상 중 '현재 공정' 목록에 포함된 클라이언트의 완료 수 / 전체 수입니다.",
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Aurora Remote Controller  |  Server")
        self.root.geometry(f"{_px(1300)}x{_px(860)}")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)
        self.quit_app = root.destroy   # main() 에서 트레이 종료까지 포함한 함수로 교체

        self.client_rows        = {}
        self.deploy_check_vars  = {}
        self.deploy_combo_vars  = {}
        self.deploy_combos      = {}
        self.deploy_dst_labels  = {}
        # 서버 뷰 입력 필드용 (타이핑 전용)
        self.version_input_var   = tk.StringVar(value=config.get("deploy_version", ""))
        # 배포 뷰 라벨용 (적용 버튼 클릭 시에만 갱신)
        self.shared_version_var  = tk.StringVar(value=config.get("deploy_version", ""))
        # 단일 배포 뷰
        self.single_file_vars         = {}   # {filename: BooleanVar}
        self.single_client_vars       = {}   # {ip: BooleanVar}
        self.single_client_users      = {}   # {ip: StringVar} — 서버 뷰에서 관리
        self.single_client_pws        = {}   # {ip: StringVar} — 서버 뷰에서 관리
        self.single_client_status_lbls = {}  # {ip: Label} — 배포 상태 표시

        self._build_ui()
        self._start_services()
        self._refresh_loop()

    # ── UI 최상위 ─────────────────────────
    def _build_ui(self):
        # 헤더 (로고 + 앱 이름 + SERVER 배지 …… HTTP 서버 상태 / 온라인 카운트)
        hdr = tk.Frame(self.root, bg=self.PANEL, height=_px(56))
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        logo = self._load_logo(_px(22))
        if logo:
            tk.Label(hdr, image=logo, bg=self.PANEL, bd=0).pack(side="left", padx=(_px(20), 0))
            tk.Frame(hdr, bg=self.BORDER, width=max(1, _px(1)), height=_px(22)).pack(side="left", padx=_px(16))
            title_padx = (0, 0)
        else:
            title_padx = (_px(20), 0)
        tk.Label(hdr, text="Aurora Remote Controller",
                 font=self.FONT_APP_TITLE, fg=self.TEXT, bg=self.PANEL).pack(side="left", padx=title_padx)
        server_badge = tk.Label(hdr, text="SERVER", font=self.FONT_SMALL, padx=_px(8), pady=_px(2),
                                fg=self.TEXT_DIM, bg=self.IDLE_BG)
        server_badge.pack(side="left", padx=(_px(16), 0))
        self._tip_key(server_badge, "SERVER")
        ver_badge = tk.Label(hdr, text=f"v{APP_VERSION}", font=self.FONT_SMALL, padx=_px(8), pady=_px(2),
                             fg=self.TEXT_DIM, bg=self.IDLE_BG)
        ver_badge.pack(side="left", padx=(_px(6), 0))
        self._tip_key(ver_badge, "버전")

        self.online_label = tk.Label(hdr, text="온라인: 0 / 0",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL)
        self.online_label.pack(side="right", padx=(_px(16), _px(20)))
        self._tip_key(self.online_label, "온라인 카운트")
        self.server_status_lbl = tk.Label(hdr, text="", font=self.FONT_SMALL,
                 padx=_px(8), pady=_px(2), fg=self.YELLOW, bg=self.PANEL)
        self.server_status_lbl.pack(side="right")
        self._tip_key(self.server_status_lbl, "서버 상태")
        self._set_badge(self.server_status_lbl, "● HTTP 서버 시작 중...", self.YELLOW)
        self._update_btn = self._sec_btn(hdr, "최신 버전 확인", lambda: self._check_update(manual=True))
        self._update_btn.pack(side="right", padx=(0, _px(16)))
        self._tip_key(self._update_btn, "버전")

        tk.Frame(self.root, bg=self.BORDER, height=max(1, _px(1))).pack(fill="x")

        # 본문: 사이드바 + 컨텐츠
        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True)

        nav = tk.Frame(body, bg=self.PANEL, width=_px(200))
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)
        tk.Frame(body, bg=self.BORDER, width=max(1, _px(1))).pack(side="left", fill="y")
        tk.Frame(nav, bg=self.PANEL, height=_px(12)).pack(fill="x")

        content = tk.Frame(body, bg=self.BG)
        content.pack(side="left", fill="both", expand=True)

        server_tab        = tk.Frame(content, bg=self.BG)
        deploy_tab        = tk.Frame(content, bg=self.BG)
        single_deploy_tab = tk.Frame(content, bg=self.BG)
        self._pages = [server_tab, deploy_tab, single_deploy_tab]
        self._nav_items = []
        for idx, text in enumerate(["서버 뷰", "배포 뷰", "단일 배포 뷰"]):
            self._nav_items.append(self._make_nav_item(nav, text, idx))

        self._build_server_tab(server_tab)
        self._build_deploy_tab(deploy_tab)
        self._build_single_deploy_tab(single_deploy_tab)
        self._select_view(0)

    def _make_nav_item(self, nav, text: str, idx: int):
        row = tk.Frame(nav, bg=self.PANEL, height=_px(40), cursor="hand2")
        row.pack(fill="x")
        row.pack_propagate(False)
        bar = tk.Frame(row, bg=self.PANEL, width=_px(3))
        bar.pack(side="left", fill="y")
        lbl = tk.Label(row, text=text, font=self.FONT_NAV, fg=self.TEXT_DIM,
                       bg=self.PANEL, anchor="w", cursor="hand2")
        lbl.pack(side="left", fill="both", expand=True, padx=(_px(13), 0))
        self._tip_key(lbl, text)
        for w in (row, bar, lbl):
            w.bind("<Button-1>", lambda e, i=idx: self._select_view(i))
        return row, bar, lbl

    def _select_view(self, idx: int):
        """사이드바 메뉴 선택 — 해당 뷰 프레임만 표시"""
        for i, page in enumerate(self._pages):
            row, bar, lbl = self._nav_items[i]
            if i == idx:
                page.pack(fill="both", expand=True, padx=_px(24), pady=_px(20))
                row.config(bg=self.NAV_ON_BG)
                bar.config(bg=self.BRAND)
                lbl.config(bg=self.NAV_ON_BG, fg=self.BLUE, font=self.FONT_NAV_ON)
            else:
                page.pack_forget()
                row.config(bg=self.PANEL)
                bar.config(bg=self.PANEL)
                lbl.config(bg=self.PANEL, fg=self.TEXT_DIM, font=self.FONT_NAV)

    def _load_logo(self, height: int):
        """assets/ubisam_logo.png 를 높이에 맞춰 로드 (실패 시 None → 로고 생략)"""
        try:
            img = PILImage.open(_resource_path(os.path.join("assets", "ubisam_logo.png"))).convert("RGB")
            width = max(1, int(img.width * height / img.height))
            resample = getattr(PILImage, "Resampling", PILImage).LANCZOS
            img = img.resize((width, height), resample)
            self._logo_img = ImageTk.PhotoImage(img)
            return self._logo_img
        except Exception:
            return None

    # ── 서버 뷰 탭 ───────────────────────
    def _build_server_tab(self, parent):
        # 제어 버튼
        ctrl = self._card(parent)
        ctrl.pack(fill="x")
        inner = tk.Frame(ctrl, bg=self.PANEL)
        inner.pack(fill="x", padx=_px(20), pady=_px(16))

        # 전체 공정
        self._tip_key(tk.Label(inner, text="전체 공정",
                               font=self.FONT_TITLE, fg=self.TEXT, bg=self.PANEL),
                      "전체 공정").pack(side="left", padx=(0, _px(12)))
        self._btn(inner, "▶  실행", self.BTN_START, self.BTN_START_H,
                  self._cmd_start).pack(side="left", padx=(0, _px(8)))
        self._btn(inner, "■  종료", self.BTN_STOP, self.BTN_STOP_H,
                  self._cmd_stop).pack(side="left")

        # 구분선
        tk.Frame(inner, bg=self.BORDER, width=max(1, _px(1)), height=_px(28)).pack(side="left", padx=_px(28))

        # 현재 공정
        self._tip_key(tk.Label(inner, text="현재 공정",
                               font=self.FONT_TITLE, fg=self.TEXT, bg=self.PANEL),
                      "현재 공정").pack(side="left", padx=(0, _px(12)))
        self._btn(inner, "▶  실행", self.BTN_START, self.BTN_START_H,
                  self._cmd_start_current).pack(side="left", padx=(0, _px(8)))
        self._btn(inner, "■  종료", self.BTN_STOP, self.BTN_STOP_H,
                  self._cmd_stop_current).pack(side="left")

        # 구분선
        tk.Frame(inner, bg=self.BORDER, width=max(1, _px(1)), height=_px(28)).pack(side="left", padx=_px(28))

        # 핑 모드
        self._tip_key(tk.Label(inner, text="핑 모드",
                               font=self.FONT_TITLE, fg=self.TEXT, bg=self.PANEL),
                      "핑 모드").pack(side="left", padx=(0, _px(12)))
        ping_btn_row = tk.Frame(inner, bg=self.PANEL)
        ping_btn_row.pack(side="left")
        self._ping_mode = config.get("ping_mode", "manual")
        self.ping_manual_btn = self._sec_btn(ping_btn_row, "Manual",
                                             lambda: self._set_ping_mode("manual"))
        self.ping_manual_btn.pack(side="left")
        self.ping_auto_btn = self._sec_btn(ping_btn_row, "Auto",
                                           lambda: self._set_ping_mode("auto"))
        self.ping_auto_btn.pack(side="left")
        self.ping_now_btn = self._sec_btn(inner, "전체 핑", self._cmd_ping_now)
        self.ping_now_btn.pack(side="left", padx=(_px(8), 0))
        self._update_ping_mode_btns()

        # 설정 카드 (포트 / 자동 시작 / 버전명)
        port_frame = self._card(parent)
        port_frame.pack(fill="x", pady=(_px(14), 0))

        port_row = tk.Frame(port_frame, bg=self.PANEL)
        port_row.pack(fill="x", padx=_px(20), pady=(_px(16), _px(10)))

        title_cell = tk.Frame(port_row, bg=self.PANEL, width=_px(92), height=_px(34))
        title_cell.pack(side="left")
        title_cell.pack_propagate(False)
        self._tip_key(tk.Label(title_cell, text="포트 설정",
                               font=self.FONT_TITLE, fg=self.TEXT, bg=self.PANEL),
                      "포트 설정").pack(side="left")

        self._tip_key(tk.Label(port_row, text="서버 포트",
                               font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL),
                      "서버 포트").pack(side="left", padx=(0, _px(8)))
        self.server_port_var = tk.StringVar(value=str(config.get("server_port", 9000)))
        self._entry(port_row, self.server_port_var, 10).pack(side="left", ipady=_px(1), padx=(0, _px(20)))

        self._tip_key(tk.Label(port_row, text="클라이언트 포트",
                               font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL),
                      "클라이언트 포트").pack(side="left", padx=(0, _px(8)))
        self.client_port_var = tk.StringVar(value=str(config.get("client_port", 9001)))
        self._entry(port_row, self.client_port_var, 10).pack(side="left", ipady=_px(1), padx=(0, _px(8)))

        self._sec_btn(port_row, "적용 (재시작 필요)", self._apply_ports).pack(side="left")

        # ── PC 시작 시 자동 실행 ─────────────
        tk.Frame(port_row, bg=self.BORDER, width=max(1, _px(1)), height=_px(28)).pack(side="left", padx=_px(28))
        self._tip_key(tk.Label(port_row, text="자동 시작",
                               font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL),
                      "자동 시작").pack(side="left", padx=(0, _px(8)))
        self._autostart_btn = self._sec_btn(
            port_row,
            "ON  ●" if autostart_enabled() else "OFF ○",
            self._toggle_autostart)
        self._autostart_btn.pack(side="left")
        self._set_autostart_look(autostart_enabled())
        tk.Label(port_row, text="  PC 시작 시 서버 자동 실행",
                 font=self.FONT_DESC, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")

        # 버전명 (배포 뷰와 공유)
        ver_row = tk.Frame(port_frame, bg=self.PANEL)
        ver_row.pack(fill="x", padx=_px(20), pady=(0, _px(16)))
        ver_title_cell = tk.Frame(ver_row, bg=self.PANEL, width=_px(92), height=_px(34))
        ver_title_cell.pack(side="left")
        ver_title_cell.pack_propagate(False)
        self._tip_key(tk.Label(ver_row, text="배포 버전명",
                               font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL),
                      "배포 버전명").pack(side="left", padx=(0, _px(8)))
        self._entry(ver_row, self.version_input_var, 25).pack(side="left", ipady=_px(1), padx=(0, _px(8)))
        self._sec_btn(ver_row, "적용", self._apply_version).pack(side="left", padx=(0, _px(8)))
        self._btn(ver_row, "전체 경로 적용", self.GREEN, self.GREEN_H,
                  self._apply_path_to_all).pack(side="left", padx=(0, _px(16)))
        tk.Label(ver_row, text="← 배포 뷰와 공유됩니다",
                 font=self.FONT_DESC, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")

        # 현재 공정 IP 관리 (하단 고정 높이)
        cur_tbl, cur_tb = self._table_card(parent, "현재 공정 IP 관리")
        cur_tbl.pack(side="bottom", fill="x", pady=(_px(14), 0))
        self.current_count_lbl = self._count_badge(cur_tb)
        self._tip_key(self.current_count_lbl, "현재 공정 개수")
        cur_btn_row = tk.Frame(cur_tb, bg=self.PANEL)
        cur_btn_row.pack(side="right")
        self._sec_btn(cur_btn_row, "+ 추가", self._add_current_ip).pack(side="left", padx=(_px(8), 0))
        self._sec_btn(cur_btn_row, "− 삭제", self._del_current_ip).pack(side="left", padx=(_px(8), 0))

        self._table_header(cur_tbl, self.CUR_COLS)
        self.current_rows_frame = self._table_region(cur_tbl, height=(self.ROW_H + 1) * 3)
        self.current_client_rows = {}

        # 클라이언트 IP 관리 (남는 높이 채움)
        tbl_outer, all_tb = self._table_card(parent, "전체 공정 IP 관리")
        tbl_outer.pack(fill="both", expand=True, pady=(_px(14), 0))
        self.all_count_lbl = self._count_badge(all_tb)
        self._tip_key(self.all_count_lbl, "전체 공정 개수")
        btn_row = tk.Frame(all_tb, bg=self.PANEL)
        btn_row.pack(side="right")
        self._sec_btn(btn_row, "새로고침", self._refresh_paths).pack(side="left", padx=(_px(8), 0))
        self._sec_btn(btn_row, "+ 추가", self._add_ip).pack(side="left", padx=(_px(8), 0))
        self._sec_btn(btn_row, "− 삭제", self._del_ip).pack(side="left", padx=(_px(8), 0))

        self._table_header(tbl_outer, self.ALL_COLS)
        self.rows_frame = self._table_region(tbl_outer)

        self._rebuild_current_rows()

        self._rebuild_rows()

    # 표 열 정의: (제목, 폭(px), side) — 폭 None 은 남는 폭 채움. 헤더와 행이 같은 정의를 공유.
    ALL_COLS = [("CLIENT", 98), ("IP", 138), ("연결 상태", 104), ("Aurora", 98),
                ("마지막 응답", 98), ("핑", 78), ("ID", 108), ("PW", 108), ("경로", None)]
    CUR_COLS = [("CLIENT", 98), ("IP", 138), ("연결 상태", 104), ("Aurora", 98),
                ("마지막 응답", 98), ("핑", 78)]
    DEPLOY_COLS = [("선택", 48), ("CLIENT", 98), ("IP", 138), ("소스 폴더 선택", 198),
                   ("진행률", 220, "right"), ("저장 경로", None)]
    SINGLE_COLS = [("", 32), ("CLIENT", 118), ("IP", 158), ("배포 상태", None)]

    # ── 배포 뷰 탭 ───────────────────────
    def _build_deploy_tab(self, parent):
        # 소스 폴더 선택
        src_panel = self._card(parent)
        src_panel.pack(fill="x")
        src_body = tk.Frame(src_panel, bg=self.PANEL)
        src_body.pack(fill="x", padx=_px(20), pady=_px(16))

        src_row = tk.Frame(src_body, bg=self.PANEL)
        src_row.pack(fill="x")
        self._field_label(src_row, "배포 소스 폴더")

        self.deploy_src_var = tk.StringVar(value=config.get("deploy_src", ""))
        self._entry(src_row, self.deploy_src_var, 55).pack(side="left", ipady=_px(1), padx=(0, _px(8)))

        self._sec_btn(src_row, "폴더 선택", self._browse_deploy_src).pack(side="left", padx=(0, _px(8)))
        self._sec_btn(src_row, "적용", self._apply_deploy_src).pack(side="left")
        self.deploy_stop_btn = self._btn(src_row, "■ 중단", self.BTN_STOP, self.BTN_STOP_H,
                                         self._cmd_deploy_stop, padx=20, pady=9)
        self.deploy_stop_btn.pack(side="right")
        self.deploy_stop_btn.config(state="disabled")
        self._btn(src_row, "▲ 배포하기", self.BTN_DEPLOY, self.BTN_DEPLOY_H,
                  self._cmd_deploy_selected, padx=20, pady=9).pack(side="right", padx=(0, _px(8)))

        # 내부 경로 (소스 폴더 안의 실제 파일 위치)
        inner_row = tk.Frame(src_body, bg=self.PANEL)
        inner_row.pack(fill="x", pady=(_px(10), 0))
        self._field_label(inner_row, "내부 경로")
        self.deploy_inner_var = tk.StringVar(value=config.get("deploy_inner_path", ""))
        self._entry(inner_row, self.deploy_inner_var, 25).pack(side="left", ipady=_px(1), padx=(0, _px(8)))
        self._sec_btn(inner_row, "적용", self._apply_deploy_inner).pack(side="left", padx=(0, _px(10)))
        tk.Label(inner_row, text="예: x64-Debug  (비우면 소스 폴더 직접 사용)",
                 font=self.FONT_DESC, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")

        # 추가 저장 경로
        dst_row = tk.Frame(src_body, bg=self.PANEL)
        dst_row.pack(fill="x", pady=(_px(10), 0))
        self._field_label(dst_row, "추가 저장 경로")
        self.deploy_dst_folder_var = tk.StringVar(value=config.get("deploy_dst_folder", ""))
        self._entry(dst_row, self.deploy_dst_folder_var, 25).pack(side="left", ipady=_px(1), padx=(0, _px(8)))
        self._sec_btn(dst_row, "적용", self._apply_deploy_dst_folder).pack(side="left", padx=(0, _px(10)))
        self.deploy_dst_preview_lbl = tk.Label(dst_row,
                 text=self._dst_preview_text(),
                 font=self.FONT_MONO, fg=self.BLUE, bg=self.PANEL)
        self.deploy_dst_preview_lbl.pack(side="left")
        self._tip_key(self.deploy_dst_preview_lbl, "저장 경로 미리보기")

        # 전체 진행률 (클라이언트 목록 위)
        prog_card = self._card(parent)
        prog_card.pack(fill="x", pady=(_px(14), 0))
        overall_wrap = tk.Frame(prog_card, bg=self.PANEL)
        overall_wrap.pack(fill="x", padx=_px(20), pady=_px(16))
        self._tip_key(tk.Label(overall_wrap, text="전체 진행률", font=self.FONT_TITLE,
                               fg=self.TEXT, bg=self.PANEL),
                      "전체 진행률").pack(side="left", padx=(0, _px(14)))
        self.deploy_status_lbl = tk.Label(overall_wrap, text="",
                 font=self.FONT_SMALL, padx=_px(8), pady=_px(2), fg=self.TEXT_DIM, bg=self.PANEL,
                 wraplength=_px(460), justify="left")
        self.deploy_status_lbl.pack(side="right", padx=(_px(14), 0))
        self._tip_key(self.deploy_status_lbl, "배포 상태 메시지")
        self.deploy_overall_lbl = tk.Label(overall_wrap, text="0 / 0",
                 font=self.FONT_MONO_BOLD, fg=self.TEXT, bg=self.PANEL, width=8, anchor="e")
        self.deploy_overall_lbl.pack(side="right", padx=(_px(14), 0))
        self._tip_key(self.deploy_overall_lbl, "전체 진행률 수치")
        self.deploy_progress_overall = ttk.Progressbar(overall_wrap, orient="horizontal",
                                                       mode="determinate")
        self.deploy_progress_overall.pack(side="left", fill="x", expand=True)

        # 배포 로그 (하단 고정)
        self.deploy_log_text = self._build_log_view(parent, "배포 LOG", 170)

        # 클라이언트 목록
        tbl_outer, hdr_row = self._table_card(parent, "배포 대상 클라이언트")
        tbl_outer.pack(fill="both", expand=True, pady=(_px(14), 0))
        btn_row = tk.Frame(hdr_row, bg=self.PANEL)
        btn_row.pack(side="right")
        self._sec_btn(btn_row, "새로고침", self._refresh_deploy_src).pack(side="left", padx=(_px(8), 0))
        self._sec_btn(btn_row, "적용", self._save_deploy_combos).pack(side="left", padx=(_px(8), 0))
        self._sec_btn(btn_row, "전체 선택", self._deploy_check_all).pack(side="left", padx=(_px(8), 0))
        self._sec_btn(btn_row, "전체 해제", self._deploy_uncheck_all).pack(side="left", padx=(_px(8), 0))

        hdr_labels = self._table_header(tbl_outer, self.DEPLOY_COLS)
        self.deploy_dst_col_hdr = hdr_labels[-1]
        self.deploy_dst_col_hdr.config(text=self._deploy_dst_col_text())

        self.deploy_rows_frame = self._table_region(tbl_outer)

        self._rebuild_deploy_rows()

    # ── 단일 배포 뷰 탭 ──────────────────
    def _build_single_deploy_tab(self, parent):

        # 상단 컨트롤
        ctrl_panel = self._card(parent)
        ctrl_panel.pack(fill="x")
        ctrl_body = tk.Frame(ctrl_panel, bg=self.PANEL)
        ctrl_body.pack(fill="x", padx=_px(20), pady=_px(16))

        ctrl_row = tk.Frame(ctrl_body, bg=self.PANEL)
        ctrl_row.pack(fill="x")
        self._tip_key(tk.Label(ctrl_row, text="배포 소스 폴더",
                               font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL),
                      "단일 배포 소스 폴더").pack(side="left", padx=(0, _px(8)))

        self.single_src_var = tk.StringVar()
        self._entry(ctrl_row, self.single_src_var, 58).pack(side="left", ipady=_px(1), padx=(0, _px(8)))
        self._sec_btn(ctrl_row, "폴더 선택", self._single_browse).pack(side="left", padx=(0, _px(8)))
        self._sec_btn(ctrl_row, "적용", self._single_apply).pack(side="left", padx=(0, _px(12)))
        self.single_status_lbl = tk.Label(ctrl_row, text="",
                 font=self.FONT_SMALL, padx=_px(8), pady=_px(2), fg=self.TEXT_DIM, bg=self.PANEL)
        self.single_status_lbl.pack(side="left")
        self._tip_key(self.single_status_lbl, "단일 배포 상태 메시지")
        self._btn(ctrl_row, "▲ 배포하기", self.BTN_DEPLOY, self.BTN_DEPLOY_H,
                  self._single_deploy_cmd, padx=20, pady=9).pack(side="right")

        # ── 진행 상황 프로그레스바 ────────
        prog_panel = tk.Frame(ctrl_body, bg=self.PANEL)
        prog_panel.pack(fill="x", pady=(_px(12), 0))
        prog_panel.columnconfigure(0, weight=1, uniform="prog")
        prog_panel.columnconfigure(1, weight=1, uniform="prog")

        for col, (lbl_text, attr_bar, attr_lbl) in enumerate([
            ("전체 배포", "prog_all", "prog_all_lbl"),
            ("현재 공정", "prog_cur", "prog_cur_lbl"),
        ]):
            row = tk.Frame(prog_panel, bg=self.PANEL)
            row.grid(row=0, column=col, sticky="ew", padx=(0, _px(12)) if col == 0 else (_px(12), 0))
            hint_key = "전체 배포" if attr_bar == "prog_all" else "단일 현재 공정"
            self._tip_key(tk.Label(row, text=lbl_text, width=8, anchor="w",
                                   font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL),
                          hint_key).pack(side="left")
            cnt_lbl = tk.Label(row, text="0 / 0", width=8, anchor="e",
                               font=self.FONT_MONO_BOLD, fg=self.TEXT, bg=self.PANEL)
            cnt_lbl.pack(side="right", padx=(_px(10), 0))
            self._tip_key(cnt_lbl, hint_key)
            bar = ttk.Progressbar(row, orient="horizontal", mode="determinate")
            bar.pack(side="left", fill="x", expand=True)
            setattr(self, attr_bar, bar)
            setattr(self, attr_lbl, cnt_lbl)

        # 하단 로그 (고정 높이)
        self.single_log_text = self._build_log_view(parent, "단일 배포 LOG", 150)

        # 2단 레이아웃 (파일 목록 | 클라이언트 목록)
        body = tk.Frame(parent, bg=self.BG)
        body.pack(fill="both", expand=True, pady=(_px(14), 0))

        # ── 좌측: 파일 목록 (고정 폭) ──
        file_outer, file_hdr = self._table_card(body, "파일 목록")
        file_outer.configure(width=_px(420))
        file_outer.pack(side="left", fill="y")
        file_outer.pack_propagate(False)
        btn_row = tk.Frame(file_hdr, bg=self.PANEL)
        btn_row.pack(side="right")
        self._sec_btn(btn_row, "전체 선택", self._single_file_check_all).pack(side="left", padx=(_px(8), 0))
        self._sec_btn(btn_row, "전체 해제", self._single_file_uncheck_all).pack(side="left", padx=(_px(8), 0))

        tk.Frame(file_outer, bg=self.BORDER, height=max(1, _px(1))).pack(fill="x")
        self.single_file_frame = self._table_region(file_outer)
        self.single_file_canvas = self.single_file_frame.canvas

        # ── 우측: 클라이언트 목록 (확장) ─
        cli_outer, cli_hdr = self._table_card(body, "배포 대상")
        cli_outer.pack(side="left", fill="both", expand=True, padx=(_px(14), 0))
        cli_btn_row = tk.Frame(cli_hdr, bg=self.PANEL)
        cli_btn_row.pack(side="right")
        self._sec_btn(cli_btn_row, "전체 선택", self._single_client_check_all).pack(side="left", padx=(_px(8), 0))
        self._sec_btn(cli_btn_row, "전체 해제", self._single_client_uncheck_all).pack(side="left", padx=(_px(8), 0))

        # 컬럼 헤더
        self._table_header(cli_outer, self.SINGLE_COLS)

        self.single_client_frame = self._table_region(cli_outer)

        self._rebuild_single_client_rows()

    # ── 공용 UI 헬퍼 ─────────────────────
    def _tip(self, widget, text):
        """위젯에 툴팁(설명)을 붙이고 위젯을 그대로 반환"""
        _Tooltip(widget, text)
        return widget

    def _tip_key(self, widget, key: str):
        """LABEL_HINTS 에서 key 의 설명을 찾아 툴팁으로 붙임 (없으면 붙이지 않음)"""
        text = self.LABEL_HINTS.get(key)
        if text:
            _Tooltip(widget, text)
        return widget

    def _card(self, parent, **kwargs) -> tk.Frame:
        """흰 배경 + 얇은 테두리 패널"""
        return tk.Frame(parent, bg=self.PANEL, highlightthickness=1,
                        highlightbackground=self.BORDER,
                        highlightcolor=self.BORDER, **kwargs)

    def _card_border(self, parent) -> tk.Frame:
        """입력 테두리(INPUT_BORDER)가 있는 흰 Frame — Listbox 를 안쪽 여백과 함께 감쌀 때 사용"""
        return tk.Frame(parent, bg=self.PANEL, highlightthickness=1,
                        highlightbackground=self.INPUT_BORDER,
                        highlightcolor=self.INPUT_BORDER)

    def _entry(self, parent, var, width, font=None, **kwargs) -> tk.Entry:
        return tk.Entry(parent, textvariable=var, width=width,
                        font=font or self.FONT_MONO,
                        bg=self.PANEL, fg=self.TEXT, insertbackground=self.TEXT,
                        relief="flat", bd=_px(6),   # flat 테두리가 안쪽 여백 역할
                        highlightthickness=1,
                        highlightbackground=self.INPUT_BORDER,
                        highlightcolor=self.BLUE, **kwargs)

    def _field_label(self, parent, text: str):
        """입력 행 왼쪽의 고정 폭 라벨 (행끼리 입력 시작 위치를 맞춤)"""
        cell = tk.Frame(parent, bg=self.PANEL, width=_px(116), height=_px(34))
        cell.pack(side="left")
        cell.pack_propagate(False)
        lbl = tk.Label(cell, text=text, font=self.FONT, fg=self.TEXT_DIM,
                       bg=self.PANEL)
        lbl.pack(side="left")
        self._tip_key(lbl, text)

    def _table_card(self, parent, title: str):
        """표 카드 + 툴바(제목 왼쪽, 버튼은 호출부에서 오른쪽에 배치). (카드, 툴바) 반환"""
        card = self._card(parent)
        toolbar = tk.Frame(card, bg=self.PANEL)
        toolbar.pack(fill="x", padx=_px(20), pady=(_px(16), _px(10)))
        title_lbl = tk.Label(toolbar, text=title, font=self.FONT_TITLE,
                             fg=self.TEXT, bg=self.PANEL)
        title_lbl.pack(side="left")
        self._tip_key(title_lbl, title)
        # 버튼 높이(34)만큼 툴바 높이를 확보해 카드마다 높이를 통일
        tk.Frame(toolbar, bg=self.PANEL, width=1, height=_px(34)).pack(side="right")
        return card, toolbar

    def _count_badge(self, toolbar) -> tk.Label:
        lbl = tk.Label(toolbar, text="0", font=self.FONT_MONO_BOLD, padx=_px(8), pady=_px(1),
                       fg=self.TEXT_DIM, bg=self.IDLE_BG)
        lbl.pack(side="left", padx=(_px(8), 0))
        return lbl

    def _scrollbar_width(self) -> int:
        if not hasattr(self, "_sb_width"):
            probe = ttk.Scrollbar(self.root, orient="vertical")
            self._sb_width = probe.winfo_reqwidth()
            probe.destroy()
        return self._sb_width

    def _cell(self, parent, width, bg, height=None, side="left") -> tk.Frame:
        """표의 한 칸 — 폭(px)을 고정해 헤더와 행의 열이 정확히 맞도록 함. width None 이면 남는 폭을 채움."""
        cell = tk.Frame(parent, bg=bg, width=_px(width) if width else 1, height=_px(height or self.ROW_H))
        cell.pack_propagate(False)
        if width:
            cell.pack(side=side)
        else:
            cell.pack(side="left", fill="x", expand=True)
        return cell

    def _table_header(self, card, cols) -> list:
        """표 헤더 행(PANEL_ALT) 생성. 열 라벨 목록 반환."""
        tk.Frame(card, bg=self.BORDER, height=max(1, _px(1))).pack(fill="x")
        hdr = tk.Frame(card, bg=self.PANEL_ALT)
        hdr.pack(fill="x")
        tk.Frame(hdr, bg=self.PANEL_ALT, width=_px(14), height=1).pack(side="left")
        # 데이터 영역 스크롤바 폭만큼 오른쪽을 비워 열을 맞춤
        tk.Frame(hdr, bg=self.PANEL_ALT, width=self._scrollbar_width(), height=1).pack(side="right")
        labels = []
        for col in cols:
            text, width = col[0], col[1]
            side = col[2] if len(col) > 2 else "left"
            cell = self._cell(hdr, width, self.PANEL_ALT, self.HDR_H, side)
            lbl = tk.Label(cell, text=text, font=self.FONT_SMALL, fg=self.TEXT_DIM,
                           bg=self.PANEL_ALT, anchor="w")
            lbl.pack(side="left")
            self._tip_key(lbl, text)
            labels.append(lbl)
        return labels

    def _table_region(self, card, height=None) -> tk.Frame:
        """표 데이터 영역(스크롤 가능). 내부 Frame 반환."""
        box = tk.Frame(card, bg=self.PANEL)
        if height:
            box.configure(height=_px(height))
            box.pack(fill="x")
            box.pack_propagate(False)
        else:
            box.pack(fill="both", expand=True)
        return self._make_scroll_frame(box, bg=self.PANEL)

    def _row_line(self, parent):
        tk.Frame(parent, bg=self.ROW_LINE, height=max(1, _px(1))).pack(fill="x")

    def _build_log_view(self, parent, title: str, height: int) -> tk.Text:
        """제목 + 스크롤 가능한 읽기 전용 다크 로그 Text 를 담은 고정 높이 카드를 parent 하단에 만들고 Text 를 반환."""
        card = self._card(parent, height=_px(height))
        card.pack(side="bottom", fill="x", pady=(_px(14), 0))
        card.pack_propagate(False)
        title_lbl = tk.Label(card, text=title,
                             font=self.FONT_TITLE, fg=self.TEXT, bg=self.PANEL)
        title_lbl.pack(anchor="w", padx=_px(20), pady=(_px(16), _px(10)))
        self._tip_key(title_lbl, title)
        log_inner = tk.Frame(card, bg=self.LOG_BG)
        log_inner.pack(fill="both", expand=True, padx=_px(20), pady=(0, _px(16)))
        text = tk.Text(log_inner, font=self.FONT_MONO, bg=self.LOG_BG,
                 fg=self.LOG_FG, insertbackground=self.LOG_FG, relief="flat", bd=0,
                 padx=_px(12), pady=_px(10), spacing1=2, height=1,
                 state="disabled", wrap="word", highlightthickness=0)
        sb = ttk.Scrollbar(log_inner, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        return text

    def _make_status_labels(self, row, row_bg, name_text: str, ip: str) -> dict:
        """서버 뷰 표의 상태 라벨 한 줄(name/ip/online/aurora/last/ping)을 만들어 dict 로 반환.
        online/aurora 는 배지, 나머지는 글자색만."""
        labels = {}
        tk.Frame(row, bg=row_bg, width=_px(14), height=1).pack(side="left")
        for col, w, val in [
            ("name",   98, name_text),
            ("ip",    138, ip),
            ("online", 104, "○ 오프라인"),
            ("aurora", 98, "—"),
            ("last",   98, "—"),
            ("ping",   78, "—"),
        ]:
            cell = self._cell(row, w, row_bg)
            if col in ("online", "aurora"):
                lbl = tk.Label(cell, text=val, font=self.FONT_SMALL, padx=_px(8), pady=_px(2),
                               fg=self.TEXT_DIM, bg=self.IDLE_BG)
            elif col == "name":
                lbl = tk.Label(cell, text=val, font=self.FONT_BOLD, anchor="w",
                               fg=self.TEXT, bg=row_bg)
            elif col == "ping":
                lbl = tk.Label(cell, text=val, font=self.FONT_MONO_BOLD, anchor="w",
                               fg=self.TEXT_DIM, bg=row_bg)
            else:
                lbl = tk.Label(cell, text=val, font=self.FONT_MONO, anchor="w",
                               fg=self.TEXT if col == "ip" else self.TEXT_DIM, bg=row_bg)
            lbl.pack(side="left")
            status_hint_key = {"online": "연결 상태", "aurora": "Aurora", "last": "마지막 응답", "ping": "핑"}.get(col)
            if status_hint_key:
                self._tip_key(lbl, status_hint_key)
            labels[col] = lbl
        return labels

    def _badge_bg(self, fg: str) -> str:
        """글자색(fg)으로부터 배지 배경색 결정"""
        if fg == self.GREEN:
            return self.OK_BG
        if fg == self.RED:
            return self.ERR_BG
        if fg == self.YELLOW:
            return self.WARN_BG
        if fg == self.BLUE:
            return self.NAV_ON_BG
        return self.IDLE_BG

    def _set_badge(self, lbl, text: str, fg: str):
        """상태 라벨을 배지(글자색 + 옅은 배경)로 표시. 빈 문구면 배지를 숨김(부모 배경색)."""
        bg = self._badge_bg(fg) if text else lbl.master.cget("bg")
        if lbl.cget("text") != text or lbl.cget("fg") != fg or lbl.cget("bg") != bg:
            lbl.config(text=text, fg=fg, bg=bg)

    def _make_scroll_frame(self, parent, bg=None) -> tk.Frame:
        """스크롤 가능한 캔버스+내부 Frame을 생성해 내부 Frame을 반환."""
        bg = bg or self.BG
        canvas = tk.Canvas(parent, bg=bg, highlightthickness=0, width=1, height=1)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=bg)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=(0, 0, e.width, e.height)))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win, width=e.width))
        self._bind_scroll(canvas)
        inner.canvas = canvas
        return inner

    def _bind_scroll(self, canvas):
        """마우스가 캔버스 영역 안에 있을 때만 해당 캔버스를 스크롤"""
        def _scroll(e):
            canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        def _on_enter(_e):
            canvas.bind_all("<MouseWheel>", _scroll)
        def _on_leave(e):
            # 자식 위젯으로 이동한 경우(Leave)는 무시하고,
            # 실제로 캔버스 영역 밖으로 나갔을 때만 해제
            try:
                cx = canvas.winfo_rootx()
                cy = canvas.winfo_rooty()
                cw = canvas.winfo_width()
                ch = canvas.winfo_height()
                if not (cx <= e.x_root < cx + cw and cy <= e.y_root < cy + ch):
                    canvas.unbind_all("<MouseWheel>")
            except Exception:
                canvas.unbind_all("<MouseWheel>")
        canvas.bind("<Enter>", _on_enter)
        canvas.bind("<Leave>", _on_leave)

    def _btn(self, parent, text, bg, abg, cmd, fg=None, border=None, padx=14, pady=7):
        """채워진 버튼(기본) / fg·border 를 주면 보조 버튼. hover 는 base_bg/hover_bg 속성 사용."""
        fg = fg or self.WHITE
        border = border or bg
        b = _StyledButton(parent, border, text=text, font=self.FONT_BOLD,
                          fg=fg, bg=bg, activeforeground=fg,
                          disabledforeground=self.DISABLED_TEXT,
                          activebackground=abg, bd=0, cursor="hand2",
                          relief="flat", padx=_px(padx), pady=_px(pady), command=cmd,
                          highlightthickness=0)
        b.base_bg = bg
        b.hover_bg = abg
        b.base_border = border
        b.bind("<Enter>", lambda e: b.config(bg=b.hover_bg) if str(b.cget("state")) != "disabled" else None)
        b.bind("<Leave>", lambda e: b.config(bg=b.base_bg) if str(b.cget("state")) != "disabled" else None)
        return b

    def _sec_btn(self, parent, text, cmd):
        """보조 버튼: 흰 배경 + 테두리 + 본문 글자"""
        return self._btn(parent, text, self.PANEL, self.BG, cmd,
                         fg=self.TEXT, border=self.INPUT_BORDER)

    def _set_btn_look(self, b, bg, abg, fg, border):
        """이미 만든 버튼의 모양(배경/hover/글자/테두리)을 바꿈 — 토글·세그먼트 용"""
        b.base_bg = bg
        b.hover_bg = abg
        b.config(fg=fg, activeforeground=fg, activebackground=abg)
        b.set_border(border)
        if str(b.cget("state")) != "disabled":
            b.config(bg=bg)

    def _set_autostart_look(self, enabled: bool):
        if enabled:
            self._set_btn_look(self._autostart_btn, self.OK_BG, self.OK_BG_H, self.GREEN, self.OK_BORDER)
        else:
            self._set_btn_look(self._autostart_btn, self.PANEL, self.BG, self.TEXT_DIM, self.INPUT_BORDER)

    # ── 서버 탭 클라이언트 행 재구성 ─────
    def _rebuild_rows(self):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self.client_rows = {}

        with state_lock:
            ip_list = sorted(shared_state["clients"].keys(), key=_ip_sort_key)
            shared_state["clients"] = {ip: shared_state["clients"][ip] for ip in ip_list}

        self.all_count_lbl.config(text=str(len(ip_list)))

        for idx, ip in enumerate(ip_list):
            row_bg = self.PANEL
            self._row_line(self.rows_frame)
            row = tk.Frame(self.rows_frame, bg=row_bg)
            row.pack(fill="x")
            saved_name = config.get("client_names", {}).get(ip, f"CLIENT {idx+1:02d}")
            labels = self._make_status_labels(row, row_bg, saved_name, ip)

            # 이름 더블클릭 → 이름 재설정
            def _rename(_ip=ip, _lbl=labels["name"]):
                new_name = simpledialog.askstring(
                    "이름 변경", f"{_ip} 의 새 이름을 입력하세요:",
                    initialvalue=_lbl.cget("text"), parent=self.root)
                if new_name is None:
                    return
                new_name = new_name.strip() or _lbl.cget("text")
                config.setdefault("client_names", {})[_ip] = new_name
                save_config(config)
                _lbl.config(text=new_name)
            labels["name"].bind("<Double-Button-1>", lambda e, f=_rename: f())
            labels["name"].config(cursor="hand2")
            self._tip_key(labels["name"], "이름 변경")

            # ID / PW 입력
            saved = config.get("deploy_client_creds", {}).get(ip, {})
            user_var = tk.StringVar(value=saved.get("user", "user"))
            pw_var   = tk.StringVar(value=saved.get("pw",   "1"))
            self.single_client_users[ip] = user_var
            self.single_client_pws[ip]   = pw_var

            user_cell = self._cell(row, 108, row_bg)
            self._entry(user_cell, user_var, 8, self.FONT_MONO
                        ).pack(side="left", fill="x", expand=True, padx=(0, _px(8)), ipady=0)
            pw_cell = self._cell(row, 108, row_bg)
            self._entry(pw_cell, pw_var, 8, self.FONT_MONO, show="*"
                        ).pack(side="left", fill="x", expand=True, padx=(0, _px(8)), ipady=0)

            def _save_cred(*_, _ip=ip, _u=user_var, _p=pw_var):
                config.setdefault("deploy_client_creds", {})[_ip] = {
                    "user": _u.get(), "pw": _p.get()
                }
                save_config(config)

            user_var.trace_add("write", _save_cred)
            pw_var.trace_add("write",   _save_cred)

            with state_lock:
                cur_path = shared_state["clients"].get(ip, {}).get("aurora_path", "")
            path_cell = self._cell(row, None, row_bg)
            path_lbl = tk.Label(path_cell, text=cur_path if cur_path else "—", anchor="w",
                                font=self.FONT_MONO, fg=self.TEXT if cur_path else self.TEXT_DIM,
                                bg=row_bg)
            path_lbl.pack(side="left")
            self._tip_key(path_lbl, "경로")
            labels["path"] = path_lbl

            self.client_rows[ip] = labels

    def _refresh_paths(self):
        """모든 클라이언트에 getpath 명령 없이, 다음 status report 수신 시 경로가 자동 갱신됨.
        버튼 클릭 시 즉시 UI 재구성 — 현재 shared_state 값을 바로 반영."""
        self._rebuild_rows()

    # ── 배포 탭 클라이언트 행 재구성 ─────
    def _rebuild_deploy_rows(self):
        for w in self.deploy_rows_frame.winfo_children():
            w.destroy()
        self.deploy_check_vars  = {}
        self.deploy_combo_vars  = {}
        self.deploy_combos      = {}
        self.deploy_dst_labels  = {}
        self.deploy_row_progress = {}
        self.deploy_row_prog_lbl = {}

        with state_lock:
            ip_list = sorted(shared_state["clients"].keys(), key=_ip_sort_key)

        current_versions = self._get_version_list()
        saved_folders    = config.get("deploy_client_folders", {})

        for idx, ip in enumerate(ip_list):
            row_bg = self.PANEL
            self._row_line(self.deploy_rows_frame)
            row = tk.Frame(self.deploy_rows_frame, bg=row_bg)
            row.pack(fill="x")
            tk.Frame(row, bg=row_bg, width=_px(14), height=1).pack(side="left")

            # 체크박스
            chk_var = tk.BooleanVar(value=True)
            self.deploy_check_vars[ip] = chk_var
            chk_cell = self._cell(row, 48, row_bg)
            chk = tk.Checkbutton(chk_cell, variable=chk_var,
                                 bg=row_bg, activebackground=row_bg,
                                 selectcolor=self.PANEL, bd=0, highlightthickness=0)
            chk.pack(side="left")

            # CLIENT 이름
            client_name = config.get("client_names", {}).get(ip, f"CLIENT {idx+1:02d}")
            name_cell = self._cell(row, 98, row_bg)
            tk.Label(name_cell, text=client_name, anchor="w",
                     font=self.FONT_BOLD, fg=self.TEXT, bg=row_bg).pack(side="left")

            # IP
            ip_cell = self._cell(row, 138, row_bg)
            tk.Label(ip_cell, text=ip, anchor="w",
                     font=self.FONT_MONO, fg=self.TEXT, bg=row_bg).pack(side="left")

            # 소스 폴더 콤보박스 — 저장된 선택값 복원
            saved_folder = saved_folders.get(ip, "")
            init_folder  = saved_folder if saved_folder in current_versions else (current_versions[0] if current_versions else "")
            combo_var = tk.StringVar(value=init_folder)
            self.deploy_combo_vars[ip] = combo_var
            combo_cell = self._cell(row, 198, row_bg)
            combo = ttk.Combobox(combo_cell, textvariable=combo_var,
                                 values=current_versions,
                                 font=self.FONT_MONO,
                                 state="readonly")
            combo.pack(side="left", fill="x", expand=True, padx=(0, _px(8)))
            combo.bind("<MouseWheel>", lambda e: "break")
            self.deploy_combos[ip] = combo

            # 저장 경로 라벨
            dst_text = self._dst_label_text()
            # 진행률 바를 오른쪽에 고정 (헤더 "진행률"과 맞춤)
            prog_cell = self._cell(row, 220, row_bg, side="right")
            prog_lbl = tk.Label(prog_cell, text="", font=self.FONT_MONO,
                                fg=self.TEXT_DIM, bg=row_bg, width=6, anchor="e")
            prog_lbl.pack(side="right")
            self._tip_key(prog_lbl, "진행률")
            prog = ttk.Progressbar(prog_cell, orient="horizontal", mode="determinate")
            prog.pack(side="left", fill="x", expand=True, padx=(0, _px(8)))

            dst_cell = self._cell(row, None, row_bg)
            dst_lbl = tk.Label(dst_cell, text=dst_text, anchor="w",
                               font=self.FONT_MONO, fg=self.BLUE, bg=row_bg)
            dst_lbl.pack(side="left")
            self._tip_key(dst_lbl, "저장 경로")
            self.deploy_dst_labels[ip] = dst_lbl
            self.deploy_row_progress[ip] = prog
            self.deploy_row_prog_lbl[ip] = prog_lbl

            # 콤보 선택 시 config에 자동 저장
            # 콤보 변경은 화면에만 반영, 저장은 [적용] 버튼으로만

        # 공유 버전명 변경 시 모든 저장 경로 라벨 갱신 (최초 1회만 등록)
        if not hasattr(self, "_dst_label_trace_id"):
            self._dst_label_trace_id = self.shared_version_var.trace_add(
                "write", self._refresh_dst_labels
            )

    def _update_ping_mode_btns(self):
        is_auto = self._ping_mode == "auto"
        selected   = (self.BLUE, self.BLUE_H, self.WHITE, self.BLUE)
        unselected = (self.PANEL, self.BG, self.TEXT_DIM, self.INPUT_BORDER)
        self._set_btn_look(self.ping_auto_btn, *(selected if is_auto else unselected))
        self._set_btn_look(self.ping_manual_btn, *(unselected if is_auto else selected))
        self.ping_now_btn.config(state="normal" if not is_auto else "disabled")

    def _set_ping_mode(self, mode: str):
        self._ping_mode = mode
        config["ping_mode"] = mode
        save_config(config)
        self._update_ping_mode_btns()
        send_to_online(f"setpingmode:{mode}")
        add_log(f"핑 모드 변경: {mode}")
        if mode == "auto":
            self._start_auto_ping()
        else:
            self._stop_auto_ping()

    def _cmd_ping_now(self):
        send_to_online("ping")
        threading.Thread(target=self._do_server_ping, daemon=True).start()

    def _do_server_ping(self):
        ping_all_clients()

    def _start_auto_ping(self):
        if getattr(self, "_auto_ping_running", False):
            return
        self._auto_ping_running = True
        self._auto_ping_stop = threading.Event()
        def _loop():
            while self._ping_mode == "auto":
                self._do_server_ping()
                send_to_online("ping")
                self._auto_ping_stop.wait(timeout=5)
                self._auto_ping_stop.clear()
            self._auto_ping_running = False
        threading.Thread(target=_loop, daemon=True).start()

    def _stop_auto_ping(self):
        if getattr(self, "_auto_ping_stop", None):
            self._auto_ping_stop.set()

    def _apply_version(self):
        v = self.version_input_var.get().strip()
        if not v:
            messagebox.showerror("오류", "버전명을 입력하세요.", parent=self.root)
            return
        config["deploy_version"] = v
        save_config(config)
        self.shared_version_var.set(v)
        add_log(f"배포 버전명 적용: {v}")

    def _apply_path_to_all(self):
        v = self.shared_version_var.get().strip()
        if not v:
            messagebox.showerror("오류", "버전명을 먼저 입력하고 적용하세요.", parent=self.root)
            return
        folder = config.get("deploy_dst_folder", "").strip()
        exe_folder = f"Aurora_EXE_{v}"
        new_path = f"D:\\{folder}\\{exe_folder}" if folder else f"D:\\{exe_folder}"
        with state_lock:
            online_ips = [ip for ip, info in shared_state["clients"].items() if info["online"]]
        if not online_ips:
            messagebox.showwarning("알림", "온라인 클라이언트가 없습니다.", parent=self.root)
            return
        for ip in online_ips:
            threading.Thread(target=push_command,
                             args=(ip, f"setpath:{new_path}"), daemon=True).start()
        add_log(f"→ 전체 경로 적용: {new_path}  ({len(online_ips)}대)")

    def _deploy_dst_col_text(self) -> str:
        return "저장 경로"

    def _dst_preview_text(self) -> str:
        folder = config.get("deploy_dst_folder", "").strip()
        v      = self.shared_version_var.get().strip() if hasattr(self, "shared_version_var") else ""
        exe    = f"Aurora_EXE_{v}" if v else "Aurora_EXE_버전명"
        return f"D:\\{folder}\\{exe}" if folder else f"D:\\{exe}"

    def _apply_deploy_dst_folder(self):
        folder = self.deploy_dst_folder_var.get().strip()
        config["deploy_dst_folder"] = folder
        save_config(config)
        self._refresh_dst_labels()
        add_log(f"추가 저장 경로 적용: {folder or '없음'}")

    def _dst_label_text(self) -> str:
        v      = self.shared_version_var.get().strip()
        folder = config.get("deploy_dst_folder", "").strip()
        exe    = f"Aurora_EXE_{v}" if v else "—"
        return f"{folder}\\{exe}" if folder else exe

    def _refresh_dst_labels(self, *args):
        text = self._dst_label_text()
        for lbl in self.deploy_dst_labels.values():
            lbl.config(text=text)
        if hasattr(self, "deploy_dst_preview_lbl"):
            self.deploy_dst_preview_lbl.config(text=self._dst_preview_text())
        if hasattr(self, "deploy_dst_col_hdr"):
            self.deploy_dst_col_hdr.config(text=self._deploy_dst_col_text())

    # ── 단일 배포 뷰 메서드 ───────────────
    def _rebuild_single_client_rows(self):
        for w in self.single_client_frame.winfo_children():
            w.destroy()
        self.single_client_vars = {}
        self.single_client_status_lbls = {}

        with state_lock:
            ip_list = sorted(shared_state["clients"].keys(), key=_ip_sort_key)

        for idx, ip in enumerate(ip_list):
            row_bg = self.PANEL
            self._row_line(self.single_client_frame)
            row = tk.Frame(self.single_client_frame, bg=row_bg)
            row.pack(fill="x")
            tk.Frame(row, bg=row_bg, width=_px(14), height=1).pack(side="left")

            var = tk.BooleanVar(value=True)
            self.single_client_vars[ip] = var
            chk_cell = self._cell(row, 32, row_bg)
            tk.Checkbutton(chk_cell, variable=var, bg=row_bg,
                           activebackground=row_bg,
                           selectcolor=self.PANEL, bd=0, highlightthickness=0).pack(side="left")
            client_name = config.get("client_names", {}).get(ip, f"CLIENT {idx+1:02d}")
            name_cell = self._cell(row, 118, row_bg)
            tk.Label(name_cell, text=client_name, anchor="w",
                     font=self.FONT_BOLD, fg=self.TEXT, bg=row_bg).pack(side="left")
            ip_cell = self._cell(row, 158, row_bg)
            tk.Label(ip_cell, text=ip, anchor="w",
                     font=self.FONT_MONO, fg=self.TEXT, bg=row_bg).pack(side="left")
            status_cell = self._cell(row, None, row_bg)
            status_lbl = tk.Label(status_cell, text="", anchor="w",
                                  font=self.FONT_SMALL, padx=_px(8), pady=_px(2), fg=self.TEXT_DIM, bg=self.IDLE_BG)
            status_lbl.pack(side="left")
            self._tip_key(status_lbl, "배포 상태")
            self._set_badge(status_lbl, "배포 예정", self.TEXT_DIM)
            self.single_client_status_lbls[ip] = status_lbl

    def _single_browse(self):
        folder = filedialog.askdirectory(title="단일 배포 소스 폴더 선택", parent=self.root)
        if folder:
            self.single_src_var.set(folder)

    def _single_apply(self):
        src = self.single_src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("오류", f"유효한 폴더를 선택하세요:\n{src}", parent=self.root)
            return

        try:
            files = sorted([
                f for f in os.listdir(src)
                if os.path.isfile(os.path.join(src, f))
            ])
        except Exception as e:
            messagebox.showerror("오류", str(e), parent=self.root)
            return

        # 파일 목록 재구성
        for w in self.single_file_frame.winfo_children():
            w.destroy()
        self.single_file_vars = {}

        if not files:
            tk.Label(self.single_file_frame, text="파일이 없습니다",
                     font=self.FONT_DESC, fg=self.TEXT_DIM, bg=self.PANEL).pack(padx=_px(12), pady=_px(8))
            self._set_badge(self.single_status_lbl, "파일 없음", self.YELLOW)
            return

        for idx, fname in enumerate(files):
            row_bg = self.PANEL
            self._row_line(self.single_file_frame)
            row = tk.Frame(self.single_file_frame, bg=row_bg, height=_px(self.ROW_H))
            row.pack(fill="x")
            row.pack_propagate(False)

            var = tk.BooleanVar(value=True)
            self.single_file_vars[fname] = var
            tk.Checkbutton(row, variable=var, bg=row_bg,
                           activebackground=row_bg,
                           selectcolor=self.PANEL, bd=0, highlightthickness=0).pack(side="left", padx=(_px(10), 0))
            tk.Label(row, text=fname, anchor="w",
                     font=self.FONT_MONO, fg=self.TEXT, bg=row_bg
                     ).pack(side="left", padx=(_px(6), _px(12)))

        self.single_file_canvas.update_idletasks()
        self.single_file_canvas.configure(
            scrollregion=(0, 0,
                          self.single_file_frame.winfo_width(),
                          self.single_file_frame.winfo_height()))
        self._set_badge(self.single_status_lbl,
                        f"✓ 파일 {len(files)}개 로드됨", self.GREEN)
        self._single_log(f"소스 폴더 적용: {src}  ({len(files)}개 파일)")

    def _single_file_check_all(self):
        for v in self.single_file_vars.values():
            v.set(True)

    def _single_file_uncheck_all(self):
        for v in self.single_file_vars.values():
            v.set(False)

    def _single_client_check_all(self):
        for v in self.single_client_vars.values():
            v.set(True)

    def _single_client_uncheck_all(self):
        for v in self.single_client_vars.values():
            v.set(False)

    def _single_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        add_log(msg)
        self.single_log_text.config(state="normal")
        self.single_log_text.insert("end", line + "\n")
        self.single_log_text.see("end")
        self.single_log_text.config(state="disabled")

    def _single_deploy_cmd(self):
        src = self.single_src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("오류", "소스 폴더를 먼저 선택하고 [적용]하세요.", parent=self.root)
            return

        checked_files = [f for f, v in self.single_file_vars.items() if v.get()]
        if not checked_files:
            messagebox.showwarning("알림", "배포할 파일을 선택하세요.", parent=self.root)
            return

        # {ip: (user, pw)}
        checked_targets = {
            ip: (self.single_client_users[ip].get(), self.single_client_pws[ip].get())
            for ip, v in self.single_client_vars.items() if v.get()
        }
        if not checked_targets:
            messagebox.showwarning("알림", "배포 대상 클라이언트를 선택하세요.", parent=self.root)
            return

        total      = len(checked_targets)
        cur_ips    = set(config.get("current_process_ips", []))
        cur_total  = len([ip for ip in checked_targets if ip in cur_ips])

        self._set_badge(self.single_status_lbl,
                        f"배포 중... ({total}대 / {len(checked_files)}개 파일)", self.YELLOW)
        self._single_log(f"▲ 단일 배포 시작 — 파일 {len(checked_files)}개 / 대상 {total}대")

        # 프로그레스바 초기화
        self.prog_all["maximum"] = total
        self.prog_all["value"]   = 0
        self.prog_all_lbl.config(text=f"0 / {total}")
        self.prog_cur["maximum"] = max(cur_total, 1)
        self.prog_cur["value"]   = 0
        self.prog_cur_lbl.config(text=f"0 / {cur_total}")

        # 상태 라벨 초기화
        for ip in self.single_client_status_lbls:
            if ip in checked_targets:
                self._set_badge(self.single_client_status_lbls[ip], "배포 중...", self.YELLOW)
            else:
                self._set_badge(self.single_client_status_lbls[ip], "배포 예정", self.TEXT_DIM)

        completed_all = [0]
        completed_cur = [0]

        def done_fn(ip, success):
            completed_all[0] += 1
            if ip in cur_ips:
                completed_cur[0] += 1
            status_text = "배포 완료" if success else "✗ 실패"
            status_fg   = self.GREEN if success else self.RED
            ca = completed_all[0]
            cc = completed_cur[0]
            def _ui():
                if ip in self.single_client_status_lbls:
                    self._set_badge(self.single_client_status_lbls[ip], status_text, status_fg)
                self.prog_all["value"] = ca
                self.prog_all_lbl.config(text=f"{ca} / {total}")
                self.prog_cur["value"] = cc
                self.prog_cur_lbl.config(text=f"{cc} / {cur_total}")
            self.root.after(0, _ui)

        def run():
            fail_count = _single_deploy_all(src, checked_files, checked_targets,
                                            self._single_log, done_fn)
            ok = total - fail_count
            if fail_count == 0:
                self.root.after(0, lambda: self._set_badge(
                    self.single_status_lbl, f"✓ 배포 완료 ({total}대)", self.GREEN))
            else:
                self.root.after(0, lambda: self._set_badge(
                    self.single_status_lbl, f"⚠ 완료 {ok}대 / 실패 {fail_count}대", self.YELLOW))

        threading.Thread(target=run, daemon=True).start()

    # ── 배포 소스 폴더 관련 ──────────────
    def _browse_deploy_src(self):
        folder = filedialog.askdirectory(title="배포 소스 폴더 선택", parent=self.root)
        if folder:
            self.deploy_src_var.set(folder)

    def _get_version_list(self) -> list:
        src = self.deploy_src_var.get().strip()
        if not src or not os.path.isdir(src):
            return []
        try:
            return sorted([
                d for d in os.listdir(src)
                if os.path.isdir(os.path.join(src, d))
            ])
        except Exception:
            return []

    def _apply_deploy_src(self):
        src = self.deploy_src_var.get().strip()
        if not src:
            messagebox.showerror("오류", "배포 소스 폴더를 먼저 선택하세요.", parent=self.root)
            return
        if not os.path.isdir(src):
            messagebox.showerror("오류", f"폴더를 찾을 수 없습니다:\n{src}", parent=self.root)
            return

        versions = self._get_version_list()
        if not versions:
            messagebox.showwarning("알림", "선택한 폴더 안에 하위 폴더가 없습니다.", parent=self.root)
            return

        ver = self.shared_version_var.get().strip()
        saved_folders = config.get("deploy_client_folders", {})
        for ip, combo in self.deploy_combos.items():
            combo["values"] = versions
            # 저장된 선택값 유지, 없으면 첫 번째로
            saved = saved_folders.get(ip, "")
            pick = saved if saved in versions else versions[0]
            combo.set(pick)
            self.deploy_combo_vars[ip].set(pick)
            if ip in self.deploy_dst_labels:
                self.deploy_dst_labels[ip].config(
                    text=f"Aurora_EXE_{ver}" if ver else "—"
                )

        config["deploy_src"] = src
        save_config(config)
        self._set_badge(
            self.deploy_status_lbl,
            f"✓ 적용 완료 — 버전 {len(versions)}개: {', '.join(versions)}",
            self.GREEN
        )
        add_log(f"배포 소스 폴더 적용: {src}  버전: {versions}")

    def _save_deploy_combos(self):
        """클라이언트별 현재 콤보 선택값을 저장"""
        for ip, var in self.deploy_combo_vars.items():
            config.setdefault("deploy_client_folders", {})[ip] = var.get()
        save_config(config)
        self._set_badge(self.deploy_status_lbl, "✓ 소스 폴더 선택 저장됨", self.GREEN)

    def _apply_deploy_inner(self):
        inner = self.deploy_inner_var.get().strip()
        config["deploy_inner_path"] = inner
        save_config(config)
        add_log(f"내부 경로 적용: {inner or '(없음)'}")

    def _refresh_deploy_src(self):
        """마지막으로 저장된 소스 폴더 선택으로 되돌림"""
        versions = self._get_version_list()
        saved_folders = config.get("deploy_client_folders", {})
        for ip, combo in self.deploy_combos.items():
            if versions:
                combo["values"] = versions
            saved = saved_folders.get(ip, "")
            if saved and (not versions or saved in versions):
                combo.set(saved)
                self.deploy_combo_vars[ip].set(saved)
            elif versions:
                combo.set(versions[0])
                self.deploy_combo_vars[ip].set(versions[0])
        self._set_badge(self.deploy_status_lbl, "↺ 마지막 저장 값으로 복원됨", self.BLUE)

    # ── 배포하기 ─────────────────────────
    def _cmd_deploy_selected(self):
        src = self.deploy_src_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showerror("오류", "유효한 배포 소스 폴더를 먼저 선택하고 [적용]하세요.", parent=self.root)
            return

        version = self.shared_version_var.get().strip()
        if not version:
            messagebox.showerror("오류", "배포 버전명을 서버 뷰에 입력하세요.", parent=self.root)
            return

        targets = []
        for ip, chk_var in self.deploy_check_vars.items():
            if chk_var.get():
                src_folder = self.deploy_combo_vars[ip].get().strip()
                if not src_folder:
                    messagebox.showwarning("알림", f"[{ip}] 소스 폴더가 선택되지 않았습니다.", parent=self.root)
                    return
                targets.append((ip, src_folder, version))

        if not targets:
            messagebox.showwarning("알림", "배포할 클라이언트를 선택하세요.", parent=self.root)
            return

        user       = config.get("deploy_user", "user")
        pw         = config.get("deploy_pw",   "1")
        dst_folder = self.deploy_dst_folder_var.get().strip()

        summary = ", ".join(f"{ip}→{folder}" for ip, folder, _ in targets)
        config["deploy_version"]    = version
        config["deploy_dst_folder"] = dst_folder
        save_config(config)
        total = len(targets)
        self._deploy_cancel = threading.Event()
        self._set_badge(self.deploy_status_lbl, f"배포 중... ({total}대)", self.YELLOW)
        self.deploy_progress_overall.configure(maximum=total, value=0)
        self.deploy_overall_lbl.config(text=f"0 / {total}")
        self.deploy_stop_btn.config(state="normal")
        # 모든 대상 행 프로그레스바 초기화
        for ip_t, _, _ in targets:
            if ip_t in self.deploy_row_progress:
                self.deploy_row_progress[ip_t].configure(maximum=100, value=0)
                self.deploy_row_prog_lbl[ip_t].config(text="")
        add_log(f"▲ 배포 시작 — 버전: {version}  추가경로: {dst_folder or '없음'}  대상: {summary}")

        done_count = [0]

        def _progress(msg):
            self.root.after(0, lambda m=msg: self._set_badge(
                self.deploy_status_lbl, m, self.YELLOW))

        def _file_progress(ip_t, cur, tot):
            def _update(_ip=ip_t, c=cur, t=tot):
                if _ip in self.deploy_row_progress:
                    self.deploy_row_progress[_ip].configure(maximum=t, value=c)
                    self.deploy_row_prog_lbl[_ip].config(text=f"{c}/{t}")
            self.root.after(0, _update)

        def _on_client_done(ok):
            done_count[0] += 1
            n = done_count[0]
            def _update(v=n):
                self.deploy_progress_overall.configure(value=v)
                self.deploy_overall_lbl.config(text=f"{v} / {total}")
            self.root.after(0, _update)

        def run():
            fail_count = deploy_selected(targets, src, user, pw, dst_folder,
                                         _progress, _on_client_done,
                                         _file_progress, self._deploy_cancel,
                                         config.get("deploy_inner_path", ""))
            ok_count = total - fail_count
            cancelled = self._deploy_cancel.is_set()
            def _finish(f=fail_count, o=ok_count, c=cancelled):
                self.deploy_stop_btn.config(state="disabled")
                if c:
                    self._set_badge(self.deploy_status_lbl, f"■ 중단됨 (완료 {o}대)", self.RED)
                elif f == 0:
                    self._set_badge(self.deploy_status_lbl, f"✓ 배포 완료 ({total}대)", self.GREEN)
                    self.deploy_progress_overall.configure(value=total)
                    self.deploy_overall_lbl.config(text=f"{total} / {total}")
                else:
                    self._set_badge(
                        self.deploy_status_lbl, f"⚠ 완료 {ok_count}대 / 실패 {f}대", self.YELLOW)
            self.root.after(0, _finish)

        threading.Thread(target=run, daemon=True).start()

    def _cmd_deploy_stop(self):
        if hasattr(self, "_deploy_cancel"):
            self._deploy_cancel.set()
        self.deploy_stop_btn.config(state="disabled")

    # ── 전체 선택/해제 ───────────────────
    def _deploy_check_all(self):
        for var in self.deploy_check_vars.values():
            var.set(True)

    def _deploy_uncheck_all(self):
        for var in self.deploy_check_vars.values():
            var.set(False)

    # ── IP 추가/삭제 ─────────────────────
    def _add_ip(self):
        ip = simpledialog.askstring("IP 추가", "추가할 클라이언트 IP를 입력하세요:",
                                    parent=self.root)
        if not ip:
            return
        ip = ip.strip()
        with state_lock:
            if ip in shared_state["clients"]:
                messagebox.showwarning("중복", f"{ip} 는 이미 등록되어 있습니다.")
                return
            shared_state["clients"][ip] = {
                "online": False, "aurora_running": False, "last_seen": None
            }

        config["client_ips"] = list(shared_state["clients"].keys())
        save_config(config)
        self._rebuild_rows()
        self._rebuild_deploy_rows()
        self._rebuild_single_client_rows()
        add_log(f"클라이언트 추가: {ip}")

    def _del_ip(self):
        with state_lock:
            ip_list = list(shared_state["clients"].keys())
        if not ip_list:
            return

        popup = tk.Toplevel(self.root)
        popup.title("IP 삭제")
        popup.configure(bg=self.BG)
        popup.geometry(f"{_px(300)}x{_px(320)}")
        popup.grab_set()

        tk.Label(popup, text="삭제할 IP를 선택하세요  (Ctrl/Shift 다중 선택)",
                 font=self.FONT_DESC, fg=self.TEXT_DIM, bg=self.BG).pack(pady=_px(10))

        list_box = self._card_border(popup)
        list_box.pack(fill="both", expand=True, padx=_px(16))
        listbox = tk.Listbox(list_box, font=self.FONT_MONO,
                             bg=self.PANEL, fg=self.TEXT,
                             selectbackground=self.BLUE, selectforeground=self.WHITE, bd=0,
                             highlightthickness=0, activestyle="none",
                             selectmode=tk.EXTENDED)
        listbox.pack(fill="both", expand=True, padx=_px(8), pady=_px(8))
        for ip in ip_list:
            listbox.insert("end", ip)

        def confirm():
            sel = listbox.curselection()
            if not sel:
                return
            selected_ips = [ip_list[i] for i in sel]
            with state_lock:
                for ip in selected_ips:
                    if ip in shared_state["clients"]:
                        del shared_state["clients"][ip]
            config["client_ips"] = list(shared_state["clients"].keys())
            save_config(config)
            self._rebuild_rows()
            self._rebuild_deploy_rows()
            self._rebuild_single_client_rows()
            for ip in selected_ips:
                add_log(f"클라이언트 삭제: {ip}")
            popup.destroy()

        self._btn(popup, "삭제", self.BTN_STOP, self.BTN_STOP_H, confirm).pack(pady=_px(14))

    # ── 현재 공정 IP 관리 ────────────────
    def _rebuild_current_rows(self):
        for w in self.current_rows_frame.winfo_children():
            w.destroy()
        self.current_client_rows = {}

        ips = config.get("current_process_ips", [])
        self.current_count_lbl.config(text=str(len(ips)))
        for idx, ip in enumerate(ips):
            row_bg = self.PANEL
            self._row_line(self.current_rows_frame)
            row = tk.Frame(self.current_rows_frame, bg=row_bg)
            row.pack(fill="x")
            labels = self._make_status_labels(row, row_bg, f"공정 {idx+1:02d}", ip)
            self.current_client_rows[ip] = labels

    def _add_current_ip(self):
        ip = simpledialog.askstring("현재 공정 IP 추가", "추가할 IP를 입력하세요:",
                                    parent=self.root)
        if not ip:
            return
        ip = ip.strip()
        if ip in config.get("current_process_ips", []):
            messagebox.showwarning("중복", f"{ip} 는 이미 등록되어 있습니다.")
            return
        config.setdefault("current_process_ips", []).append(ip)
        save_config(config)
        self._rebuild_current_rows()
        add_log(f"현재 공정 추가: {ip}")

    def _del_current_ip(self):
        ips = config.get("current_process_ips", [])
        if not ips:
            return

        popup = tk.Toplevel(self.root)
        popup.title("현재 공정 IP 삭제")
        popup.configure(bg=self.BG)
        popup.geometry(f"{_px(300)}x{_px(280)}")
        popup.grab_set()

        tk.Label(popup, text="삭제할 IP를 선택하세요",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.BG).pack(pady=_px(10))

        list_box = self._card_border(popup)
        list_box.pack(fill="both", expand=True, padx=_px(16))
        listbox = tk.Listbox(list_box, font=self.FONT_MONO,
                             bg=self.PANEL, fg=self.TEXT,
                             selectbackground=self.BLUE, selectforeground=self.WHITE, bd=0,
                             highlightthickness=0, activestyle="none")
        listbox.pack(fill="both", expand=True, padx=_px(8), pady=_px(8))
        for ip in ips:
            listbox.insert("end", ip)

        def confirm():
            sel = listbox.curselection()
            if not sel:
                return
            ip = ips[sel[0]]
            config["current_process_ips"].remove(ip)
            save_config(config)
            self._rebuild_current_rows()
            add_log(f"현재 공정 삭제: {ip}")
            popup.destroy()

        self._btn(popup, "삭제", self.BTN_STOP, self.BTN_STOP_H, confirm).pack(pady=_px(14))

    def _toggle_autostart(self):
        try:
            new_state = not autostart_enabled()
            set_autostart(new_state)
            self._autostart_btn.config(
                text="ON  ●" if new_state else "OFF ○")
            self._set_autostart_look(new_state)
            add_log(f"자동 시작 {'활성화' if new_state else '비활성화'}")
        except Exception as e:
            messagebox.showerror("오류", f"레지스트리 쓰기 실패:\n{e}")

    # ── 포트 적용 ────────────────────────
    def _apply_ports(self):
        try:
            sp = int(self.server_port_var.get().strip())
            cp = int(self.client_port_var.get().strip())
            if not (1024 <= sp <= 65535 and 1024 <= cp <= 65535):
                raise ValueError
        except ValueError:
            messagebox.showerror("오류", "포트는 1024~65535 사이의 숫자여야 합니다.")
            return
        config["server_port"]    = sp
        config["client_port"]    = cp
        config["deploy_version"] = self.shared_version_var.get().strip()
        save_config(config)
        add_log(f"포트 변경 저장 완료 — 서버:{sp} / 클라이언트:{cp}")
        add_log("※ 변경사항은 프로그램 재시작 후 적용됩니다.")

    # ── 서버 명령 ────────────────────────
    def _cmd_start(self):
        add_log("▶ [전체 공정] 실행 명령 발행")
        threading.Thread(target=send_to_online, args=("start",), daemon=True).start()

    def _cmd_stop(self):
        add_log("■ [전체 공정] 종료 명령 발행")
        threading.Thread(target=send_to_online, args=("stop",), daemon=True).start()

    def _cmd_start_current(self):
        ips = config.get("current_process_ips", [])
        if not ips:
            messagebox.showwarning("알림", "현재 공정 IP 목록이 비어있습니다.")
            return
        add_log(f"▶ [현재 공정] 실행 명령 발행 ({len(ips)}대)")
        threading.Thread(target=send_to_online, args=("start", ips), daemon=True).start()

    def _cmd_stop_current(self):
        ips = config.get("current_process_ips", [])
        if not ips:
            messagebox.showwarning("알림", "현재 공정 IP 목록이 비어있습니다.")
            return
        add_log(f"■ [현재 공정] 종료 명령 발행 ({len(ips)}대)")
        threading.Thread(target=send_to_online, args=("stop", ips), daemon=True).start()

    # ── 주기 갱신 ────────────────────────
    def _refresh_loop(self):
        self._update_table()
        self._update_log()
        self.root.after(2000, self._refresh_loop)

    def _update_table(self):
        now = time.time()
        with state_lock:
            clients_copy = {ip: dict(v) for ip, v in shared_state["clients"].items()}

        online_count = sum(1 for v in clients_copy.values() if v["online"])
        total = len(clients_copy)
        self.online_label.config(
            text=f"온라인: {online_count} / {total}",
            fg=self.GREEN if online_count > 0 else self.TEXT_DIM
        )

        def _set(lbl, text, fg):
            if lbl.cget("text") != text or lbl.cget("fg") != fg:
                lbl.config(text=text, fg=fg)

        _set_badge = self._set_badge

        def _apply_status(labels, info):
            online  = info["online"]
            aurora  = info["aurora_running"]
            last    = info["last_seen"]
            ping_ms = info.get("ping_ms")
            _set_badge(labels["online"],
                       "● 온라인" if online else "○ 오프라인",
                       self.GREEN if online else self.TEXT_DIM)
            if not online:
                _set_badge(labels["aurora"], "—", self.TEXT_DIM)
            elif aurora:
                _set_badge(labels["aurora"], "● 실행중", self.GREEN)
            else:
                _set_badge(labels["aurora"], "○ 중지", self.RED)
            if last:
                elapsed = int(now - last)
                _set(labels["last"],
                     f"{elapsed}초 전" if elapsed < 60 else f"{elapsed//60}분 전",
                     self.GREEN if elapsed < 5 else self.YELLOW if elapsed < 10 else self.TEXT_DIM)
            if "ping" in labels:
                if ping_ms is None:
                    _set(labels["ping"], "시간초과", self.RED)
                else:
                    fg = self.GREEN if ping_ms < 10 else self.YELLOW if ping_ms < 50 else self.RED
                    _set(labels["ping"], f"{ping_ms}ms", fg)
            if "path" in labels:
                p = info.get("aurora_path", "")
                _set(labels["path"], p if p else "—", self.TEXT if p else self.TEXT_DIM)

        # 전체 공정 행 갱신
        for ip, info in clients_copy.items():
            if ip in self.client_rows:
                _apply_status(self.client_rows[ip], info)

        # 현재 공정 행 갱신
        for ip, labels in self.current_client_rows.items():
            if ip in clients_copy:
                _apply_status(labels, clients_copy[ip])
            else:
                _set_badge(labels["online"], "○ 오프라인", self.TEXT_DIM)
                _set_badge(labels["aurora"], "—", self.TEXT_DIM)
                labels["last"].config(text="—", fg=self.TEXT_DIM)
                labels["ping"].config(text="—", fg=self.TEXT_DIM)

    def _update_log(self):
        with state_lock:
            logs = list(shared_state["log"])
            seq  = shared_state["log_seq"]

        # 배포 탭 로그 — 누적 개수로 새 줄만 판별 (로그 앞부분이 잘려도 정확)
        new_count = min(seq - self._log_seq_shown, len(logs))
        self._log_seq_shown = seq
        new_lines = logs[len(logs) - new_count:] if new_count > 0 else []
        d_new = [l for l in new_lines if any(
            kw in l for kw in ["배포", "복사", "공유", "Aurora 경로", "▲", "◆"]
        )]
        if d_new:
            self.deploy_log_text.config(state="normal")
            for line in d_new:
                self.deploy_log_text.insert("end", line + "\n")
            # 최근 50줄만 유지
            # 각 줄이 "\n" 으로 끝나므로 end-1c 는 마지막 빈 줄 → 실제 줄 수는 그보다 1 적음
            line_count = int(self.deploy_log_text.index("end-1c").split(".")[0]) - 1
            if line_count > 50:
                self.deploy_log_text.delete("1.0", f"{line_count - 50 + 1}.0")
            self.deploy_log_text.see("end")
            self.deploy_log_text.config(state="disabled")

    def _start_services(self):
        self._log_seq_shown = 0
        # 자동 시작이 켜져 있으면 등록 경로를 지금 exe 로 갱신 (D:\AuroraRemote 로 옮겨도 예전 exe 가 켜지지 않게)
        if getattr(sys, "frozen", False) and autostart_enabled():
            try:
                set_autostart(True)
            except Exception:
                pass
        threading.Thread(target=run_http_server, daemon=True).start()
        threading.Thread(target=check_offline,   daemon=True).start()
        add_log(f"HTTP 서버 시작: 0.0.0.0:{SERVER_PORT}")
        self._set_badge(self.server_status_lbl,
                        f"● HTTP 서버 실행중  :{SERVER_PORT}", self.GREEN)
        if config.get("ping_mode", "manual") == "auto":
            self._start_auto_ping()
        add_log(f"서버 버전 v{APP_VERSION}")
        # 켜질 때 업데이트 폴더 자동 확인 (화면이 뜬 뒤)
        self.root.after(3000, lambda: self._check_update(manual=False))

    # ── 자동 업데이트 (RemoteConfig/Update) ──
    def _check_update(self, manual: bool):
        """manual=False: 시작 시 자동 확인 (같은 버전 자동 시도는 1회만)
        manual=True : 버튼 — 결과를 팝업으로 알림"""
        def _do():
            res = aurora_update.check("server", APP_VERSION)
            if res["status"] != "newer":
                if manual:
                    self.root.after(0, lambda: messagebox.showinfo("업데이트", res["msg"], parent=self.root))
                return
            if not manual and aurora_update.auto_already_tried("server", res["version"]):
                add_log(f"⚠ v{res['version']} 자동 업데이트를 이미 시도했지만 버전이 그대로입니다 — Update 폴더의 exe 확인 필요")
                return
            add_log(f"⬆ {res['msg']} — 업데이트 후 재시작합니다")
            err = aurora_update.apply("server", res["exe"], res["version"])
            if err:
                add_log(f"업데이트 실패: {err}")
                if manual:
                    self.root.after(0, lambda: messagebox.showerror("업데이트 실패", err, parent=self.root))
                return
            self.root.after(0, self.quit_app)
        if manual:
            self._update_btn.config(state="disabled")
            self.root.after(2000, lambda: self._update_btn.config(state="normal"))
        threading.Thread(target=_do, daemon=True).start()


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


_RUN_KEY      = r"Software\Microsoft\Windows\CurrentVersion\Run"
_SERVER_APP   = "AuroraServer"

def _get_server_exe() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])

def autostart_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, _SERVER_APP)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False

def set_autostart(enable: bool):
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE)
    if enable:
        winreg.SetValueEx(key, _SERVER_APP, 0, winreg.REG_SZ, f'"{_get_server_exe()}"')
    else:
        try:
            winreg.DeleteValue(key, _SERVER_APP)
        except FileNotFoundError:
            pass
    winreg.CloseKey(key)

def main():
    _acquire_lock(19700)
    root = tk.Tk()
    global SCALE
    SCALE = root.winfo_fpixels("1i") / 96.0

    # 창 아이콘 설정 (타이틀바 + 작업표시줄)
    for ico_path in [os.path.join(getattr(sys, "_MEIPASS", ""), "aurora_server.ico"),
                     os.path.join(BASE_DIR, "aurora_server.ico")]:
        if os.path.exists(ico_path):
            root.iconbitmap(ico_path)
            break

    A = AuroraServerApp
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Vertical.TScrollbar",
                    background=A.BORDER, troughcolor=A.BG,
                    arrowcolor=A.TEXT_DIM, bordercolor=A.BG,
                    lightcolor=A.BORDER, darkcolor=A.BORDER,
                    arrowsize=_px(12), borderwidth=0)
    style.configure("Treeview",
                    background=A.PANEL, fieldbackground=A.PANEL,
                    foreground=A.TEXT, rowheight=_px(26), borderwidth=0,
                    font=A.FONT)
    style.configure("Treeview.Heading",
                    background=A.BG, foreground=A.TEXT,
                    font=A.FONT_BOLD, borderwidth=0, relief="flat")
    style.map("Treeview",
              background=[("selected", A.SELECT_BG)],
              foreground=[("selected", A.TEXT)])
    style.configure("TEntry",
                    fieldbackground=A.PANEL, foreground=A.TEXT,
                    bordercolor=A.BORDER, lightcolor=A.BORDER,
                    darkcolor=A.BORDER)
    style.configure("TCombobox",
                    fieldbackground=A.PANEL, background=A.PANEL,
                    foreground=A.TEXT, selectbackground=A.SELECT_BG,
                    selectforeground=A.TEXT, arrowcolor=A.TEXT_DIM,
                    bordercolor=A.INPUT_BORDER, lightcolor=A.INPUT_BORDER,
                    darkcolor=A.INPUT_BORDER, padding=_px(6))
    root.option_add("*TCombobox*Listbox.font", A.FONT_MONO)
    style.map("TCombobox",
              fieldbackground=[("readonly", A.PANEL)],
              foreground=[("readonly", A.TEXT)])
    style.configure("Horizontal.TProgressbar",
                    background=A.BRAND, troughcolor=A.BORDER,
                    bordercolor=A.BORDER, lightcolor=A.BRAND, darkcolor=A.BRAND,
                    thickness=_px(8), borderwidth=0)

    app = AuroraServerApp(root)

    tray_icon_img = load_tray_icon("aurora_server.ico")
    tray = None

    def show_window(icon=None, item=None):
        root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))

    def quit_app(icon=None, item=None):
        if tray:
            tray.stop()
        root.after(0, root.destroy)

    def on_close():
        root.withdraw()
        add_log("트레이로 최소화됨 — 서버는 계속 실행 중")

    app.quit_app = lambda: quit_app()

    tray_menu = pystray.Menu(
        pystray.MenuItem("Aurora Server 열기", show_window, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", quit_app),
    )
    tray = pystray.Icon("AuroraServer", tray_icon_img, "Aurora Server", tray_menu)

    threading.Thread(target=tray.run, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    if tray:
        tray.stop()


if __name__ == "__main__":
    main()
