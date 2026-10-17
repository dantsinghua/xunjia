"""询价侧密封包。不连网，不用真实密钥。"""
from __future__ import annotations

import unittest

from imlink.crypto import open_sealed, seal

_KEY = b"0123456789abcdef0123456789abcdef"
_PLAIN = "固收群报价".encode()


class SealTest(unittest.TestCase):
    def test_roundtrip_hides_plaintext_and_key(self):
        blob = seal(_KEY, _PLAIN)
        self.assertNotIn(_PLAIN, blob)
        self.assertNotIn(_KEY, blob)
        self.assertEqual(open_sealed(_KEY, blob), _PLAIN)
        self.assertNotEqual(seal(_KEY, _PLAIN), blob)

    def test_wrong_key_does_not_return_plaintext(self):
        blob = seal(_KEY, _PLAIN)
        with self.assertRaises(Exception) as caught:
            open_sealed(b"fedcba9876543210fedcba9876543210", blob)
        self.assertNotIn("固收", str(caught.exception))
