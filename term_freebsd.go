//go:build freebsd

package main

import "syscall"

// termios ioctl requests; see term_bsd.go for why only these three differ.
const (
	tcGet      = syscall.TIOCGETA
	tcSet      = syscall.TIOCSETA
	tcSetFlush = syscall.TIOCSETAF
)

// selectRead asks whether stdin has anything to read, without waiting.
//
// Identical to term_bsd.go's but for the field name: FreeBSD's syscall.FdSet
// calls it X__fds_bits where every other platform here says Bits, which is
// the whole reason this file exists apart from that one. A cross-compile for
// freebsd/amd64 is what said so, on a tag, with the other four targets
// already built.
func selectRead() (bool, error) {
	var fds syscall.FdSet
	fds.X__fds_bits[0] = 1 // fd 0 is the only one asked about
	tv := syscall.Timeval{}
	if err := syscall.Select(1, &fds, nil, nil, &tv); err != nil {
		return false, err
	}
	return fds.X__fds_bits[0]&1 != 0, nil
}
