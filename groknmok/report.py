"""Search output, shared by the 40k-stockcheck pipeline and the GroknMok client:
store names and locations, currency and the CAD approximation, the post-confirm
annotation (annotate), the default, --all and --share outputs, and the results
CSV. It works on the matches structure both sides build (domains -> products ->
variants), whatever the data source.

Moved verbatim from the pipeline's search.py (2026-10-11); annotate now takes the
USD/CAD rate source (fx_rate) as an argument."""

import csv
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

from groknmok.rules import (
    canadian_page_usd, clean, display_title, code_like, currency_mismatch, excluded_by, in_range, infer_preorders,
    load_title_excludes, note_release, resolve_releases, show_title, split_codes, to_ascii, verdict, words)
from groknmok.live import rank

DEFAULT_LIMIT = 300
COUNTRY_NAMES = {"CA": "Canada", "US": "United States"}
COUNTRY_CURRENCY = {"CA": "CAD", "US": "USD"}
PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
STATES = {"AK", "AL", "AR", "AZ", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI", "IA", "ID", "IL", "IN",
          "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ",
          "NM", "NV", "NY", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA",
          "WI", "WV", "WY", "PR", "GU", "VI", "AS", "MP"}
NO_STOCK = "No stock found anywhere, oof!"
SHARE_LIMIT = {"discord": 2000, "reddit": 10000}  # characters per message / comment
SHARE_FOOTER = "Matched by product title; barcode confirmed where marked. Always check the store page before buying."
CSV_FIELDS = ["searched_at", "query", "min_price", "max_price", "domain", "platform", "stores", "ships",
              "title", "display_title", "variant", "sku", "barcode", "catalogue", "price", "country", "currency",
              "currency_source", "cad_approx",
              "status", "status_source", "release", "release_source",
              "confirmed_at", "snapshot_finished_at", "snapshot_age_hours", "match", "used", "url",
              "shown", "hidden_reason", "verdict_reason"]
LEGAL_SUFFIX = re.compile(r"(?:[\s,]+(?:inc|llc|l\.l\.c|ltd|lt[e\u00e9]e|corp)\.?(?:\s+\d+)?)+$", re.I)  # "Inc 1" too
CITY_KEEP_AFTER = {"of", "in", "at", "on", "de", "du", "des", "d", "la", "le", "les", "the", "and", "en"}


def age_text(finished):
    secs = (datetime.now(timezone.utc) - datetime.fromisoformat(finished)).total_seconds()
    return f"{secs / 3600:.0f}h" if secs < 172800 else f"{secs / 86400:.0f}d"


def link(url, text, on):
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\" if on and url else text


def store_lines(d, stores):
    return d.get("shown_stores") or [stores[i] for i in d["finder_ids"] if i in stores]


def same_site(a, b):
    """True when two website URLs name the same site: scheme, "www.", case, a
    trailing slash, query and fragment ignored."""
    def key(u):
        u = (u or "").strip()
        p = urlsplit(u if "//" in u else "//" + u)
        return p.netloc.lower().removeprefix("www.") + p.path.rstrip("/").lower()
    return bool((a or "").strip()) and key(a) == key(b)


def store_name(st):
    """Store name for the terminal and --share: the accepted Google listing name
    when there is one (st["google_name"]), else the finder name; either way legal
    suffixes (Inc, LLC, Ltd, Ltee, Corp, also followed by a number: "Inc 1"), a
    " - <location>" suffix that repeats the city, and trailing words equal to the
    city ("Astral Games Medford") are dropped, and all-capitals names are
    title-cased. A city after "of", "de" and the like stays ("Games of
    Concord"). Data files keep the finder names."""
    name = display_name((st.get("google_name") or st["name"]).strip())  # case decided on the whole name
    city = words(st.get("city"))
    m = re.search(r"\s+-\s+([^-]+)$", name)
    if m and words(m.group(1)) == city:
        name = name[:m.start()]
    name = LEGAL_SUFFIX.sub("", name).strip(" ,-") or name
    toks = name.split()
    if city and len(toks) > len(city) and words(" ".join(toks[-len(city):])) == city:
        head = " ".join(toks[:-len(city)]).rstrip(" ,-")
        if head.count("(") > head.count(")"):  # "Backstage Hobbies & Games (Downtown Ludington)"
            head = head[:head.rindex("(")].rstrip(" ,-")
        # A one-word remainder only when it reads as a brand ("HobbyTown", "TGS"), so a
        # city that is part of the name stays ("Jumping Jasper", "9311-9501 Quebec").
        one = len(head.split()) == 1
        if (re.search(r"[A-Za-z]", head) and words(head)[-1] not in CITY_KEEP_AFTER
                and (not one or re.search(r".[A-Z]", head))):
            name = head
    return name


