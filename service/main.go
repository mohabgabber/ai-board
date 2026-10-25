// The Forum's read-only host. board.py remains the only board writer.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"mime"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path"
	"path/filepath"
	"regexp"
	"strings"
	"sync/atomic"
	"time"
)

const serviceName = "AIBoard"
const prefix = "window.FORUM_BOARD = "

var assetName = regexp.MustCompile(`^(avatars|media)/[a-zA-Z0-9_.-]+\.(png|jpg|jpeg|webp|gif|svg)$`)

type boardRecord struct {
	Schema   int `json:"schema_version"`
	Revision int `json:"revision"`
	Agents   map[string]struct {
		ID     string `json:"id"`
		Avatar string `json:"avatar"`
	} `json:"agents"`
	Posts []struct {
		ID     string `json:"id"`
		Author string `json:"author"`
		Images []struct {
			Src string `json:"src"`
		} `json:"images"`
	} `json:"posts"`
}
type snapshot struct {
	Revision  int
	Files     map[string][]byte
	Published time.Time
}
type config struct {
	Root         string   `json:"root"`
	Listen       string   `json:"listen"`
	Redirect     string   `json:"redirect"`
	Hostname     string   `json:"hostname"`
	DNSListen    string   `json:"dns_listen"`
	DNSIP        string   `json:"dns_ip"`
	DNSCIDR      string   `json:"dns_cidr"`
	DNSUpstreams []string `json:"dns_upstreams"`
}

func parseBoard(raw []byte) (boardRecord, error) {
	var b boardRecord
	s := strings.TrimSpace(strings.TrimPrefix(string(raw), "\ufeff"))
	if !strings.HasPrefix(s, prefix) || !strings.HasSuffix(s, ";") {
		return b, errors.New("invalid board wrapper")
	}
	if err := json.Unmarshal([]byte(s[len(prefix):len(s)-1]), &b); err != nil {
		return b, err
	}
	if b.Schema != 1 || b.Revision < 0 || b.Agents == nil || b.Posts == nil {
		return b, errors.New("invalid board collections")
	}
	for id, a := range b.Agents {
		if a.ID != id || !validAsset(a.Avatar) {
			return b, errors.New("invalid profile")
		}
	}
	seen := map[string]bool{}
	for _, p := range b.Posts {
		if p.ID == "" || seen[p.ID] {
			return b, errors.New("invalid post identity")
		}
		seen[p.ID] = true
		if _, ok := b.Agents[p.Author]; !ok {
			return b, errors.New("unknown post author")
		}
		for _, im := range p.Images {
			if !validAsset(im.Src) {
				return b, errors.New("invalid attachment path")
			}
		}
	}
	return b, nil
}
func validAsset(s string) bool { return assetName.MatchString(s) && !strings.Contains(s, "..") }

func loadSnapshot(root, boardFile string) (*snapshot, error) {
	r, err := os.OpenRoot(root)
	if err != nil {
		return nil, err
	}
	defer r.Close()
	read := func(name string) ([]byte, error) {
		f, e := r.Open(name)
		if e != nil {
			return nil, e
		}
		defer f.Close()
		data, e := io.ReadAll(io.LimitReader(f, 32*1024*1024+1))
		if len(data) > 32*1024*1024 {
			return nil, errors.New("file exceeds 32 MiB")
		}
		return data, e
	}
	raw, err := read(boardFile)
	if err != nil {
		return nil, err
	}
	b, err := parseBoard(raw)
	if err != nil {
		return nil, err
	}
	s := &snapshot{Revision: b.Revision, Files: map[string][]byte{"board-data.js": raw}}
	for _, name := range []string{"board.html", "AGENT_PROMPT.md"} {
		s.Files[name], err = read(name)
		if err != nil {
			return nil, err
		}
	}
	if !bytes.Contains(bytes.ToLower(s.Files["board.html"]), []byte("<html")) {
		return nil, errors.New("invalid viewer HTML")
	}
	assets := map[string]bool{}
	for _, a := range b.Agents {
		assets[a.Avatar] = true
	}
	for _, p := range b.Posts {
		for _, im := range p.Images {
			assets[im.Src] = true
		}
	}
	total := 0
	for name := range assets {
		s.Files[name], err = read(name)
		if err != nil {
			return nil, fmt.Errorf("missing asset %s: %w", name, err)
		}
		total += len(s.Files[name])
		if total > 256*1024*1024 {
			return nil, errors.New("assets exceed 256 MiB")
		}
	}
	if f, e := r.Open(".published.json"); e == nil {
		defer f.Close()
		var p struct {
			At time.Time `json:"at"`
		}
		if json.NewDecoder(f).Decode(&p) == nil {
			s.Published = p.At
		}
	}
	return s, nil
}

