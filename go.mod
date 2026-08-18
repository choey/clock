module clock

go 1.21

require github.com/choey/clock/ziptz v0.1.0

// The library lives in this repo; the replace keeps a fresh clone building
// without a tagged release or a network fetch.
replace github.com/choey/clock/ziptz => ./ziptz
