"""
Aurora 자동 업데이트 (서버·클라이언트 공용)
=====================================
D:\\RemoteConfig\\Update 폴더에 빌드 결과물(dist\\latest 내용)을 넣어두면
- 프로그램이 켜질 때 자동으로 새 버전을 감지해 교체 후 재시작
- [최신 버전 확인] 버튼으로 수동 확인 → 새 버전이면 교체 후 재시작

업데이트 폴더 구성 (build.bat 이 dist\\latest 에 만들어 줌)
    AuroraClient.exe / AuroraServer.exe / version.json
    version.json = {"client": "2.8.0", "server": "2.8.0", "built": "..."}
"""

import os
import sys
import json
import shutil
import subprocess
import tempfile

UPDATE_DIR   = r"D:\RemoteConfig\Update"
_STATE_FILE  = r"D:\RemoteConfig\update_state.json"
_EXE_NAMES   = {"client": "AuroraClient.exe", "server": "AuroraServer.exe"}


def parse_ver(s: str) -> tuple:
    """'2.8.0' / 'v2.8' → (2, 8, 0) — 숫자가 아닌 부분은 무시"""
    parts = []
    for p in str(s).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def check(kind: str, current: str, update_dir: str = UPDATE_DIR) -> dict:
    """업데이트 폴더 확인 결과
    {"status": "newer"|"latest"|"none"|"error", "version": 폴더 버전, "exe": 새 exe 경로, "msg": 설명}"""
    manifest = os.path.join(update_dir, "version.json")
    exe = os.path.join(update_dir, _EXE_NAMES[kind])
    if not os.path.isfile(manifest):
        return {"status": "none", "msg": f"업데이트 파일 없음 ({manifest})"}
    try:
        with open(manifest, "r", encoding="utf-8-sig") as f:
            ver = str(json.load(f).get(kind, "")).strip()
    except Exception as e:
        return {"status": "error", "msg": f"version.json 읽기 실패: {e}"}
    if not ver:
        return {"status": "none", "msg": f"version.json 에 {kind} 버전 없음"}
    if parse_ver(ver) <= parse_ver(current):
        return {"status": "latest", "version": ver, "msg": f"최신 버전 사용 중 (현재 v{current} / 폴더 v{ver})"}
    if not os.path.isfile(exe):
        return {"status": "error", "version": ver, "msg": f"새 버전 v{ver} 이 있지만 {exe} 파일이 없음"}
    return {"status": "newer", "version": ver, "exe": exe,
            "msg": f"새 버전 발견: v{current} → v{ver}"}


# ── 재시작 반복 방지 ─────────────────────
# version.json 만 올리고 exe 를 안 바꾼 경우 등, 교체 후에도 버전이 그대로면
# 켜질 때마다 업데이트→재시작이 무한 반복될 수 있으므로 "자동" 시도는 버전당 1회만 한다.
def _load_state() -> dict:
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def auto_already_tried(kind: str, version: str) -> bool:
    return _load_state().get(kind) == version


def mark_tried(kind: str, version: str):
    st = _load_state()
    st[kind] = version
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def apply(kind: str, new_exe: str, version: str, args: list | None = None) -> str | None:
    """새 exe 로 교체 후 재시작하는 스크립트를 띄운다. args: 재시작할 때 넘길 인자 (예: ["--tray"])
    성공하면 None (호출 측은 즉시 프로그램을 종료해야 함), 실패하면 오류 메시지."""
    if not getattr(sys, "frozen", False):
        return "개발 환경(.py 실행)에서는 자동 업데이트를 할 수 없습니다 (EXE 에서만 동작)"
    target = sys.executable
    staged = target + ".new"
    try:
        # 실행 중에도 복사는 가능 → 복사 실패(권한·용량)는 여기서 미리 걸러낸다
        shutil.copy2(new_exe, staged)
    except Exception as e:
        return f"새 파일 복사 실패: {e}"
    mark_tried(kind, version)

    # 현재 프로세스(onefile 부트로더 포함)가 끝나 exe 잠금이 풀릴 때까지 move 를 재시도한 뒤 재시작
    bat = os.path.join(tempfile.gettempdir(), f"aurora_{kind}_update.bat")
    extra = "".join(f' "{a}"' for a in (args or []))
    script = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "set N=0\r\n"
        ":retry\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        f'move /y "{staged}" "{target}" >nul 2>&1 && goto :run\r\n'
        "set /a N+=1\r\n"
        "if %N% lss 60 goto :retry\r\n"
        ":run\r\n"
        f'start "" "{target}"{extra}\r\n'
        'del "%~f0"\r\n'
    )
    try:
        with open(bat, "w", encoding="utf-8") as f:
            f.write(script)
        # 새 exe 가 이 프로세스의 임시 폴더(_MEIxxxx, 종료 시 삭제됨)를 물려받지 않게
        # → 안 하면 재시작 때 'Failed to load Python DLL ... _MEIxxxx\python314.dll' 오류
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
        subprocess.Popen(["cmd", "/c", bat], env=env,
                         # 숨은 콘솔 1개를 ping 등 하위 명령도 같이 쓰도록 (창 깜빡임 없음)
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
    except Exception as e:
        return f"업데이트 스크립트 실행 실패: {e}"
    return None
