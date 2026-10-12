"""groknmok: search independent hobby stores' published stock, offline.

  groknmok "telemon" [--share [discord|reddit]] [--top [N]] [--country CA|US|all]
                     [--province ON,QC] [--state WA] [--min N] [--max N]
                     [--barcode EAN] [--used] [--all] [--live] [--csv]
  groknmok update [--force]        check for a new dataset now
  groknmok status                  dataset build, age and parts
  groknmok config [core-only on|off] [watch-live on|off]
  groknmok watch add|list|remove|run|test   restock alerts (groknmok watch -h)
  groknmok timer install|remove|status      hourly update and watch check
  groknmok self-update [--check|--rollback] replace this program with the
                                            latest signed release
  groknmok version

Searches read the local copy of the published dataset, which is checked for a
new build at most every 15 minutes (a failed check keeps the current copy).
Results come from the stores' last snapshot and say how old it is. --live
re-checks the matched products on the stores' own sites first: at most 25
products a search and 2 per store, politely (robots.txt, 2 s between requests
to a site, its Crawl-delay, stop on 403 / 429)."""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from datetime import datetime

from groknmok import NOTICE, REPO, __version__, client, dataset, fetch, live, report

LIVE_LIMIT = 25        # products live-confirmed per search
LIVE_PER_DOMAIN = 2    # of them per store domain
UA_TOKEN = "groknmok"  # the robots.txt user-agent token GroknMok obeys
LIVE_UA = f"groknmok/{__version__} (stock search live confirm; +https://github.com/{REPO})"
COMMANDS = ("update", "status", "config", "version", "watch", "timer", "self-update")


class Fetcher(fetch.CrawlDelayFetcher):
    """The client's polite fetcher; its request log goes to Fetcher.hook (None: silent)."""
    hook = None

    def __init__(self):
        super().__init__(LIVE_UA, UA_TOKEN)

    def _log(self, msg):
        if Fetcher.hook:
            Fetcher.hook(msg)


def confirm_domain(d):
    return live.confirm_domain(d, Fetcher)


def fx_of(meta):
    rate = float(meta["fx_usdcad_rate"]) if meta.get("fx_usdcad_rate") else None
    return lambda: (rate, meta.get("fx_usdcad_date") or None)


def note(text):
    print(text, file=sys.stderr, flush=True)


def ensure_dataset(p, force=False):
    """Update if due; notes go to stderr. Exits 1 when there is no dataset at all."""
    try:
        _, notes = client.update(p, force=force)
    except client.UpdateError as e:
        sys.exit(f"groknmok: {e}")
    for n in notes:
        note(n)


def live_confirm(domains, limit, verbose):
    over = live.plan_confirm(domains, limit, per_domain=LIVE_PER_DOMAIN)
    Fetcher.hook = fetch.log if verbose else None
    pool = ThreadPoolExecutor(max_workers=8)
    futs = {pool.submit(confirm_domain, d): name for name, d in domains.items()
            if any(x.get("confirm") for x in d["products"].values())}
    for d in domains.values():
        if not any(x.get("confirm") for x in d["products"].values()):
            d["confirm_note"] = f"not confirmed: --limit {limit} reached"
    unfinished = []
    try:
        for _ in as_completed(futs, timeout=live.CONFIRM_TIMEOUT):
            pass
    except FuturesTimeout:
        unfinished = [futs[f] for f in futs if not f.done()]
        for name in unfinished:
            domains[name]["confirm_note"] = f"not confirmed: still running after {live.CONFIRM_TIMEOUT} s"
    pool.shutdown(wait=False, cancel_futures=True)
    return over, unfinished


