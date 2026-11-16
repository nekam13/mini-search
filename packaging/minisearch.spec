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
import sys

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

binaries = []

# extruct (and its rdflib/jstyleson stack) is optional; only pull it in when
# installed. collect_all also grabs the package *data* files, which a bare
# hiddenimport misses: mf2py ships "backcompat-rules" as a data file and raises
# FileNotFoundError at import time without it, and rdflib ships plugin files.
try:
    import extruct  # noqa: F401
    from PyInstaller.utils.hooks import collect_all

    for _pkg in ("extruct", "mf2py", "rdflib", "jstyleson"):
        try:
            _d, _b, _h = collect_all(_pkg)
        except Exception:
            continue
        datas += _d
        binaries += _b
        hiddenimports += _h
except Exception:
    pass

a = Analysis(
    [os.path.join(ROOT, "desktop", "launcher.py")],
    pathex=[ROOT, os.path.join(ROOT, "desktop")],
    binaries=binaries,
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

# On macOS, also wrap the collected directory in a double-clickable .app bundle.
# This lives in the spec (not as a `--windowed` CLI flag) because PyInstaller
# rejects make-spec options when a .spec file is given.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier="cz.minisearch.desktop",
    )
