#!/usr/bin/env python3
"""Doctor checks against the mock registry: token shape, write access, tag length."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import npm_c2
import mock_registry
from npm_c2.doctor import _run_checks

FAILS = []


def step(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main():
    reg = mock_registry.Registry()
    srv, base = mock_registry.serve(reg, auth_token="npm_good")
    npm_c2.API = base + "/-/package/{}/dist-tags"
    # doctor builds its own URL from API, so patch at the module level it imports
    import npm_c2.doctor as doc
    doc.API = npm_c2.API

    pkg = "doc/pkg"
    reg.tags.setdefault(pkg, {})["latest"] = "1.0.0"

    checks = {c.name: c for c in _run_checks(pkg, "npm_good", base=npm_c2.API)}
    step("token shape accepted", checks["token shape"].ok)
    step("package reachable", checks["package reachable (no auth)"].ok)
    step("write access confirmed", checks["token can write a dist-tag"].ok)
    step("tag length probed", "max tag name length" in checks,
         checks.get("max tag name length", type("X", (), {"detail": ""})()).detail)

    bad = {c.name: c for c in _run_checks(pkg, "npm_wrong", base=npm_c2.API)}
    step("wrong token fails the write check", not bad["token can write a dist-tag"].ok,
         bad["token can write a dist-tag"].detail[:50])

    missing = {c.name: c for c in _run_checks("nope/missing", "npm_good",
                                              base=npm_c2.API)}
    step("missing package is fatal", not missing["package reachable (no auth)"].ok
         and missing["package reachable (no auth)"].fatal)

    anon = {c.name: c for c in _run_checks(pkg, None, base=npm_c2.API)}
    step("no token is a warning, not a failure",
         not anon["token present"].ok and not anon["token present"].fatal)

    # probes must not survive the run
    leftovers = [t for t in reg.snapshot(pkg) if t.startswith("zzprobe-")]
    step("doctor cleans up its probe tags", leftovers == [], f"{leftovers}")

    srv.shutdown()
    print(f"\n{'doctor checks behave' if not FAILS else 'failing: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
