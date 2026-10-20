# -*- mode: python ; coding: utf-8 -*-

# 사용하지 않는데 의존성으로 딸려오는 무거운 모듈 제외 (빌드 시간·EXE 크기 단축)
EXCLUDES = [
    'numpy', 'setuptools', 'pkg_resources', 'packaging',
    'unittest', 'pydoc', 'doctest', '_pyrepl', 'lib2to3', 'xmlrpc', 'sqlite3',
    'tkinter.test', 'test', 'idlelib',
    'PIL._avif', 'PIL.AvifImagePlugin', 'PIL._webp', 'PIL.WebPImagePlugin',
    'PIL.ImageQt', 'PIL.ImageMath', 'PIL.ImageCms', 'PIL._imagingcms', 'PIL._imagingmath',
    'gi', 'Xlib', 'pystray._gtk', 'pystray._xorg', 'pystray._appindicator', 'pystray._darwin',
    # PyInstaller 가 이 목록에 '__main__' 을 직접 추가해 캐시에 저장하므로 미리 넣어둔다
    # (없으면 매 빌드마다 'excludes changed' 로 캐시가 무효화되어 전체 재분석)
    '__main__',
]


a = Analysis(
    ['aurora_client.py'],
    pathex=[],
    binaries=[],
    datas=[('aurora_client.ico', '.'), ('assets', 'assets')],
    # WAN 원격(wan 패키지) — 함수 안에서 늦게 불러오는 모듈들이라 명시. 설치되지 않은 것은 빌드 때 경고만 나고
    # 건너뜀(aiortc 가 없으면 직접 연결 없이 비상 중계만, websockets 도 없으면 WAN 탭만 "사용할 수 없음")
    hiddenimports=['wan.runtime', 'wan.ui', 'wan.viewer', 'wan.supervisor', 'wan.update', 'wan.client',
                   'wan.agent', 'wan.server', 'wan.bridge', 'wan.mux', 'wan.links', 'wan.config', 'wan.usage',
                   'wan.protocol', 'wan.relay_run', 'wan.netutil', 'websockets.asyncio.client', 'websockets.asyncio.server',
                   'aiortc', 'aioice', 'pylibsrtp', 'pyee', 'dns', 'ifaddr', 'google_crc32c'],
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
    name='AuroraClient',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['aurora_client.ico'],
)
