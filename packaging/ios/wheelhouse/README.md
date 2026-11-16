# wheelhouse – pure-Python wheels for the iOS build

Briefcase installs iOS requirements with `--only-binary :all:` (it will not
compile C extensions for the target), and two of our dependencies only publish
C-extension wheels:

- **MarkupSafe** (pulled in by Flask). There is no iOS wheel on PyPI or on the
  BeeWare index. Its C speedups are optional — `setup.py` retries the build
  without the extension and ships a pure-Python fallback — so a `py3-none-any`
  wheel works on iOS.

`pyproject.toml` therefore passes `--find-links ./wheelhouse` via
`requirement_installer_args`, and pip finds this wheel instead of failing.

## Regenerating the wheel

Built from the pinned sdist, with the C extension deliberately disabled:

```bash
pip download markupsafe==3.0.4 --no-binary :all: --no-deps -d /tmp/ms
tar -xzf /tmp/ms/markupsafe-3.0.4.tar.gz -C /tmp/ms
cd /tmp/ms/markupsafe-3.0.4
CC=/bin/false python3 -m pip wheel . --no-deps -w out   # pure-Python wheel
cp out/markupsafe-3.0.4-py3-none-any.whl packaging/ios/wheelhouse/
```

## Integrity

| File | SHA-256 |
|---|---|
| `markupsafe-3.0.4-py3-none-any.whl` | `d26ba0134d749ecd0b64cde4455423baaeae6720763479b093a990c2ef5d8ca9` |

MarkupSafe is BSD-3-Clause; the wheel is the unmodified upstream package.
