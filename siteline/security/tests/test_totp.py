"""RFC 4226 / RFC 6238 test vectors for the standard-library TOTP."""
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase

from security import totp


class HotpVectorTests(SimpleTestCase):
    def test_rfc4226_appendix_d(self):
        key = b'12345678901234567890'
        expected = ['755224', '287082', '359152', '969429', '338314',
                    '254676', '287922', '162583', '399871', '520489']
        self.assertEqual([totp.hotp(key, c) for c in range(10)], expected)


class TotpVectorTests(SimpleTestCase):
    """RFC 6238 Appendix B: 8 digits, SHA-1, SHA-256 and SHA-512 seeds."""
    SEEDS = {
        hashlib.sha1: b'12345678901234567890',
        hashlib.sha256: b'12345678901234567890123456789012',
        hashlib.sha512: b'1234567890123456789012345678901234567890123456789012345678901234',
    }
    VECTORS = [
        (59, '94287082', '46119246', '90693936'),
        (1111111109, '07081804', '68084774', '25091201'),
        (1111111111, '14050471', '67062674', '99943326'),
        (1234567890, '89005924', '91819424', '93441116'),
        (2000000000, '69279037', '90698825', '38618901'),
        (20000000000, '65353130', '77737706', '47863826'),
    ]

    def test_appendix_b(self):
        for t, sha1, sha256, sha512 in self.VECTORS:
            for digest, want in ((hashlib.sha1, sha1), (hashlib.sha256, sha256), (hashlib.sha512, sha512)):
                self.assertEqual(totp.hotp(self.SEEDS[digest], t // 30, digits=8, digest=digest), want, (t, digest))

    def test_base32_secret_round_trip(self):
        secret = base64.b32encode(self.SEEDS[hashlib.sha1]).decode().rstrip('=')
        self.assertEqual(totp.code_at(secret, now=59), '287082')  # the 6-digit tail of 94287082

    def test_new_secret_is_160_bits_base32(self):
        s = totp.new_secret()
        self.assertEqual(len(base64.b32decode(s + '=' * (-len(s) % 8))), 20)
        self.assertNotEqual(s, totp.new_secret())


class MatchingTests(SimpleTestCase):
    secret = base64.b32encode(b'12345678901234567890').decode().rstrip('=')

    def test_accepts_one_step_either_side_only(self):
        now = 1111111111
        step = now // 30
        for offset, ok in ((-2, False), (-1, True), (0, True), (1, True), (2, False)):
            code = totp.hotp(b'12345678901234567890', step + offset)
            self.assertEqual(totp.matching_step(self.secret, code, now=now) == step + offset, ok, offset)

    def test_spaces_allowed_wrong_length_refused(self):
        code = totp.code_at(self.secret, now=59)
        self.assertIsNotNone(totp.matching_step(self.secret, f'{code[:3]} {code[3:]}', now=59))
        self.assertIsNone(totp.matching_step(self.secret, code[:5], now=59))
        self.assertIsNone(totp.matching_step(self.secret, '', now=59))
        self.assertIsNone(totp.matching_step(self.secret, 'abcdef', now=59))

    def test_provisioning_uri(self):
        uri = totp.provisioning_uri(self.secret, 'owner@demo.test', 'Siteline')
        parts = urlsplit(uri)
        self.assertEqual((parts.scheme, parts.netloc), ('otpauth', 'totp'))
        self.assertEqual(parts.path, '/Siteline:owner@demo.test')
        q = parse_qs(parts.query)
        self.assertEqual(q['secret'], [self.secret])
        self.assertEqual(q['issuer'], ['Siteline'])
        self.assertEqual((q['digits'], q['period'], q['algorithm']), (['6'], ['30'], ['SHA1']))