def display_name(text):
    """All-capitals finder names read as shouting in a post; title-case those only."""
    if not text or not text.isupper():
        return text or ""
    # Capitalise after a space, hyphen, slash or bracket only; str.title() would give "Bootown'S".
    return re.sub(r"(^|[\s\-/(])([a-z])", lambda m: m.group(1) + m.group(2).upper(), text.lower())


def ships_text(sts):
    """CSV ships value: yes / no / unknown, or one per store ("no; unknown") when they differ."""
    vals = [st.get("ships") or "unknown" for st in sts]
    return vals[0] if len(set(vals)) == 1 else "; ".join(vals)


def pickup(st):
    """' (local pickup only)' for a store whose override says ships=no."""
    return " (local pickup only)" if st.get("ships") == "no" else ""


def md(text):
    """Escape Markdown that Discord and Reddit would otherwise format."""
    return re.sub(r"([\\`*_~|\[\]])", r"\\\1", text or "")


def store_filter(args):
    """(countries, regions) to keep, from --country / --province / --state; validated."""
    def codes(text, valid, flag):
        got = {c.strip().upper() for c in (text or "").split(",") if c.strip()}
        bad = got - valid
        if bad:
            sys.exit(f"{flag}: unknown code(s) {', '.join(sorted(bad))}")
        return got
    provinces = codes(args.province, PROVINCES, "--province")
    states = codes(args.state, STATES, "--state")
    if (provinces and args.country == "US") or (states and args.country == "CA"):
        sys.exit("--province is for Canada and --state for the US; they conflict with --country")
    regions = {("CA", p) for p in provinces} | {("US", s) for s in states}
    countries = {c for c, _ in regions} or ({"CA", "US"} if args.country == "all" else {args.country})
    return countries, regions


def keep_stores(domains, stores, countries, regions):
    """Drop domains with no store in the countries / regions; on the rest set
    shown_stores, country and override_currency. Mutates domains."""
    for name, d in list(domains.items()):
        shown = [stores[i] for i in d["finder_ids"] if i in stores
                 and (stores[i].get("country") or "CA") in countries
                 and (not regions or ((stores[i].get("country") or "CA"), stores[i]["province"]) in regions)]
        if not shown:
            del domains[name]
            continue
        d["shown_stores"], d["country"] = shown, shown[0].get("country") or "CA"
        d["override_currency"] = next((stores[i]["override_currency"] for i in d["finder_ids"]
                                       if (stores.get(i) or {}).get("override_currency")), "")


def resolve_currency(d, detected):
    """Live response first, then the snapshot's weekly check, then detect_platform,
    else the store country's currency (assumed). A detected currency that differs
    from the store's country is kept and flagged (d["currency_mismatch"])."""
    det = next((detected[i]["currency"] for i in d["finder_ids"] if (detected.get(i) or {}).get("currency")), "")
    live = canadian_page_usd(d.get("live_currency"), d.get("live_currency_src"), [d["country"]])
    d["currency_mismatch"] = False
    if d.get("override_currency"):  # confirmed by hand in overrides.csv
        d["currency"], d["currency_source"] = d["override_currency"], "override: currency confirmed"
        return
    for code, src in ((live[0], "live: " + str(live[1])), (d["db_currency"], f"snapshot: {d['db_currency_source']}"),
                      (det, "detect_platform")):
        if code:
            d["currency"], d["currency_source"] = code, src
            d["currency_mismatch"] = currency_mismatch(code, src, [d["country"]])
            return
    d["currency"], d["currency_source"] = COUNTRY_CURRENCY.get(d["country"], "?"), "assumed"


