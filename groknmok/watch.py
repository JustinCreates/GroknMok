"""Restock watches, shared by the 40k-stockcheck pipeline and the GroknMok client.

A run evaluates every active watch offline against the source's data, compares
the hits with the previous run's (the state file), live-confirms only new hits
(at most MAX_CONFIRM_RUN products a run), judges them like search output, and
sends one ntfy message per watch: newly in stock, a new preorder listing, or a
price that dropped into range. The same (store, product, status) never alerts
twice in a row; a run that sees it in another status, or gone, re-arms it. A
new watch's first run is a silent baseline. The data source (see run) is the
only part that differs between the pipeline and the client.

Moved verbatim from the pipeline's watch.py (2026-10-11); its data access now
goes through the source object."""

import csv
import json
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from datetime import datetime, timezone
from types import SimpleNamespace

from groknmok.live import CONFIRM_TIMEOUT, plan_confirm
from groknmok.notify import load_config, log, send
from groknmok.report import cad_text, display_name, keep_stores, pickup, store_lines, store_name
from groknmok.rules import excluded_by, load_title_excludes, show_title, to_ascii, verdict

WATCH_FIELDS = ["id", "query", "min", "max", "country", "region", "code", "active", "note", "created"]
MAX_CONFIRM_RUN = 40      # products live-confirmed per run, all watches together
NTFY_LIMIT = 3900         # bytes of message body (ntfy's limit is 4,096)
HIT_STATUSES = ("in_stock", "preorder")


def load_watches(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return [{k: (r.get(k) or "").strip() for k in WATCH_FIELDS} for r in csv.DictReader(fh)]


def save_watches(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="ascii") as fh:
        w = csv.DictWriter(fh, fieldnames=WATCH_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: to_ascii(r.get(k, "")) for k in WATCH_FIELDS})
    os.replace(tmp, path)


def regions_of(w):
    """(countries, regions) for a watch, in report.store_filter's form."""
    regions = {tuple(x.split("-", 1)) for x in w["region"].split(",") if "-" in x}
    country = w["country"] or "all"
    countries = {c for c, _ in regions} or ({"CA", "US"} if country == "all" else {country})
    return countries, regions


def load_state(path):
    try:
        with open(path, encoding="ascii") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"version": 1, "watches": {}}


def save_state(path, state):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".watch_state.")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def key_of(d, p, v):
    return f"{d['domain']}|{p['key']}|{v['key']}"


def in_range(price, w):
    lo, hi = (float(w["min"]) if w["min"] else None), (float(w["max"]) if w["max"] else None)
    if lo is None and hi is None:
        return True
    return price is not None and (lo is None or price >= lo) and (hi is None or price <= hi)


def code_ok(v, w):
    if not w["code"]:
        return True
    code = re.sub(r"\D", "", w["code"])
    return code in (re.sub(r"\D", "", v.get("barcode") or ""), v.get("catalogue") or "")


def offline(w, src, stores, excludes):
    """(domains, matches) for one watch from the snapshot data: matches is
    {key: [offline status, snapshot price]} for every matching variant."""
    domains = src.load_matches(w["query"])
    keep_stores(domains, stores, *regions_of(w))
    matches = {}
    for d in domains.values():
        for p in d["products"].values():
            for v in p["variants"]:
                full = p["title"] + (f" {v['title']}" if v["title"] else "")
                if not code_ok(v, w) or excluded_by(full, excludes):
                    continue
                matches[key_of(d, p, v)] = [verdict(v, full)[0], v["price"]]
    return domains, matches


def hits_of(matches, w):
    return {k: m for k, m in matches.items() if m[0] in HIT_STATUSES and in_range(m[1], w)}


