//go:build windows

package main

import (
	"fmt"
	"os"
)

// The clock needs a POSIX terminal: cbreak mode via termios, and ANSI cursor
// movement it can count on. Rather than fail to build with a dozen undefined
// symbols, say so. pyclock.py prints the same line for the same reason.
func main() {
	fmt.Fprintln(os.Stderr, "clock: Windows is not supported (needs a POSIX terminal); try WSL")
	os.Exit(1)
}
