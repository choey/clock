//go:build ignore

// Dumps what this library answers for every ZIP code there is: all 1,000
// three-digit prefixes and all 100,000 five-digit codes, one per line. Its twin
// tools/sweep.py prints the same thing from the Python side, and the two files
// are compared -- which is the only way to know the two lookups agree on inputs
// nobody thought to write a case for.
//
//	go run tools/sweep.go > /tmp/go.sweep
package main

import (
	"bufio"
	"fmt"
	"os"

	"github.com/choey/clock/ziptz"
)

func main() {
	out := bufio.NewWriterSize(os.Stdout, 1<<20)
	defer out.Flush()
	for n := 0; n < 1000; n++ {
		say(out, fmt.Sprintf("%03d", n))
	}
	for n := 0; n < 100000; n++ {
		say(out, fmt.Sprintf("%05d", n))
	}
}

// say prints one answer, error text included: what it refuses and why is as
// much a part of the port as what it resolves.
func say(out *bufio.Writer, token string) {
	name, err := ziptz.Zone(token)
	if err != nil {
		fmt.Fprintf(out, "%s\t!%s\n", token, err)
		return
	}
	generic, err := ziptz.Generic(token)
	if err != nil {
		fmt.Fprintf(out, "%s\t!%s\n", token, err)
		return
	}
	fmt.Fprintf(out, "%s\t%s\t%s\n", token, name, generic)
}
