"""groknmok self-update: replace this program with the latest signed release.

  groknmok self-update            check, download, verify, ask, replace
  groknmok self-update --check    only say whether a newer release exists
  groknmok self-update --rollback put the previous program back

The latest release comes from GitHub's releases API (unauthenticated;
GROKNMOK_RELEASES_URL overrides it). Its groknmok.pyz must carry a minisign
signature (groknmok.pyz.minisig) from the release key built into this
program, match SHA256SUMS when the release has one, be a zip archive, and
say inside that it is the version the release names; only a newer version
is offered. The new file is written next to the current one and renamed
over it, after you confirm at a terminal: a run without a terminal (the
timer) never replaces the program; it sends one ntfy note per new version
instead. The replaced program is kept in ~/.local/share/groknmok/ for
--rollback. A program run from source is never replaced (use git)."""

import argparse
import hashlib
import io
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime

import requests

from groknmok import REPO, __version__, client, keys, minisign, notify

RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = 24 * 3600
MAX_SIZE = 50 * 1024 * 1024
TIMEOUT = (15, 60)


class UpdateError(Exception):
    pass


def program_path():
    """Path of the zipapp this program runs from, or None when it runs from source."""
    import groknmok
    archive = os.path.dirname(os.path.dirname(os.path.abspath(groknmok.__file__)))
    return archive if os.path.isfile(archive) and zipfile.is_zipfile(archive) else None


def latest(s):
    """{version, tag, assets: {name: url}} of the latest release, or None if there is none."""
    url = os.environ.get("GROKNMOK_RELEASES_URL") or RELEASES_URL
    r = s.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise UpdateError(f"releases: HTTP {r.status_code}")
    j = r.json()
    m = re.fullmatch(r"v(\d+\.\d+\.\d+)", j.get("tag_name") or "")
    if not m:
        raise UpdateError(f"latest release tag {j.get('tag_name')!r} is not vX.Y.Z")
    return {"version": m.group(1), "tag": j["tag_name"],
            "assets": {a["name"]: a["browser_download_url"] for a in j.get("assets", [])}}


def newer(info):
    return bool(info) and client.version_tuple(info["version"]) > client.version_tuple(__version__)


def check(p, force=False):
    """Latest release info, at most once a day unless forced (cached in state.json);
    the last known answer when the check fails (no network)."""
    su = client.load_json(p.state, {}).get("selfupdate", {})
    if not force and su.get("checked_at") and \
            (client.now() - datetime.fromisoformat(su["checked_at"])).total_seconds() < CHECK_EVERY:
        return su.get("latest")
    try:
        info = latest(client.session())
    except (requests.RequestException, UpdateError, ValueError):
        info = su.get("latest")
    st = client.load_json(p.state, {})
    st.setdefault("selfupdate", {}).update(checked_at=client.now().isoformat(), latest=info)
    client.save_json(p.state, st)
    return info


def _get(s, url, limit):
    with s.get(url, stream=True, timeout=TIMEOUT) as r:
        if r.status_code != 200:
            raise UpdateError(f"{url.rsplit('/', 1)[-1]}: HTTP {r.status_code}")
        data = b""
        for chunk in r.iter_content(1 << 20):
            data += chunk
            if len(data) > limit:
                raise UpdateError(f"{url.rsplit('/', 1)[-1]}: larger than {limit} bytes")
        return data


def verified_download(info, pubkey=None):
    """The release's groknmok.pyz bytes, checked: signature, SHA256SUMS, zip, version."""
    a = info["assets"]
    if "groknmok.pyz" not in a or "groknmok.pyz.minisig" not in a:
        raise UpdateError("the release has no signed groknmok.pyz")
    s = client.session()
    data = _get(s, a["groknmok.pyz"], MAX_SIZE)
    sig = _get(s, a["groknmok.pyz.minisig"], 4096).decode("utf-8", "replace")
    try:
        minisign.verify(pubkey or keys.RELEASE_KEY, data, sig)
    except minisign.BadSignature as e:
        raise UpdateError(f"groknmok.pyz signature check failed: {e}") from None
    if "SHA256SUMS" in a:
        sums = _get(s, a["SHA256SUMS"], 4096).decode("ascii", "replace")
        want = next((x.split()[0] for x in sums.splitlines() if x.strip().endswith(" groknmok.pyz")), None)
        if want != hashlib.sha256(data).hexdigest():
            raise UpdateError("groknmok.pyz does not match SHA256SUMS")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            init = z.read("groknmok/__init__.py").decode()
            z.getinfo("__main__.py")
    except (zipfile.BadZipFile, KeyError):
        raise UpdateError("groknmok.pyz is not a groknmok program") from None
    inside = re.search(r'__version__ = "([^"]+)"', init)
    if not inside or inside.group(1) != info["version"]:
        raise UpdateError(f"groknmok.pyz says it is {inside and inside.group(1)}, the release says {info['version']}")
    return data


