#!/bin/sh
# Run unbound's own test suites, make test (unit tests, testbound replay
# tests) and make longtest (.tdir tests), for a branch, in Debian, the way
# upstream runs it. Needs no tools on the host besides git and docker.
# usage: [UNBOUND_SRC=path/to/unbound] ./longtest.sh [branch]
set -eu
cd "$(dirname "$0")"
BRANCH="${1:-forward-sequential}"
git -C "${UNBOUND_SRC:-../unbound}" archive --format=tar -o "$PWD/src.tar" "$BRANCH"
docker build -q -f Dockerfile.tdir -t unbound-longtest . >/dev/null
docker run --rm unbound-longtest
