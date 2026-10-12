#!/usr/bin/env python3
"""Build groknmok.pyz: one file, run by the system python3 (3.10 or newer).

The archive holds the groknmok package (not the tests) and the dependencies in
requirements-pyz.txt (pure-Python wheels, pinned by sha256, unpacked with
their licence files), plus a __main__.py. It is reproducible: entries sorted,
fixed timestamps and permissions, so the same sources and wheels give the
same bytes on any machine. Writes dist/groknmok.pyz (mode 0755) and
dist/SHA256SUMS.

Signing is a separate, manual step with the release key (it has a password):
  minisign -S -s ~/.config/groknmok/minisign.key -m dist/groknmok.pyz

Usage:
  python3 scripts/build_pyz.py [--out dist] [--wheels build/wheels]
  python3 scripts/build_pyz.py --test-keys DATASET.pub RELEASE.pub --out DIR
      a test build that trusts other public keys (never publish one)
"""

import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMP = (1980, 1, 1, 0, 0, 0)
MAIN = b'''import sys

if sys.version_info < (3, 10):
    sys.exit("groknmok needs Python 3.10 or newer (this is %d.%d)" % sys.version_info[:2])

from groknmok.cli import main  # noqa: E402

sys.exit(main())
'''


def wheels(dest):
    os.makedirs(dest, exist_ok=True)
    subprocess.run([sys.executable, "-m", "pip", "download", "-q", "--disable-pip-version-check", "--no-deps",
                    "--require-hashes", "--only-binary=:all:", "--platform", "any", "--python-version", "3.10",
                    "--implementation", "py", "--abi", "none", "-d", dest,
                    "-r", os.path.join(ROOT, "requirements-pyz.txt")], check=True)
    want = {re.split(r"[=\s]", x.strip(), maxsplit=1)[0].replace("-", "_").lower()
            for x in open(os.path.join(ROOT, "requirements-pyz.txt")) if x.strip() and not x.startswith("#")}
    found = sorted(f for f in os.listdir(dest) if f.endswith(".whl") and f.split("-")[0].lower() in want)
    if len(found) != len(want):
        sys.exit(f"expected {len(want)} wheels in {dest}, found {found}")
    return [os.path.join(dest, f) for f in found]


def key_line(path):
    lines = [x.strip() for x in open(path) if x.strip() and not x.startswith("untrusted comment:")]
    if len(lines) != 1:
        sys.exit(f"{path}: not a minisign public key")
    return lines[0]


def entries(wheel_paths, test_keys):
    """{archive name: bytes}"""
    out = {}
    for w in wheel_paths:
        with zipfile.ZipFile(w) as z:
            for name in z.namelist():
                if name.endswith("/") or "__pycache__" in name or name.endswith(".pyc"):
                    continue
                if ".dist-info/" in name and not re.search(r"\.dist-info/(METADATA|licenses/.*|LICEN[CS]E.*)$", name):
                    continue
                out[name] = z.read(name)
    pkg = os.path.join(ROOT, "groknmok")
    for f in sorted(os.listdir(pkg)):
        if f.endswith(".py"):
            with open(os.path.join(pkg, f), "rb") as fh:
                out[f"groknmok/{f}"] = fh.read()
    with open(os.path.join(ROOT, "LICENSE"), "rb") as fh:
        out["groknmok/LICENSE"] = fh.read()
    if test_keys:
        ds, rel = (key_line(p) for p in test_keys)
        out["groknmok/keys.py"] = (f'"""TEST BUILD: trusts test keys only. Never publish this build."""\n\n'
                                   f'DATASET_KEY = "{ds}"\nRELEASE_KEY = "{rel}"\n').encode()
    out["__main__.py"] = MAIN
    return out


def write_pyz(path, files):
    buf = io.BytesIO()
    buf.write(b"#!/usr/bin/env python3\n")  # before the archive, so its offsets count it (as zipapp does)
    with zipfile.ZipFile(buf, "w") as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            z.writestr(info, files[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    data = buf.getvalue()
    with open(path + ".tmp", "wb") as fh:
        fh.write(data)
    os.chmod(path + ".tmp", 0o755)
    os.replace(path + ".tmp", path)
    return hashlib.sha256(data).hexdigest(), len(data)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--wheels", default=os.path.join(ROOT, "build", "wheels"))
    ap.add_argument("--test-keys", nargs=2, metavar=("DATASET_PUB", "RELEASE_PUB"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    files = entries(wheels(a.wheels), a.test_keys)
    version = re.search(r'__version__ = "([^"]+)"', files["groknmok/__init__.py"].decode()).group(1)
    path = os.path.join(a.out, "groknmok.pyz")
    digest, size = write_pyz(path, files)
    with open(os.path.join(a.out, "SHA256SUMS"), "w", encoding="ascii", newline="\n") as fh:
        fh.write(f"{digest}  groknmok.pyz\n")
    print(f"groknmok {version}{' (TEST KEYS)' if a.test_keys else ''}: {path}, {size:,} bytes, "
          f"{len(files)} files, sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
