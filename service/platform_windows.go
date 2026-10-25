package main

import (
	"context"
	"errors"
	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"os"
	"path/filepath"
)

func replaceFile(from, to string) error {
	a, e := windows.UTF16PtrFromString(from)
	if e != nil {
		return e
	}
	b, e := windows.UTF16PtrFromString(to)
	if e != nil {
		return e
	}
	return windows.MoveFileEx(a, b, windows.MOVEFILE_REPLACE_EXISTING|windows.MOVEFILE_WRITE_THROUGH)
}
func publisherLock(root string) (func(), error) {
	if err := os.MkdirAll(root, 0755); err != nil {
		return nil, err
	}
	f, e := os.OpenFile(filepath.Join(root, ".publish.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return nil, e
	}
	var o windows.Overlapped
	if e = windows.LockFileEx(windows.Handle(f.Fd()), windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, &o); e != nil {
		f.Close()
		return nil, errors.New("a publisher is already running")
	}
	return func() { windows.UnlockFileEx(windows.Handle(f.Fd()), 0, 1, 0, &o); f.Close() }, nil
}

type forumService struct{ config config }

func (s *forumService) Execute(_ []string, requests <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending, WaitHint: 10000}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- host(ctx, s.config) }()
	state := svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	status <- state
	for {
		select {
		case err := <-done:
			if err != nil {
				return true, 1
			}
			return false, 0
		case request := <-requests:
			switch request.Cmd {
			case svc.Interrogate:
				status <- state
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending, WaitHint: 10000}
				cancel()
				<-done
				return false, 0
			}
		}
	}
}
func runService(c config) error { return svc.Run(serviceName, &forumService{config: c}) }
