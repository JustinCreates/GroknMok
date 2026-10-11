"""Live confirm: re-check matched products on the store's own site, one domain at a
time through the polite fetcher. Shared by the 40k-stockcheck pipeline and the
GroknMok client.

  shopify      /products/{handle}.js   (status, price, barcode)
  woocommerce  /wp-json/wc/store/v1/products/{id}
  lightspeed   <product>.html?format=json (stock, price, EAN)
  other        the product page's JSON-LD offer, product name and release text

Moved verbatim from the pipeline's search.py (2026-10-11); confirm_domain now takes
the fetcher factory and the stop exceptions as arguments."""

import html
import json
import re
from datetime import datetime, timezone

import requests

from groknmok.fetch import Blocked, Disallowed
from groknmok.rules import is_preorder, release_date, shopify_status, woo_price, woo_status, yearless

MAX_CONFIRM = 5
CONFIRM_MAX_DELAY = 10.0
CONFIRM_TIMEOUT = 90
STATUS_ORDER = ["in_stock", "preorder", "backorder", "out_of_stock", "unknown", "blocked", "unconfirmed"]
LD_STATUS = {"instock": "in_stock", "limitedavailability": "in_stock", "onlineonly": "in_stock",
             "instoreonly": "in_stock", "outofstock": "out_of_stock", "soldout": "out_of_stock",
             "discontinued": "out_of_stock", "preorder": "preorder", "presale": "preorder",
             "backorder": "backorder"}
PAGE_RELEASE = re.compile(r"\b(?:releases|release date|available|ships)\b", re.I)


def ld_offers(html):
    """(status, price, currency) from the first schema.org Product offer in the page's JSON-LD."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, dict):
                if "@graph" in node:
                    stack.extend(node["@graph"])
                offers = node.get("offers")
                if offers:
                    offer = offers[0] if isinstance(offers, list) else offers
                    avail = str(offer.get("availability", "")).rsplit("/", 1)[-1].lower()
                    price = offer.get("price", offer.get("lowPrice"))
                    try:
                        price = float(price) if price not in (None, "") else None
                    except (TypeError, ValueError):
                        price = None
                    return LD_STATUS.get(avail, "unknown"), price, str(offer.get("priceCurrency") or "").upper()
    return None, None, ""


def page_name(html_text):
    """Product name from the page: JSON-LD Product name, else og:title; None if neither."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html_text, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop(0)
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                stack.extend(node.get("@graph") or [])
                kind = node.get("@type")
                kinds = kind if isinstance(kind, list) else [kind]
                if "Product" in kinds and isinstance(node.get("name"), str) and node["name"].strip():
                    return node["name"]
    m = (re.search(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)', html_text, re.I)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:title', html_text, re.I))
    return m.group(1) if m and m.group(1).strip() else None


