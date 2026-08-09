//go:build linux

package main

import "syscall"

// termios ioctl requests; see term_bsd.go for why only these three differ.
//
// TCSETSF is TCSETS + 2: the stdlib omits the name, but the four requests are
// always allocated consecutively (TCGETS, TCSETS, TCSETSW, TCSETSF), so the
// arithmetic holds everywhere the literal 0x5404 would not — mips numbers them
// 0x540D..0x5410, powerpc 0x802c7413..0x802c7416.
const (
	tcGet      = syscall.TCGETS
	tcSet      = syscall.TCSETS
	tcSetFlush = syscall.TCSETS + 2
)
