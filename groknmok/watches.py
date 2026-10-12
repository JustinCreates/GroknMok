"""groknmok watch ... and groknmok timer ...: restock alerts from the dataset.

  groknmok watch add "telemon" [--min N] [--max N] [--country CA|US|all]
                               [--province ON,QC] [--state WA] [--code EAN]
                               [--note "..."] [--alert-existing]
  groknmok watch list [--all]
  groknmok watch remove <id> [--pause]
  groknmok watch run [--dry-run]       evaluate the watches now
  groknmok watch test                  send one test notification
  groknmok timer install|remove|status a systemd user timer: every hour,
                                       update the dataset and run the
                                       watches when it changed

Watches use the same rules as the pipeline's (groknmok.watch): the first run
of a new watch is a silent baseline; afterwards a listing that turns in stock,
a new preorder, or a price that drops into range sends one ntfy message per
watch. New hits are judged on the dataset alone unless `groknmok config
watch-live on`, which re-checks them on the stores' sites (at most 25
products a run, 2 per store). ntfy settings: ~/.config/groknmok/ntfy.conf,
JSON with server (default https://ntfy.sh), topic and an optional token, mode
0600; the topic and token are never printed."""

import argparse
import os
import shutil
import subprocess
import sys

from groknmok import __version__, client, dataset, notify, report, watch
from groknmok.cli import LIVE_LIMIT, LIVE_PER_DOMAIN, LIVE_UA, Fetcher, confirm_domain, fx_of

UNIT = "groknmok-watch"
PASS_ENV = ("GROKNMOK_CONFIG_DIR", "GROKNMOK_DATA_DIR", "GROKNMOK_DATASET_URL", "GROKNMOK_RELEASES_URL")


class DatasetSource:
    """groknmok.watch's data source for the client: the local dataset."""
    app = "groknmok"
    confirm_cap, confirm_per_domain = LIVE_LIMIT, LIVE_PER_DOMAIN

    def __init__(self, p, live_on):
        self.watch_file, self.state_file, self.config = p.watch_file, p.watch_state, p.ntfy
        self.exclude_list, self.user_agent, self.live = p.exclude_list, LIVE_UA, live_on
        self.conn, self.meta, self.st, _ = client.open_local(p)

    def load_stores(self):
        return dataset.load_stores(self.conn)

    def load_matches(self, query):
        return dataset.load_matches(self.conn, query, False)

    def confirm_domain(self, d):
        return confirm_domain(d)

    def annotate(self, domains, detected, args):
        return report.annotate(domains, detected, args, fx_of(self.meta))

    def count_requests(self, callback):
        Fetcher.hook = callback


def run(p, mode, dry_run=False, if_new=False):
    """Update the dataset (timer: always checks), then evaluate the watches;
    with if_new only when the dataset build changed since the last run."""
    try:
        changed, notes = client.update(p, force=mode == "timer")
    except client.UpdateError as e:
        notify.log(f"watch: {e}")
        return 1
    for n in notes:
        notify.log(n)
    st = client.load_json(p.state, {})
    if if_new and st.get("watched_build") == st.get("build"):
        notify.log(f"watch: dataset build {st.get('build')} already checked; nothing to do")
        return 0
    if mode == "timer":
        from groknmok import selfupdate
        selfupdate.notify_if_available(p)
    src = DatasetSource(p, client.settings(p)["watch_live"])
    try:
        rc = watch.run(src, mode, dry_run)
    finally:
        src.conn.close()
    if not dry_run:
        st = client.load_json(p.state, {})
        st["watched_build"] = src.st.get("build")
        client.save_json(p.state, st)
    return rc