def fx_line(fx):
    return f"USD/CAD {fx[0]:.4f} (Bank of Canada daily average, {fx[1]})"


def cad_approx(v, d):
    """CAD amount for the CSV: the CAD price, or the USD price at the day's rate."""
    if v["price"] is None:
        return None
    if d["currency"] == "CAD":
        return v["price"]
    return round(v["price"] * d["fx"][0], 2) if d["currency"] == "USD" and d.get("fx") else None


def cad_text(v, d):
    """' (~$136 CAD)' after a USD price when the day's rate is known."""
    if d["currency"] != "USD" or not d.get("fx") or v["price"] is None:
        return ""
    return f" (~${v['price'] * d['fx'][0]:,.0f} CAD)"


def money(v, d):
    if v["price"] is None:
        return "?"
    return (f"${v['price']:.2f} {d['currency']}" + ("*" if "assumed" in d["currency_source"] else "")
            + ("!" if d.get("currency_mismatch") else "") + cad_text(v, d))


def confirmed_text(d):
    confirmed = max((v.get("confirmed_at") or "" for p in d["products"].values() for v in p["variants"]),
                    default="")
    return (f"confirmed {datetime.fromisoformat(confirmed).astimezone().strftime('%H:%M')}"
            if confirmed else "not confirmed live")


def annotate(domains, detected, args, fx_rate):
    """After live confirm: currency, barcode / catalogue match, price range, title
    exclude list, display verdict, shown / hidden reason, release dates, inferred
    preorders and the in-stock-before-release note, for every variant. Shared by
    search output and watch.py. args needs no_exclude, exclude_list, barcode,
    query, lo, hi and all. fx_rate() returns (USD/CAD rate, its date) or (None, None);
    it is called only when a USD price is present."""
    excludes = [] if args.no_exclude else load_title_excludes(args.exclude_list)
    want_code = re.sub(r"\D", "", args.barcode or (args.query if code_like(args.query) else ""))
    for d in domains.values():
        resolve_currency(d, detected)
    fx = fx_rate() if any(d["currency"] == "USD" for d in domains.values()) else (None, None)
    for d in domains.values():
        d["fx"] = fx if d["currency"] == "USD" and fx[0] else None
        for p in d["products"].values():
            for v in p["variants"]:
                live_bc, live_cat = split_codes(v.get("barcode"), v.get("sku"))  # a 99... "barcode" is
                v["barcode"], v["catalogue"] = live_bc, live_cat or v.get("catalogue")  # a catalogue number
                if want_code and v["barcode"] and re.sub(r"\D", "", v["barcode"]) == want_code:
                    v["match"] = "barcode match"
                elif want_code and v.get("catalogue") == want_code:
                    v["match"] = "catalogue number match"
                v["in_range"], reason = in_range(v, args.lo, args.hi)
                full_title = p["title"] + (f" {v['title']}" if v["title"] else "")
                term = excluded_by(full_title, excludes)
                v["listed"] = v["in_range"] and term is None  # eligible for any output, incl. --all
                v["verdict"], v["verdict_reason"] = verdict(v, full_title)
                live_ok = v["verdict"] in ("in_stock", "preorder") and v["status_source"] == "live"
                v["shown"] = v["listed"] and (args.all or live_ok)
                v["hidden_reason"] = (f"title excludes '{term}'" if term else reason) or ("" if v["shown"] else
                                                f"verdict {v['verdict_reason']}" if v["verdict_reason"]
                                                else "not in stock" if v["status"] != "in_stock"
                                                else "not confirmed live")
    resolve_releases(domains)
    infer_preorders(domains)
    note_release(domains)


