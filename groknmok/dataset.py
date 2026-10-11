"""The published GroknMok dataset: schema (format version 1) and reader.

A dataset is one or two SQLite files:
  core       stores, domains, and the items of every structured store
             (Shopify, WooCommerce, Lightspeed, ...) plus the GW-looking
             links of sitemap-only stores; republished after every run.
  catalogue  the other sitemap links (whole store catalogues); republished
             when the weekly sitemap refresh changes them. Optional.

Tables:
  meta(key, value)     format_version, part, build_time, min_client_version,
                       notice, fx_usdcad_rate, fx_usdcad_date, title_excludes
                       (the exclude list's text), rows
  stores               store_id (opaque), name (cleaned finder name), city,
                       region, country, website, ships, override_currency,
                       detected_currency
  domains              domain, platform, base_url, store_ids (comma list, the
                       snapshot's store order), currency, currency_source (the
                       snapshot's weekly currency check)
  items                domain, product_key (null when it equals url, as for
                       sitemap links), variant_key, handle, url, title,
                       variant_title, sku, price (text, as stored), status
                       (raw), release_date, barcode, catalogue, used (1 / 0 /
                       null), snapshot_at, snapshot_status; no index (search
                       scans the table) and no display title (the client
                       derives it with rules.display_title)

Items hold, per domain, the latest good snapshot's products with a newer
--quick snapshot's products in their place, exactly as the pipeline's search
reads them. No descriptions, images, addresses, phone numbers, finder ids,
notes or map-listing fields.

load_matches() returns the same structure the pipeline's search.load_matches
does, so groknmok.report and groknmok.watch work on either."""

import re
import sqlite3

from groknmok.rules import code_like, words

FORMAT_VERSION = 1
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE stores (
  store_id TEXT PRIMARY KEY, name TEXT, city TEXT, region TEXT, country TEXT, website TEXT, ships TEXT,
  override_currency TEXT, detected_currency TEXT);
CREATE TABLE domains (
  domain TEXT PRIMARY KEY, platform TEXT, base_url TEXT, store_ids TEXT, currency TEXT, currency_source TEXT);
CREATE TABLE items (
  domain TEXT, product_key TEXT, variant_key TEXT, handle TEXT, url TEXT, title TEXT, variant_title TEXT,
  sku TEXT, price TEXT, status TEXT, release_date TEXT, barcode TEXT, catalogue TEXT, used INTEGER,
  snapshot_at TEXT, snapshot_status TEXT);
"""
ITEM_FIELDS = ["domain", "product_key", "variant_key", "handle", "url", "title", "variant_title", "sku", "price",
               "status", "release_date", "barcode", "catalogue", "used", "snapshot_at", "snapshot_status"]


def open_dataset(core_path, catalogue_path=None):
    """A read-only connection to the core part, with the catalogue part attached
    as "cat" when given."""
    conn = sqlite3.connect(f"file:{core_path}?mode=ro", uri=True)
    if catalogue_path:
        conn.execute("ATTACH DATABASE ? AS cat", (f"file:{catalogue_path}?mode=ro",))
    return conn


def meta(conn):
    return dict(conn.execute("SELECT key, value FROM meta"))


def has_catalogue(conn):
    return any(r[1] == "cat" for r in conn.execute("PRAGMA database_list"))


def load_stores(conn):
    """(stores, detected) keyed by store_id, in the shape groknmok.report expects:
    stores rows carry name, city, province, country, website, ships and
    override_currency; detected rows carry the detected site currency."""
    stores, detected = {}, {}
    for sid, name, city, region, country, website, ships, ocur, dcur in conn.execute(
            "SELECT store_id, name, city, region, country, website, ships, override_currency, detected_currency"
            " FROM stores"):
        stores[sid] = {"finder_id": sid, "name": name, "city": city, "province": region, "country": country,
                       "website": website, "ships": ships or "unknown", "override_currency": ocur or ""}
        detected[sid] = {"currency": dcur or ""}
    return stores, detected


def load_matches(conn, query, used):
    """Matches for a query, in the pipeline's search.load_matches structure: every
    query word must appear as a whole word in the title (or, for a code-like
    query, in the SKU, barcode or catalogue number); used stock is dropped unless
    used is true. A domain's finished_at and snap_status are those of its newest
    matching item, as in the pipeline."""
    qw = words(query)
    if not qw:
        raise SystemExit("empty query")
    fields = ["lower(title)", "lower(coalesce(sku,''))", "coalesce(barcode,'')", "coalesce(catalogue,'')"]
    likes = " AND ".join("(" + " OR ".join(f"{fl} LIKE ?" for fl in fields) + ")" for _ in qw)
    params = [f"%{w}%" for w in qw for _ in fields]
    cols = ", ".join(ITEM_FIELDS)
    if has_catalogue(conn):
        params = params + params
        sql = (f"SELECT * FROM (SELECT {cols} FROM items WHERE {likes} UNION ALL "
               f"SELECT {cols} FROM cat.items WHERE {likes}) ORDER BY snapshot_at DESC")
    else:
        sql = f"SELECT {cols} FROM items WHERE {likes} ORDER BY snapshot_at DESC"
    doms = {r[0]: r for r in conn.execute(
        "SELECT domain, platform, base_url, store_ids, currency, currency_source FROM domains")}
    code = code_like(query)
    qcode = re.sub(r"\D", "", query) if code else ""
    domains = {}
    for (dom, pkey, vkey, handle, url, title, vtitle, sku, price, raw, rdate, barcode, cat, pused,
         fin, sst) in conn.execute(sql, params):
        pkey = pkey or url
        title_ok = set(qw) <= set(words(title))
        sku_ok = code and sku and set(qw) <= set(words(sku))
        code_hit = ("catalogue number match" if code and qcode == (cat or "")
                    else "barcode match" if code and qcode == re.sub(r"\D", "", barcode or "") else "")
        if not (title_ok or sku_ok or code_hit):
            continue
        if pused == 1 and not used:
            continue
        _, plat, base, sids, cur, cur_src = doms[dom]
        p = None if price in (None, "") else float(price)
        d = domains.setdefault(dom, {"domain": dom, "platform": plat, "finished_at": fin, "snap_status": sst,
                                     "base_url": base, "finder_ids": (sids or "").split(","), "products": {},
                                     "db_currency": cur or "", "db_currency_source": cur_src or ""})
        prod = d["products"].setdefault(pkey, {"key": pkey, "handle": handle, "url": url, "title": title,
                                                "used": pused, "release_date": rdate, "variants": []})
        prod["variants"].append({"key": vkey, "title": vtitle, "sku": sku, "price": p, "status": raw,
                                 "status_source": "snapshot", "barcode": barcode, "catalogue": cat,
                                 "match": code_hit or ("sku match" if sku_ok and not title_ok
                                                       else "title match, low confidence")})
    return domains
