##### Abusing npm Registry for C2

Commands are encoded as npm dist-tag names on a controlled package. The
implant polls the public registry API, reads tags matching a known prefix,
executes the decoded command, and optionally writes results back as new
tags. No authentication is needed to read tags.

![npm registry](npm_logo.png)

#### How It Works

npm dist-tags are key-value pairs attached to published packages. They
normally map human-readable names to version strings (e.g. "latest" ->
"1.0.0"). The registry allows arbitrary tag names up to 250 characters,
restricted to [a-zA-Z0-9._-]. The operator creates tags that look like
CI build artifacts. The value is always "1.0.0" (the only published
version). All data is in the tag name.

Tag naming convention:

    <prefix><nonce>_<base64url(payload)>

Prefixes distinguish tag types:

| prefix | meaning | who writes |
|--------|---------|------------|
| x64- | command | operator |
| arm64- | result | implant |
| ia32- | heartbeat | implant |

Communication flow:

    operator:  PUT /-/package/<name>/dist-tags/x64-<nonce>_<b64cmd>
    implant:   GET  /-/package/<name>/dist-tags  (no auth needed)
    implant:   decode x64-* tags, execute commands
    implant:   PUT /-/package/<name>/dist-tags/arm64-<nonce>_<b64out>
    implant:   PUT /-/package/<name>/dist-tags/ia32-<nonce>
    operator:  GET  /-/package/<name>/dist-tags
    operator:  decode arm64-* tags, read output

The implant polls every N seconds with jitter. Every 10 cycles it sends
a heartbeat tag (ia32-<nonce>) to signal liveness.

#### Why It Is Hard To Detect

1. Traffic goes to registry.npmjs.org, which is whitelisted by most
   corporate proxies for npm install and update operations.

2. HTTP requests to read dist-tags are GETs to a Cloudflare-cached
   endpoint, identical to what npm outdated, npm view, or Dependabot
   produce.

3. No credentials required for reading. The implant never stores or
   transmits secrets on the read path.

4. dist-tags are invisible in the npm web UI. They appear only in the
   API response and are not shown on package pages or README files.

5. Tag names look like platform-specific build artifacts (x64-a3f2c8,
   arm64-b7e91d). They do not stand out in registry logs.

6. The published package contains only a package.json. No malicious
   code is ever published to the registry. Only metadata tags change.

#### Key Detection Opportunities

1. A non-Node.js process making periodic GET requests to
   registry.npmjs.org is anomalous on machines where Node.js
   development is not standard.

2. The polling pattern (regular intervals with jitter) is detectable
   through statistical analysis of outbound HTTPS timing.

3. User-Agent mismatch: Python's urllib sends "Python-urllib/3.x"
   by default, which differs from the npm CLI User-Agent
   "npm/<version> node/<version> <platform>".

4. TLS fingerprint (JA3) of Python differs from Node.js (which uses
   Schannel on Windows).

5. Process name: python.exe or a non-Node binary making npm registry
   calls stands out compared to node.exe.

6. An npm token stored on disk or in memory in a non-Node.js context
   suggests abuse.

7. A package with only version 1.0.0 but accumulating hundreds of
   dist-tags that change every few minutes.

#### API Detection

```
GET Requests to:
https://registry.npmjs.org/-/package/*/dist-tags

PUT Requests to:
https://registry.npmjs.org/-/package/*/dist-tags/*

DELETE Requests to:
https://registry.npmjs.org/-/package/*/dist-tags/*
```

#### References

- npm dist-tags API: https://github.com/npm/registry/blob/master/docs/SPEC.md
- npm granular access tokens: https://docs.npmjs.com/creating-and-viewing-access-tokens
- npm-c2: https://github.com/ikarus981/npm-c2
