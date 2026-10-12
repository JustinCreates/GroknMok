"""Client watches on a local stand-in release, alerts to a local ntfy stand-in."""

import http.server
import json
import os
import sqlite3
import tempfile
import threading
import unittest

from helpers import Release, Signer

from groknmok import client, watch, watches

SIGNER = Signer(b"\x11" * 32)


class Ntfy:
    def __init__(self):
        self.posts = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                outer.posts.append((self.headers.get("Title"), body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        work = os.path.join(self.tmp.name, "work")
        os.makedirs(work)
        os.environ["GROKNMOK_CONFIG_DIR"] = os.path.join(self.tmp.name, "cfg")
        os.environ["GROKNMOK_DATA_DIR"] = os.path.join(self.tmp.name, "data")
        self.p = client.paths()
        self.rel, self.ntfy = Release(), Ntfy()
        os.environ["GROKNMOK_DATASET_URL"] = self.rel.base
        self.rel.publish(SIGNER, work, "20261011T030000Z")
        client.update(self.p, force=True, pubkey=SIGNER.pub_text())
        os.makedirs(self.p.config_dir)
        with open(self.p.ntfy, "w") as fh:
            json.dump({"server": self.ntfy.url, "topic": "t"}, fh)
        os.chmod(self.p.ntfy, 0o600)

    def tearDown(self):
        self.rel.close()
        self.ntfy.server.shutdown()
        self.ntfy.server.server_close()
        self.tmp.cleanup()

    def set_status(self, key, status):
        con = sqlite3.connect(self.p.core)
        con.execute("UPDATE items SET status = ? WHERE product_key = ?", (status, key))
        con.commit()
        con.close()

    def test_baseline_then_restock_alerts_once(self):
        wid = watch.add_watch(self.p.watch_file, self.p.watch_state, "telemon", country="CA")
        self.assertEqual(wid, "w1")
        self.assertEqual(watches.run(self.p, "manual"), 0)  # silent baseline
        self.assertEqual(self.ntfy.posts, [])
        self.set_status("telemon", "out_of_stock")
        watches.run(self.p, "manual")
        self.set_status("telemon", "in_stock")
        watches.run(self.p, "manual")
        self.assertEqual(len(self.ntfy.posts), 1)
        title, body = self.ntfy.posts[0]
        self.assertEqual(title, "groknmok: telemon (1)")
        self.assertIn("Hobby Den, Ottawa ON: $119.00 CAD - in stock (newly in stock)", body)
        watches.run(self.p, "manual")
        self.assertEqual(len(self.ntfy.posts), 1)  # no repeat

    def test_if_new_skips_a_checked_build(self):
        watch.add_watch(self.p.watch_file, self.p.watch_state, "telemon")
        watches.run(self.p, "manual")
        st = client.load_json(self.p.state, {})
        self.assertEqual(st["watched_build"], "20261011T030000Z")
        state_before = open(self.p.watch_state).read()
        watches.run(self.p, "manual", if_new=True)
        self.assertEqual(open(self.p.watch_state).read(), state_before)

    def test_units(self):
        u = watches.units()
        self.assertIn("watch run --mode timer --if-new", u["groknmok-watch.service"])
        self.assertIn("OnUnitActiveSec=1h", u["groknmok-watch.timer"])


if __name__ == "__main__":
    unittest.main()
