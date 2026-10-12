"""The GroknMok client's local state: where things live, its settings, and
keeping the dataset up to date.

Files (XDG locations; GROKNMOK_CONFIG_DIR / GROKNMOK_DATA_DIR override them):
  ~/.config/groknmok/client.json     settings: core_only, watch_live
  ~/.config/groknmok/ntfy.conf       ntfy settings for watch alerts (0600)
  ~/.config/groknmok/watch.csv       your watches
  ~/.local/share/groknmok/           core.sqlite, catalogue.sqlite, state.json,
                                     title_exclude.txt, watch_state.json

Updating (update()): fetch manifest.json from the dataset release, check its
minisign signature against the dataset key built into this program and that
the signature names the manifest's build, check the format and minimum client
version, then download only the parts whose sha256 changed, check size and
sha256, decompress next to the current file, check it opens as the right part,
and swap it in with a rename. Anything that goes wrong on the way - no
network, a 404, a bad signature, a manifest older than the data we have (the
release URL can lag a minute or two), a corrupt download - leaves the current
dataset in place and is reported as a note; the next check tries again. Only
having no dataset at all is an error."""

import fcntl
import hashlib
import json
import lzma
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

import requests

from groknmok import REPO, __version__, dataset, keys, minisign

USER_AGENT = f"groknmok/{__version__} (+https://github.com/{REPO})"
DATASET_URL = f"https://github.com/{REPO}/releases/download/dataset/"
CHECK_EVERY = 15 * 60  # seconds between automatic dataset checks
TIMEOUT = (15, 60)
PART_TIMEOUT = (15, 120)
ASSET = re.compile(r"(core|catalogue)-\d{8}T\d{6}Z\.sqlite\.xz")
SIGNATURE = re.compile(r"manifest-\d{8}T\d{6}Z\.json\.minisig")
DEFAULT_SETTINGS = {"core_only": False, "watch_live": False}


class UpdateError(Exception):
    pass


def paths():
    home = os.path.expanduser("~")
    cfg = os.environ.get("GROKNMOK_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "groknmok")
    data = os.environ.get("GROKNMOK_DATA_DIR") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "groknmok")
    j = os.path.join
    return SimpleNamespace(config_dir=cfg, data_dir=data, settings=j(cfg, "client.json"), ntfy=j(cfg, "ntfy.conf"),
                           watch_file=j(cfg, "watch.csv"), state=j(data, "state.json"),
                           core=j(data, "core.sqlite"), catalogue=j(data, "catalogue.sqlite"),
                           exclude_list=j(data, "title_exclude.txt"), watch_state=j(data, "watch_state.json"),
                           lock=j(data, ".update.lock"))


def dataset_url():
    url = os.environ.get("GROKNMOK_DATASET_URL") or DATASET_URL
    return url if url.endswith("/") else url + "/"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def settings(p):
    return {**DEFAULT_SETTINGS, **load_json(p.settings, {})}


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def age_text(iso):
    """'3 h ago' / '2 days ago' for an ISO time."""
    secs = (now() - datetime.fromisoformat(iso)).total_seconds()
    if secs < 5400:
        return f"{max(secs, 0) / 60:.0f} min ago"
    return f"{secs / 3600:.0f} h ago" if secs < 172800 else f"{secs / 86400:.0f} days ago"


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3])


def session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def fetch_manifest(s, base, pubkey):
    """The signed manifest, or UpdateError."""
    r = s.get(base + "manifest.json", timeout=TIMEOUT)
    if r.status_code != 200:
        raise UpdateError(f"manifest.json: HTTP {r.status_code}")
    raw = r.content
    try:
        m = json.loads(raw)
    except ValueError:
        raise UpdateError("manifest.json is not JSON") from None
    sig = m.get("signature") if isinstance(m, dict) else None
    if not isinstance(sig, str) or not SIGNATURE.fullmatch(sig):
        raise UpdateError("manifest.json is not signed")
    rs = s.get(base + sig, timeout=TIMEOUT)
    if rs.status_code != 200:
        raise UpdateError(f"{sig}: HTTP {rs.status_code}")
    try:
        trusted = minisign.verify(pubkey, raw, rs.text)
    except minisign.BadSignature as e:
        raise UpdateError(f"manifest signature check failed: {e}") from None
    if trusted != f"groknmok dataset manifest {m.get('build')}":
        raise UpdateError("manifest signature names another build")
    return m


