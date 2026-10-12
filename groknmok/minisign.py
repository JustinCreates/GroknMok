"""Verify minisign signatures in pure Python (no minisign binary needed).

Supports what `minisign -S` writes by default: a prehashed signature
(algorithm "ED", BLAKE2b-512 of the file signed with Ed25519) plus the signed
trusted comment. Legacy unhashed signatures ("Ed") are refused.

The Ed25519 verification is the reference algorithm of RFC 8032, section 6,
with the curve arithmetic in extended coordinates. It is slow by crypto
standards (tens of milliseconds) and only ever checks public data, so it
needs no constant-time care."""

import base64
import binascii
import hashlib

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


class BadSignature(Exception):
    pass


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    if x2 % _P == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _add(a, b):
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    pa = (y1 - x1) * (y2 - x2) % _P
    pb = (y1 + x1) * (y2 + x2) % _P
    pc = 2 * t1 * t2 * _D % _P
    pd = 2 * z1 * z2 % _P
    e, f, g, h = pb - pa, pd - pc, pd + pc, pb + pa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s, pt):
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _same(a, b):
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _point(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def ed25519_verify(public, message, signature):
    """True when signature (64 bytes) is public's (32 bytes) Ed25519 signature of message."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a, r = _point(public), _point(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _L
    return _same(_mul(s, _G), _add(r, _mul(h, a)))


def _b64(text, size, what):
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise BadSignature(f"{what} is not valid base64") from None
    if len(raw) != size:
        raise BadSignature(f"{what} has {len(raw)} bytes, expected {size}")
    return raw


def public_key(text):
    """(key id, 32-byte key) from a minisign public key: the base64 line alone, or
    the whole .pub file."""
    lines = [x for x in text.strip().splitlines() if x.strip() and not x.startswith("untrusted comment:")]
    if len(lines) != 1:
        raise BadSignature("public key: expected one base64 line")
    raw = _b64(lines[0], 42, "public key")
    if raw[:2] != b"Ed":
        raise BadSignature("public key: not an Ed25519 minisign key")
    return raw[2:10], raw[10:]


def verify(public, data, sig_text):
    """Check a .minisig (text) over data (bytes) with a minisign public key (text).
    Returns the trusted comment; raises BadSignature on any failure."""
    key_id, key = public_key(public)
    lines = sig_text.replace("\r\n", "\n").split("\n")
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:") \
            or not lines[2].startswith("trusted comment: "):
        raise BadSignature("signature file: unexpected layout")
    sig = _b64(lines[1], 74, "signature")
    if sig[:2] != b"ED":
        raise BadSignature("signature: only prehashed (ED) signatures are accepted")
    if sig[2:10] != key_id:
        raise BadSignature("signature: made with another key")
    if not ed25519_verify(key, hashlib.blake2b(data, digest_size=64).digest(), sig[10:]):
        raise BadSignature("signature does not match the data")
    trusted = lines[2][len("trusted comment: "):]
    if not ed25519_verify(key, sig[10:] + trusted.encode("utf-8"), _b64(lines[3], 64, "global signature")):
        raise BadSignature("trusted comment signature does not match")
    return trusted
