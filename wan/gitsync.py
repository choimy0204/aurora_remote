"""
사내 Git 서버로 접속 정보 주고받기
==================================
중계 PC 가 접속 정보 파일(aurora_wan.conn)을 사내 Git 저장소에 올려 두면, 다른 PC 들이 그 저장소에서 가져와 자동으로 접속한다.
  - 저장소에 접근할 수 있는 사람(회사 사람)만 접속 정보를 볼 수 있다 — 인증은 Git 이 이미 하고 있는 방식(자격 증명 관리자 · SSH 키)을 그대로 쓴다.
  - 공용 키를 바꾸면 저장소에 새 접속 정보를 다시 올리기만 하면 모든 PC 가 알아서 따라온다.
  - git 명령(PC 에 설치된 git)을 쓴다.  비밀번호를 물어보느라 멈추지 않도록 터미널 입력은 끈다 (GIT_TERMINAL_PROMPT=0).
  ⚠ 접속 정보에는 공용 키가 들어 있다 — 반드시 **접근이 제한된 사내 저장소**에 올릴 것 (공개 저장소 · GitHub 금지).
"""

import os
import shutil
import subprocess

from . import connect_code, connfile

DEFAULT_FILE = connfile.FILE_NAME
TIMEOUT = 60


class GitError(Exception):
    """화면에 그대로 보여 줄 수 있는 한국어 메시지를 가진 오류"""


def available() -> bool:
    return shutil.which("git") is not None


def _git(args, cwd=None, timeout=TIMEOUT) -> str:
    if not available():
        raise GitError("이 PC 에 git 이 설치되어 있지 않습니다 (Git for Windows 를 설치하세요)")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", LC_ALL="C")
    try:
        r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise GitError("Git 서버가 응답하지 않습니다 (주소 · 네트워크를 확인하세요)") from None
    except OSError as e:
        raise GitError(f"git 을 실행하지 못했습니다: {e}") from None
    if r.returncode != 0:
        raise GitError(_explain((r.stderr or r.stdout or "").strip(), args))
    return r.stdout


def _explain(text: str, args) -> str:
    low = text.lower()
    if any(k in low for k in ("authentication failed", "could not read username", "permission denied", "access denied",
                              "terminal prompts disabled", "403", "401")):
        return "Git 서버 로그인에 실패했습니다 — 이 PC 에서 한 번 직접 git 으로 로그인(자격 증명 저장)하거나 SSH 키를 등록하세요"
    if any(k in low for k in ("repository not found", "does not appear to be a git repository", "not found",
                              "does not exist")):
        return "Git 저장소를 찾을 수 없습니다 — 주소를 확인하세요"
    if any(k in low for k in ("could not resolve host", "unable to access", "connection refused", "timed out")):
        return "Git 서버에 닿지 않습니다 — 사내망(VPN)에 연결되어 있는지 확인하세요"
    last = text.splitlines()[-1] if text else "알 수 없는 오류"
    return f"git {args[0]} 실패: {last[:200]}"


def sync(source: str, dest: str, branch: str = "") -> None:
    """저장소의 최신 내용을 dest 에 맞춘다 (없으면 가볍게 clone, 있으면 fetch 후 맞춤).  쓰기 작업 흔적은 버린다"""
    if os.path.isdir(os.path.join(dest, ".git")):
        try:
            _git(["remote", "set-url", "origin", source], cwd=dest)
            _git(["fetch", "--depth", "1", "origin", *([branch] if branch else [])], cwd=dest)
            _git(["reset", "--hard", "FETCH_HEAD"], cwd=dest)
            return
        except GitError:
            shutil.rmtree(dest, ignore_errors=True)            # 망가진 복제본이면 새로 받는다
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    _git(["clone", "--depth", "1", *(["--branch", branch] if branch else []), source, dest])


def fetch_conn(source: str, dest: str, filename: str = DEFAULT_FILE, branch: str = "") -> str:
    """저장소에서 접속 설정 파일 내용을 가져온다 (없거나 접속 정보가 아니면 GitError)"""
    sync(source, dest, branch)
    path = os.path.join(dest, filename)
    try:
        return connfile.read(path)
    except ValueError as e:
        raise GitError(f"저장소에서 '{filename}' 를 읽지 못했습니다 — {e}") from None


def publish(source: str, dest: str, code: str, filename: str = DEFAULT_FILE, branch: str = "",
            message: str = "Aurora WAN 접속 정보 갱신") -> bool:
    """접속 정보(code)를 저장소의 파일로 올린다 → 실제로 바뀌어 올렸으면 True, 이미 같은 내용이면 False"""
    if connect_code.kind_of(code) == "":
        raise GitError("올릴 접속 정보가 없습니다 — 먼저 '자동 설정'을 누르세요")
    try:
        sync(source, dest, branch)
    except GitError as e:
        if "저장소를 찾을 수 없" in str(e) or not os.path.isdir(os.path.join(dest, ".git")):
            raise
    if not os.path.isdir(os.path.join(dest, ".git")):
        raise GitError("저장소를 가져오지 못했습니다")
    path = os.path.join(dest, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    connfile.write(path, code)
    _git(["add", "--", filename], cwd=dest)
    if not _git(["status", "--porcelain", "--", filename], cwd=dest).strip():
        return False
    ident = [] if _has_identity(dest) else ["-c", "user.name=Aurora WAN", "-c", "user.email=aurora-wan@localhost"]
    _git([*ident, "commit", "-m", message], cwd=dest)
    _git(["push", "origin", f"HEAD:{branch}" if branch else "HEAD"], cwd=dest)
    return True


def _has_identity(cwd) -> bool:
    try:
        return bool(_git(["config", "user.name"], cwd=cwd).strip() and _git(["config", "user.email"], cwd=cwd).strip())
    except GitError:
        return False
