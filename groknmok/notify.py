"""ntfy notifications, shared by the 40k-stockcheck pipeline and the GroknMok client.
Settings come from a JSON file (server, default https://ntfy.sh, or a self-hosted
server; topic; optional token), used only when it is mode 0600 and owned by the
user; the topic and token are never printed. A failed send is logged, never
raised.

Moved verbatim from the pipeline's watch.py (2026-10-11); send() now takes the
User-Agent as an argument."""

import json
import os
import stat
import sys
from datetime import datetime

import requests

from groknmok.rules import to_ascii

NTFY_TIMEOUT = 15         # seconds per send attempt


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def load_config(path):
    """ntfy settings, or None. Refuses a file others can read, or not owned by us."""
    if not os.path.exists(path):
        return None
    st = os.stat(path)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        log(f"ntfy: {path} must be mode 0600 and owned by you; not used")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as e:
        log(f"ntfy: {path} unreadable ({type(e).__name__}); not used")
        return None
    if not cfg.get("topic"):
        log("ntfy: config has no topic; not used")
        return None
    cfg["server"] = (cfg.get("server") or "https://ntfy.sh").rstrip("/")
    return cfg


def send(cfg, title, body, tags="package", priority=3, click=None, user_agent="groknmok"):
    """POST one message; True on success. Never raises; never logs the topic or token."""
    if cfg is None:
        log(f"ntfy: no config; not sent: {title}")
        return False
    headers = {"Title": to_ascii(title)[:200], "Tags": tags, "Priority": str(priority),
               "User-Agent": user_agent}
    if click:
        headers["Click"] = click
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    data = to_ascii(body).encode("ascii", "replace")
    for attempt in (1, 2):
        try:
            r = requests.post(f"{cfg['server']}/{cfg['topic']}", data=data, headers=headers,
                              timeout=NTFY_TIMEOUT)
            if r.status_code < 300:
                log(f"ntfy: sent (HTTP {r.status_code})")
                return True
            log(f"ntfy: attempt {attempt}: HTTP {r.status_code}")
        except requests.RequestException as e:
            log(f"ntfy: attempt {attempt}: {type(e).__name__}")
    return False
