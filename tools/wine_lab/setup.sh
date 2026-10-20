#!/bin/bash
# Linux 에서 Wine 위에 "Windows 용 Python + tkinter" 를 만든다.
# 목적: winreg · ctypes.windll · user32 · Tk 같은 Windows 전용 코드를 가짜(tests/winshim.py) 없이 실제 Windows 구현(Wine)으로 시험.
# (진짜 Windows 는 아니므로 화면 캡처(DXGI)·입력 주입 같은 하드웨어/드라이버 경로는 대상이 아님)
#
# 준비(Ubuntu):  sudo apt-get install -y wine64 xvfb p7zip-full msitools curl python3
# 사용:          tools/wine_lab/setup.sh            → $WORK(기본 ~/winelab) 아래에 prefix 와 Python 을 만든다
#                tools/wine_lab/run_tests.sh [pytest 인자...]
set -euo pipefail
PYVER="${PYVER:-3.12.7}"
WORK="${WORK:-$HOME/winelab}"
export WINEPREFIX="$WORK/prefix" WINEDEBUG=-all WINEARCH=win64
mkdir -p "$WORK"

echo "[1/6] Wine prefix 만들기"
xvfb-run -a wineboot --init > /dev/null 2>&1 || true

echo "[2/6] Windows 용 Python $PYVER (NuGet 배포본 — tkinter 는 빠져 있음)"
curl -fsSL -o "$WORK/py.nupkg" "https://www.nuget.org/api/v2/package/python/$PYVER"
rm -rf "$WORK/nuget" && mkdir -p "$WORK/nuget"
python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$WORK/py.nupkg" "$WORK/nuget"

echo "[3/6] python.org 설치 파일에서 tcl/tk 꺼내기 (설치 파일 안의 CAB → tcltk.msi)"
curl -fsSL -o "$WORK/py-installer.exe" "https://www.python.org/ftp/python/$PYVER/python-$PYVER-amd64.exe"
rm -rf "$WORK/cab" "$WORK/tk" && mkdir -p "$WORK/cab" "$WORK/tk"
python3 - "$WORK/py-installer.exe" "$WORK/cab" <<'PY'
import re, struct, sys
data = open(sys.argv[1], "rb").read()
best = max(((struct.unpack_from("<I", data, m.start() + 8)[0], m.start())
            for m in re.finditer(b"MSCF\x00\x00\x00\x00", data)))      # 가장 큰 CAB 에 패키지(msi)들이 들어 있음
open(sys.argv[2] + "/big.cab", "wb").write(data[best[1]:best[1] + best[0]])
PY
7z x -y -o"$WORK/cab/files" "$WORK/cab/big.cab" > /dev/null
for f in "$WORK"/cab/files/*; do                                      # 이름 없는 msi 들 중 _tkinter.pyd 가 든 것
    if msiextract --list "$f" 2>/dev/null | grep -q '_tkinter.pyd'; then
        msiextract -C "$WORK/tk" "$f" > /dev/null
        break
    fi
done
test -f "$WORK/tk/DLLs/_tkinter.pyd" || { echo "tcl/tk 를 찾지 못함" >&2; exit 1; }

echo "[4/6] 합치기 → C:\\Python312"
cp -r "$WORK/tk/." "$WORK/nuget/tools/"
rm -rf "$WINEPREFIX/drive_c/Python312"
cp -r "$WORK/nuget/tools" "$WINEPREFIX/drive_c/Python312"

echo "[5/6] D: 드라이브 연결 (앱이 D:\\RemoteConfig 를 쓰므로)"
mkdir -p "$WORK/d"
ln -sfn "$WORK/d" "$WINEPREFIX/dosdevices/d:"

echo "[6/6] 패키지 설치"
export PIP_CERT="${PIP_CERT:-}" PYTHONUTF8=1
xvfb-run -a wine 'C:\Python312\python.exe' -m ensurepip > /dev/null 2>&1 || true
xvfb-run -a wine 'C:\Python312\python.exe' -m pip install --no-warn-script-location -q \
    pytest websockets aiortc requests psutil pillow pystray < /dev/null
xvfb-run -a wine 'C:\Python312\python.exe' -c "import tkinter, sys; print('OK: Windows Python', sys.version.split()[0], 'tkinter', tkinter.TkVersion)"
