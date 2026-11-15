//go:build darwin || dragonfly || netbsd || openbsd

package main

import (
	"syscall"
	"time"
)

// termios ioctl requests. The BSDs spell them TIOC*; Linux spells them TC*.
// Only these three names differ — syscall.Termios, ECHO, ICANON, TIOCGWINSZ
// and SYS_IOCTL exist on both, so the rest of the clock needs no build tags.
const (
	tcGet      = syscall.TIOCGETA
	tcSet      = syscall.TIOCSETA
	tcSetFlush = syscall.TIOCSETAF
)

// selectRead asks whether stdin has anything to read, waiting up to wait for
// it -- zero, not at all.
//
// syscall.Select is the fourth name that differs, and differs twice over: the
// BSDs' wrapper hands back an error alone where Linux's also returns a count,
// and FreeBSD spells FdSet's one field differently from everybody else. So
// the whole question is asked per platform rather than the set being built in
// one place and passed in -- three small functions, no build tags in the
// clock itself.
func selectRead(wait time.Duration) (bool, error) {
	var fds syscall.FdSet
	fds.Bits[0] = 1 // fd 0 is the only one asked about
	tv := syscall.NsecToTimeval(int64(wait))
	if err := syscall.Select(1, &fds, nil, nil, &tv); err != nil {
		return false, err
	}
	return fds.Bits[0]&1 != 0, nil
}
