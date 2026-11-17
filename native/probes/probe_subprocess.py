"""Probe: which subprocess invocation survives a --noconsole PE under Wine?

Writes each result to NPMC2_LOG so a windowed build can report anything at all.
"""
import os
import subprocess
import sys
import traceback

LOG = os.environ.get("PROBE_LOG", r"C:\Py312\probe.log")


def say(*parts):
    with open(LOG, "a", encoding="utf-8", errors="replace") as f:
        f.write(" ".join(str(p) for p in parts) + "\n")


def attempt(label, argv, **kw):
    try:
        r = subprocess.run(argv, timeout=20, **kw)
        out = (r.stdout or b"")[:40]
        say(label, "OK rc=", r.returncode, repr(out))
    except Exception as e:
        say(label, "FAIL", type(e).__name__, e)
        say(traceback.format_exc()[-400:])


def main():
    say("=== probe start; stdout is", sys.stdout, "stderr is", sys.stderr)
    say("std handles:", [os.isatty(i) for i in (0, 1, 2)])
    cmd = ["cmd.exe", "/c", "echo hi"]

    attempt("capture_output", cmd, capture_output=True)
    attempt("explicit-pipes", cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    attempt("pipes+stdin-pipe", cmd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, stdin=subprocess.PIPE)
    attempt("pipes+stdin-devnull", cmd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    attempt("pipes-inherit", cmd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, stdin=None)
    attempt("close_fds-false", cmd, capture_output=True, close_fds=False)
    attempt("shell-true", "echo hi", shell=True, capture_output=True)

    # does a child inheriting our rebound fds work?
    null = open(os.devnull, "w")
    attempt("stderr-to-null", cmd, stdout=subprocess.PIPE, stderr=null)
    say("=== done")


if __name__ == "__main__":
    main()
