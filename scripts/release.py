#!/usr/bin/env python3
"""Publish a signed program release: dist/groknmok.pyz, its minisign signature
and SHA256SUMS, as GitHub release v<version> of the GroknMok repo.

Before anything is uploaded it checks: the signature verifies with the
release key built into groknmok (groknmok/keys.py), SHA256SUMS matches, the
archive is not a test build, the version inside it equals the tag, the commit
the tag will point at (HEAD) is on GitHub, and no release with that tag
exists. The token comes from ~/.config/groknmok/github-token (mode 0600,
Contents read and write) and is never printed.

Steps for a release:
  python3 scripts/build_pyz.py
  minisign -S -s ~/.config/groknmok/minisign.key -m dist/groknmok.pyz
  python3 scripts/release.py --dry-run      # all checks, nothing uploaded
  python3 scripts/release.py

Usage: release.py [--dist dist] [--dry-run] [--prerelease] [--token-file FILE]
"""

import argparse
import hashlib
import io
import os
import re
import stat
import subprocess
import sys
import zipfile

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from groknmok import NOTICE, REPO, keys, minisign  # noqa: E402

API = f"https://api.github.com/repos/{REPO}"
NAMES = ("groknmok.pyz", "groknmok.pyz.minisig", "SHA256SUMS")


def fail(msg):
    sys.exit(f"release: {msg}")


def check_dist(dist):
    files = {}
    for n in NAMES:
        path = os.path.join(dist, n)
        if not os.path.isfile(path):
            fail(f"{path} missing" + (" (sign it: minisign -S -s ~/.config/groknmok/minisign.key -m "
                                      f"{os.path.join(dist, 'groknmok.pyz')})" if n.endswith(".minisig") else ""))
        with open(path, "rb") as fh:
            files[n] = fh.read()
    pyz = files["groknmok.pyz"]
    with zipfile.ZipFile(io.BytesIO(pyz)) as z:
        if b"TEST BUILD" in z.read("groknmok/keys.py"):
            fail("this is a test build (scripts/build_pyz.py --test-keys); rebuild without it")
        version = re.search(r'__version__ = "([^"]+)"', z.read("groknmok/__init__.py").decode()).group(1)
    try:
        trusted = minisign.verify(keys.RELEASE_KEY, pyz, files["groknmok.pyz.minisig"].decode())
    except minisign.BadSignature as e:
        fail(f"signature: {e}")
    digest = hashlib.sha256(pyz).hexdigest()
    if files["SHA256SUMS"].decode().split() != [digest, "groknmok.pyz"]:
        fail("SHA256SUMS does not match groknmok.pyz")
    return files, version, digest, trusted


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--dry-run", action="store_true", help="every check, nothing uploaded")
    ap.add_argument("--prerelease", action="store_true", help="mark it a pre-release (never the latest)")
    ap.add_argument("--token-file", default=os.path.expanduser("~/.config/groknmok/github-token"))
    a = ap.parse_args()
    files, version, digest, trusted = check_dist(a.dist)
    tag = f"v{version}"
    head = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    print(f"{tag}: groknmok.pyz {len(files['groknmok.pyz']):,} bytes, sha256 {digest}")
    print(f"signature ok ({trusted}); tag {tag} at {head[:12]}")
    st = os.stat(a.token_file)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        fail(f"{a.token_file} must be mode 0600 and owned by you")
    with open(a.token_file, encoding="utf-8") as fh:
        s = requests.Session()
        s.headers.update({"Authorization": f"Bearer {fh.read().strip()}", "Accept": "application/vnd.github+json",
                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": f"groknmok-release (+https://github.com/{REPO})"})
    if s.get(f"{API}/commits/{head}", timeout=30).status_code != 200:
        fail(f"commit {head[:12]} is not on GitHub yet: push main first")
    if s.get(f"{API}/releases/tags/{tag}", timeout=30).status_code != 404:
        fail(f"a release {tag} already exists")
    if a.dry_run:
        print("dry run: every check passed; nothing uploaded")
        return 0
    body = (f"groknmok {version}\n\nInstall: download groknmok.pyz to ~/.local/bin/groknmok and make it executable "
            f"(Python 3.10 or newer).\n\nsha256 {digest}\n\nVerify: minisign -Vm groknmok.pyz -P {keys.RELEASE_KEY}\n\n"
            f"{NOTICE}\n")
    r = s.post(f"{API}/releases", timeout=30, json={
        "tag_name": tag, "target_commitish": head, "name": f"groknmok {version}", "body": body,
        "prerelease": a.prerelease, "make_latest": "false" if a.prerelease else "true"})
    if r.status_code != 201:
        fail(f"create release: HTTP {r.status_code} {r.json().get('message', '')}")
    rel = r.json()
    up = rel["upload_url"].split("{")[0]
    for n in NAMES:
        ctype = "application/octet-stream" if n.endswith(".pyz") else "text/plain"
        u = s.post(up, params={"name": n}, data=files[n], headers={"Content-Type": ctype}, timeout=(20, 300))
        if u.status_code != 201:
            fail(f"upload {n}: HTTP {u.status_code}; the release exists without it, delete it and retry")
        print(f"uploaded {n}")
    print(f"released {tag}: {rel['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
