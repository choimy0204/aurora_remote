"""
deploy.bat 본체 — 각 PC 의 D 드라이브 공유(\\\\IP\\D)에 Aurora 를 설치/갱신

  대상 PC   : D:\\RemoteConfig\\server_config.json 의 client_ips (서버 뷰 클라이언트 목록)
  서버 PC   : D:\\RemoteConfig\\client_config.json 의 server_ips  → Server + Client 설치
  나머지 PC : Client 만 설치

각 PC 에 만드는 것
  D:\\RemoteConfig\\Update\\    실행 폴더 — AuroraClient.exe (+ 서버 PC 는 AuroraServer.exe), version.json, 매뉴얼
                                → 실행 중인 exe 는 이름을 *.old 로 비켜 두고 새 exe 를 넣음 (다음 실행부터 새 버전)
  D:\\RemoteConfig\\            client_config.json (+ 서버 PC 는 server_config.json)
                                → 이미 있으면 그 PC 설정을 지키기 위해 덮어쓰지 않음 (--overwrite-config 로 강제)
  D:\\RemoteVersion\\           새 exe + version.json → 켜져 있는 프로그램이 업데이트 신호·재시작 때 여기서 가져감

사용:
  deploy.bat                         전체 PC (확인 후 진행)
  deploy.bat 192.168.110.21 ...      지정한 PC 만
  deploy.bat --dry                   복사 없이 계획만 표시
  deploy.bat --overwrite-config      RemoteConfig 의 json 도 이 PC 것으로 덮어씀
"""

import os
import sys
import json
import shutil
import argparse
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor

FROZEN     = getattr(sys, "frozen", False)   # AuroraDeploy.exe (Python 없는 PC 용)
ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# exe 는 dist\latest 안에 AuroraClient/Server.exe 와 함께 있으므로 자기 폴더가 원본
DEFAULT_SRC = os.path.dirname(sys.executable) if FROZEN else os.path.join(ROOT, "dist", "latest")
CONFIG_DIR = r"D:\RemoteConfig"
CLIENT_EXE = "AuroraClient.exe"
SERVER_EXE = "AuroraServer.exe"
MANUAL     = "Aurora_사용매뉴얼.md"
# 새 PC 에 client_config.json 을 넣을 때 빼는 항목 (이 PC 전용 값)
PC_SPECIFIC_KEYS = {"aurora_path", "kill_processes", "auto_delete_rules",
                    "export_save_dir", "autostart_off", "server_ip"}

_print_lock = threading.Lock()


def log(msg: str):
    with _print_lock:
        print(msg, flush=True)


def load_json(name: str) -> dict:
    try:
        with open(os.path.join(CONFIG_DIR, name), encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        sys.exit(f"[오류] {CONFIG_DIR}\\{name} 읽기 실패: {e}")


def _same_file(src: str, dst: str) -> bool:
    try:
        a, b = os.stat(src), os.stat(dst)
        return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)
    except OSError:
        return False


def copy(src: str, dst: str) -> str:
    """'복사' / '동일' / '잠김' 반환 (그 외 오류는 예외)"""
    if _same_file(src, dst):
        return "동일"
    try:
        shutil.copy2(src, dst)
        return "복사"
    except PermissionError:
        return "잠김"   # 대상 PC 에서 실행 중인 exe


def replace_running(src: str, dst: str) -> str:
    """실행 중이라 덮어쓸 수 없는 exe — 이름 바꾸기는 되므로 *.old 로 비켜 두고 새 파일을 넣는다.
    지금 떠 있는 프로그램은 그대로 돌고, 다음 실행(업데이트 신호·재부팅)부터 새 버전. '교체' / '잠김' 반환"""
    for n in range(10):
        old = dst + (".old" if n == 0 else f".old{n}")
        try:
            if os.path.exists(old):
                os.remove(old)   # 예전에 비켜 둔 파일 — 아직 실행 중이면 지워지지 않으므로 다음 이름
            os.rename(dst, old)
            break
        except OSError:
            continue
    else:
        return "잠김"
    try:
        shutil.copy2(src, dst)
        return "교체"
    except OSError:
        os.rename(old, dst)   # 복사 실패 → 원래대로
        return "잠김"


def remove_old(folder: str):
    """지난 배포에서 비켜 둔 *.old* exe 정리 (아직 실행 중이면 남겨 둠)"""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for n in names:
        if ".exe.old" in n.lower():
            try:
                os.remove(os.path.join(folder, n))
            except OSError:
                pass


def share_root(ip: str) -> str:
    return f"\\\\{ip}\\D"


def connect(ip: str, user: str, pw: str) -> tuple[bool, bool]:
    """(접속 성공, net use 를 새로 연결했는지)"""
    unc = share_root(ip)
    if os.path.isdir(unc):
        return True, False
    if not user:
        return False, False
    r = subprocess.run(["net", "use", unc, f"/user:{user}", pw, "/persistent:no"],
                       capture_output=True, text=True, encoding="cp949", errors="replace")
    return r.returncode == 0 and os.path.isdir(unc), r.returncode == 0


