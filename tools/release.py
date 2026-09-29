"""
build.bat 보조 스크립트 — dist 폴더를 최신/구버전으로 정리

  dist/latest/         최신 빌드 (AuroraClient.exe, AuroraServer.exe, version.json, 매뉴얼)
                       → 이 폴더 내용을 각 PC 의 D:\\RemoteConfig\\Update 에 넣으면 자동 업데이트
  dist/old/v<버전>/    이전 버전 보관 (버전이 바뀐 빌드를 할 때 latest 를 여기로 옮김)

사용:
  python tools/release.py prepare    빌드 전 — dist 에서 실행 중인 exe 종료, 버전이 바뀌었으면 latest → old 이동
  python tools/release.py manifest   빌드 후 — dist/latest/version.json 기록
"""

import os
import re
import sys
import json
import shutil
from datetime import datetime

ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST   = os.path.join(ROOT, "dist")
LATEST = os.path.join(DIST, "latest")
OLD    = os.path.join(DIST, "old")
SOURCES = {"client": "aurora_client.py", "server": "aurora_server.py"}


def source_versions() -> dict:
    """소스의 APP_VERSION 값"""
    vers = {}
    for kind, fn in SOURCES.items():
        with open(os.path.join(ROOT, fn), encoding="utf-8") as f:
            m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        if not m:
            sys.exit(f"{fn} 에 APP_VERSION 이 없습니다")
        vers[kind] = m.group(1)
    return vers


def latest_versions() -> dict:
    try:
        with open(os.path.join(LATEST, "version.json"), encoding="utf-8") as f:
            d = json.load(f)
        return {k: d.get(k, "") for k in SOURCES}
    except Exception:
        return {}


def _folder_name(vers: dict) -> str:
    if vers["client"] == vers["server"]:
        return f"v{vers['client']}"
    return f"client_v{vers['client']}_server_v{vers['server']}"


def _kill_dist_processes():
    """dist 폴더에서 실행 중인 exe 만 종료 (다른 경로에서 쓰는 Aurora 는 건드리지 않음)"""
    try:
        import psutil
    except ImportError:
        return
    dist = os.path.normcase(DIST)
    for p in psutil.process_iter(["name", "exe"]):
        try:
            if p.info["name"] in ("AuroraClient.exe", "AuroraServer.exe") and \
               p.info["exe"] and os.path.normcase(p.info["exe"]).startswith(dist):
                p.kill()
                print(f"종료: {p.info['exe']}")
        except Exception:
            pass


def prepare():
    _kill_dist_processes()
    new, prev = source_versions(), latest_versions()
    if prev and all(prev.values()) and prev != new and os.path.isdir(LATEST):
        dst = os.path.join(OLD, _folder_name(prev))
        if os.path.exists(dst):
            shutil.rmtree(dst)
        os.makedirs(OLD, exist_ok=True)
        shutil.move(LATEST, dst)
        print(f"이전 버전 보관: dist/latest → dist/old/{_folder_name(prev)}")
    os.makedirs(LATEST, exist_ok=True)
    print(f"빌드 버전: {_folder_name(new)}")


def manifest():
    vers = source_versions()
    vers["built"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(LATEST, "version.json"), "w", encoding="utf-8") as f:
        json.dump(vers, f, ensure_ascii=False, indent=2)
    print(f"version.json 기록: client v{vers['client']} / server v{vers['server']}")


if __name__ == "__main__":
    {"prepare": prepare, "manifest": manifest}[sys.argv[1]]()