def cmd_watch(argv):
    ap = argparse.ArgumentParser(prog="groknmok watch", description="restock alerts via ntfy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_add = sub.add_parser("add", help="add a watch")
    a_add.add_argument("query")
    a_add.add_argument("--min", type=float)
    a_add.add_argument("--max", type=float)
    a_add.add_argument("--country", choices=["CA", "US", "all"], default="all")
    a_add.add_argument("--province")
    a_add.add_argument("--state")
    a_add.add_argument("--code", help="EAN/UPC or GW catalogue number (99...)")
    a_add.add_argument("--note", default="")
    a_add.add_argument("--alert-existing", action="store_true", help="no silent baseline on the first run")
    a_list = sub.add_parser("list", help="list watches")
    a_list.add_argument("--all", action="store_true", help="include paused watches")
    a_rm = sub.add_parser("remove", help="remove (or pause) a watch")
    a_rm.add_argument("id")
    a_rm.add_argument("--pause", action="store_true", help="set active=no and keep its state")
    a_run = sub.add_parser("run", help="evaluate every active watch now")
    a_run.add_argument("--dry-run", action="store_true", help="print alerts; send nothing, save no state")
    a_run.add_argument("--mode", default="manual", help=argparse.SUPPRESS)
    a_run.add_argument("--if-new", action="store_true", help="only when the dataset changed since the last run")
    sub.add_parser("test", help="send one test notification")
    a = ap.parse_args(argv)
    p = client.paths()
    os.makedirs(p.config_dir, exist_ok=True)
    os.makedirs(p.data_dir, exist_ok=True)
    if a.cmd == "add":
        print(watch.add_watch(p.watch_file, p.watch_state, a.query, a.min, a.max, a.country, a.province, a.state,
                              a.code, a.note, a.alert_existing))
        return 0
    if a.cmd == "list":
        lines = watch.watch_lines(p.watch_file, p.watch_state, a.all)
        print("\n".join(lines) if lines else "no watches yet: groknmok watch add \"<query>\"")
        return 0
    if a.cmd == "remove":
        if not watch.remove_watch(p.watch_file, p.watch_state, a.id, a.pause):
            sys.exit(f"no watch {a.id}")
        return 0
    if a.cmd == "run":
        return run(p, a.mode, a.dry_run, a.if_new)
    cfg = notify.load_config(p.ntfy)
    if cfg is None:
        print(f"no usable ntfy config at {p.ntfy} (JSON: server, topic, optional token; mode 0600)")
        return 1
    ok = notify.send(cfg, "groknmok: test notification", "If you can read this, restock alerts will reach you.",
                     tags="white_check_mark", user_agent=LIVE_UA)
    print("sent" if ok else "not sent (see the message above)")
    return 0 if ok else 1


def unit_dir():
    return os.environ.get("GROKNMOK_UNIT_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "systemd", "user")


def program_command():
    """(argv, environment) that start this program from a systemd unit."""
    from groknmok import selfupdate
    archive = selfupdate.program_path()
    py = (shutil.which("python3") or sys.executable) if archive else sys.executable  # source: its own venv
    env = {k: os.environ[k] for k in PASS_ENV if os.environ.get(k)}
    if archive:
        return [py, archive], env
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [py, "-m", "groknmok"], {**env, "PYTHONPATH": src}


def quote(arg):
    return arg if arg and all(c.isalnum() or c in "/._-+=:,@" for c in arg) else '"' + arg.replace('"', '\\"') + '"'


def units():
    cmd, env = program_command()
    service = "\n".join([
        "[Unit]",
        "Description=GroknMok: update the dataset and check restock watches",
        "",
        "[Service]",
        "Type=oneshot",
        "ExecStart=" + " ".join(quote(x) for x in cmd + ["watch", "run", "--mode", "timer", "--if-new"]),
        *[f"Environment={quote(f'{k}={v}')}" for k, v in sorted(env.items())],
        "Nice=10",
        "TimeoutStartSec=15min",
        ""])
    timer = "\n".join([
        "[Unit]",
        "Description=GroknMok: hourly dataset update and watch check",
        "",
        "[Timer]",
        "OnBootSec=10min",
        "OnUnitActiveSec=1h",
        "RandomizedDelaySec=10min",
        "",
        "[Install]",
        "WantedBy=timers.target",
        ""])
    return {f"{UNIT}.service": service, f"{UNIT}.timer": timer}


def systemctl(*args):
    if not shutil.which("systemctl"):
        print("systemctl not found: start the timer yourself", file=sys.stderr)
        return 1
    return subprocess.call(["systemctl", "--user", *args])


def cmd_timer(argv):
    ap = argparse.ArgumentParser(prog="groknmok timer", description="hourly dataset update and watch check")
    ap.add_argument("action", choices=["install", "remove", "status"])
    a = ap.parse_args(argv)
    d = unit_dir()
    if a.action == "install":
        os.makedirs(d, exist_ok=True)
        for name, text in units().items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"wrote {os.path.join(d, name)}")
        if os.environ.get("GROKNMOK_UNIT_DIR"):
            print("GROKNMOK_UNIT_DIR is set: units written, not enabled")
            return 0
        rc = systemctl("daemon-reload") or systemctl("enable", "--now", f"{UNIT}.timer")
        if rc == 0:
            print(f"timer on: every hour (log: journalctl --user -u {UNIT}.service)")
        return rc
    if a.action == "remove":
        systemctl("disable", "--now", f"{UNIT}.timer")
        for name in units():
            path = os.path.join(d, name)
            if os.path.exists(path):
                os.remove(path)
                print(f"removed {path}")
        return systemctl("daemon-reload")
    return systemctl("list-timers", f"{UNIT}.timer", "--no-pager")
