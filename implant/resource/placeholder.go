//go:build ignore

// Resource script generator: emits a .syso that embeds the Node.js icon,
// version info and an asInvoker manifest into the PE.
//
// build.py runs windres against node.rc when mingw is available; otherwise
// the compiled .syso files in this directory are used as-is.
package main
