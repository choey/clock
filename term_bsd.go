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

// selectRead asks whether a descriptor has anything to read, without waiting.
// syscall.Select is the fourth name that differs: the BSDs' wrapper hands
// back an error alone where Linux's also returns the count, so the count is
// what this adds -- ready or not, which is all pendingKeys asks.
func selectRead(fds *syscall.FdSet, tv *syscall.Timeval) (bool, error) {
	err := syscall.Select(1, fds, nil, nil, tv)
	if err != nil {
		return false, err
	}
	return fds.Bits[0]&1 != 0, nil
}