def print_default(domains, stores, links):
    """Default output: live-confirmed in-stock results, then a Preorders section,
    each cheapest first and grouped per domain; one section per country when
    results come from more than one. Nothing else."""
    def blocks(group, kind):
        out = []
        for d in group:
            items = [(p, v) for p in d["products"].values() for v in p["variants"]
                     if v["shown"] and v["verdict"] == kind]
            if items:
                items.sort(key=lambda pv: pv[1]["price"] if pv[1]["price"] is not None else float("inf"))
                first = items[0][1]["price"]
                foreign = d["currency"] != COUNTRY_CURRENCY.get(d["country"])  # own currency first
                out.append(((foreign, d["currency"], first if first is not None else float("inf")), d, items))
        return sorted(out, key=lambda b: (b[0], b[1]["domain"]))

    def show(bs, label):
        for _, d, items in bs:
            for p, v in items:
                price = money(v, d)
                title = show_title(p, v)
                flag = "  [title match, low confidence]" if v["match"].startswith("title") else ""
                tag = label
                if v.get("release_note"):
                    tag += f" (per store; other stores list release {v['release_note']})"
                if v.get("inferred_release"):
                    tag += f" (inferred, release {v['inferred_release']})"
                elif label == "PRE-ORDER":
                    rdate = p.get("release")
                    if not rdate:
                        tag += " (release date unknown)"
                    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", rdate):
                        tag += f" (release {rdate})"
                    else:
                        tag += f" (release {rdate}, as written)"  # ambiguous day/month
                print(f"{price:>13}  {link(p['url'], title, links)}  {tag}{flag}")
            print(f"               {link(d['base_url'], d['domain'], links)}  "
                  f"{confirmed_text(d)}, snapshot {age_text(d['finished_at'])} old")
            for st in store_lines(d, stores):
                print(f"               - {store_name(st)}, {st['city']} {st['province']}{pickup(st)}")
            print()

    groups = {}
    for d in domains.values():
        groups.setdefault(d["country"], []).append(d)
    sections = [(c, blocks(g, "in_stock"), blocks(g, "preorder"))
                for c, g in sorted(groups.items(), key=lambda kv: (kv[0] != "CA", kv[0]))]
    sections = [s for s in sections if s[1] or s[2]]
    if not sections:
        print(NO_STOCK)
        return
    for c, in_stock, preorders in sections:
        if len(sections) > 1:
            print(f"== {COUNTRY_NAMES.get(c, c)} ==")
            print()
        show(in_stock, "IN STOCK!")
        if preorders:
            print("Preorders")
            print()
            show(preorders, "PRE-ORDER")
    if any("assumed" in b[1]["currency_source"] for _, ins, pre in sections for b in ins + pre):
        print("* currency assumed: not detected for that site yet")
    if any(b[1].get("currency_mismatch") for _, ins, pre in sections for b in ins + pre):
        print("! site currency differs from the store's country: possible wrong match (stockcheck --currency-review)")
    fx = next((b[1]["fx"] for _, ins, pre in sections for b in ins + pre if b[1].get("fx")), None)
    if fx:
        print(fx_line(fx))


