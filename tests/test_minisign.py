"""minisign verification: RFC 8032, a real minisign 0.12 signature, refusals."""

import os
import unittest

from helpers import Signer

from groknmok import minisign

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def read(name, mode="r"):
    with open(os.path.join(FIX, name), mode) as fh:
        return fh.read()


class MinisignTest(unittest.TestCase):
    def test_rfc8032_test1(self):
        pk = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e3970"
                            "1cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertTrue(minisign.ed25519_verify(pk, b"", sig))
        self.assertFalse(minisign.ed25519_verify(pk, b"\x00", sig))

    def test_signature_made_by_minisign(self):
        self.assertEqual(minisign.verify(read("test.pub"), read("data.txt", "rb"), read("data.txt.minisig")),
                         "groknmok test fixture")

    def test_refusals(self):
        pub, data, sig = read("test.pub"), read("data.txt", "rb"), read("data.txt.minisig")
        cases = [(pub, data + b"!", sig), (pub, data, sig.replace("fixture", "fixturE")),
                 (Signer(b"\x05" * 32).pub_text(), data, sig), (pub, data, read("data.txt.legacy.minisig")),
                 (pub, data, "garbage"), ("not a key", data, sig)]
        for args in cases:
            with self.assertRaises(minisign.BadSignature):
                minisign.verify(*args)


if __name__ == "__main__":
    unittest.main()
