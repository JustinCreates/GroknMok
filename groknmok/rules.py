"""Shared rules: text, codes, pre-order and release-date reading, display verdicts,
currency checks. Pure functions, no network; used by the 40k-stockcheck pipeline
and the GroknMok client alike, so both read the data the same way.

Moved verbatim from the pipeline's find_websites.py, snapshot.py,
detect_platform.py and search.py (2026-10-11)."""

import html
import os
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlsplit

# Punctuation NFKD does not decompose, mapped before non-ASCII is dropped.
ASCII_MAP = str.maketrans({
    "\u2022": "-", "\u00b7": "-", "\u2013": "-", "\u2014": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...", "\u00a0": " ",
})
USED = re.compile(r"\bused\b|pre[\s-]?owned|second[\s-]?hand|\boccasion\b|\busag[e\u00e9]e?s?\b", re.I)
CATALOGUE = re.compile(r"(?<!\d)99\d{9}(?!\d)")  # GW catalogue number: 11 digits starting 99 (99123043012)
PREORDER = re.compile(r"pre[\s-]?order|pre[\s-]?sale|pr[e\u00e9]commande", re.I)  # presale / pre-sale / pre sale
# Display verdict: a title that says it is not really available overrides an
# "available" store flag. The CSV keeps the raw status.
VERDICT = [(re.compile(r"pre[\s-]?order|pre[\s-]?sale", re.I), "preorder"),
           (re.compile(r"back[\s-]?order|awaiting", re.I), "backorder")]
INFER_MIN_STORES = 2  # domains listing a barcode with a future release date before "in stock" becomes preorder
RELEASE_NOTE_MIN = 3  # domains giving the same future release date before in-stock title matches get a note
MISMATCH = "possible wrong match"  # marks a site currency that differs from the store's country
SLUG_GENERIC = {"product", "products", "item", "items", "p", "shop", "store", "catalog"}  # never a title
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_FR_MONTHS = {"janvier": 1, "f\u00e9vrier": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
              "juillet": 7, "ao\u00fbt": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
              "d\u00e9cembre": 12, "decembre": 12}
_MONTH = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|janvier|f[e\u00e9]vrier|mars|avril|"
          r"mai|juin|juillet|ao[u\u00fb]t|septembre|octobre|novembre|d[e\u00e9]cembre)")
DATE_MD = re.compile(_MONTH + r"\.?\s+(\d{1,2})(?:st|nd|rd|th|er)?\b(?:,?\s+(20\d\d))?", re.I)
DATE_DM = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th|er)?\s+" + _MONTH + r"\b(?:,?\s+(20\d\d))?", re.I)
DATE_ISO = re.compile(r"\b(20\d\d)([-/])(\d{1,2})\2(\d{1,2})\b")  # 2026-10-17 or 2026/10/17
# Numeric day/month or month/day with a 4-digit year, "/" or "." separated
# ("10/17/2026"). Dashes are left out: GW product codes look like "77-02".
DATE_NUM = re.compile(r"(?<![\d/.])(\d{1,2})[/.](\d{1,2})[/.](20\d\d)(?![\d/.])")
# A numeric month/day with no 4-digit year ("Preorder 10/17", "(10/17/26)"): only after a release word
# or an opening bracket, so sizes and scales ("1/8 scale") are not read as dates.
RELEASE_NUM = re.compile(r"(?:pre[\s-]?orders?|presale|releas\w*(?:\s+date)?|available|ships?|arriv\w*|due|[(\[])"
                         r"\W{0,12}?(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2}))?(?![\d/.])", re.I)


def to_ascii(s):
    s = str(s or "").translate(ASCII_MAP)
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def words(text):
    return re.findall(r"[a-z0-9]+", to_ascii(text or "").lower())


def code_like(query):
    return bool(re.fullmatch(r"\s*[0-9][0-9\- ]{3,}\s*", query))


def clean(text):
    """HTML entities decoded (WooCommerce titles carry "&#8211;"), runs of
    whitespace (line breaks too) collapsed to one space."""
    return " ".join(html.unescape(text or "").split())


def slug_title(url):
    seg = [s for s in urlsplit(url).path.split("/") if s]
    # Last segment with words: skip numeric IDs (Square's /product/<slug>/7152) and
    # generic path words. If none is left, the last segment, as before.
    words = [s for s in seg if re.search(r"[^\W\d_]", s) and s.lower() not in SLUG_GENERIC]
    pick = words[-1] if words else (seg[-1] if seg else None)
    return re.sub(r"[-_]+", " ", pick).strip() if pick else url