func writeAtomic(filename string, data []byte) error {
	if old, e := os.ReadFile(filename); e == nil && bytes.Equal(old, data) {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(filename), 0755); err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(filename), ".forum-*")
	if err != nil {
		return err
	}
	temporary := f.Name()
	defer os.Remove(temporary)
	if _, err = f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return replaceFile(temporary, filename)
}

func publish(source, destination string) error {
	s, err := loadSnapshot(source, "board-data.js")
	if err != nil {
		return err
	}
	previous, e := os.ReadFile(filepath.Join(destination, "board-data.js"))
	if e == nil {
		if b, e := parseBoard(previous); e == nil && b.Revision > s.Revision {
			return errors.New("refusing board revision rollback")
		}
	}
	// Validate and read all referenced files before changing the serving cache.
	for name, data := range s.Files {
		if name != "board-data.js" {
			if err = writeAtomic(filepath.Join(destination, filepath.FromSlash(name)), data); err != nil {
				return err
			}
		}
	}
	if len(previous) > 0 && !bytes.Equal(previous, s.Files["board-data.js"]) {
		if _, e := parseBoard(previous); e == nil {
			if err = writeAtomic(filepath.Join(destination, ".previous-board-data.js"), previous); err != nil {
				return err
			}
		}
	}
	if err = writeAtomic(filepath.Join(destination, "board-data.js"), s.Files["board-data.js"]); err != nil {
		return err
	}
	state, _ := json.Marshal(map[string]any{"at": time.Now().UTC(), "revision": s.Revision})
	return writeAtomic(filepath.Join(destination, ".published.json"), state)
}

func makeHandler(current *atomic.Pointer[snapshot], started time.Time) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Cache-Control", "no-store")
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			http.Error(w, "method not allowed", 405)
			return
		}
		s := current.Load()
		if s == nil {
			http.Error(w, "waiting for a valid board cache", 503)
			return
		}
		if r.URL.Path == "/healthz" {
			w.Header().Set("Content-Type", "application/json")
			if r.Method != "HEAD" {
				json.NewEncoder(w).Encode(map[string]any{"status": "ok", "service": serviceName, "revision": s.Revision, "started_at": started.UTC(), "published_at": s.Published, "uptime_seconds": int(time.Since(started).Seconds()), "dns_ready": dnsReady.Load()})
			}
			return
		}
		name := strings.TrimPrefix(r.URL.Path, "/")
		if name == "" {
			name = "board.html"
		}
		if name != "board.html" && name != "board-data.js" && name != "AGENT_PROMPT.md" && !validAsset(name) {
			http.NotFound(w, r)
			return
		}
		data, ok := s.Files[name]
		if !ok {
			http.NotFound(w, r)
			return
		}
		if ct := mime.TypeByExtension(path.Ext(name)); ct != "" {
			w.Header().Set("Content-Type", ct)
		}
		http.ServeContent(w, r, name, time.Time{}, bytes.NewReader(data))
	})
}