def page_release_text(html_text):
    """The first "releases / release date / available / ships <date>" in a product
    page's text (or a JSON description), word and date together; None if none."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text or "")
    t = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", t)).split())
    for m in PAGE_RELEASE.finditer(t):
        snippet = t[m.start():m.end() + 28]
        if release_date(snippet) or yearless(snippet):
            return snippet
    return None


def lightspeed_status(stock, title):
    """Raw status from a Lightspeed product JSON's stock block: not available ->
    out_of_stock; available and on stock -> in_stock; available but not on stock
    (out-of-stock sale allowed) -> backorder. A pre-order title makes either
    orderable status "preorder"."""
    if not stock or stock.get("available") is None:
        return "unknown"
    if not stock.get("available"):
        return "out_of_stock"
    orderable = "in_stock" if stock.get("on_stock") else "backorder"
    return "preorder" if is_preorder(title) else orderable


def confirm_domain(d, make_fetcher, stop_errors=()):
    """Live-confirm the products plan_confirm() picked for one domain; mutates d in place.
    make_fetcher() returns the polite fetcher to use (with the caller's User-Agent and
    known-blocked hosts); stop_errors are exceptions with a .note that end the domain
    as "not confirmed: <note>" (the pipeline's Crawl-delay Stop)."""
    f = make_fetcher()
    base = d["base_url"]
    products = list(d["products"].values())
    todo = [p for p in products if p.get("confirm")]
    d["confirm_note"] = ""
    try:
        f.allowed(base + "/")  # loads robots.txt, and with it any Crawl-delay
        delay = max(f.delay.values(), default=0.0)
        if delay > CONFIRM_MAX_DELAY:
            d["confirm_note"] = f"not confirmed: robots.txt Crawl-delay {delay:g} s"
            return d
        for prod in todo:
            confirm_product(f, d, base, prod)
        if len(todo) < len(products):
            d["confirm_note"] = (f"confirmed {len(todo)} of {len(products)} products"
                                 + (" (--limit reached)" if d.get("limited") else ""))
    except stop_errors as e:
        d["confirm_note"] = f"not confirmed: {e.note}"
    except Blocked as e:
        d["confirm_note"] = f"blocked: {e}"
        for prod in products:
            for v in prod["variants"]:
                v["status"], v["status_source"] = "blocked", "live"
    finally:
        f.session.close()
    return d


def confirm_product(f, d, base, prod):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        if d["platform"] == "shopify":
            r, _ = f.get(f"{base}/products/{prod['handle']}.js")
            if r.status_code != 200:
                return mark(prod, f"HTTP {r.status_code}")
            data = r.json()
            tags = data.get("tags") or []
            prod["page_release_text"] = page_release_text(data.get("description") or "")
            live = {str(v["id"]): v for v in data.get("variants") or []}
            for v in prod["variants"]:
                lv = live.get(v["key"])
                if lv is None:
                    v["status_source"] = "snapshot (variant gone)"
                    continue
                v["status"] = shopify_status(lv.get("available"), data.get("title"), tags)
                v["price"] = lv["price"] / 100 if isinstance(lv.get("price"), int) else v["price"]
                v["barcode"] = lv.get("barcode") or None
                v["status_source"], v["confirmed_at"] = "live", now
        elif d["platform"] == "lightspeed":
            r, _ = f.get(prod["url"] + "?format=json")
            if r.status_code != 200:
                return mark(prod, f"HTTP {r.status_code}")
            j = r.json()
            pj = j.get("product") or {}
            stock = pj.get("stock") if isinstance(pj.get("stock"), dict) else {}
            prod["page_release_text"] = page_release_text(str(pj.get("content") or pj.get("description") or ""))
            shop_cur = str((j.get("shop") or {}).get("currency") or "").upper()
            d["live_currency"] = shop_cur or d.get("live_currency", "")
            d["live_currency_src"] = "Lightspeed shop currency"
            price = (pj.get("price") or {}).get("price") if isinstance(pj.get("price"), dict) else None
            for v in prod["variants"]:
                v["status"] = lightspeed_status(stock, prod["title"])
                v["price"] = float(price) if price not in (None, "") else v["price"]
                v["barcode"] = re.sub(r"\D", "", str(pj.get("ean") or "")) or v["barcode"]
                v["stock_level"] = stock.get("level")
                v["status_source"], v["confirmed_at"] = "live", now
        elif d["platform"] == "woocommerce":
            r, _ = f.get(f"{base}/wp-json/wc/store/v1/products/{prod['key']}")
            if r.status_code != 200:
                return mark(prod, f"HTTP {r.status_code}")
            p = r.json()
            prod["page_release_text"] = page_release_text(f"{p.get('short_description') or ''} "
                                                          f"{p.get('description') or ''}")
            status = woo_status(p)  # same pre-order rule as the snapshot
            price = woo_price(p)
            d["live_currency"] = str((p.get("prices") or {}).get("currency_code") or "").upper() \
                or d.get("live_currency", "")
            d["live_currency_src"] = "Store API"
            for v in prod["variants"]:
                v["status"], v["status_source"], v["confirmed_at"] = status, "live", now
                v["price"] = float(price) if price else v["price"]
        else:
            r, _ = f.get(prod["url"])
            if r.status_code != 200:
                return mark(prod, f"HTTP {r.status_code}")
            status, price, cur = ld_offers(r.text)
            prod["live_name"] = page_name(r.text)
            prod["page_release_text"] = page_release_text(r.text)
            d["live_currency"] = cur or d.get("live_currency", "")
            d["live_currency_src"] = "page meta"  # JSON-LD priceCurrency
            for v in prod["variants"]:
                v["status"] = status or "unknown"
                v["price"] = price if price is not None else v["price"]
                v["status_source"] = "live" if status else "live (no JSON-LD offer)"
                v["confirmed_at"] = now
    except Disallowed:
        mark(prod, "robots.txt disallows")
    except requests.RequestException as e:
        mark(prod, f"error: {type(e).__name__}")
    except ValueError:
        mark(prod, "not JSON")


def mark(prod, why):
    for v in prod["variants"]:
        v["status_source"] = f"snapshot (confirm failed: {why})"


def rank(v):
    return STATUS_ORDER.index(v["status"]) if v["status"] in STATUS_ORDER else len(STATUS_ORDER)


def prio(p):
    """Confirm order: best snapshot status, then cheapest snapshot price."""
    price = min((v["price"] for v in p["variants"] if v["price"] is not None), default=float("inf"))
    return min(rank(v) for v in p["variants"]), price


def plan_confirm(domains, limit):
    """Mark the products to live-confirm: per domain at most MAX_CONFIRM, best
    snapshot status and cheapest first; across domains round-robin (every
    domain's best product before any domain's second), at most limit.
    Returns how many candidates the limit left out."""
    cands = []
    for d in domains.values():
        cands += [((i,) + prio(p), d["domain"], p, d)
                  for i, p in enumerate(sorted(d["products"].values(), key=prio)[:MAX_CONFIRM])]
    cands.sort(key=lambda c: (c[0], c[1]))
    for _, _, p, d in cands[:limit]:
        p["confirm"] = True
    for _, _, p, d in cands[limit:]:
        d["limited"] = True
    return max(0, len(cands) - limit)
