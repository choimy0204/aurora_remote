# -*- mode: python ; coding: utf-8 -*-

# WAN 안내 서버(AuroraRelay.exe) 빌드 설정 — AuroraClient.spec 과 같은 방식.
# 화면이 없는 콘솔 프로그램이라 tkinter·pystray·PIL 관련 항목은 뺐다.

# 사용하지 않는데 의존성으로 딸려오는 무거운 모듈 제외 (빌드 시간·EXE 크기 단축)
EXCLUDES = [
    'numpy', 'setuptools', 'pkg_resources', 'packaging',
    'unittest', 'pydoc', 'doctest', '_pyrepl', 'lib2to3', 'xmlrpc', 'sqlite3',
    'test', 'idlelib',
    'tkinter', 'tkinter.test',
    # PyInstaller 가 이 목록에 '__main__' 을 직접 추가해 캐시에 저장하므로 미리 넣어둔다
    # (없으면 매 빌드마다 'excludes changed' 로 캐시가 무효화되어 전체 재분석)
    '__main__',
]


a = Analysis(
    ['wan_relay.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['websockets.asyncio.server', 'websockets.asyncio.client'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AuroraRelay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
