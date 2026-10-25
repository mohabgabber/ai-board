package main

import (
	"bytes"
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func fixture(t *testing.T, root string, revision int) {
	t.Helper()
	for name, data := range map[string][]byte{
		"board.html":      []byte("<!doctype html><html><body>The Forum</body></html>"),
		"AGENT_PROMPT.md": []byte("A test prompt"),
		"avatars/cat.png": []byte("test image bytes"),
	} {
		if err := writeAtomic(filepath.Join(root, filepath.FromSlash(name)), data); err != nil {
			t.Fatal(err)
		}
	}
	b := map[string]any{"schema_version": 1, "revision": revision, "agents": map[string]any{"tester": map[string]any{"id": "tester", "avatar": "avatars/cat.png"}}, "posts": []any{}}
	raw, _ := json.Marshal(b)
	if err := writeAtomic(filepath.Join(root, "board-data.js"), append(append([]byte(prefix), raw...), ';')); err != nil {
		t.Fatal(err)
	}
}

func TestPublisherPreservesLastGoodBoardOnSourceFailure(t *testing.T) {
	source, dest := t.TempDir(), t.TempDir()
	fixture(t, source, 2)
	if err := publish(source, dest); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(filepath.Join(dest, "board-data.js"))
	os.WriteFile(filepath.Join(source, "board-data.js"), []byte("corrupt"), 0600)
	if publish(source, dest) == nil {
		t.Fatal("accepted corrupt board")
	}
	after, _ := os.ReadFile(filepath.Join(dest, "board-data.js"))
	if !bytes.Equal(before, after) {
		t.Fatal("changed last good board")
	}
	fixture(t, source, 3)
	os.Remove(filepath.Join(source, "avatars", "cat.png"))
	if publish(source, dest) == nil {
		t.Fatal("accepted missing asset")
	}
	after, _ = os.ReadFile(filepath.Join(dest, "board-data.js"))
	if !bytes.Equal(before, after) {
		t.Fatal("published an incomplete board")
	}
	fixture(t, source, 1)
	if publish(source, dest) == nil {
		t.Fatal("allowed revision rollback")
	}
}

func TestPublisherRepairsCacheAndRetainsPreviousRevision(t *testing.T) {
	source, dest := t.TempDir(), t.TempDir()
	fixture(t, source, 1)
	if e := publish(source, dest); e != nil {
		t.Fatal(e)
	}
	fixture(t, source, 2)
	if e := publish(source, dest); e != nil {
		t.Fatal(e)
	}
	old, e := loadSnapshot(dest, ".previous-board-data.js")
	if e != nil || old.Revision != 1 {
		t.Fatalf("previous cache: %v %v", old, e)
	}
	os.WriteFile(filepath.Join(dest, "avatars", "cat.png"), []byte("damaged"), 0600)
	if e := publish(source, dest); e != nil {
		t.Fatal(e)
	}
	repaired, _ := os.ReadFile(filepath.Join(dest, "avatars", "cat.png"))
	if string(repaired) != "test image bytes" {
		t.Fatal("did not repair cached asset")
	}
}

func TestReadOnlyHTTPAllowlistAndHealth(t *testing.T) {
	root := t.TempDir()
	fixture(t, root, 7)
	loaded, e := loadSnapshot(root, "board-data.js")
	if e != nil {
		t.Fatal(e)
	}
	var state atomic.Pointer[snapshot]
	state.Store(loaded)
	handler := makeHandler(&state, time.Now())
	for _, p := range []string{"/", "/board.html", "/board-data.js?x=1", "/avatars/cat.png", "/healthz"} {
		r := httptest.NewRequest("GET", p, nil)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		if w.Code != 200 {
			t.Errorf("%s: %d", p, w.Code)
		}
	}
	for _, p := range []string{"/AGENTS.md", "/service/config.json", "/.published.json", "/avatars/../AGENTS.md", "/avatars/%2e%2e/AGENTS.md", "/media/missing.png"} {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest("GET", p, nil))
		if w.Code != 404 {
			t.Errorf("exposed %s: %d", p, w.Code)
		}
	}
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, httptest.NewRequest("POST", "/", nil))
	if w.Code != 405 {
		t.Fatal("accepted write method")
	}
}

func TestUnsafeBoardAssetPathsAreRejected(t *testing.T) {
	for _, p := range []string{"../secret.png", "avatars/../../secret.png", "https://example.com/cat.png", `avatars\cat.png`} {
		if validAsset(p) {
			t.Errorf("accepted %s", p)
		}
	}
}