def incompatible(m):
    """Why this program cannot read the manifest's dataset, or None."""
    if m.get("format_version") != dataset.FORMAT_VERSION:
        return (f"the published dataset uses format {m.get('format_version')}; this groknmok reads format "
                f"{dataset.FORMAT_VERSION}: run `groknmok self-update`")
    need = m.get("min_client_version") or "0"
    if version_tuple(need) > version_tuple(__version__):
        return f"This dataset needs groknmok {need} or newer: run `groknmok self-update`"
    return None


def _progress(text):
    if sys.stderr.isatty():
        print(text, file=sys.stderr, flush=True)


def download_part(s, base, part, data_dir):
    """Download, check and decompress one part next to the data; returns the
    temporary SQLite path. UpdateError on any mismatch (temporary files removed)."""
    name, asset = part.get("name"), part.get("asset") or ""
    if name not in ("core", "catalogue") or not ASSET.fullmatch(asset) or not asset.startswith(name + "-"):
        raise UpdateError(f"manifest names an unexpected file {asset!r}")
    xz_tmp = os.path.join(data_dir, f".{name}.sqlite.xz.tmp")
    sql_tmp = os.path.join(data_dir, f".{name}.sqlite.tmp")
    _progress(f"downloading {name} ({part['size'] / 1e6:.1f} MB) ...")
    try:
        h, n = hashlib.sha256(), 0
        with s.get(base + asset, stream=True, timeout=PART_TIMEOUT) as r:
            if r.status_code != 200:
                raise UpdateError(f"{asset}: HTTP {r.status_code}")
            with open(xz_tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    n += len(chunk)
                    if n > part["size"]:
                        raise UpdateError(f"{asset}: larger than the manifest says")
                    h.update(chunk)
                    fh.write(chunk)
        if n != part["size"] or h.hexdigest() != part["sha256"]:
            raise UpdateError(f"{asset}: download does not match the manifest's sha256")
        _progress(f"unpacking {name} ...")
        try:
            with lzma.open(xz_tmp) as src, open(sql_tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        except lzma.LZMAError:
            raise UpdateError(f"{asset}: not a valid xz file") from None
        try:
            con = sqlite3.connect(f"file:{sql_tmp}?mode=ro", uri=True)
            try:
                got = dict(con.execute("SELECT key, value FROM meta"))
            finally:
                con.close()
        except sqlite3.Error as e:
            raise UpdateError(f"{asset}: unpacked file is not a dataset ({e})") from None
        if got.get("part") != name or got.get("format_version") != str(dataset.FORMAT_VERSION):
            raise UpdateError(f"{asset}: unpacked file is not a format {dataset.FORMAT_VERSION} {name} part")
        return sql_tmp
    except BaseException:
        for f in (xz_tmp, sql_tmp):
            if os.path.exists(f):
                os.remove(f)
        raise
    finally:
        if os.path.exists(xz_tmp):
            os.remove(xz_tmp)


def write_excludes(p):
    """The title exclude list from the core part's meta, for report.annotate."""
    con = dataset.open_dataset(p.core)
    try:
        text = dataset.meta(con).get("title_excludes", "")
    finally:
        con.close()
    with open(p.exclude_list + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(p.exclude_list + ".tmp", p.exclude_list)


def have_data(p, st):
    return bool(st.get("build")) and os.path.exists(p.core)


def update(p, force=False, pubkey=keys.DATASET_KEY, base=None):
    """Bring the local dataset up to date. Returns (changed, notes). Raises
    UpdateError only when there is no usable dataset afterwards."""
    os.makedirs(p.data_dir, exist_ok=True)
    st = load_json(p.state, {})
    if not force and have_data(p, st) and st.get("checked_at") \
            and (now() - datetime.fromisoformat(st["checked_at"])).total_seconds() < CHECK_EVERY:
        return False, []
    with open(p.lock, "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | (fcntl.LOCK_NB if have_data(p, st) else 0))
        except BlockingIOError:
            return False, ["another groknmok is updating the dataset; using the current one"]
        st = load_json(p.state, {})  # another process may have just updated it
        return _update_locked(p, st, pubkey, base or dataset_url())


def _update_locked(p, st, pubkey, base):
    have = have_data(p, st)
    since = f"; using the dataset from {age_text(st['build_time'])}" if have else ""

    def keep(why):
        st["checked_at"] = now().isoformat()
        st["last_problem"] = why
        save_json(p.state, st)
        if not have:
            raise UpdateError(f"no dataset yet, and {why}")
        return False, [f"could not update the dataset: {why}{since}"]

    s = session()
    try:
        m = fetch_manifest(s, base, pubkey)
    except UpdateError as e:
        return keep(str(e))
    except requests.RequestException as e:
        return keep(f"network error ({type(e).__name__})")
    why = incompatible(m)
    if why:
        return keep(why)
    if have and m["build"] < st["build"]:  # the release URL lags after a publish
        st["checked_at"] = now().isoformat()
        save_json(p.state, st)
        return False, []
    core_only = settings(p)["core_only"]
    wanted = [x for x in m["parts"] if not (core_only and x["name"] == "catalogue")]
    if not any(x["name"] == "core" for x in wanted):
        return keep("manifest has no core part")
    local = st.get("parts", {})
    todo = [x for x in wanted if (local.get(x["name"]) or {}).get("sha256") != x["sha256"]
            or not os.path.exists(getattr(p, x["name"]))]
    need = sum(x["size"] + x["sqlite_bytes"] for x in todo)
    if todo and shutil.disk_usage(p.data_dir).free < need * 1.1:
        return keep(f"not enough disk space ({need / 1e6:.0f} MB needed)")
    done = {}
    try:
        for x in todo:
            done[x["name"]] = download_part(s, base, x, p.data_dir)
    except UpdateError as e:
        for tmp in done.values():
            os.remove(tmp)
        return keep(str(e))
    except requests.RequestException as e:
        for tmp in done.values():
            os.remove(tmp)
        return keep(f"network error while downloading ({type(e).__name__})")
    for name in ("catalogue", "core"):  # core last: it names the domains
        if name in done:
            os.replace(done[name], getattr(p, name))
    parts = {x["name"]: {k: x[k] for k in ("asset", "sha256", "size", "sqlite_bytes", "rows", "build_time")}
             for x in wanted}
    if core_only and os.path.exists(p.catalogue):
        os.remove(p.catalogue)
    st.update(build=m["build"], build_time=m["build_time"], parts=parts, checked_at=now().isoformat(),
              updated_at=now().isoformat(), notice=m.get("notice", ""), last_problem="")
    if "core" in done or not os.path.exists(p.exclude_list):
        write_excludes(p)
    save_json(p.state, st)
    return bool(done), ([f"dataset updated to build {m['build']} ({', '.join(done)} downloaded)"] if done else [])


def open_local(p):
    """(connection, meta, state, catalogue_used) for the local dataset."""
    st = load_json(p.state, {})
    use_cat = not settings(p)["core_only"] and "catalogue" in st.get("parts", {}) and os.path.exists(p.catalogue)
    conn = dataset.open_dataset(p.core, p.catalogue if use_cat else None)
    return conn, dataset.meta(conn), st, use_cat
