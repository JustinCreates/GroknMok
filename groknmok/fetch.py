"""Polite HTTP fetching, shared by the 40k-stockcheck pipeline and the GroknMok
client: robots.txt fetched and obeyed (protego; 4xx means no rules, 5xx or
unreachable means disallow all), at least MIN_GAP seconds between requests to
the same host (redirect hops included), one request at a time per
multi-tenant platform host across all subdomains and threads, and a 403 / 429
or bot challenge marks the host blocked with no further requests.

Moved verbatim from the pipeline's detect_platform.py (2026-10-11). Fetcher
now takes its User-Agent and robots.txt token as arguments, and logs through
its _log method so a caller can route the log."""

import sys
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests
from protego import Protego

MIN_GAP = 2.0
TIMEOUT = 15
MAX_REDIRECTS = 5
# Multi-tenant platform hosts: every store on one is a subdomain of the same service,
# so all of them share one politeness slot: one request at a time, MIN_GAP apart,
# across every Fetcher and thread in the process.
SHARED_HOSTS = (
    "square.site", "company.site", "wixsite.com", "mybigcommerce.com", "myshopify.com",
    "crystalcommerce.com", "tcgplayerpro.com", "squarespace.com", "shoplightspeed.com",
    "webshopapp.com", "weebly.com", "weeblysite.com", "editmysite.com", "godaddysites.com",
)
_shared = {}  # shared host -> [lock, monotonic time the last request ended]
_shared_guard = threading.Lock()


def shared_host(host):
    """The multi-tenant platform host a hostname belongs to, or None."""
    host = (host or "").lower()
    return next((h for h in SHARED_HOSTS if host == h or host.endswith("." + h)), None)


def _shared_slot(host):
    key = shared_host(host)
    if key is None:
        return None
    with _shared_guard:
        return _shared.setdefault(key, [threading.Lock(), None])


class Blocked(Exception):
    """403/429 or bot challenge on this host."""


class Disallowed(Exception):
    """robots.txt disallows this URL."""


_LOG_LOCK = threading.Lock()


def log(msg):
    # One write per line under a lock: print() writes the text and the newline
    # separately, so lines from parallel domains could run together.
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(now))
    line = f"{stamp}.{int(now * 1000) % 1000:03d} {msg}\n"
    with _LOG_LOCK:
        sys.stderr.write(line)
        sys.stderr.flush()


def origin_of(url):
    """scheme://host[:port] for a URL or bare domain, or None if unparseable."""
    s = url.strip()
    if not s:
        return None
    if "://" not in s:
        s = "https://" + s
    parts = urlsplit(s)
    if not parts.hostname:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def domain_key(host):
    """Politeness key: www.example.com and example.com count as one domain."""
    return (host or "").removeprefix("www.")


def block_reason(r):
    if r.status_code in (403, 429):
        return f"HTTP {r.status_code}"
    if r.headers.get("cf-mitigated"):
        return "Cloudflare challenge"
    if r.headers.get("x-amzn-waf-action"):
        return "AWS WAF " + r.headers["x-amzn-waf-action"]
    return None


class Fetcher:
    def __init__(self, user_agent, ua_token):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.ua_token = ua_token  # the robots.txt user-agent token this fetcher obeys
        self.last = {}     # host -> monotonic time the last request to it ended
        self.robots = {}   # origin -> Protego, or None meaning disallow all
        self.robots_err = {}  # origin -> why robots.txt was unavailable
        self.blocked = {}  # host -> reason
        self.homepage = {}  # origin -> homepage HTML fetched by detect(), reused for currency

    def _log(self, msg):
        log(msg)

    def _throttle(self, host):
        prev = self.last.get(host)
        if prev is None:
            return None
        wait = MIN_GAP - (time.monotonic() - prev)
        if wait > 0:
            time.sleep(wait)
        return time.monotonic() - prev

    def allowed(self, url):
        origin = origin_of(url)
        if origin not in self.robots:
            self.robots[origin] = self._load_robots(origin)
        rp = self.robots[origin]
        return rp is not None and rp.can_fetch(url, self.ua_token)

    def _load_robots(self, origin):
        # RFC 9309: 4xx means no rules; 5xx or unreachable means disallow all.
        try:
            r, _ = self.get(origin + "/robots.txt", check_robots=False)
        except requests.RequestException as e:
            self.robots_err[origin] = type(e).__name__
            return None
        if r.status_code >= 500:
            self.robots_err[origin] = f"HTTP {r.status_code}"
            return None
        if r.status_code >= 400:
            return Protego.parse("")
        return Protego.parse(r.text)

    def get(self, url, check_robots=True, timeout=TIMEOUT):
        """GET with manual redirects so every hop is throttled and robots-checked.

        Returns (response, final_url). Raises Blocked, Disallowed or
        requests.RequestException. timeout is per hop (TIMEOUT unless a
        caller such as the sitemap reader asks for more).
        """
        for _ in range(MAX_REDIRECTS + 1):
            host = domain_key(urlsplit(url).hostname)
            if host in self.blocked:
                raise Blocked(f"{host} already blocked: {self.blocked[host]}")
            if check_robots and not self.allowed(url):
                raise Disallowed(url)
            gap = self._throttle(host)
            slot = _shared_slot(host)
            if slot:  # one request at a time across the whole shared host, MIN_GAP apart
                slot[0].acquire()
                if slot[1] is not None:
                    wait = MIN_GAP - (time.monotonic() - slot[1])
                    if wait > 0:
                        time.sleep(wait)
            try:
                r = self.session.get(url, allow_redirects=False, timeout=timeout)
            except requests.RequestException as e:
                self._log(f"GET ERR {url} {type(e).__name__}")
                raise
            finally:
                self.last[host] = time.monotonic()
                if slot:
                    slot[1] = self.last[host]
                    slot[0].release()
            gap_s = "first" if gap is None else f"{gap:.2f}s"
            self._log(f"GET {r.status_code} {url} gap={gap_s}")
            reason = block_reason(r)
            if reason:
                self.blocked[host] = reason
                raise Blocked(f"{host}: {reason}")
            if r.is_redirect:
                url = urljoin(url, r.headers["Location"])
                continue
            return r, url
        raise requests.TooManyRedirects(url)