def deploy_one(ip: str, name: str, is_server: bool, src: str, cfg: dict, args) -> tuple:
    role = "Server+Client" if is_server else "Client"
    tag = f"[{name} {ip}]"
    exes = [CLIENT_EXE, SERVER_EXE] if is_server else [CLIENT_EXE]
    if args.dry:
        log(f"{tag} {role} → D:\\RemoteConfig\\Update, D:\\RemoteVersion (계획만)")
        return ip, name, role, "계획", ""

    ok, mounted = connect(ip, cfg.get("deploy_user", ""), cfg.get("deploy_pw", ""))
    if not ok:
        log(f"{tag} [실패] \\\\{ip}\\D 접속 실패 (전원·네트워크·D 공유·계정 확인)")
        return ip, name, role, "실패", "D 공유 접속 불가"
    try:
        d = share_root(ip)
        conf_dir = d + r"\RemoteConfig"
        app_dir = conf_dir + r"\Update"      # 실행 폴더
        upd_dir = d + r"\RemoteVersion"      # 자동 업데이트 원본
        for p in (app_dir, conf_dir, upd_dir):
            os.makedirs(p, exist_ok=True)

        notes = []
        # 1) 실행 파일 → D:\RemoteConfig\Update (실행 폴더)
        remove_old(app_dir)
        for exe in exes:
            s, t = os.path.join(src, exe), os.path.join(app_dir, exe)
            if copy(s, t) == "잠김":
                if replace_running(s, t) == "교체":
                    notes.append(f"{exe} 실행 중 → 교체해 둠 ([클라이언트 업데이트] 신호·재시작 때 새 버전)")
                else:
                    notes.append(f"{exe} 실행 중 → 교체 못 함, RemoteVersion 으로 자동 업데이트")
        # 이전 버전(v2.14.0)은 실행 폴더의 version.json 을 보고 업데이트하므로 여기에도 둔다
        for f in ("version.json", MANUAL):
            if os.path.isfile(os.path.join(src, f)):
                copy(os.path.join(src, f), os.path.join(app_dir, f))

        # 2) 설정 → D:\RemoteConfig (기존 설정은 보존)
        confs = ["client_config.json"] + (["server_config.json"] if is_server else [])
        for c in confs:
            dst = os.path.join(conf_dir, c)
            if os.path.exists(dst) and not args.overwrite_config:
                continue
            if c == "client_config.json":
                # 실행 경로·종료 목록 등 PC 마다 다른 값은 빼고 공통 값만 → 나머지는 클라이언트 기본값 사용
                data = {k: v for k, v in load_json(c).items() if k not in PC_SPECIFIC_KEYS}
                # 덮어쓰기(--overwrite-config)여도 그 PC 의 실행 경로·종료 목록 등은 유지
                try:
                    with open(dst, encoding="utf-8-sig") as f:
                        old = json.load(f)
                    data.update({k: v for k, v in old.items() if k in PC_SPECIFIC_KEYS})
                except Exception:
                    pass
                with open(dst, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                shutil.copy2(os.path.join(CONFIG_DIR, c), dst)
            notes.append(f"{c} 설치")

        # 3) 자동 업데이트 원본 → D:\RemoteVersion (update_state.json 은 PC 별 기록이라 복사하지 않음)
        for f in exes + ["version.json"]:
            copy(os.path.join(src, f), os.path.join(upd_dir, f))

        log(f"{tag} [OK] {role}" + (f"  ({' / '.join(notes)})" if notes else ""))
        return ip, name, role, "완료", " / ".join(notes)
    except Exception as e:
        log(f"{tag} [실패] {e}")
        return ip, name, role, "실패", str(e)
    finally:
        if mounted:
            subprocess.run(["net", "use", f"\\\\{ip}\\D", "/delete", "/yes"], capture_output=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Aurora 원격 설치/갱신")
    ap.add_argument("ips", nargs="*", help="대상 IP (생략 시 서버 설정의 클라이언트 전체)")
    ap.add_argument("--src", default=DEFAULT_SRC, help="exe 원본 폴더")
    ap.add_argument("--dry", action="store_true", help="복사 없이 계획만 표시")
    ap.add_argument("--overwrite-config", action="store_true", help="대상 PC 의 RemoteConfig json 덮어쓰기")
    ap.add_argument("-y", "--yes", action="store_true", help="확인 없이 진행")
    args = ap.parse_args()

    for f in (CLIENT_EXE, SERVER_EXE, "version.json"):
        if not os.path.isfile(os.path.join(args.src, f)):
            sys.exit(f"[오류] {args.src}\\{f} 없음 — build.bat 을 먼저 실행하세요")

    scfg, ccfg = load_json("server_config.json"), load_json("client_config.json")
    names   = scfg.get("client_names", {})
    servers = set(ccfg.get("server_ips", []))
    targets = args.ips or scfg.get("client_ips", [])
    if not targets:
        sys.exit("[오류] 대상 IP 가 없습니다 (server_config.json 의 client_ips)")
    with open(os.path.join(args.src, "version.json"), encoding="utf-8") as f:
        ver = json.load(f)

    print(f"배포 버전: Client v{ver.get('client')} / Server v{ver.get('server')}  (원본 {args.src})")
    print(f"대상 {len(targets)}대  (Server+Client {sum(ip in servers for ip in targets)}대)")
    for ip in targets:
        print(f"  {'S+C' if ip in servers else ' C '}  {ip:<16} {names.get(ip, '')}")
    if args.overwrite_config:
        print("※ 대상 PC 의 RemoteConfig json 을 이 PC 설정으로 덮어씁니다")
    if not args.dry and not args.yes:
        if input("\n진행할까요? (Y/N) ").strip().lower() != "y":
            sys.exit("취소했습니다")
    print()

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda ip: deploy_one(ip, names.get(ip, ip), ip in servers,
                                                    args.src, scfg, args), targets))

    fails = [r for r in results if r[3] == "실패"]
    print(f"\n완료 {sum(r[3] == '완료' for r in results)}대 · 실패 {len(fails)}대")
    for ip, name, _, _, why in fails:
        print(f"  [실패] {name} ({ip}) — {why}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    try:
        main()
    finally:
        if FROZEN:   # 더블클릭 실행 시 결과를 보기 전에 창이 닫히지 않게
            try:
                input("\n엔터를 누르면 창을 닫습니다...")
            except Exception:
                pass