def print_all(domains, stores, links, args, secs):
    """--all: every status, grouped per domain (debugging)."""
    rng = ("" if args.lo is None and args.hi is None
           else f"  live price {args.lo if args.lo is not None else 0:.2f}-"
                f"{args.hi:.2f}" if args.hi is not None else f"  live price >= {args.lo:.2f}")
    kept = {k: d for k, d in domains.items()
            if any(v["listed"] for p in d["products"].values() for v in p["variants"])}
    n_var = sum(1 for d in kept.values() for p in d["products"].values() for v in p["variants"] if v["listed"])
    live = sum(1 for d in kept.values() for p in d["products"].values() for v in p["variants"]
               if v["listed"] and v["status_source"] == "live")
    print(f'stockcheck "{args.query}"{rng}  ({"used included" if args.used else "used hidden"})')
    fx = next((d["fx"] for d in kept.values() if d.get("fx")), None)
    if fx:
        print(fx_line(fx))
    print(f"{n_var} match(es) at {len(kept)} domain(s); {live} confirmed live in {secs:.1f} s\n")

    def best(d):
        return min(rank(v) for p in d["products"].values() for v in p["variants"] if v["listed"])

    for d in sorted(kept.values(), key=lambda d: (best(d), d["domain"])):
        print(f"{link(d['base_url'], d['domain'], links)}  [{d['platform']}]  "
              f"snapshot {age_text(d['finished_at'])} old, {confirmed_text(d)}"
              + (f"  (snapshot {d['snap_status']})" if d["snap_status"] != "ok" else ""))
        if d.get("confirm_note"):
            print(f"    note: {d['confirm_note']}")
        items = [(p, v) for p in d["products"].values() for v in p["variants"] if v["listed"]]
        for p, v in sorted(items, key=lambda pv: (rank(pv[1]), pv[1]["price"] or 0)):
            price = money(v, d) if v["price"] is not None else "   ?   "
            title = show_title(p, v)
            src = "" if v["status_source"] == "live" else f"  [{v['status_source']}]"
            flags = ("  [used]" if p["used"] == 1 else "") + ("  [condition unknown]" if p["used"] is None else "")
            flags += f"  [verdict {v['verdict_reason']}]" if v["verdict_reason"] else ""
            print(f"    {v['status']:<12} {price:>13}  {link(p['url'], title, links)}"
                  f"  [{v['match']}]{flags}{src}")
        for st in store_lines(d, stores):
            print(f"      - {store_name(st)}, {st['city']} {st['province']}{pickup(st)}")
        print()


def write_csv(domains, stores, args):
    """Every match, shown or not, with its full status."""
    searched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for d in domains.values():
        names = store_lines(d, stores)
        for p in d["products"].values():
            for v in p["variants"]:
                rows.append({
                    "searched_at": searched_at, "query": args.query, "min_price": args.lo,
                    "max_price": args.hi, "domain": d["domain"], "platform": d["platform"],
                    "stores": "; ".join(f"{s['name']} ({s['city']}, {s['province']})" for s in names),
                    "ships": ships_text(names),
                    "title": p["title"], "display_title": display_title(p), "variant": v["title"],
                    "sku": v["sku"], "barcode": v["barcode"], "catalogue": v.get("catalogue"),
                    "price": v["price"], "country": d["country"], "currency": d["currency"],
                    "currency_source": d["currency_source"], "cad_approx": cad_approx(v, d),
                    "status": v["status"], "status_source": v["status_source"],
                    "release": p.get("release"), "release_source": p.get("release_source"),
                    "confirmed_at": v.get("confirmed_at"), "snapshot_finished_at": d["finished_at"],
                    "snapshot_age_hours": round((datetime.now(timezone.utc)
                                                 - datetime.fromisoformat(d["finished_at"])).total_seconds()
                                                / 3600, 1),
                    "match": v["match"], "used": {1: "yes", 0: "no", None: "unknown"}[p["used"]],
                    "url": p["url"], "shown": "yes" if v["shown"] else "no", "hidden_reason": v["hidden_reason"],
                    "verdict_reason": v["verdict_reason"]})
    os.makedirs("results", exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", to_ascii(args.query).lower()).strip("-")[:40] or "query"
    stem = f"results/{datetime.now().strftime('%Y-%m-%d-%H%M')}-{slug}"
    path, n = f"{stem}.csv", 2
    while os.path.exists(path):  # same query twice in one minute
        path, n = f"{stem}-{n}.csv", n + 1
    with open(path, "x", newline="", encoding="ascii") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: to_ascii("" if r[k] is None else r[k]) for k in CSV_FIELDS})
    return path, len(rows)


