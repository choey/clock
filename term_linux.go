//go:build linux

package main

import (
	"syscall"
	"time"
)

// termios ioctl requests; see term_bsd.go for why only these three differ.
//
// TCSETSF is TCSETS + 2: the stdlib omits that name, and TCSETSW with it, but
// the kernel allocates the three set requests consecutively — TCSETS, TCSETSW,
// TCSETSF — so the arithmetic holds everywhere the literal 0x5404 would not:
// the mips run is 0x540E..0x5410, the powerpc one 0x802c7414..0x802c7416.
// TCGETS is not part of that run on every architecture; powerpc numbers it
// 0x402c7413, a read where the other three are writes.
const (
	tcGet      = syscall.TCGETS
	tcSet      = syscall.TCSETS
	tcSetFlush = syscall.TCSETS + 2
)

// selectRead asks whether stdin has anything to read, waiting up to wait for it
// (zero: not at all); see
// term_bsd.go for why it is written three times.
func selectRead(wait time.Duration) (bool, error) {
	var fds syscall.FdSet
	fds.Bits[0] = 1 // fd 0 is the only one asked about
	tv := syscall.NsecToTimeval(int64(wait))
	n, err := syscall.Select(1, &fds, nil, nil, &tv)
	if err != nil {
		return false, err
	}
	return n > 0, nil
}
