"""Test helpers: an Ed25519 / minisign signer (tests only; the program only
verifies), a tiny dataset builder, and a local HTTP server standing in for
the GitHub release."""

import base64
import hashlib
import http.server
import json
import lzma
import os
import sqlite3
import threading

from groknmok import dataset, minisign as m

KEY_ID = bytes.fromhex("0102030405060708")


def _encode(pt):
    x, y, z, _ = pt
    zi = pow(z, m._P - 2, m._P)
    x, y = x * zi % m._P, y * zi % m._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


class Signer:
    """RFC 8032 Ed25519 signing from a 32-byte seed, writing minisign files."""

    def __init__(self, seed, key_id=KEY_ID):
        h = hashlib.sha512(seed).digest()
        a = int.from_bytes(h[:32], "little")
        a &= (1 << 254) - 8
        a |= 1 << 254
        self.a, self.prefix, self.key_id = a, h[32:], key_id
        self.public = _encode(m._mul(a, m._G))

    def sign_raw(self, msg):
        r = int.from_bytes(hashlib.sha512(self.prefix + msg).digest(), "little") % m._L
        big_r = _encode(m._mul(r, m._G))
        k = int.from_bytes(hashlib.sha512(big_r + self.public + msg).digest(), "little") % m._L
        return big_r + ((r + k * self.a) % m._L).to_bytes(32, "little")

    def pub_text(self):
        return "untrusted comment: test key\n" + base64.b64encode(b"Ed" + self.key_id + self.public).decode() + "\n"

    def minisig(self, data, trusted):
        sig = self.sign_raw(hashlib.blake2b(data, digest_size=64).digest())
        glob = self.sign_raw(sig + trusted.encode())
        return ("untrusted comment: test signature\n" + base64.b64encode(b"ED" + self.key_id + sig).decode()
                + f"\ntrusted comment: {trusted}\n" + base64.b64encode(glob).decode() + "\n")


STORES = [("s1", "Hobby Den", "Ottawa", "ON", "CA", "https://den.example/", "unknown", "", "CAD"),
          ("s2", "Dice Barn", "Seattle", "WA", "US", "https://barn.example/", "yes", "", "USD")]
DOMAINS = [("den.example", "shopify", "https://den.example", "s1", "CAD", "weekly check"),
           ("barn.example", "woocommerce", "https://barn.example", "s2", "USD", "weekly check")]


def item(dom, key, title, price, status, snap="2026-10-11T03:00:00+00:00"):
    return (dom, key, "1", key, f"https://{dom}/products/{key}", title, None, None, price, status, None, None, None,
            0, snap, "ok")


CORE_ITEMS = [item("den.example", "telemon", "Telemon Heavy Dreadnought", "119.00", "in_stock"),
              item("den.example", "custodes-guard", "Custodian Guard", "70.00", "out_of_stock"),
              item("barn.example", "telemon-us", "Custodes: Telemon Heavy Dreadnought", "97.75", "in_stock")]
CAT_ITEMS = [("den.example", None, "0", None, "https://den.example/products/telemon-poster", "Telemon Poster",
              None, None, None, "unknown", None, None, None, None, "2026-10-10T03:00:00+00:00", "ok")]


def build_part(path, part, items, build_time, fmt=dataset.FORMAT_VERSION):
    """Write part.sqlite and part.sqlite.xz; returns the manifest entry (without asset)."""
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript(dataset.SCHEMA)
    meta = {"format_version": fmt, "part": part, "build_time": build_time, "min_client_version": "0.1.0",
            "notice": "test notice", "fx_usdcad_rate": "1.4", "fx_usdcad_date": "2026-10-09",
            "title_excludes": "Proxy\n", "rows": len(items)}
    con.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
    if part == "core":
        con.executemany("INSERT INTO stores VALUES (?,?,?,?,?,?,?,?,?)", STORES)
        con.executemany("INSERT INTO domains VALUES (?,?,?,?,?,?)", DOMAINS)
    con.executemany(f"INSERT INTO items VALUES ({','.join('?' * len(dataset.ITEM_FIELDS))})", items)
    con.commit()
    con.close()
    with open(path, "rb") as src:
        raw = src.read()
    xz = lzma.compress(raw)
    with open(path + ".xz", "wb") as fh:
        fh.write(xz)
    return {"name": part, "sha256": hashlib.sha256(xz).hexdigest(), "size": len(xz), "sqlite_bytes": len(raw),
            "rows": len(items), "build_time": build_time}, xz


class Release:
    """A dataset release served from memory: files[name] = bytes."""

    def __init__(self):
        self.files, self.requests = {}, []
        rel = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                name = self.path.lstrip("/").split("/", 1)[-1]
                rel.requests.append(name)
                body = rel.files.get(name)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/dataset/"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def publish(self, signer, workdir, build, core_items=CORE_ITEMS, cat_items=CAT_ITEMS, cat_build=None,
                fmt=dataset.FORMAT_VERSION, min_client="0.1.0", tamper=None):
        """Build and serve a release; tamper(manifest_bytes, files) may corrupt it after signing."""
        iso = f"{build[:4]}-{build[4:6]}-{build[6:8]}T{build[9:11]}:{build[11:13]}:{build[13:15]}+00:00"
        parts = []
        for name, items, b in (("core", core_items, build), ("catalogue", cat_items, cat_build or build)):
            biso = iso if b == build else (f"{b[:4]}-{b[4:6]}-{b[6:8]}T{b[9:11]}:{b[11:13]}:{b[13:15]}+00:00")
            entry, xz = build_part(os.path.join(workdir, f"{name}-{b}.sqlite"), name, items, biso, fmt)
            entry["asset"] = f"{name}-{b}.sqlite.xz"
            self.files[entry["asset"]] = xz
            parts.append(entry)
        man = {"format_version": fmt, "min_client_version": min_client, "build": build, "build_time": iso,
               "previous_build": None, "notice": "test notice", "signature": f"manifest-{build}.json.minisig",
               "parts": parts}
        raw = (json.dumps(man, indent=1) + "\n").encode()
        self.files["manifest.json"] = raw
        self.files[man["signature"]] = signer.minisig(raw, f"groknmok dataset manifest {build}").encode()
        if tamper:
            tamper(man, self.files)
        return man
