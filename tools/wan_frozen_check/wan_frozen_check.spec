# -*- mode: python ; coding: utf-8 -*-
# WAN 번들 점검용 spec — AuroraClient.spec 의 EXCLUDES · hiddenimports 를 그대로 읽어 적용한다.
# 목적: 제외 목록·숨은 import 때문에 aiortc(직접 연결)·websockets(중계)가 exe 안에서 깨지지 않는지 확인.
#   pyinstaller tools\wan_frozen_check\wan_frozen_check.spec --distpath tools\wan_frozen_check\dist --workpath tools\wan_frozen_check\build
#   tools\wan_frozen_check\dist\wan_frozen_check.exe     → 마지막 줄 FROZEN SELFTEST OK
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
src = open(os.path.join(ROOT, "AuroraClient.spec"), encoding="utf-8").read()
ns = {}
start = src.index("EXCLUDES = [")
exec(src[start:src.index("]\n", start) + 1], ns)
h = src.index("hiddenimports=") + len("hiddenimports=")
hidden = eval(src[h:src.index("],", h) + 1])

a = Analysis([os.path.join(SPECPATH, "entry.py")], pathex=[ROOT], datas=[], hiddenimports=hidden,
             excludes=ns["EXCLUDES"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="wan_frozen_check", debug=False, upx=False, console=True)
