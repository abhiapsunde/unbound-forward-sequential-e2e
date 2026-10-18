#!/bin/sh
# Build unbound from a branch of an unbound git checkout and run the e2e
# scenarios.
# usage: [UNBOUND_SRC=path/to/unbound] ./run.sh [branch]
#   UNBOUND_SRC defaults to ../unbound, branch to forward-sequential
set -eu
cd "$(dirname "$0")"
BRANCH="${1:-forward-sequential}"

git -C "${UNBOUND_SRC:-../unbound}" archive --format=tar -o "$PWD/src.tar" "$BRANCH"
docker build -q -t unbound-e2e . >/dev/null
rm -rf out && mkdir out

docker compose up -d up1 up2 up3 unbound-seq unbound-rtt
sleep 2
status=0
docker compose run --rm runner || status=$?

# per-upstream outbound query counts from the packet captures
for inst in seq rtt; do
	echo "outbound queries captured from unbound-$inst:"
	docker compose exec -T "unbound-$inst" sh -c \
		"tcpdump -nr /out/unbound-$inst.pcap 'udp and dst port 53' 2>/dev/null" \
		| awk '{print $5}' | cut -d. -f1-4 | sort | uniq -c
done

docker compose logs --no-color up1 up2 up3 > out/upstreams.log
docker compose down -t 2 >/dev/null 2>&1
echo "artifacts in ./out: results.json, unbound-*.log, unbound-*.pcap, upstreams.log"
exit $status