def display_title(p):
    """Product title as displayed (terminal, --share, CSV display_title): the
    live page's product name in place of a slug-derived sitemap title, cleaned.
    The CSV's title column keeps the raw snapshot title."""
    slug = p["handle"] is None and p["title"] == slug_title(p["url"] or "")
    return clean(p["live_name"] if slug and p.get("live_name") else p["title"])


def show_title(p, v):
    """display_title plus the variant title."""
    return display_title(p) + (f" - {clean(v['title'])}" if v["title"] and v["title"] != "Default Title" else "")


def catalogue_number(*values):
    """The first GW catalogue number (11 digits starting 99) in any of the values
    (EAN, barcode or SKU fields), or None."""
    return next((m.group(0) for v in values if v for m in [CATALOGUE.search(str(v))] if m), None)


def split_codes(barcode, sku):
    """(barcode, catalogue number): a barcode field holding a GW catalogue number
    is not a barcode, so it moves to the catalogue number."""
    digits = re.sub(r"\D", "", str(barcode or ""))
    if digits and CATALOGUE.fullmatch(digits):
        return None, digits
    return barcode or None, catalogue_number(sku)


def product_codes(v):
    """[(kind, code)]: the variant's barcode and GW catalogue number, each a key that
    links the same product across stores."""
    bc = re.sub(r"\D", "", v.get("barcode") or "")
    cat = v.get("catalogue")
    return ([("barcode", bc)] if bc else []) + ([("catalogue number", cat)] if cat else [])


def is_preorder(title, tags=(), cart_text=""):
    """Pre-order signal: store flag (tags, WooCommerce add-to-cart text) or title."""
    return bool(PREORDER.search(title or "") or any(PREORDER.search(t or "") for t in tags)
                or PREORDER.search(cart_text or ""))


def shopify_status(available, title, tags):
    if available is None:
        return "unknown"
    if not available:
        return "out_of_stock"  # a sold-out pre-order cannot be ordered either
    return "preorder" if is_preorder(title, tags) else "in_stock"


def woo_status(p):
    """Raw status of a Store API product, with pre-order detection."""
    pre = is_preorder(p.get("name"), [t.get("name") for t in p.get("tags") or []],
                      (p.get("add_to_cart") or {}).get("text"))
    if p.get("is_on_backorder"):
        return "preorder" if pre else "backorder"
    if p.get("is_in_stock"):
        return "preorder" if pre else "in_stock"
    return "out_of_stock" if p.get("is_in_stock") is False else "unknown"


def woo_price(p):
    pr = p.get("prices") or {}
    try:
        return f"{int(pr['price']) / 10 ** int(pr.get('currency_minor_unit', 2)):.2f}"
    except (KeyError, TypeError, ValueError):
        return None


def verdict(v, title):
    """(display status, reason). Only an in_stock raw status can be overridden."""
    if v["status"] == "in_stock":
        for rx, status in VERDICT:
            m = rx.search(title or "")
            if m:
                return status, f"{status}: title says '{m.group(0)}'"
    return v["status"], ""


def in_range(v, lo, hi):
    """Range applies to the live-confirmed price; no range keeps everything."""
    if lo is None and hi is None:
        return True, ""
    if v["status_source"] != "live" or v["price"] is None:
        return False, "live price not confirmed"
    if (lo is not None and v["price"] < lo) or (hi is not None and v["price"] > hi):
        return False, "live price out of range"
    return True, ""


