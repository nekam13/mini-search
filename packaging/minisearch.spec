# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Mini Search desktop app (Linux / Windows / macOS).

The bundle ships the whole Python runtime plus the Flask backend, so the user
needs neither Python nor Termux. ``templates/`` and ``static/`` are added as
*data* files; Flask finds them relative to ``app_combined.py``, which
``launcher.py`` puts on ``sys.path`` when running frozen.

Build:
    pyinstaller --clean --noconfirm packaging/minisearch.spec

Model files are intentionally *not* bundled: the desktop build falls back to
FTS5 (and the dependency-free hashing embedder) unless the user drops an ONNX
model next to the executable and sets ``MINISEARCH_MODEL``.
"""

import os

# SPECPATH is the directory that contains this spec file (packaging/), so the
# repository root is one level up.
ROOT = os.path.abspath(os.path.dirname(SPECPATH))
APP_NAME = "mini-search"

datas = [
    (os.path.join(ROOT, "templates"), "templates"),
    (os.path.join(ROOT, "static"), "static"),
    # Ship the app source so Flask's relative template lookup works when frozen.
    (os.path.join(ROOT, "app_combined.py"), "."),
]

hiddenimports = [
    "app_combined",
    "apscheduler.schedulers.background",
    "apscheduler.triggers.interval",
    "apscheduler.triggers.cron",
    "bs4",
    "lxml",
    "lxml.etree",
    "feedparser",
    "werkzeug.serving",
    "jinja2",
    "markupsafe",
]

# extruct (and its rdflib stack) is optional; only pull it in when installed.
try:
    import extruct  # noqa: F401
    hiddenimports += ["extruct", "rdflib", "jstyleson"]
except Exception:
    pass

a = Analysis(
    [os.path.join(ROOT, "desktop", "launcher.py")],
    pathex=[ROOT, os.path.join(ROOT, "desktop")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2", "sentence_transformers"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
