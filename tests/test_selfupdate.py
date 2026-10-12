"""Self-update checks against a local stand-in for GitHub releases."""

import hashlib
import http.server
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile

from helpers import Signer

from groknmok import client, selfupdate

SIGNER, OTHER = Signer(b"\x31" * 32), Signer(b"\x32" * 32, key_id=bytes.fromhex("1111111111111111"))


def pyz(version):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("__main__.py", "")
        z.writestr("groknmok/__init__.py", f'__version__ = "{version}"\n')
    return b"#!/usr/bin/env python3\n" + buf.getvalue()


class Releases:
    def __init__(self):
        self.files, self.tag = {}, "v9.0.0"
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/latest":
                    body = json.dumps({"tag_name": outer.tag, "assets": [
                        {"name": n, "browser_download_url": f"{outer.base}/d/{n}"} for n in outer.files]}).encode()
                else:
                    body = outer.files.get(self.path.rsplit("/", 1)[-1])
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
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def release(self, version, signer=SIGNER, data=None, sums=True):
        data = data or pyz(version)
        self.files = {"groknmok.pyz": data,
                      "groknmok.pyz.minisig": signer.minisig(data, "timestamp:1\tfile:groknmok.pyz\thashed").encode()}
        if sums:
            self.files["SHA256SUMS"] = f"{hashlib.sha256(data).hexdigest()}  groknmok.pyz\n".encode()


class SelfUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GROKNMOK_DATA_DIR"] = self.tmp.name
        self.rel = Releases()
        os.environ["GROKNMOK_RELEASES_URL"] = self.rel.base + "/latest"
        self.p = client.paths()

    def tearDown(self):
        self.rel.server.shutdown()
        self.rel.server.server_close()
        self.tmp.cleanup()

    def info(self):
        return selfupdate.latest(client.session())

    def test_good_release(self):
        self.rel.release("9.0.0")
        info = self.info()
        self.assertTrue(selfupdate.newer(info))
        self.assertEqual(selfupdate.verified_download(info, SIGNER.pub_text())[:2], b"#!")

    def test_refusals(self):
        cases = [("made with another key", dict(signer=OTHER)),
                 ("is not a groknmok program", dict(data=b"#!/usr/bin/env python3\nnot a zip")),
                 ("says it is 8.0.0, the release says 9.0.0", dict(data=pyz("8.0.0")))]
        for text, kw in cases:
            self.rel.release("9.0.0", **kw)
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.verified_download(self.info(), SIGNER.pub_text())
            self.assertIn(text, str(cm.exception))
        self.rel.release("9.0.0")
        self.rel.files["SHA256SUMS"] = b"00  groknmok.pyz\n"
        with self.assertRaises(selfupdate.UpdateError):
            selfupdate.verified_download(self.info(), SIGNER.pub_text())
        del self.rel.files["groknmok.pyz.minisig"]
        with self.assertRaises(selfupdate.UpdateError):
            selfupdate.verified_download(self.info(), SIGNER.pub_text())

    def test_older_or_equal_is_not_offered(self):
        for tag in ("v0.0.9", "v0.1.0"):
            self.rel.tag = tag
            self.assertFalse(selfupdate.newer(self.info()))

    def test_bad_tag(self):
        self.rel.tag = "dataset"
        with self.assertRaises(selfupdate.UpdateError):
            self.info()

    def test_daily_check_is_cached(self):
        self.rel.release("9.0.0")
        self.assertEqual(selfupdate.check(self.p)["version"], "9.0.0")
        self.rel.tag = "v9.9.9"
        self.assertEqual(selfupdate.check(self.p)["version"], "9.0.0")  # cached for a day
        self.assertEqual(selfupdate.check(self.p, force=True)["version"], "9.9.9")

    def test_no_terminal_never_replaces(self):
        self.assertIsNone(selfupdate.confirm("replace?"))  # stdin is not a terminal under unittest


if __name__ == "__main__":
    unittest.main()
