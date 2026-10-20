#!/bin/bash
# Wine 위의 Windows 용 Python 으로 pytest 실행.  예)  tools/wine_lab/run_tests.sh tests/test_wan_realtk.py -v
# 주의: 표준 입력/출력이 연결되어 있어야 한다 (파일로 직접 리다이렉트하지 말고 파이프로: ... | tee 결과.txt)
set -euo pipefail
WORK="${WORK:-$HOME/winelab}"
export WINEPREFIX="$WORK/prefix" WINEDEBUG=-all PYTHONUTF8=1 PYTHONIOENCODING=utf-8 LC_ALL=C.UTF-8 AURORA_HOST_TESTS=1 AURORA_HOST_INPUT=1   # Wine 이 한글 파일 이름을 만들려면 UTF-8 로케일이 필요
cd "$(dirname "$0")/../.."
exec xvfb-run -a -s "-screen 0 1400x1000x24" wine 'C:\Python312\python.exe' -m pytest -p no:cacheprovider "${@:-tests}"