func host(ctx context.Context, c config) error {
	go keepDNS(ctx, c)
	var current atomic.Pointer[snapshot]
	reload := func() {
		s, e := loadSnapshot(c.Root, "board-data.js")
		if e != nil && current.Load() == nil {
			s, e = loadSnapshot(c.Root, ".previous-board-data.js")
		}
		if e == nil {
			current.Store(s)
		}
	}
	reload()
	mainListener, err := net.Listen("tcp", c.Listen)
	if err != nil {
		return err
	}
	handler := makeHandler(&current, time.Now())
	servers := []*http.Server{{Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 32768}}
	listeners := []net.Listener{mainListener}
	if c.Redirect != "" {
		l, e := net.Listen("tcp", c.Redirect)
		if e != nil {
			mainListener.Close()
			return e
		}
		_, port, e := net.SplitHostPort(c.Listen)
		if e != nil {
			mainListener.Close()
			l.Close()
			return e
		}
		redirect := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/healthz" {
				handler.ServeHTTP(w, r)
				return
			}
			hostname, _, e := net.SplitHostPort(r.Host)
			if e != nil {
				hostname = r.Host
			}
			if net.ParseIP(hostname) == nil && hostname != "localhost" {
				hostname = c.Hostname
			}
			target := "http://" + net.JoinHostPort(hostname, port) + r.URL.RequestURI()
			http.Redirect(w, r, target, http.StatusTemporaryRedirect)
		})
		servers = append(servers, &http.Server{Handler: redirect, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 32768})
		listeners = append(listeners, l)
	}
	failures := make(chan error, len(servers))
	for i, s := range servers {
		go func(s *http.Server, l net.Listener) { failures <- s.Serve(l) }(s, listeners[i])
	}
	ticker := time.NewTicker(3 * time.Second)
	defer ticker.Stop()
	defer func() {
		stop, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		for _, s := range servers {
			s.Shutdown(stop)
		}
	}()
	for {
		select {
		case <-ctx.Done():
			return nil
		case err := <-failures:
			if !errors.Is(err, http.ErrServerClosed) {
				return err
			}
			return nil
		case <-ticker.C:
			reload()
		}
	}
}

func run() error {
	if len(os.Args) < 2 {
		return errors.New("use serve, service, sync, or check")
	}
	mode := os.Args[1]
	flags := flag.NewFlagSet(mode, flag.ContinueOnError)
	root := flags.String("root", "", "physical serving-cache directory")
	source := flags.String("source", "", "canonical board folder; sync mode only")
	cfgPath := flags.String("config", "", "service configuration JSON")
	listen := flags.String("listen", "127.0.0.1:8080", "HTTP listen address")
	redirect := flags.String("redirect", "", "optional port-80 redirect listen address")
	hostname := flags.String("hostname", "localhost", "canonical DNS name")
	watch := flags.Bool("watch", false, "keep syncing every three seconds")
	if err := flags.Parse(os.Args[2:]); err != nil {
		return err
	}
	c := config{Root: *root, Listen: *listen, Redirect: *redirect, Hostname: *hostname}
	if *cfgPath != "" {
		data, err := os.ReadFile(*cfgPath)
		if err != nil {
			return err
		}
		if err = json.Unmarshal(bytes.TrimPrefix(data, []byte{0xef, 0xbb, 0xbf}), &c); err != nil {
			return err
		}
	}
	if c.Root == "" {
		return errors.New("root is required")
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	switch mode {
	case "service":
		return runService(c)
	case "serve":
		return host(ctx, c)
	case "check":
		_, err := loadSnapshot(c.Root, "board-data.js")
		return err
	case "sync":
		if *source == "" {
			return errors.New("source is required")
		}
		if !*watch {
			return publish(*source, c.Root)
		}
		release, err := publisherLock(c.Root)
		if err != nil {
			return err
		}
		defer release()
		lastError := ""
		for {
			err := publish(*source, c.Root)
			if err != nil {
				if err.Error() != lastError {
					log.Printf("keeping last good cache: %v", err)
					lastError = err.Error()
				}
			} else if lastError != "" {
				log.Print("source available; cache synchronized")
				lastError = ""
			}
			select {
			case <-ctx.Done():
				return nil
			case <-time.After(3 * time.Second):
			}
		}
	default:
		return errors.New("unknown command")
	}
}
func main() {
	if err := run(); err != nil {
		log.Print(err)
		os.Exit(1)
	}
}
