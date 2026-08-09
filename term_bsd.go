//go:build darwin || dragonfly || freebsd || netbsd || openbsd

package main

import "syscall"

// termios ioctl requests. The BSDs spell them TIOC*; Linux spells them TC*.
// Only these three names differ — syscall.Termios, ECHO, ICANON, TIOCGWINSZ
// and SYS_IOCTL exist on both, so the rest of the clock needs no build tags.
const (
	tcGet      = syscall.TIOCGETA
	tcSet      = syscall.TIOCSETA
	tcSetFlush = syscall.TIOCSETAF
)
