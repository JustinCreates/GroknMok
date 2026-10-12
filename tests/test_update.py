"""Dataset updates against a local stand-in release: every failure keeps the
cached dataset; only having no dataset at all is an error."""

import hashlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from helpers import CORE_ITEMS, Release, Signer, item

from groknmok import client, dataset

SIGNER = Signer(b"\x11" * 32)
OTHER = Signer(b"\x22" * 32, key_id=bytes.fromhex("0807060504030201"))


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = os.path.join(self.tmp.name, "work")
        os.makedirs(self.work)
        os.environ["GROKNMOK_CONFIG_DIR"] = os.path.join(self.tmp.name, "cfg")
        os.environ["GROKNMOK_DATA_DIR"] = os.path.join(self.tmp.name, "data")
        self.p = client.paths()
        self.rel = Release()
        os.environ["GROKNMOK_DATASET_URL"] = self.rel.base

    def tearDown(self):
        self.rel.close()
        self.tmp.cleanup()

    def update(self, force=True):
        return client.update(self.p, force=force, pubkey=SIGNER.pub_text())

    def first(self):
        self.rel.publish(SIGNER, self.work, "20261011T030000Z")
        changed, notes = self.update()
        self.assertTrue(changed)
        return client.load_json(self.p.state, {})

    def test_first_download_and_search_structure(self):
        st = self.first()
        self.assertEqual(st["build"], "20261011T030000Z")
        conn, meta, st, use_cat = client.open_local(self.p)
        self.assertTrue(use_cat)
        doms = dataset.load_matches(conn, "telemon", False)
        self.assertEqual(sorted(doms), ["barn.example", "den.example"])
        self.assertIn("https://den.example/products/telemon-poster", doms["den.example"]["products"])  # key restored
        self.assertEqual(open(self.p.exclude_list).read(), "Proxy\n")

    def test_unchanged_downloads_nothing(self):
        self.first()
        self.rel.requests.clear()
        changed, notes = self.update()
        self.assertFalse(changed)
        self.assertEqual(notes, [])
        self.assertEqual(self.rel.requests, ["manifest.json", "manifest-20261011T030000Z.json.minisig"])

    def test_not_due_makes_no_request(self):
        self.first()
        self.rel.requests.clear()
        self.assertEqual(self.update(force=False), (False, []))
        self.assertEqual(self.rel.requests, [])

    def test_changed_core_downloads_one_part(self):
        self.first()
        cat_before = sha(self.p.catalogue)
        self.rel.requests.clear()
        self.rel.publish(SIGNER, self.work, "20261011T060000Z", cat_build="20261011T030000Z",
                         core_items=CORE_ITEMS[:2])
        changed, notes = self.update()
        self.assertTrue(changed)
        self.assertEqual([r for r in self.rel.requests if r.endswith(".xz")], ["core-20261011T060000Z.sqlite.xz"])
        self.assertEqual(sha(self.p.catalogue), cat_before)
        self.assertIn("core downloaded", notes[0])

    def assertKept(self, notes, text):
        self.assertEqual(len(notes), 1)
        self.assertIn(text, notes[0])
        self.assertIn("using the dataset from", notes[0])
        self.assertEqual(client.load_json(self.p.state, {})["build"], "20261011T030000Z")
        self.assertEqual(sha(self.p.core), self.core_sha)
        self.assertEqual([f for f in os.listdir(self.p.data_dir) if f.endswith(".tmp")], [])

    def kept_after(self, text, **publish):
        self.first()
        self.core_sha = sha(self.p.core)
        self.rel.publish(SIGNER, self.work, "20261011T060000Z", **publish)
        changed, notes = self.update()
        self.assertFalse(changed)
        self.assertKept(notes, text)

    def test_corrupt_download_refused(self):
        def corrupt(man, files):
            name = man["parts"][0]["asset"]
            files[name] = files[name][:-1] + bytes([files[name][-1] ^ 1])
        self.kept_after("does not match the manifest's sha256", tamper=corrupt)

    def test_oversize_download_refused(self):
        def grow(man, files):
            files[man["parts"][0]["asset"]] += b"x" * 10
        self.kept_after("larger than the manifest says", tamper=grow)

    def test_tampered_manifest_refused(self):
        def edit(man, files):
            files["manifest.json"] = files["manifest.json"].replace(b'"rows": 3', b'"rows": 4')
        self.kept_after("signature does not match", tamper=edit)

    def test_other_key_refused(self):
        def resign(man, files):
            files[man["signature"]] = OTHER.minisig(files["manifest.json"],
                                                    f"groknmok dataset manifest {man['build']}").encode()
        self.kept_after("made with another key", tamper=resign)

    def test_signature_for_other_build_refused(self):
        def resign(man, files):
            files[man["signature"]] = SIGNER.minisig(files["manifest.json"], "groknmok dataset manifest x").encode()
        self.kept_after("names another build", tamper=resign)

    def test_missing_signature_refused(self):
        self.kept_after("HTTP 404", tamper=lambda man, files: files.pop(man["signature"]))

    def test_manifest_404_kept(self):
        self.kept_after("manifest.json: HTTP 404", tamper=lambda man, files: files.pop("manifest.json"))

    def test_format_bump_asks_for_self_update(self):
        self.kept_after("run `groknmok self-update`", fmt=2)

    def test_min_client_version(self):
        self.kept_after("This dataset needs groknmok 9.0.0 or newer: run `groknmok self-update`",
                        min_client="9.0.0")

    def test_stale_manifest_kept_quietly(self):
        self.first()
        self.rel.publish(SIGNER, self.work, "20261011T060000Z")
        self.update()
        core = sha(self.p.core)
        self.rel.publish(SIGNER, self.work, "20261011T030000Z")  # the release URL lagging
        self.assertEqual(self.update(), (False, []))
        self.assertEqual(client.load_json(self.p.state, {})["build"], "20261011T060000Z")
        self.assertEqual(sha(self.p.core), core)

    def test_no_dataset_and_no_release_is_an_error(self):
        with self.assertRaises(client.UpdateError):
            self.update()

    def test_core_only(self):
        self.first()
        client.save_json(self.p.settings, {"core_only": True})
        self.rel.requests.clear()
        self.rel.publish(SIGNER, self.work, "20261011T060000Z")
        self.update()
        self.assertFalse(os.path.exists(self.p.catalogue))
        self.assertNotIn("catalogue-20261011T060000Z.sqlite.xz", self.rel.requests)
        conn, meta, st, use_cat = client.open_local(self.p)
        self.assertFalse(use_cat)
        self.assertNotIn("https://den.example/products/telemon-poster",
                         dataset.load_matches(conn, "telemon", False)["den.example"]["products"])

    def test_old_catalogue_with_unknown_domain(self):
        cat = [item("gone.example", "t", "Telemon Print", None, "unknown")]
        self.rel.publish(SIGNER, self.work, "20261011T030000Z", cat_items=cat)
        self.update()
        conn, *_ = client.open_local(self.p)
        self.assertNotIn("gone.example", dataset.load_matches(conn, "telemon", False))

    def test_search_cli_snapshot_mode(self):
        self.first()
        from groknmok import cli
        real, client.update = client.update, lambda p, force=False, **kw: (False, [])  # no network here
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main(["telemon", "--links", "never"])
        finally:
            client.update = real
        text = out.getvalue()
        self.assertIn("$119.00 CAD  Telemon Heavy Dreadnought  IN STOCK!", text)
        self.assertIn("not confirmed live", text)
        self.assertIn("snapshot data, not live-confirmed", text)
        self.assertIn("$97.75 USD (~$137 CAD)", text)


if __name__ == "__main__":
    unittest.main()