def share_lines(domains, stores, fmt, top=None):
    """Markdown list lines, in stock before preorders, cheapest first; one bold
    heading per country when results come from more than one. top: every
    in-stock line, but only the top cheapest preorders per country, then one
    line saying how many were left out."""
    rows = []
    for d in domains.values():
        sts = store_lines(d, stores)
        where = ""
        if sts:
            where = (f"{store_name(sts[0])}, {display_name(sts[0]['city'])} {sts[0]['province']}"
                     + pickup(sts[0]))
            if len(sts) > 1:
                where += f" (+{len(sts) - 1} more location{'s' if len(sts) > 2 else ''})"
        for p in d["products"].values():
            for v in p["variants"]:
                if not (v["shown"] and v["verdict"] in ("in_stock", "preorder")):
                    continue
                title = show_title(p, v)
                url = f"<{p['url']}>" if fmt == "discord" else p["url"]
                price = ("price ?" if v["price"] is None else f"${v['price']:.2f} {d['currency']}"
                         + (" (assumed)" if "assumed" in d["currency_source"] else "") + cad_text(v, d)
                         + (" (site currency differs from store country)" if d.get("currency_mismatch") else ""))
                if v["verdict"] == "in_stock":
                    status = ("in stock" if not v.get("release_note")
                              else f"in stock per store; other stores list release {v['release_note']}")
                elif v.get("inferred_release"):
                    status = f"preorder (inferred, release {v['inferred_release']})"
                else:
                    rdate = p.get("release")
                    status = ("preorder, release date unknown" if not rdate
                              else f"preorder, release {rdate}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", rdate)
                              else f"preorder, release {rdate} (as written)")
                flag = {"barcode match": " (barcode confirmed)",
                        "catalogue number match": " (catalogue number confirmed)"}.get(v["match"], "")
                rows.append(((d["country"] != "CA", d["country"], v["verdict"] != "in_stock",
                              d["currency"] != COUNTRY_CURRENCY.get(d["country"]), d["currency"],
                              v["price"] if v["price"] is not None else float("inf"), d["domain"]),
                             d["country"],
                             f"- [{md(title)}]({url}) - {md(where)} - {price} - {status}{flag}"))
    rows.sort(key=lambda r: r[0])
    countries = {r[1] for r in rows}
    out, last, kept, left = [], None, {}, {}
    for key, country, line in rows:
        if country != last:
            if left.get(last):
                out.append(f"*{left[last]} more preorder{'s' if left[last] > 1 else ''} not listed.*")
            if len(countries) > 1:
                out += ([""] if out else []) + [f"**{COUNTRY_NAMES.get(country, country)}**"]
            last = country
        if top is not None and key[2]:  # a preorder
            kept[country] = kept.get(country, 0) + 1
            if kept[country] > top:
                left[country] = left.get(country, 0) + 1
                continue
        out.append(line)
    if left.get(last):
        out.append(f"*{left[last]} more preorder{'s' if left[last] > 1 else ''} not listed.*")
    return out


def print_share(domains, stores, args):
    lines = share_lines(domains, stores, args.share, args.top)
    if not lines:
        print(NO_STOCK)
        return
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    head = f"**{md(args.query)}** - checked {stamp}, live-confirmed"
    fx = next((d["fx"] for d in domains.values() if d.get("fx") and d["currency"] == "USD"
               and any(v["shown"] for p in d["products"].values() for v in p["variants"])), None)
    if fx:
        head += f" - {fx_line(fx)}"
    limit, chunks, cur = SHARE_LIMIT[args.share], [], [head, ""]
    for line in lines + ["", SHARE_FOOTER]:
        if len("\n".join(cur + [line])) > limit:
            chunks.append(cur)
            cur = [f"**{md(args.query)}** (continued)", ""]
        cur.append(line)
    chunks.append(cur)
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            if i > 1:
                print(flush=True)  # keeps messages apart when stdout goes to a file
            print(f"----- message {i} of {len(chunks)} -----", file=sys.stderr, flush=True)
        print("\n".join(chunk), flush=True)
