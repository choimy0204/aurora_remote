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
from PIL import Image as PILImage
import socket as _socket
import shutil
import sys

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
            self._set_headers()
            self.wfile.write(b'{"ok": true}')
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
class AuroraServerApp:

    # ── 색상 팔레트 ──
    BG          = "#f1f5f9"
    PANEL       = "#ffffff"
    BORDER      = "#e2e8f0"
    TEXT        = "#0f172a"
    TEXT_DIM    = "#64748b"
    GREEN       = "#16a34a"
    RED         = "#dc2626"
    YELLOW      = "#d97706"
    BLUE        = "#4f46e5"
    BTN_START   = "#16a34a"
    BTN_START_H = "#15803d"
    BTN_STOP    = "#dc2626"
    BTN_STOP_H  = "#b91c1c"
    BTN_DEPLOY  = "#4f46e5"
    BTN_DEPLOY_H= "#4338ca"
    BTN_NEUTRAL   = "#64748b"
    BTN_NEUTRAL_H = "#475569"
    WHITE       = "#ffffff"
    ROW_ALT     = "#e9eef5"   # BG 위 줄무늬 행
    PANEL_ALT   = "#f8fafc"   # PANEL 위 줄무늬 행
    SELECT_BG   = "#e0e7ff"

    # ── 폰트 ──
    FONT           = ("맑은 고딕", 10)
    FONT_BOLD      = ("맑은 고딕", 10, "bold")
    FONT_TITLE     = ("맑은 고딕", 11, "bold")
    FONT_APP_TITLE = ("맑은 고딕", 14, "bold")
    FONT_SMALL     = ("맑은 고딕", 9)
    FONT_SMALL_BOLD = ("맑은 고딕", 9, "bold")
    FONT_MONO      = ("Consolas", 9)
    FONT_MONO_BOLD = ("Consolas", 9, "bold")
    FONT_MONO_L    = ("Consolas", 10)

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Aurora Remote Controller  |  Server")
        self.root.geometry("1300x860")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)

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
        # 타이틀 바
        tf = tk.Frame(self.root, bg=self.BG)
        tf.pack(fill="x", padx=20, pady=(14, 0))
        tk.Label(tf, text="◈  AURORA REMOTE CONTROLLER",
                 font=self.FONT_APP_TITLE, fg=self.BLUE, bg=self.BG).pack(side="left")
        self.server_status_lbl = tk.Label(tf, text="● HTTP 서버 시작 중...",
                 font=self.FONT_SMALL, fg=self.YELLOW, bg=self.BG)
        self.server_status_lbl.pack(side="right")

        tk.Frame(self.root, bg=self.BORDER, height=1).pack(fill="x", padx=20, pady=(14, 0))

        # Notebook
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=24, pady=(14, 20))

        server_tab        = tk.Frame(nb, bg=self.BG)
        deploy_tab        = tk.Frame(nb, bg=self.BG)
        single_deploy_tab = tk.Frame(nb, bg=self.BG)
        nb.add(server_tab,        text="  서버 뷰  ")
        nb.add(deploy_tab,        text="  배포 뷰  ")
        nb.add(single_deploy_tab, text="  단일 배포 뷰  ")

        self._build_server_tab(server_tab)
        self._build_deploy_tab(deploy_tab)
        self._build_single_deploy_tab(single_deploy_tab)

    # ── 서버 뷰 탭 ───────────────────────
    def _build_server_tab(self, parent):
        parent = self._make_scroll_frame(parent)

        # 제어 버튼
        ctrl = self._card(parent)
        ctrl.pack(fill="x")
        inner = tk.Frame(ctrl, bg=self.PANEL)
        inner.pack(pady=10)

        # 전체 공정
        all_grp = tk.Frame(inner, bg=self.PANEL)
        all_grp.pack(side="left", padx=(10, 24))
        tk.Label(all_grp, text="전체 공정",
                 font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.PANEL).pack(pady=(0, 4))
        btn_row_all = tk.Frame(all_grp, bg=self.PANEL)
        btn_row_all.pack()
        self._btn(btn_row_all, "▶  실행", self.BTN_START, self.BTN_START_H,
                  self._cmd_start, 10).pack(side="left", padx=4)
        self._btn(btn_row_all, "■  종료", self.BTN_STOP, self.BTN_STOP_H,
                  self._cmd_stop, 10).pack(side="left", padx=4)

        # 구분선
        tk.Frame(inner, bg=self.BORDER, width=1).pack(side="left", fill="y", pady=4)

        # 현재 공정
        cur_grp = tk.Frame(inner, bg=self.PANEL)
        cur_grp.pack(side="left", padx=(24, 10))

        # 구분선
        tk.Frame(inner, bg=self.BORDER, width=1).pack(side="left", fill="y", pady=4)

        # 핑 모드
        ping_grp = tk.Frame(inner, bg=self.PANEL)
        ping_grp.pack(side="left", padx=(24, 10))
        tk.Label(ping_grp, text="핑 모드",
                 font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.PANEL).pack(pady=(0, 4))
        ping_btn_row = tk.Frame(ping_grp, bg=self.PANEL)
        ping_btn_row.pack()
        self._ping_mode = config.get("ping_mode", "manual")
        self.ping_manual_btn = self._btn(ping_btn_row, "Manual", self.BLUE, self.BTN_DEPLOY_H,
                                         lambda: self._set_ping_mode("manual"), 8)
        self.ping_manual_btn.pack(side="left", padx=4)
        self.ping_auto_btn = self._btn(ping_btn_row, "Auto", self.BTN_NEUTRAL, self.BTN_NEUTRAL_H,
                                       lambda: self._set_ping_mode("auto"), 8)
        self.ping_auto_btn.pack(side="left", padx=4)
        self.ping_now_btn = self._btn(ping_grp, "전체 핑", self.BTN_NEUTRAL, self.BTN_NEUTRAL_H,
                                      self._cmd_ping_now, 10)
        self.ping_now_btn.pack(pady=(4, 0))
        self._update_ping_mode_btns()
        tk.Label(cur_grp, text="현재 공정",
                 font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.PANEL).pack(pady=(0, 4))
        btn_row_cur = tk.Frame(cur_grp, bg=self.PANEL)
        btn_row_cur.pack()
        self._btn(btn_row_cur, "▶  실행", self.BTN_START, self.BTN_START_H,
                  self._cmd_start_current, 10).pack(side="left", padx=4)
        self._btn(btn_row_cur, "■  종료", self.BTN_STOP, self.BTN_STOP_H,
                  self._cmd_stop_current, 10).pack(side="left", padx=4)

        self.online_label = tk.Label(ctrl, text="온라인: 0 / 0",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL)
        self.online_label.pack(pady=(0, 8))

        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x")

        # 포트 설정
        port_frame = self._card(parent)
        port_frame.pack(fill="x", pady=(10, 0))

        tk.Label(port_frame, text="포트 설정",
                 font=self.FONT_BOLD, fg=self.TEXT_DIM,
                 bg=self.PANEL).pack(anchor="w", padx=14, pady=(8, 4))

        port_row = tk.Frame(port_frame, bg=self.PANEL)
        port_row.pack(fill="x", padx=14, pady=(0, 8))

        tk.Label(port_row, text="서버 포트", width=10, anchor="w",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")
        self.server_port_var = tk.StringVar(value=str(config.get("server_port", 9000)))
        self._entry(port_row, self.server_port_var, 8).pack(side="left", ipady=4, padx=(0, 20))

        tk.Label(port_row, text="클라이언트 포트", width=14, anchor="w",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")
        self.client_port_var = tk.StringVar(value=str(config.get("client_port", 9001)))
        self._entry(port_row, self.client_port_var, 8).pack(side="left", ipady=4, padx=(0, 12))

        self._small_btn(port_row, "적용 (재시작 필요)", self.BLUE, self._apply_ports).pack(side="left")

        # ── PC 시작 시 자동 실행 ─────────────
        auto_row = tk.Frame(port_frame, bg=self.PANEL)
        auto_row.pack(fill="x", padx=14, pady=(0, 10))
        tk.Label(auto_row, text="자동 시작", width=10, anchor="w",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")
        self._autostart_btn = self._small_btn(
            auto_row,
            "ON  ●" if autostart_enabled() else "OFF ○",
            self.GREEN if autostart_enabled() else self.TEXT_DIM,
            self._toggle_autostart)
        self._autostart_btn.pack(side="left")
        tk.Label(auto_row, text="  PC 시작 시 서버 자동 실행",
                 font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")

        # 버전명 (배포 뷰와 공유)
        ver_row = tk.Frame(port_frame, bg=self.PANEL)
        ver_row.pack(fill="x", padx=14, pady=(0, 10))
        tk.Label(ver_row, text="배포 버전명", width=10, anchor="w",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")
        self._entry(ver_row, self.version_input_var, 20).pack(side="left", ipady=4, padx=(0, 8))
        self._small_btn(ver_row, "적용", self.BLUE, self._apply_version).pack(side="left", padx=(0, 6))
        self._small_btn(ver_row, "전체 경로 적용", self.GREEN, self._apply_path_to_all).pack(side="left", padx=(0, 10))
        tk.Label(ver_row, text="← 배포 뷰와 공유됩니다",
                 font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")

        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", pady=(8, 0))

        # 클라이언트 IP 관리
        ip_frame = tk.Frame(parent, bg=self.BG)
        ip_frame.pack(fill="x", pady=(10, 0))

        hdr_row = tk.Frame(ip_frame, bg=self.BG)
        hdr_row.pack(fill="x")
        tk.Label(hdr_row, text="전체 공정  IP  관리",
                 font=self.FONT_TITLE, fg=self.TEXT_DIM, bg=self.BG).pack(side="left")

        btn_row = tk.Frame(hdr_row, bg=self.BG)
        btn_row.pack(side="right")
        self._small_btn(btn_row, "새로고침", self.GREEN, self._refresh_paths, self.BG).pack(side="left", padx=4)
        self._small_btn(btn_row, "+ 추가", self.BLUE, self._add_ip, self.BG).pack(side="left", padx=4)
        self._small_btn(btn_row, "− 삭제", self.RED,  self._del_ip, self.BG).pack(side="left", padx=4)

        tbl_outer = self._card(ip_frame)
        tbl_outer.pack(fill="x", pady=8)

        hdr = tk.Frame(tbl_outer, bg=self.PANEL)
        hdr.pack(fill="x")
        for col, w in [("CLIENT", 12), ("IP", 20), ("연결 상태", 14), ("Aurora", 14), ("마지막 응답", 14), ("핑", 10), ("ID", 10), ("PW", 10), ("경로", 30)]:
            tk.Label(hdr, text=col, width=w, anchor="center",
                     font=self.FONT_MONO_BOLD, fg=self.TEXT_DIM,
                     bg=self.PANEL, pady=5).pack(side="left")

        tk.Frame(tbl_outer, bg=self.BORDER, height=1).pack(fill="x")

        self.rows_frame = tk.Frame(tbl_outer, bg=self.BG)
        self.rows_frame.pack(fill="x")

        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", pady=(4, 4))

        # 현재 공정 IP 관리
        cur_frame = tk.Frame(parent, bg=self.BG)
        cur_frame.pack(fill="x", pady=(4, 0))

        cur_hdr = tk.Frame(cur_frame, bg=self.BG)
        cur_hdr.pack(fill="x")
        tk.Label(cur_hdr, text="현재 공정  IP  관리",
                 font=self.FONT_TITLE, fg=self.TEXT_DIM, bg=self.BG).pack(side="left")
        cur_btn_row = tk.Frame(cur_hdr, bg=self.BG)
        cur_btn_row.pack(side="right")
        self._small_btn(cur_btn_row, "+ 추가", self.BLUE, self._add_current_ip, self.BG).pack(side="left", padx=4)
        self._small_btn(cur_btn_row, "− 삭제", self.RED,  self._del_current_ip, self.BG).pack(side="left", padx=4)

        cur_tbl = self._card(cur_frame)
        cur_tbl.pack(fill="x", pady=(6, 6))

        cur_col_hdr = tk.Frame(cur_tbl, bg=self.PANEL)
        cur_col_hdr.pack(fill="x")
        for col, w in [("CLIENT", 12), ("IP", 20), ("연결 상태", 14), ("Aurora", 14), ("마지막 응답", 14), ("핑", 10)]:
            tk.Label(cur_col_hdr, text=col, width=w, anchor="center",
                     font=self.FONT_MONO_BOLD, fg=self.TEXT_DIM,
                     bg=self.PANEL, pady=5).pack(side="left")

        tk.Frame(cur_tbl, bg=self.BORDER, height=1).pack(fill="x")

        self.current_rows_frame = tk.Frame(cur_tbl, bg=self.BG)
        self.current_rows_frame.pack(fill="x")
        self.current_client_rows = {}

        self._rebuild_current_rows()

        self._rebuild_rows()

    # ── 배포 뷰 탭 ───────────────────────
    def _build_deploy_tab(self, parent):
        parent = self._make_scroll_frame(parent)

        # 소스 폴더 선택
        src_panel = self._card(parent)
        src_panel.pack(fill="x")

        tk.Label(src_panel, text="배포 소스 폴더",
                 font=self.FONT_BOLD, fg=self.TEXT_DIM,
                 bg=self.PANEL).pack(anchor="w", padx=14, pady=(10, 4))

        src_row = tk.Frame(src_panel, bg=self.PANEL)
        src_row.pack(fill="x", padx=14, pady=(0, 10))

        self.deploy_src_var = tk.StringVar(value=config.get("deploy_src", ""))
        self._entry(src_row, self.deploy_src_var, 42).pack(side="left", ipady=4, padx=(0, 8))

        self._small_btn(src_row, "폴더 선택", self.TEXT_DIM,
                        self._browse_deploy_src).pack(side="left", padx=(0, 6))
        self._small_btn(src_row, "적용", self.BLUE,
                        self._apply_deploy_src).pack(side="left", padx=(0, 12))
        self._btn(src_row, "▲ 배포하기", self.BTN_DEPLOY, self.BTN_DEPLOY_H,
                  self._cmd_deploy_selected, 14).pack(side="left")
        self.deploy_stop_btn = self._btn(src_row, "■ 중단", self.BTN_STOP, self.BTN_STOP_H,
                                         self._cmd_deploy_stop, 12)
        self.deploy_stop_btn.pack(side="left", padx=(6, 0))
        self.deploy_stop_btn.config(state="disabled")

        # 내부 경로 (소스 폴더 안의 실제 파일 위치)
        inner_row = tk.Frame(src_panel, bg=self.PANEL)
        inner_row.pack(fill="x", padx=14, pady=(0, 4))
        tk.Label(inner_row, text="내부 경로",
                 font=self.FONT_BOLD, fg=self.TEXT_DIM,
                 bg=self.PANEL, width=12, anchor="w").pack(side="left")
        self.deploy_inner_var = tk.StringVar(value=config.get("deploy_inner_path", ""))
        self._entry(inner_row, self.deploy_inner_var, 20).pack(side="left", ipady=4, padx=(0, 8))
        self._small_btn(inner_row, "적용", self.BLUE,
                        self._apply_deploy_inner).pack(side="left", padx=(0, 10))
        tk.Label(inner_row, text="예: x64-Debug  (비우면 소스 폴더 직접 사용)",
                 font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.PANEL).pack(side="left")

        # 추가 저장 경로
        dst_row = tk.Frame(src_panel, bg=self.PANEL)
        dst_row.pack(fill="x", padx=14, pady=(0, 4))
        tk.Label(dst_row, text="추가 저장 경로",
                 font=self.FONT_BOLD, fg=self.TEXT_DIM,
                 bg=self.PANEL, width=12, anchor="w").pack(side="left")
        self.deploy_dst_folder_var = tk.StringVar(value=config.get("deploy_dst_folder", ""))
        self._entry(dst_row, self.deploy_dst_folder_var, 20).pack(side="left", ipady=4, padx=(0, 8))
        self._small_btn(dst_row, "적용", self.BLUE,
                        self._apply_deploy_dst_folder).pack(side="left", padx=(0, 10))
        self.deploy_dst_preview_lbl = tk.Label(dst_row,
                 text=self._dst_preview_text(),
                 font=self.FONT_MONO, fg=self.BLUE, bg=self.PANEL)
        self.deploy_dst_preview_lbl.pack(side="left")

        self.deploy_status_lbl = tk.Label(src_panel, text="",
                 font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.PANEL)
        self.deploy_status_lbl.pack(anchor="w", padx=14, pady=(0, 4))

        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x")

        # 전체 진행률 (클라이언트 목록 위)
        overall_wrap = tk.Frame(parent, bg=self.BG)
        overall_wrap.pack(fill="x", padx=14, pady=(8, 2))
        tk.Label(overall_wrap, text="전체 진행률", font=self.FONT_BOLD,
                 fg=self.TEXT_DIM, bg=self.BG).pack(side="left", padx=(0, 8))
        self.deploy_progress_overall = ttk.Progressbar(overall_wrap, orient="horizontal",
                                                       mode="determinate")
        self.deploy_progress_overall.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.deploy_overall_lbl = tk.Label(overall_wrap, text="0 / 0",
                 font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.BG, width=8, anchor="w")
        self.deploy_overall_lbl.pack(side="left")

        # 클라이언트 목록 헤더
        list_frame = tk.Frame(parent, bg=self.BG)
        list_frame.pack(fill="x", pady=(6, 0))

        hdr_row = tk.Frame(list_frame, bg=self.BG)
        hdr_row.pack(fill="x")
        tk.Label(hdr_row, text="배포 대상 클라이언트",
                 font=self.FONT_TITLE, fg=self.TEXT_DIM, bg=self.BG).pack(side="left")
        self._small_btn(hdr_row, "전체 해제", self.TEXT_DIM,
                        self._deploy_uncheck_all, self.BG).pack(side="right", padx=4)
        self._small_btn(hdr_row, "전체 선택", self.BLUE,
                        self._deploy_check_all, self.BG).pack(side="right", padx=4)
        tk.Frame(hdr_row, bg=self.BORDER, width=1).pack(side="right", fill="y", padx=6)
        self._small_btn(hdr_row, "새로고침", self.TEXT_DIM,
                        self._refresh_deploy_src, self.BG).pack(side="right", padx=4)
        self._small_btn(hdr_row, "적용", self.BLUE,
                        self._save_deploy_combos, self.BG).pack(side="right", padx=4)

        tbl_outer = self._card(list_frame)
        tbl_outer.pack(fill="x", pady=8)

        col_hdr = tk.Frame(tbl_outer, bg=self.PANEL)
        col_hdr.pack(fill="x")
        # 행의 패딩과 동일하게 맞춤: 체크박스 padx=(8,0), CLIENT/IP 패딩 없음, 콤보 padx=(8,6)
        tk.Label(col_hdr, text="선택", width=5, anchor="center",
                 font=self.FONT_MONO_BOLD, fg=self.TEXT_DIM,
                 bg=self.PANEL, pady=5).pack(side="left", padx=(8, 0))
        for text, w in [("CLIENT", 11), ("IP", 18)]:
            tk.Label(col_hdr, text=text, width=w, anchor="center",
                     font=self.FONT_MONO_BOLD, fg=self.TEXT_DIM,
                     bg=self.PANEL, pady=5).pack(side="left")
        tk.Label(col_hdr, text="소스 폴더 선택", width=24, anchor="center",
                 font=self.FONT_MONO_BOLD, fg=self.TEXT_DIM,
                 bg=self.PANEL, pady=5).pack(side="left", padx=(8, 6))
        self.deploy_dst_col_hdr = tk.Label(col_hdr, text=self._deploy_dst_col_text(),
                     anchor="w",
                     font=self.FONT_MONO_BOLD, fg=self.TEXT_DIM,
                     bg=self.PANEL, pady=5)
        self.deploy_dst_col_hdr.pack(side="left", fill="x", expand=True)
        tk.Label(col_hdr, text="진행률", anchor="center", width=16,
                 font=self.FONT_MONO_BOLD, fg=self.TEXT_DIM,
                 bg=self.PANEL, pady=5).pack(side="right", padx=(0, 70))

        tk.Frame(tbl_outer, bg=self.BORDER, height=1).pack(fill="x")

        self.deploy_rows_frame = tk.Frame(tbl_outer, bg=self.BG)
        self.deploy_rows_frame.pack(fill="x")

        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", pady=(4, 8))

        # 배포 로그
        log_frame = tk.Frame(parent, bg=self.BG)
        log_frame.pack(fill="both", expand=True)
        self.deploy_log_text = self._build_log_view(log_frame, "배포 LOG", 8)

        self._rebuild_deploy_rows()

    # ── 단일 배포 뷰 탭 ──────────────────
    def _build_single_deploy_tab(self, parent):

        # 상단 컨트롤
        ctrl_panel = self._card(parent)
        ctrl_panel.pack(fill="x")

        tk.Label(ctrl_panel, text="배포 소스 폴더",
                 font=self.FONT_BOLD, fg=self.TEXT_DIM,
                 bg=self.PANEL).pack(anchor="w", padx=14, pady=(10, 4))

        ctrl_row = tk.Frame(ctrl_panel, bg=self.PANEL)
        ctrl_row.pack(fill="x", padx=14, pady=(0, 10))

        self.single_src_var = tk.StringVar()
        self._entry(ctrl_row, self.single_src_var, 44).pack(side="left", ipady=4, padx=(0, 8))
        self._small_btn(ctrl_row, "폴더 선택", self.TEXT_DIM,
                        self._single_browse).pack(side="left", padx=(0, 6))
        self._small_btn(ctrl_row, "적용", self.BLUE,
                        self._single_apply).pack(side="left", padx=(0, 12))
        self._btn(ctrl_row, "▲ 배포하기", self.BTN_DEPLOY, self.BTN_DEPLOY_H,
                  self._single_deploy_cmd, 14).pack(side="left")

        self.single_status_lbl = tk.Label(ctrl_panel, text="",
                 font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.PANEL)
        self.single_status_lbl.pack(anchor="w", padx=14, pady=(0, 8))

        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x")

        # ── 진행 상황 프로그레스바 ────────
        prog_panel = tk.Frame(parent, bg=self.BG)
        prog_panel.pack(fill="x", padx=14, pady=(8, 4))

        for lbl_text, attr_bar, attr_lbl in [
            ("전체 배포", "prog_all", "prog_all_lbl"),
            ("현재 공정", "prog_cur", "prog_cur_lbl"),
        ]:
            row = tk.Frame(prog_panel, bg=self.BG)
            row.pack(fill="x", pady=(0, 4))
            tk.Label(row, text=lbl_text, width=9, anchor="w",
                     font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.BG).pack(side="left")
            bar = ttk.Progressbar(row, orient="horizontal", mode="determinate")
            bar.pack(side="left", fill="x", expand=True, padx=(0, 8))
            cnt_lbl = tk.Label(row, text="0 / 0", width=8, anchor="w",
                               font=self.FONT_MONO, fg=self.TEXT_DIM, bg=self.BG)
            cnt_lbl.pack(side="left")
            setattr(self, attr_bar, bar)
            setattr(self, attr_lbl, cnt_lbl)

        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", pady=(2, 0))

        # 2단 레이아웃 (파일 목록 | 클라이언트 목록)
        body = tk.Frame(parent, bg=self.BG)
        body.pack(fill="both", expand=True, pady=(10, 0))

        # ── 좌측: 파일 목록 (반반) ──
        left = tk.Frame(body, bg=self.BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))

        file_hdr = tk.Frame(left, bg=self.BG)
        file_hdr.pack(fill="x")
        tk.Label(file_hdr, text="파일 목록",
                 font=self.FONT_TITLE, fg=self.TEXT_DIM, bg=self.BG).pack(side="left")
        self._small_btn(file_hdr, "전체 해제", self.TEXT_DIM,
                        self._single_file_uncheck_all, self.BG).pack(side="right", padx=4)
        self._small_btn(file_hdr, "전체 선택", self.BLUE,
                        self._single_file_check_all, self.BG).pack(side="right", padx=4)

        file_outer = self._card(left)
        file_outer.pack(fill="both", expand=True, pady=6)

        self.single_file_frame = self._make_scroll_frame(file_outer, bg=self.PANEL)
        self.single_file_canvas = self.single_file_frame.canvas

        # ── 우측: 클라이언트 목록 (확장) ─
        right = tk.Frame(body, bg=self.BG)
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))

        cli_hdr = tk.Frame(right, bg=self.BG)
        cli_hdr.pack(fill="x")
        tk.Label(cli_hdr, text="배포 대상",
                 font=self.FONT_TITLE, fg=self.TEXT_DIM, bg=self.BG).pack(side="left")
        self._small_btn(cli_hdr, "전체 해제", self.TEXT_DIM,
                        self._single_client_uncheck_all, self.BG).pack(side="right", padx=4)
        self._small_btn(cli_hdr, "전체 선택", self.BLUE,
                        self._single_client_check_all, self.BG).pack(side="right", padx=4)

        cli_outer = self._card(right)
        cli_outer.pack(fill="both", expand=True, pady=6)

        # 컬럼 헤더
        cli_col_hdr = tk.Frame(cli_outer, bg=self.PANEL)
        cli_col_hdr.pack(fill="x")
        tk.Label(cli_col_hdr, text="", width=2, bg=self.PANEL).pack(side="left")  # 체크박스 공간
        for col, w, anchor in [("CLIENT", 10, "w"), ("IP", 16, "w"), ("배포 상태", 10, "center")]:
            tk.Label(cli_col_hdr, text=col, width=w, anchor=anchor,
                     font=self.FONT_MONO_BOLD, fg=self.TEXT_DIM,
                     bg=self.PANEL, pady=4).pack(side="left")
        tk.Frame(cli_outer, bg=self.BORDER, height=1).pack(fill="x")

        self.single_client_frame = self._make_scroll_frame(cli_outer, bg=self.PANEL)

        self._rebuild_single_client_rows()

        # 하단 로그
        tk.Frame(parent, bg=self.BORDER, height=1).pack(fill="x", pady=(4, 6))
        log_frame = tk.Frame(parent, bg=self.BG)
        log_frame.pack(fill="both", expand=True, pady=(0, 6))
        self.single_log_text = self._build_log_view(log_frame, "단일 배포 LOG", 6)

    # ── 공용 UI 헬퍼 ─────────────────────
    def _card(self, parent, **kwargs) -> tk.Frame:
        """흰 배경 + 얇은 테두리 패널"""
        return tk.Frame(parent, bg=self.PANEL, highlightthickness=1,
                        highlightbackground=self.BORDER,
                        highlightcolor=self.BORDER, **kwargs)

    def _entry(self, parent, var, width, font=None, **kwargs) -> tk.Entry:
        return tk.Entry(parent, textvariable=var, width=width,
                        font=font or self.FONT_MONO_L,
                        bg=self.PANEL, fg=self.TEXT, insertbackground=self.TEXT,
                        relief="flat", bd=0, highlightthickness=1,
                        highlightbackground=self.BORDER,
                        highlightcolor=self.BLUE, **kwargs)

    def _build_log_view(self, log_frame, title: str, text_pady: int) -> tk.Text:
        """제목 + 스크롤 가능한 읽기 전용 로그 Text 를 log_frame 안에 만들고 Text 를 반환."""
        tk.Label(log_frame, text=title,
                 font=self.FONT_TITLE, fg=self.TEXT_DIM, bg=self.BG).pack(anchor="w", pady=(0, 4))
        log_inner = self._card(log_frame)
        log_inner.pack(fill="both", expand=True)
        text = tk.Text(log_inner, font=self.FONT_MONO, bg=self.PANEL,
                 fg=self.TEXT_DIM, bd=0, padx=10, pady=text_pady,
                 state="disabled", wrap="word", highlightthickness=0)
        sb = ttk.Scrollbar(log_inner, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        return text

    def _make_status_labels(self, row, row_bg, name_text: str, ip: str) -> dict:
        """서버 뷰 표의 상태 라벨 한 줄(name/ip/online/aurora/last/ping)을 만들어 dict 로 반환."""
        labels = {}
        for col, w, val in [
            ("name",   12, name_text),
            ("ip",     20, ip),
            ("online", 14, "○ 오프라인"),
            ("aurora", 14, "—"),
            ("last",   14, "—"),
            ("ping",   10, "—"),
        ]:
            fg = self.TEXT if col == "name" else self.TEXT_DIM
            lbl = tk.Label(row, text=val, width=w, anchor="center",
                           font=self.FONT_MONO, fg=fg, bg=row_bg, pady=6)
            lbl.pack(side="left")
            labels[col] = lbl
        return labels

    def _make_scroll_frame(self, parent, bg=None) -> tk.Frame:
        """스크롤 가능한 캔버스+내부 Frame을 생성해 내부 Frame을 반환."""
        bg = bg or self.BG
        canvas = tk.Canvas(parent, bg=bg, highlightthickness=0)
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

    def _btn(self, parent, text, bg, abg, cmd, width=16):
        b = tk.Button(parent, text=text, font=self.FONT_TITLE,
                      fg=self.WHITE, bg=bg, activeforeground=self.WHITE,
                      disabledforeground=self.WHITE,
                      activebackground=abg, bd=0, cursor="hand2",
                      relief="flat", width=width, pady=8, command=cmd)
        b.base_bg = bg
        b.hover_bg = abg
        b.bind("<Enter>", lambda e: b.config(bg=b.hover_bg))
        b.bind("<Leave>", lambda e: b.config(bg=b.base_bg))
        return b

    def _small_btn(self, parent, text, fg, cmd, bg=None):
        bg = bg or self.PANEL
        b = tk.Button(parent, text=text, font=self.FONT_SMALL_BOLD,
                      fg=fg, bg=bg, activeforeground=fg,
                      activebackground=self.BORDER, bd=0, cursor="hand2",
                      relief="flat", padx=10, pady=3, command=cmd)
        return b

    # ── 서버 탭 클라이언트 행 재구성 ─────
    def _rebuild_rows(self):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self.client_rows = {}

        with state_lock:
            ip_list = sorted(shared_state["clients"].keys(), key=_ip_sort_key)
            shared_state["clients"] = {ip: shared_state["clients"][ip] for ip in ip_list}

        for idx, ip in enumerate(ip_list):
            row_bg = self.BG if idx % 2 == 0 else self.ROW_ALT
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

            # ID / PW 입력
            saved = config.get("deploy_client_creds", {}).get(ip, {})
            user_var = tk.StringVar(value=saved.get("user", "user"))
            pw_var   = tk.StringVar(value=saved.get("pw",   "1"))
            self.single_client_users[ip] = user_var
            self.single_client_pws[ip]   = pw_var

            self._entry(row, user_var, 8, self.FONT_MONO
                        ).pack(side="left", padx=(4, 2), ipady=3)
            self._entry(row, pw_var, 8, self.FONT_MONO, show="*"
                        ).pack(side="left", padx=(2, 4), ipady=3)

            def _save_cred(*_, _ip=ip, _u=user_var, _p=pw_var):
                config.setdefault("deploy_client_creds", {})[_ip] = {
                    "user": _u.get(), "pw": _p.get()
                }
                save_config(config)

            user_var.trace_add("write", _save_cred)
            pw_var.trace_add("write",   _save_cred)

            with state_lock:
                cur_path = shared_state["clients"].get(ip, {}).get("aurora_path", "")
            path_lbl = tk.Label(row, text=cur_path if cur_path else "—", width=30, anchor="w",
                                font=self.FONT_MONO, fg=self.TEXT if cur_path else self.TEXT_DIM,
                                bg=row_bg, pady=6)
            path_lbl.pack(side="left", padx=(4, 4))
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
            row_bg = self.BG if idx % 2 == 0 else self.ROW_ALT
            row = tk.Frame(self.deploy_rows_frame, bg=row_bg)
            row.pack(fill="x")

            # 체크박스
            chk_var = tk.BooleanVar(value=True)
            self.deploy_check_vars[ip] = chk_var
            chk = tk.Checkbutton(row, variable=chk_var,
                                 bg=row_bg, activebackground=row_bg,
                                 selectcolor=self.PANEL, bd=0)
            chk.pack(side="left", padx=(8, 0))

            # CLIENT 이름
            client_name = config.get("client_names", {}).get(ip, f"CLIENT {idx+1:02d}")
            tk.Label(row, text=client_name, width=11, anchor="center",
                     font=self.FONT_MONO, fg=self.TEXT, bg=row_bg, pady=6).pack(side="left")

            # IP
            tk.Label(row, text=ip, width=18, anchor="center",
                     font=self.FONT_MONO, fg=self.TEXT_DIM, bg=row_bg, pady=6).pack(side="left")

            # 소스 폴더 콤보박스 — 저장된 선택값 복원
            saved_folder = saved_folders.get(ip, "")
            init_folder  = saved_folder if saved_folder in current_versions else (current_versions[0] if current_versions else "")
            combo_var = tk.StringVar(value=init_folder)
            self.deploy_combo_vars[ip] = combo_var
            combo = ttk.Combobox(row, textvariable=combo_var,
                                 values=current_versions,
                                 font=self.FONT_MONO,
                                 state="readonly", width=24)
            combo.pack(side="left", padx=(8, 6), pady=4)
            combo.bind("<MouseWheel>", lambda e: "break")
            self.deploy_combos[ip] = combo

            # 저장 경로 라벨
            dst_text = self._dst_label_text()
            # 진행률 바를 오른쪽에 고정 (헤더 "진행률"과 맞춤)
            prog_lbl = tk.Label(row, text="", font=self.FONT_MONO,
                                fg=self.TEXT_DIM, bg=row_bg, width=6, anchor="w")
            prog_lbl.pack(side="right", padx=(0, 4))
            prog = ttk.Progressbar(row, orient="horizontal", mode="determinate", length=160)
            prog.pack(side="right", padx=(0, 4))

            dst_lbl = tk.Label(row, text=dst_text, anchor="w",
                               font=self.FONT_MONO, fg=self.BLUE, bg=row_bg, pady=6)
            dst_lbl.pack(side="left", padx=(0, 4), fill="x", expand=True)
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
        auto_bg = self.BLUE if is_auto else self.TEXT_DIM
        manual_bg = self.BLUE if not is_auto else self.TEXT_DIM
        self.ping_auto_btn.base_bg = auto_bg
        self.ping_manual_btn.base_bg = manual_bg
        self.ping_auto_btn.config(bg=auto_bg)
        self.ping_manual_btn.config(bg=manual_bg)
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
            row_bg = self.PANEL if idx % 2 == 0 else self.PANEL_ALT
            row = tk.Frame(self.single_client_frame, bg=row_bg)
            row.pack(fill="x")

            var = tk.BooleanVar(value=True)
            self.single_client_vars[ip] = var
            tk.Checkbutton(row, variable=var, bg=row_bg,
                           activebackground=row_bg,
                           selectcolor=self.BG, bd=0).pack(side="left", padx=(6, 0))
            client_name = config.get("client_names", {}).get(ip, f"CLIENT {idx+1:02d}")
            tk.Label(row, text=client_name, width=10, anchor="w",
                     font=self.FONT_MONO, fg=self.TEXT, bg=row_bg, pady=5).pack(side="left")
            tk.Label(row, text=ip, width=16, anchor="w",
                     font=self.FONT_MONO, fg=self.TEXT_DIM, bg=row_bg, pady=5).pack(side="left", padx=(4, 0))
            status_lbl = tk.Label(row, text="배포 예정", width=10, anchor="center",
                                  font=self.FONT_MONO, fg=self.TEXT_DIM, bg=row_bg, pady=5)
            status_lbl.pack(side="left", padx=(4, 8))
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
                     font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.PANEL).pack(padx=12, pady=8)
            self.single_status_lbl.config(text="파일 없음", fg=self.YELLOW)
            return

        for idx, fname in enumerate(files):
            row_bg = self.PANEL if idx % 2 == 0 else self.PANEL_ALT
            row = tk.Frame(self.single_file_frame, bg=row_bg)
            row.pack(fill="x")

            var = tk.BooleanVar(value=True)
            self.single_file_vars[fname] = var
            tk.Checkbutton(row, variable=var, bg=row_bg,
                           activebackground=row_bg,
                           selectcolor=self.BG, bd=0).pack(side="left", padx=(6, 0))
            tk.Label(row, text=fname, anchor="w",
                     font=self.FONT_MONO, fg=self.TEXT, bg=row_bg, pady=5
                     ).pack(side="left", padx=(4, 12))

        self.single_file_canvas.update_idletasks()
        self.single_file_canvas.configure(
            scrollregion=(0, 0,
                          self.single_file_frame.winfo_width(),
                          self.single_file_frame.winfo_height()))
        self.single_status_lbl.config(
            text=f"✓ 파일 {len(files)}개 로드됨", fg=self.GREEN)
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

        self.single_status_lbl.config(
            text=f"배포 중... ({total}대 / {len(checked_files)}개 파일)", fg=self.YELLOW)
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
                self.single_client_status_lbls[ip].config(text="배포 중...", fg=self.YELLOW)
            else:
                self.single_client_status_lbls[ip].config(text="배포 예정", fg=self.TEXT_DIM)

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
                    self.single_client_status_lbls[ip].config(text=status_text, fg=status_fg)
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
                self.root.after(0, lambda: self.single_status_lbl.config(
                    text=f"✓ 배포 완료 ({total}대)", fg=self.GREEN))
            else:
                self.root.after(0, lambda: self.single_status_lbl.config(
                    text=f"⚠ 완료 {ok}대 / 실패 {fail_count}대", fg=self.YELLOW))

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
        self.deploy_status_lbl.config(
            text=f"✓ 적용 완료 — 버전 {len(versions)}개: {', '.join(versions)}",
            fg=self.GREEN
        )
        add_log(f"배포 소스 폴더 적용: {src}  버전: {versions}")

    def _save_deploy_combos(self):
        """클라이언트별 현재 콤보 선택값을 저장"""
        for ip, var in self.deploy_combo_vars.items():
            config.setdefault("deploy_client_folders", {})[ip] = var.get()
        save_config(config)
        self.deploy_status_lbl.config(text="✓ 소스 폴더 선택 저장됨", fg=self.GREEN)

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
        self.deploy_status_lbl.config(text="↺ 마지막 저장 값으로 복원됨", fg=self.BLUE)

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
        self.deploy_status_lbl.config(text=f"배포 중... ({total}대)", fg=self.YELLOW)
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
            self.root.after(0, lambda m=msg: self.deploy_status_lbl.config(
                text=m, fg=self.YELLOW))

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
                    self.deploy_status_lbl.config(text=f"■ 중단됨 (완료 {o}대)", fg=self.RED)
                elif f == 0:
                    self.deploy_status_lbl.config(text=f"✓ 배포 완료 ({total}대)", fg=self.GREEN)
                    self.deploy_progress_overall.configure(value=total)
                    self.deploy_overall_lbl.config(text=f"{total} / {total}")
                else:
                    self.deploy_status_lbl.config(
                        text=f"⚠ 완료 {ok_count}대 / 실패 {f}대", fg=self.YELLOW)
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
        popup.geometry("300x320")
        popup.grab_set()

        tk.Label(popup, text="삭제할 IP를 선택하세요  (Ctrl/Shift 다중 선택)",
                 font=self.FONT_SMALL, fg=self.TEXT_DIM, bg=self.BG).pack(pady=10)

        listbox = tk.Listbox(popup, font=self.FONT_MONO_L,
                             bg=self.PANEL, fg=self.TEXT,
                             selectbackground=self.RED, bd=0,
                             highlightthickness=0,
                             selectmode=tk.EXTENDED)
        listbox.pack(fill="both", expand=True, padx=16)
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

        self._btn(popup, "삭제", self.BTN_STOP, self.BTN_STOP_H, confirm, 16).pack(pady=10)

    # ── 현재 공정 IP 관리 ────────────────
    def _rebuild_current_rows(self):
        for w in self.current_rows_frame.winfo_children():
            w.destroy()
        self.current_client_rows = {}

        ips = config.get("current_process_ips", [])
        for idx, ip in enumerate(ips):
            row_bg = self.BG if idx % 2 == 0 else self.ROW_ALT
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
        popup.geometry("300x280")
        popup.grab_set()

        tk.Label(popup, text="삭제할 IP를 선택하세요",
                 font=self.FONT, fg=self.TEXT_DIM, bg=self.BG).pack(pady=10)

        listbox = tk.Listbox(popup, font=self.FONT_MONO_L,
                             bg=self.PANEL, fg=self.TEXT,
                             selectbackground=self.RED, bd=0,
                             highlightthickness=0)
        listbox.pack(fill="both", expand=True, padx=16)
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

        self._btn(popup, "삭제", self.BTN_STOP, self.BTN_STOP_H, confirm, 16).pack(pady=10)

    def _toggle_autostart(self):
        try:
            new_state = not autostart_enabled()
            set_autostart(new_state)
            self._autostart_btn.config(
                text="ON  ●" if new_state else "OFF ○",
                fg=self.GREEN if new_state else self.TEXT_DIM)
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

        def _apply_status(labels, info):
            online  = info["online"]
            aurora  = info["aurora_running"]
            last    = info["last_seen"]
            ping_ms = info.get("ping_ms")
            _set(labels["online"],
                 "● 온라인" if online else "○ 오프라인",
                 self.GREEN if online else self.TEXT_DIM)
            if not online:
                _set(labels["aurora"], "—", self.TEXT_DIM)
            elif aurora:
                _set(labels["aurora"], "● 실행중", self.GREEN)
            else:
                _set(labels["aurora"], "○ 중지", self.RED)
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
                labels["online"].config(text="○ 오프라인", fg=self.TEXT_DIM)
                labels["aurora"].config(text="—", fg=self.TEXT_DIM)
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
        threading.Thread(target=run_http_server, daemon=True).start()
        threading.Thread(target=check_offline,   daemon=True).start()
        add_log(f"HTTP 서버 시작: 0.0.0.0:{SERVER_PORT}")
        self.server_status_lbl.config(
            text=f"● HTTP 서버 실행중  :{SERVER_PORT}", fg=self.GREEN)
        if config.get("ping_mode", "manual") == "auto":
            self._start_auto_ping()


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
                    arrowcolor=A.TEXT_DIM, bordercolor=A.BORDER,
                    lightcolor=A.BORDER, darkcolor=A.BORDER)
    style.configure("TNotebook",
                    background=A.BG, borderwidth=0, tabmargins=0)
    style.configure("TNotebook.Tab",
                    background=A.BORDER, foreground=A.TEXT_DIM,
                    font=A.FONT_BOLD, padding=[16, 8],
                    borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", A.PANEL)],
              foreground=[("selected", A.BLUE)])
    style.configure("Treeview",
                    background=A.PANEL, fieldbackground=A.PANEL,
                    foreground=A.TEXT, rowheight=26, borderwidth=0,
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
                    bordercolor=A.BORDER, lightcolor=A.BORDER,
                    darkcolor=A.BORDER)
    style.map("TCombobox",
              fieldbackground=[("readonly", A.PANEL)],
              foreground=[("readonly", A.TEXT)])
    style.configure("Horizontal.TProgressbar",
                    background=A.BLUE, troughcolor=A.BORDER,
                    bordercolor=A.BORDER, lightcolor=A.BLUE, darkcolor=A.BLUE)

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
