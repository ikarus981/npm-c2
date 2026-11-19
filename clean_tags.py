#!/usr/bin/env python3
"""Delete every dist-tag on a package except the ones you keep.

Needed because npm refuses DELETE to a granular token created with
--bypass-2fa ("may not perform this action"), no matter how many tags have
accumulated. Run this with a token that is allowed to delete: a classic
read-write token, or a granular token created WITHOUT bypass-2fa while you
have a 2FA challenge in hand.

    ./clean_tags.py -p example-pkg -t npm_xxx            # dry run
    ./clean_tags.py -p example-pkg -t npm_xxx --apply    # actually

Keeps 'latest' unless told otherwise, and stops after the first hard refusal
instead of hammering the registry.
"""
import argparse
import sys
import time

import requests

REGISTRY = "https://registry.npmjs.org"
UA = "npm/11.18.0 node/v22.7.0 win32 x64"


def fetch(session, base, pkg):
    url = f"{base}/-/package/{pkg}/dist-tags"
    r = session.get(url, timeout=20)
    if r.status_code == 404:
        sys.exit(f"[-] package {pkg!r} not found")
    if r.status_code in (401, 403):
        sys.exit(f"[-] token rejected on read ({r.status_code})")
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", "-p", required=True)
    ap.add_argument("--token", "-t", required=True)
    ap.add_argument("--base", default=REGISTRY,
                    help="registry base (default npmjs)")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without this it is a dry run")
    ap.add_argument("--keep", action="append", default=[],
                    help="tag to keep (default: latest)")
    ap.add_argument("--sleep", type=float, default=0.25,
                    help="pause between deletes (default 0.25s)")
    a = ap.parse_args()

    keep = set(a.keep) or {"latest"}
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {a.token}", "User-Agent": UA})

    tags = fetch(s, a.base, a.package)
    targets = sorted(t for t in tags if t not in keep)
    print(f"[i] {len(tags)} tag(s) on {a.package}, {len(targets)} to remove, "
          f"keeping {sorted(keep)}")
    if not targets:
        print("[i] nothing to do")
        return 0

    if not a.apply:
        for t in targets:
            print(f"    would delete {t}")
        print(f"\n[i] dry run: {len(targets)} tag(s) untouched. "
              f"Re-run with --apply.")
        return 0

    removed = failed = 0
    for i, t in enumerate(targets, 1):
        url = f"{a.base}/-/package/{a.package}/dist-tags/{t}"
        try:
            r = s.delete(url, timeout=20)
        except requests.RequestException as e:
            print(f"[-] {t}: {e}", file=sys.stderr)
            failed += 1
            continue
        if r.status_code in (200, 201, 204, 404):
            removed += 1
            print(f"[+] {i}/{len(targets)} {t}")
        elif r.status_code in (401, 403):
            # The token type cannot delete. No point trying the rest.
            print(f"\n[-] refused ({r.status_code}): {r.text[:200]}")
            print("[-] this token is not allowed to delete tags. "
                  "Stopping rather than hammering the registry.")
            failed += len(targets) - i + 1
            break
        else:
            print(f"[-] {t}: HTTP {r.status_code}", file=sys.stderr)
            failed += 1
        time.sleep(a.sleep)

    print(f"\nsummary: {removed} removed, {failed} failed")
    left = fetch(s, a.base, a.package)
    print(f"remaining: {len(left)} tag(s): {sorted(left)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