def load_title_excludes(path):
    """[(term, compiled regex)] from the exclude list; whole words only.

    A term written entirely in capitals is case-sensitive; others ignore case.
    """
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            term = line.strip()
            if not term or term.startswith("#"):
                continue
            body = r"\s+".join(re.escape(w) for w in term.split())
            flags = 0 if term.isupper() else re.I
            out.append((term, re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", flags)))
    return out


def excluded_by(title, excludes):
    return next((term for term, rx in excludes if rx.search(title or "")), None)


def currency_mismatch(code, source, countries):
    """True when a detected site currency differs from the store country's: CAD for
    Canada, USD for the US (a domain with stores in both countries: anything but
    CAD or USD). A Shopify storefront converting prices for our location is not
    the site's own currency and does not count."""
    cs = set(countries or ())
    home = {"CA": "CAD", "US": "USD"}.get(next(iter(cs))) if len(cs) == 1 else None
    if not code or "converted" in (source or ""):
        return False
    return code != home if home else code not in ("CAD", "USD")


def canadian_page_usd(code, source, countries):
    """The site currency is kept as detected, from any source. One that differs
    from the store's country (currency_mismatch) is no longer relabelled to the
    country's currency: its source is flagged MISMATCH, for the review list
    (stockcheck --currency-review). The name predates this rule."""
    if currency_mismatch(code, source, countries):
        return code, f"{source}; {code} differs from the store's country: {MISMATCH}"
    return code, source


def _month(name):
    name = name.lower()
    return _FR_MONTHS.get(name) or _MONTHS.index(name[:3]) + 1


def release_date(title, today=None):
    """Release date from a title, or None.

    ISO (YYYY-MM-DD) for "Pre-Order September 12th 2026", "12 septembre",
    "2026-09-12", "2026/09/12", and numeric dates with one number over 12 ("10/17/2026").
    A numeric date with both numbers 12 or under is ambiguous (10/09/2026 could
    be October 9 or September 10) and is returned exactly as written. A
    month-name date without a year takes the next such date on or after 60
    days before today."""
    text = title or ""
    today = today or datetime.now(timezone.utc).date()
    found = None
    m = DATE_ISO.search(text)
    n = None if m else DATE_NUM.search(text)
    if m:
        found = (int(m.group(1)), int(m.group(3)), int(m.group(4)))
    elif n:
        a, b, year = int(n.group(1)), int(n.group(2)), int(n.group(3))
        if a <= 12 and b <= 12:
            return n.group(0)  # ambiguous: keep as written, unparsed
        if a > 12 and b > 12:
            return None
        found = (year, b, a) if a > 12 else (year, a, b)
    else:
        m = DATE_MD.search(text)
        if m:
            found = (int(m.group(3)) if m.group(3) else None, _month(m.group(1)), int(m.group(2)))
        else:
            m = DATE_DM.search(text)
            if m:
                found = (int(m.group(3)) if m.group(3) else None, _month(m.group(2)), int(m.group(1)))
    if not found:
        return None
    year, month, day = found
    try:
        if year is None:
            d = datetime(today.year, month, day).date()
            if (d - today).days < -60:
                d = datetime(today.year + 1, month, day).date()
        else:
            d = datetime(year, month, day).date()
    except ValueError:
        return None
    return d.isoformat()


def yearless(title):
    """([(month, day), ...], year or None) for a release date written without a
    4-digit year: "Oct 17", "17 octobre", "Preorder 10/17", "(10/17/26)" (the
    2-digit year is 20yy). Numeric with both numbers 12 or under gives both
    readings. None when the title has a full date, or no date."""
    t = title or ""
    if DATE_ISO.search(t) or DATE_NUM.search(t):
        return None
    m = DATE_MD.search(t)
    if m:
        return None if m.group(3) else ([(_month(m.group(1)), int(m.group(2)))], None)
    m = DATE_DM.search(t)
    if m:
        return None if m.group(3) else ([(_month(m.group(2)), int(m.group(1)))], None)
    m = RELEASE_NUM.search(t)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    pairs = [(a, b)] if a <= 12 < b else [(b, a)] if b <= 12 < a else [(a, b), (b, a)] if a <= 12 else []
    return pairs, (2000 + int(m.group(3)) if m.group(3) else None)


def resolve_releases(domains, today=None):
    """Display release date for every product, in p["release"], and where it came
    from, in p["release_source"]. A full date in the title (or the snapshot's) is
    used as is. A date written without a year takes the year that other matches
    for this query give for the same month and day (most domains, then earliest
    year); else the next occurrence on or after today. An ambiguous numeric date
    stays as the snapshot had it (raw, or none). A preorder (live status or title)
    with no date in its title takes the one live confirm read from its page text
    after a release word (page_release_text), the same way."""
    today = today or datetime.now().date()
    known = {}  # (month, day) -> {year: {domains}}
    for d in domains.values():
        for p in d["products"].values():
            iso = release_date(p["title"])
            if iso and re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso) and yearless(p["title"]) is None:
                y, mo, dy = map(int, iso.split("-"))
                known.setdefault((mo, dy), {}).setdefault(y, set()).add(d["domain"])

    def on(year, month, day):
        try:
            return datetime(year, month, day).date()
        except ValueError:
            return None

    def resolve(text, raw):
        """(date, how) for the release date written in text: raw as is, or a date
        without a year given one ("year from other matches" / "next occurrence")."""
        y = yearless(text)
        if not y:
            return raw, ""
        # 2000 is a leap year, so Feb 29 passes the validity check
        pairs, year, how = [pr for pr in dict.fromkeys(y[0]) if on(2000, *pr)], y[1], ""
        seen = [pr for pr in pairs if pr in known]
        if len(seen) == 1 and year is None:
            years = known[seen[0]]
            year, how = min(years, key=lambda yr: (-len(years[yr]), yr)), "year from other matches"
            pairs = seen
        elif len(pairs) > 1:
            pairs = seen if len(seen) == 1 else []
        if len(pairs) != 1:
            return raw, ""
        month, day = pairs[0]
        if year is None:  # the next occurrence on or after today (Feb 29: the next leap year)
            year, how = next(yr for yr in range(today.year, today.year + 9)
                             if on(yr, month, day) and on(yr, month, day) >= today), "next occurrence"
        got = on(year, month, day)
        return (got.isoformat(), how) if got else (raw, "")

    for d in domains.values():
        for p in d["products"].values():
            p["release"], how = resolve(p["title"], p.get("release_date") or release_date(p["title"]))
            p["release_source"] = ("title" + (f" ({how})" if how else "")) if p["release"] else ""
            preorder = any(v["status"] == "preorder" or v.get("verdict") == "preorder" for v in p["variants"])
            if not p["release"] and preorder and p.get("page_release_text"):
                text = p["page_release_text"]
                got, how = resolve(text, release_date(text))
                if got:
                    p["release"], p["release_source"] = got, "page text" + (f" ({how})" if how else "")


def infer_preorders(domains, today=None):
    """Probable preorder by barcode or GW catalogue number. Returns {(kind, code): release date} used."""
    today = (today or datetime.now().date()).isoformat()
    seen = {}  # (kind, code) -> {release date: {domains}}
    for d in domains.values():
        for p in d["products"].values():
            rdate = p.get("release")
            if not rdate or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rdate) or rdate <= today:
                continue
            for v in p["variants"]:
                for code in product_codes(v):
                    seen.setdefault(code, {}).setdefault(rdate, set()).add(d["domain"])
    dates = {}
    for code, by_date in seen.items():
        if len(set().union(*by_date.values())) >= INFER_MIN_STORES:
            dates[code] = min(by_date.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0]  # most stores, then earliest
    for d in domains.values():
        for p in d["products"].values():
            for v in p["variants"]:
                code = next((c for c in product_codes(v) if c in dates), None)
                if code and v["verdict"] == "in_stock" and today < dates[code]:
                    n = len(set().union(*seen[code].values()))
                    v["verdict"], v["inferred_release"] = "preorder", dates[code]
                    v["verdict_reason"] = (f"preorder inferred: {n} stores list {code[0]} {code[1]} "
                                           f"with release {dates[code]}")
    return dates


def note_release(domains, today=None):
    """In stock before release: when RELEASE_NOTE_MIN or more domains' matches give
    the same future release date in the title, every other "in stock" title match
    (no date of its own) is annotated, not relabelled. Returns the date used, or None."""
    today = (today or datetime.now().date()).isoformat()
    by_date = {}  # release date -> {domains}
    for d in domains.values():
        for p in d["products"].values():
            rdate = p.get("release")
            if (rdate and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rdate) and rdate > today
                    and any(v["listed"] for v in p["variants"])):
                by_date.setdefault(rdate, set()).add(d["domain"])
    best = min(by_date.items(), key=lambda kv: (-len(kv[1]), kv[0]), default=None)  # most domains, then earliest
    if not best or len(best[1]) < RELEASE_NOTE_MIN:
        return None
    rdate, doms = best
    for d in domains.values():
        for p in d["products"].values():
            if p.get("release"):
                continue
            for v in p["variants"]:
                if v["verdict"] == "in_stock" and v["match"].startswith("title"):
                    n = len(doms - {d["domain"]})
                    v["release_note"] = rdate
                    v["verdict_reason"] = f"in stock per store; {n} other stores list release {rdate}"
    return rdate
