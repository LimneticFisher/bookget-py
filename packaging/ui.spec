# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for bookget-ui.exe
Bundles server module + built frontend (ui/dist-app/).
Double-click to launch browser UI at http://localhost:8765.
"""
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_all

ROOT = Path(SPECPATH).parent
DIST_APP = ROOT / 'ui' / 'dist-app'

if not DIST_APP.exists():
    raise FileNotFoundError(
        f"Frontend not built: {DIST_APP}\n"
        "Run: cd ui && npm run build:app"
    )

# Auto-discover all adapter modules so we never miss a new one.
adapter_imports = collect_submodules('bookget.adapters')

# Bundle Playwright + its driver + the Chromium installed with
# PLAYWRIGHT_BROWSERS_PATH=0 (lives in playwright/driver/package/.local-browsers),
# so 识典古籍 works without a separate `playwright install`.
pw_datas, pw_binaries, pw_hidden = collect_all('playwright')

a = Analysis(
    [str(ROOT / 'bookget' / 'main.py')],
    pathex=[str(ROOT)],
    binaries=[*pw_binaries],
    datas=[
        # Bundle entire frontend dist
        (str(DIST_APP), 'ui/dist-app'),
        *pw_datas,
    ],
    hiddenimports=[
        *adapter_imports,
        *pw_hidden,
        # core
        'bookget.core',
        'bookget.models',
        'bookget.pdf',
        'bookget.pdf.writer',
        'bookget.pdf.sheet',
        'bookget.pdf.build',
        'bookget.pdf.schema',
        'bookget.pdf.latex',
        'bookget.pdf.verify',
        'bookget.pdf.text_layer',
        'bookget.text_converters.pages',
        # Lazily imported for the PDF text layer and verification; bundled
        # only if installed in the build environment.
        'pymupdf',
        'bookget.server',
        'bookget.server.app',
        'bookget.server.routes',
        'bookget.server.sse',
        'bookget.server.static',
        'bookget.server.tasks',
        'bookget.server.folder_picker',
        'bookget.text_parsers',
        'bookget.text_converters',
        'bookget.ia_upload',
        'bookget.ia_metadata',
        'aiohttp',
        'aiohttp.web',
        'aiohttp.web_middlewares',
        'multidict',
        'yarl',
        'async_timeout',
        'charset_normalizer',
        # Hard dependency of bookget/shared/cjk_match (繁简/异体归一化).
        # Missing it degrades matching silently, so it must be bundled.
        'opencc',
        # Zoomify tile stitching (TNM); imported lazily inside tiles.py.
        'PIL',
        'PIL.Image',
        # optional: see cli.spec comment
        'internetarchive',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # NOTE: PIL is NOT excluded — bookget/downloaders/tiles.py imports it
    # (lazily) to stitch Zoomify tiles for TNM. Excluding it made that
    # site fail only in the packaged exe, never in dev.
    excludes=['tkinter', 'matplotlib', 'PyQt5', 'wx'],
    noarchive=False,
)

# 识典古籍 launches headless, which only needs chromium_headless_shell-*.
# Drop the full headed Chromium (~440 MB; only NCL rbook's manual captcha uses
# it). Filtered after Analysis because PyInstaller's playwright hook adds it too.
import re
_FULL_CHROMIUM = re.compile(r'[\\/]\.local-browsers[\\/]chromium-\d')
a.datas = [e for e in a.datas if not _FULL_CHROMIUM.search(e[0])]
a.binaries = [e for e in a.binaries if not _FULL_CHROMIUM.search(e[0])]

pyz = PYZ(a.pure)

# One-dir build: with Chromium bundled, one-file would unpack ~300 MB to a
# temp dir on every launch.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='bookget-ui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,   # No console window — browser-only UI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='bookget-ui',
)
