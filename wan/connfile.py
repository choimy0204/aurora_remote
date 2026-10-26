"""
접속 설정 파일 (aurora_wan.conn)
================================
중계 PC 에서 만든 접속 정보를 파일 하나로 저장해 두었다가, AuroraClient.exe 를 옮길 때 같은 폴더에 함께 두면
처음 실행할 때 자동으로 적용된다 (주소 · 공용 키를 다시 입력하거나 붙여넣을 필요 없음).
  - 내용은 접속 정보 글자(AW1-…  중계 PC / AD1-…  현장 PC 주소) 한 줄 + 설명 한 줄.
  - 같은 내용은 한 번만 적용한다 (그 뒤 화면에서 바꾼 설정을 매번 파일이 덮어쓰지 않도록 지문을 기록).
"""

import hashlib
import os
import sys

from . import connect_code

FILE_NAME = "aurora_wan.conn"
HEADER = "# Aurora WAN 접속 설정 — 이 파일을 AuroraClient.exe 와 같은 폴더에 두면 실행할 때 자동으로 적용됩니다"


def search_dirs(cfg_dir: str = "") -> list:
    """파일을 찾을 폴더: 실행 파일 옆 → 현재 폴더 → WAN 설정 폴더"""
    if getattr(sys, "frozen", False):
        here = os.path.dirname(sys.executable)
    else:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = []
    for d in (here, os.getcwd(), cfg_dir):
        if d and d not in out:
            out.append(d)
    return out


def find(dirs) -> str | None:
    for d in dirs:
        path = os.path.join(d, FILE_NAME)
        if os.path.isfile(path):
            return path
    return None


def render(code: str) -> str:
    return f"{HEADER}\n{code.strip()}\n"


def write(path: str, code: str):
    if connect_code.kind_of(code) == "":
        raise ValueError("저장할 접속 정보가 없습니다")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(code))


def read(path: str) -> str:
    """파일 내용에서 접속 정보 글자를 돌려줌 (없거나 깨졌으면 ValueError)"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read(64 * 1024)
    except OSError as e:
        raise ValueError(f"파일을 읽지 못했습니다: {e.strerror or e}") from None
    if connect_code.kind_of(text) == "":
        raise ValueError("접속 정보가 들어 있는 파일이 아닙니다")
    return text


def fingerprint(text: str) -> str:
    code = "".join(text.split())
    for prefix in (connect_code.PREFIX, connect_code.PREFIX_DIRECT):
        if prefix in code:
            code = code[code.index(prefix):]
            break
    return hashlib.sha1(code.encode("utf-8")).hexdigest()[:16]
