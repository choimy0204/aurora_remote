# -*- mode: python ; coding: utf-8 -*-

# 사용하지 않는데 의존성으로 딸려오는 무거운 모듈 제외 (빌드 시간·EXE 크기 단축)
EXCLUDES = [
    'numpy', 'setuptools', 'pkg_resources', 'packaging',
    'unittest', 'pydoc', 'doctest', '_pyrepl', 'lib2to3', 'xmlrpc', 'sqlite3',
    'tkinter.test', 'test', 'idlelib',
    'PIL._avif', 'PIL.AvifImagePlugin', 'PIL._webp', 'PIL.WebPImagePlugin',
    'PIL.ImageQt', 'PIL.ImageMath', 'PIL.ImageCms', 'PIL._imagingcms', 'PIL._imagingmath',
    'gi', 'Xlib', 'pystray._gtk', 'pystray._xorg', 'pystray._appindicator', 'pystray._darwin',
]


a = Analysis(
    ['aurora_client.py'],
    pathex=[],
    binaries=[],
    datas=[('aurora_client.ico', '.'), ('assets', 'assets')],
    hiddenimports=[],
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
