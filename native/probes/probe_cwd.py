"""Probe 3: replicate Implant._run exactly, including cwd and argv."""
import os
import platform
import subprocess
import sys
import traceback

LOG = os.environ.get("PROBE_LOG", r"C:\Py312\probe3.log")
WINDOWS = platform.system() == "Windows"


def say(*p):
    with open(LOG, "a", encoding="utf-8", errors="replace") as f:
        f.write(" ".join(str(x) for x in p) + "\n")


def attempt(label, body, cwd, **kw):
    argv = ["cmd.exe", "/c", body] if WINDOWS else ["/bin/sh", "-c", body]
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL, **kw)
        say(label, "OK rc=", r.returncode, repr((r.stdout or b"")[:30]))
    except Exception as e:
        say(label, "FAIL", type(e).__name__, e)
        say(traceback.format_exc()[-500:])


def main():
    say("platform:", platform.system(), "expanduser:", os.path.expanduser("~"))
    say("cwd exists?", os.path.isdir(os.path.expanduser("~")))
    attempt("home-cwd", "echo hi", os.path.expanduser("~"))
    attempt("none-cwd", "echo hi", None)
    attempt("cwd-dot", "echo hi", ".")

    # a cwd that does not exist
    bogus = os.path.join(os.path.expanduser("~"), "no-such-dir-xyz")
    attempt("bogus-cwd", "echo hi", bogus)

    # write a file with redirection, the real command from the test
    target = r"C:\Py312\win-marker"
    attempt("redirect-file", f"echo win-ok > {target}", None)
    say("marker exists?", os.path.exists(target))
    say("end")


if __name__ == "__main__":
    main()