def run(src, mode, dry_run=False):
    """Evaluate every active watch (see the module docstring); src is the data source:
    watch_file, state_file, config, exclude_list, user_agent, app, load_stores(),
    load_matches(query), confirm_domain(d), annotate(domains, detected, args) and
    count_requests(callback)."""
    started = time.monotonic()
    watches = [w for w in load_watches(src.watch_file) if w["active"] != "no"]
    if not watches:
        log("watch: no active watches")
        return 0
    state = load_state(src.state_file)
    cfg = None if dry_run else load_config(src.config)
    stores, detected = src.load_stores()
    excludes = load_title_excludes(src.exclude_list)
    counter = {"GET": 0}
    lock = threading.Lock()

    def count(msg):
        if msg.startswith("GET "):
            with lock:
                counter["GET"] += 1
    src.count_requests(count)  # every request through the polite fetcher is logged as "GET ..."

    plans = []  # (watch, domains, matches, hits, new keys)
    for w in watches:
        ws = state["watches"].get(w["id"])
        domains, matches = offline(w, src, stores, excludes)
        hits = hits_of(matches, w)
        if ws is None:  # first run: silent baseline
            state["watches"][w["id"]] = {"hits": hits, "seen": matches, "alerted": {},
                                         "last_run": now(), "last_mode": mode}
            log(f"watch {w['id']} '{w['query']}': baseline, {len(hits)} hit(s) of {len(matches)} match(es)")
            continue
        prev = ws.get("hits", {})
        new = [k for k, h in hits.items() if k not in prev or prev[k][0] != h[0]]
        for k, st in list(ws.get("alerted", {}).items()):  # re-arm: seen in another status, or gone
            if k not in hits or hits[k][0] != st:
                del ws["alerted"][k]
        plans.append((w, domains, matches, hits, new))

    # Live-confirm the new hits only, all watches together, at most MAX_CONFIRM_RUN products.
    todo = {}  # domain -> reduced domain dict holding only products with new hits
    for w, domains, matches, hits, new in plans:
        newset = set(new)
        for name, d in domains.items():
            for pk, p in d["products"].items():
                if any(key_of(d, p, v) in newset for v in p["variants"]):
                    rd = todo.setdefault(name, dict(d, products={}))
                    rd["products"][pk] = p
    plan_confirm(todo, MAX_CONFIRM_RUN)
    timed_out, confirmed = False, 0
    if todo:
        pool = ThreadPoolExecutor(max_workers=16)
        futs = [pool.submit(src.confirm_domain, d) for d in todo.values()
                if any(p.get("confirm") for p in d["products"].values())]
        confirmed = len(futs)
        try:
            for _ in as_completed(futs, timeout=CONFIRM_TIMEOUT):
                pass
        except FuturesTimeout:
            log("watch: some live confirms still running; their hits stay new for the next run")
            timed_out = True
        pool.shutdown(wait=False, cancel_futures=True)
        ann = SimpleNamespace(no_exclude=False, exclude_list=src.exclude_list, barcode=None, query="",
                              lo=None, hi=None, all=False)
        src.annotate(todo, detected, ann)

    total_alerts = 0
    for w, domains, matches, hits, new in plans:
        ws = state["watches"][w["id"]]
        prev_seen = ws.get("seen", {})
        alerts, unconfirmed = [], set()
        for k in new:
            dom, pk, vk = k.split("|", 2)
            d = todo.get(dom)
            p = (d or {}).get("products", {}).get(pk)
            v = next((x for x in (p or {}).get("variants", []) if x["key"] == vk), None)
            if v is None or v.get("status_source") != "live":
                unconfirmed.add(k)  # over the cap, or the confirm failed: new again next run
                continue
            if not (v["shown"] and v["verdict"] in HIT_STATUSES and in_range(v["price"], w)):
                continue  # live says otherwise; recorded as seen, not re-confirmed until it changes
            if ws["alerted"].get(k) == v["verdict"]:
                continue
            was = prev_seen.get(k)  # last run's offline status and price for this listing
            event = ("price into range" if was and was[0] == v["verdict"] and not in_range(was[1], w) else
                     "new preorder" if v["verdict"] == "preorder" else "newly in stock")
            alerts.append((event, d, p, v, k))
        sent = True
        if alerts:
            title, body, click = message(w, alerts, stores, src.app)
            total_alerts += len(alerts)
            if dry_run:
                print(f"--- {title}\n{body}\n")
            else:
                sent = send(cfg, title, body, click=click, user_agent=src.user_agent)
        if not dry_run:
            if sent:
                for event, d, p, v, k in alerts:
                    ws["alerted"][k] = v["verdict"]
            else:
                unconfirmed |= {k for *_, k in alerts}  # retried next run
            ws["hits"] = {k: h for k, h in hits.items() if k not in unconfirmed}
            # Unconfirmed or unsent listings keep last run's view, so next run judges them afresh.
            ws["seen"] = {k: (prev_seen[k] if k in unconfirmed else m) for k, m in matches.items()
                          if k not in unconfirmed or k in prev_seen}
            ws["last_run"], ws["last_mode"] = now(), mode
            if alerts and sent:
                ws["last_alert"] = now()
        log(f"watch {w['id']} '{w['query']}': {len(matches)} match(es), {len(hits)} hit(s), {len(new)} new, "
            f"{len(alerts)} alert(s){'' if sent else ' (send failed; retried next run)'}"
            f"{f', {len(unconfirmed)} left for next run' if unconfirmed else ''}")
    if not dry_run:
        save_state(src.state_file, state)
    log(f"watch run ({mode}): {len(watches)} watch(es), {confirmed} domain(s) confirmed, "
        f"{counter['GET']} request(s), {total_alerts} alert(s), {time.monotonic() - started:.1f} s")
    if timed_out:
        sys.stdout.flush()
        os._exit(0)  # a confirm thread may still be sleeping on a throttle; do not wait for it
    return 0


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def message(w, alerts, stores, app="stockcheck"):
    """(title, body, click URL) for one watch's alerts, under NTFY_LIMIT bytes."""
    lines = []
    for event, d, p, v, k in sorted(alerts, key=lambda a: (a[0] != "newly in stock",
                                                           a[3]["price"] if a[3]["price"] is not None else 1e9)):
        sts = store_lines(d, stores)
        st = sts[0] if sts else {"name": d["domain"], "city": "", "province": ""}
        where = f"{store_name(st)}, {display_name(st['city'])} {st['province']}{pickup(st)}"
        price = ("price ?" if v["price"] is None else f"${v['price']:.2f} {d['currency']}" + cad_text(v, d))
        if v["verdict"] == "preorder":
            rel = v.get("inferred_release") or p.get("release")
            status = f"preorder, release {rel}" if rel else "preorder, release date unknown"
        else:
            status = ("in stock" if not v.get("release_note")
                      else f"in stock per store; other stores list release {v['release_note']}")
        lines.append(f"{where}: {price} - {status} ({event}) - {show_title(p, v)} - {p['url']}")
    body, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > NTFY_LIMIT - 60:
            body.append(f"+{len(lines) - i} more (see {app} \"{w['query']}\")")
            break
        body.append(line)
        used += len(line) + 1
    return f"{app}: {w['query']} ({len(alerts)})", "\n".join(body), alerts[0][2]["url"]
