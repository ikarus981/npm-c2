"""Probe 2: does rebinding fds 1/2 break a child when a stream already exists?"""
import os
import subprocess
import sys

LOG = os.environ.get("PROBE_LOG", r"C:\Py312\probe2.log")


def say(*p):
    with open(LOG, "a", encoding="utf-8", errors="replace") as f:
        f.write(" ".join(str(x) for x in p) + "\n")


def attempt(label):
    try:
        r = subprocess.run(["cmd.exe", "/c", "echo hi"], capture_output=True, timeout=20,
                           stdin=subprocess.DEVNULL)
        say(label, "OK", repr(r.stdout[:12]))
    except Exception as e:
        say(label, "FAIL", type(e).__name__, e)


def main():
    say("start: stdout=", type(sys.stdout).__name__, "stderr=", type(sys.stderr).__name__)
    attempt("before-dup2")

    target = open(os.devnull, "w")
    try:
        fd = target.fileno()
        for t in (1, 2):
            os.dup2(fd, t)
        say("dup2 done onto devnull")
    except OSError as e:
        say("dup2 failed", e)
    attempt("after-dup2")

    # the alternative: leave the fds alone, only fill the None streams
    say("sys.stdout still usable?", sys.stdout is not None)
    say("end")


if __name__ == "__main__":
    main()