def confirm(question):
    """True / False from a terminal; None when there is no terminal to ask."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def replace_program(program, data):
    """Write data next to program and rename it over program (mode 0755)."""
    tmp = os.path.join(os.path.dirname(program), f".{os.path.basename(program)}.new")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, program)


def keep_copy(p, program, version):
    os.makedirs(p.data_dir, exist_ok=True)
    dest = os.path.join(p.data_dir, f"groknmok-{version}.pyz")
    shutil.copyfile(program, dest)
    return dest


def remember_previous(p, kept, version):
    st = client.load_json(p.state, {})
    st.setdefault("selfupdate", {}).update(previous=kept, previous_version=version)
    client.save_json(p.state, st)


def self_update(p, only_check=False):
    try:
        info = latest(client.session())
    except (requests.RequestException, UpdateError, ValueError) as e:
        print(f"could not check for a new release: {e if isinstance(e, UpdateError) else type(e).__name__}")
        return 1
    st = client.load_json(p.state, {})
    st.setdefault("selfupdate", {}).update(checked_at=client.now().isoformat(), latest=info)
    client.save_json(p.state, st)
    if not info:
        print("no release found; nothing to do")
        return 0
    if not newer(info):
        print(f"groknmok {__version__} is current (latest release {info['version']})")
        return 0
    print(f"groknmok {info['version']} is available (you have {__version__})")
    if only_check:
        return 0
    program = program_path()
    if not program:
        print("this groknmok runs from source; update it with git instead")
        return 1
    try:
        data = verified_download(info)
    except (UpdateError, requests.RequestException) as e:
        print(f"not updated: {e}")
        return 1
    ok = confirm(f"Replace {program} ({__version__}) with {info['version']} (signature verified)?")
    if not ok:
        print("not updated" + ("" if ok is False else ": run `groknmok self-update` in a terminal to confirm"))
        return 1
    kept = keep_copy(p, program, __version__)
    replace_program(program, data)
    remember_previous(p, kept, __version__)
    print(f"updated to {info['version']}; the previous program is kept at {kept} (groknmok self-update --rollback)")
    return 0


def rollback(p):
    program = program_path()
    su = client.load_json(p.state, {}).get("selfupdate", {})
    prev = su.get("previous")
    if not program:
        print("this groknmok runs from source; nothing to roll back")
        return 1
    if not prev or not os.path.isfile(prev):
        print("no previous program kept")
        return 1
    ok = confirm(f"Put back groknmok {su.get('previous_version')} over {program} ({__version__})?")
    if not ok:
        print("not rolled back" + ("" if ok is False else ": run it in a terminal to confirm"))
        return 1
    with open(prev, "rb") as fh:
        data = fh.read()
    kept = keep_copy(p, program, __version__)
    replace_program(program, data)
    remember_previous(p, kept, __version__)
    print(f"rolled back to {su.get('previous_version')}; {__version__} is kept at {kept}")
    return 0


def notify_if_available(p):
    """Timer runs: at most daily, one ntfy note per newer version; never replaces anything."""
    info = check(p)
    su = client.load_json(p.state, {}).get("selfupdate", {})
    if not newer(info) or su.get("notified") == info["version"]:
        return
    cfg = notify.load_config(p.ntfy)
    if cfg and notify.send(cfg, "groknmok: update available",
                           f"groknmok {info['version']} is available (you have {__version__}). "
                           "Run `groknmok self-update` in a terminal.", tags="arrow_up", priority=2,
                           user_agent=client.USER_AGENT):
        st = client.load_json(p.state, {})
        st.setdefault("selfupdate", {})["notified"] = info["version"]
        client.save_json(p.state, st)


def interactive_notice(p):
    """One stderr line after a search at a terminal, at most daily, when a newer release exists."""
    if sys.stderr.isatty() and newer(check(p)):
        print(f"groknmok {check(p)['version']} is available: groknmok self-update", file=sys.stderr)


def cmd(argv):
    ap = argparse.ArgumentParser(prog="groknmok self-update", description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="only report whether a newer release exists")
    g.add_argument("--rollback", action="store_true", help="put the previous program back")
    a = ap.parse_args(argv)
    p = client.paths()
    return rollback(p) if a.rollback else self_update(p, a.check)
