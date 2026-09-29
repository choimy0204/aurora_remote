# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:\\업무\\02_프로젝트\\Aurora\\aurora_server.py'],
    pathex=[],
    binaries=[],
    datas=[('D:\\업무\\02_프로젝트\\Aurora\\aurora_server.ico', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='AuroraServer',
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
    icon=['D:\\업무\\02_프로젝트\\Aurora\\aurora_server.ico'],
)
