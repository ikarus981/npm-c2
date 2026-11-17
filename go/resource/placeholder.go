//go:build ignore

// Resource script generator: emits a .syso that embeds the Node.js icon,
// version info and an asInvoker manifest into the PE.
//
// Run from native/build_windows.py via windres; this file documents the .rc.
package main
