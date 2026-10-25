//go:build !windows

package main

import (
	"errors"
	"os"
)

func replaceFile(from, to string) error         { return os.Rename(from, to) }
func publisherLock(root string) (func(), error) { return func() {}, nil }
func runService(c config) error {
	return errors.New("Windows service mode requires Windows; use serve")
}
