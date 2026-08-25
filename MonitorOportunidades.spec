# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all, copy_metadata

PROJECT_DIR = os.path.abspath(os.getcwd())
datas = [('app.py', '.'), ('config.example.json', '.'), ('settings.example.json', '.')]
binaries = []
hiddenimports = [
    'streamlit.web.cli',
    'config_manager',
    'scraper_engine',
    'notifier',
    'dados',
    'visual_similarity',
    'aiohttp',
]
datas += copy_metadata('streamlit')
datas += copy_metadata('streamlit_autorefresh')
datas += copy_metadata('playwright')
tmp_ret = collect_all('streamlit')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('streamlit_autorefresh')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['launcher.py'],
    pathex=[PROJECT_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'scipy',
        'tensorboard',
        'torch',
        'torchvision',
        'clip',
        'torch.utils.tensorboard',
    ],
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
    name='MonitorOportunidades',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