def search(argv):
    ap = argparse.ArgumentParser(prog="groknmok", description=__doc__.splitlines()[0],
                                 epilog="other commands: " + ", ".join(COMMANDS))
    ap.add_argument("query")
    ap.add_argument("--min", type=float, dest="lo", help="minimum price")
    ap.add_argument("--max", type=float, dest="hi", help="maximum price")
    ap.add_argument("--used", action="store_true", help="include used stock")
    ap.add_argument("--country", choices=["CA", "US", "all"], default="all")
    ap.add_argument("--province", help="Canadian province code(s), comma-separated")
    ap.add_argument("--state", help="US state code(s), comma-separated (CA = California)")
    ap.add_argument("--share", nargs="?", const="discord", choices=["discord", "reddit"],
                    help="print a Markdown list to paste into Discord (default) or Reddit")
    ap.add_argument("--top", type=int, nargs="?", const=10, metavar="N",
                    help="with --share: every in-stock result plus the N cheapest preorders per country")
    ap.add_argument("--barcode", help="EAN/UPC; a barcode equal to it is a confirmed match")
    ap.add_argument("--all", action="store_true", help="every status, grouped per store")
    ap.add_argument("--live", action="store_true",
                    help=f"re-check matches on the stores' sites (at most {LIVE_LIMIT}, {LIVE_PER_DOMAIN} per store)")
    ap.add_argument("--limit", type=int, default=LIVE_LIMIT, help=f"with --live: products to check (at most {LIVE_LIMIT})")
    ap.add_argument("--csv", action="store_true", help="also write every match to results/<time>-<query>.csv")
    ap.add_argument("--no-exclude", action="store_true", help="ignore the title exclude list")
    ap.add_argument("--links", choices=["auto", "always", "never"], default="auto")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every live request to stderr")
    args = ap.parse_args(argv)
    if args.top is not None and (not args.share or args.top < 0):
        ap.error("--top needs --share and N of 0 or more")
    if not 0 < args.limit <= LIVE_LIMIT:
        ap.error(f"--limit must be 1 to {LIVE_LIMIT}")
    countries, regions = report.store_filter(args)
    links = args.links == "always" or (args.links == "auto" and sys.stdout.isatty())
    p = client.paths()
    ensure_dataset(p)
    conn, meta, st, use_cat = client.open_local(p)
    try:
        domains = dataset.load_matches(conn, args.query, args.used)
        stores, detected = dataset.load_stores(conn)
    finally:
        conn.close()
    report.keep_stores(domains, stores, countries, regions)
    args.exclude_list, args.app = p.exclude_list, "groknmok"
    built = datetime.fromisoformat(st["build_time"]).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    over, unfinished, t0 = 0, [], time.monotonic()
    if args.live and domains:
        over, unfinished = live_confirm(domains, args.limit, args.verbose)
        args.share_basis = "live-confirmed"
    else:
        args.snapshot_ok = True
        args.share_basis = f"from store snapshots (dataset of {built}), not live-confirmed"
    secs = time.monotonic() - t0
    report.annotate(domains, detected, args, fx_of(meta))
    if args.share:
        report.print_share(domains, stores, args)
    elif args.all:
        report.print_all(domains, stores, links, args, secs)
    else:
        report.print_default(domains, stores, links, review_hint="")
    out = sys.stderr if args.share else sys.stdout
    if args.csv:
        path, n = report.write_csv(domains, stores, args)
        print(f"{n} row(s) written to {path}", file=out)
    if not args.share:
        basis = "live-confirmed where marked" if args.live else "snapshot data, not live-confirmed (--live checks now)"
        print(f"Dataset {built} ({client.age_text(st['build_time'])}); {basis}."
              + ("" if use_cat else " Whole-store sitemap catalogues not included (core-only)."))
    if over:
        print(f"--limit {args.limit}: {over} matched product(s) not live-confirmed; narrow the query", file=out)
    sys.stdout.flush()
    if unfinished:
        os._exit(0)  # a confirm thread may still be sleeping on a throttle; do not wait for it
    from groknmok import selfupdate
    selfupdate.interactive_notice(p)  # at most daily, terminal only
    return 0


def cmd_update(argv):
    ap = argparse.ArgumentParser(prog="groknmok update", description="check for a new dataset now")
    ap.add_argument("--force", action="store_true", help="check even if checked in the last 15 minutes")
    ap.parse_args(argv)
    p = client.paths()
    try:
        changed, notes = client.update(p, force=True)
    except client.UpdateError as e:
        sys.exit(f"groknmok: {e}")
    for n in notes:
        note(n)
    st = client.load_json(p.state, {})
    if not changed and not notes:
        print(f"dataset is current: build {st['build']} ({client.age_text(st['build_time'])})")
    return 0


def cmd_status(argv):
    argparse.ArgumentParser(prog="groknmok status", description="dataset build, age and parts").parse_args(argv)
    p = client.paths()
    st = client.load_json(p.state, {})
    if not client.have_data(p, st):
        print("no dataset yet: run `groknmok update`")
        return 1
    cfg = client.settings(p)
    print(f"groknmok {__version__}; data in {p.data_dir}")
    print(f"dataset build {st['build']} ({client.age_text(st['build_time'])}); "
          f"last checked {client.age_text(st['checked_at'])}"
          + (f"; last problem: {st['last_problem']}" if st.get("last_problem") else ""))
    for name, x in st.get("parts", {}).items():
        print(f"  {name:9} {x['rows']:>9,} rows, {x['sqlite_bytes'] / 1e6:6.0f} MB, built {x['build_time']}")
    print(f"core-only {'on' if cfg['core_only'] else 'off'}; watch-live {'on' if cfg['watch_live'] else 'off'}")
    print(st.get("notice") or NOTICE)
    return 0


def cmd_config(argv):
    ap = argparse.ArgumentParser(prog="groknmok config", description="show or change settings")
    ap.add_argument("setting", nargs="?", choices=["core-only", "watch-live"])
    ap.add_argument("value", nargs="?", choices=["on", "off"])
    a = ap.parse_args(argv)
    p = client.paths()
    cfg = client.settings(p)
    if a.setting and a.value:
        cfg[a.setting.replace("-", "_")] = a.value == "on"
        client.save_json(p.settings, cfg)
    elif a.setting:
        ap.error("give on or off")
    print(f"core-only {'on' if cfg['core_only'] else 'off'}   (on: skip the whole-store sitemap catalogues, "
          "about 46 MB a week)")
    print(f"watch-live {'on' if cfg['watch_live'] else 'off'}  (on: watches re-check new hits on the stores' sites)")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] in ("version", "--version"):
        print(f"groknmok {__version__}")
        return 0
    if argv[0] == "update":
        return cmd_update(argv[1:])
    if argv[0] == "status":
        return cmd_status(argv[1:])
    if argv[0] == "config":
        return cmd_config(argv[1:])
    if argv[0] in ("watch", "timer"):
        from groknmok import watches
        return (watches.cmd_watch if argv[0] == "watch" else watches.cmd_timer)(argv[1:])
    if argv[0] == "self-update":
        from groknmok import selfupdate
        return selfupdate.cmd(argv[1:])
    return search(argv)
