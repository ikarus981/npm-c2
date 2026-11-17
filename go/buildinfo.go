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
)
