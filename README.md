# unbound forward-sequential: end-to-end tests

Test harness and results for the proposed `forward-sequential` option for
[Unbound](https://github.com/NLnetLabs/unbound). The option makes a `forward-zone` use its
`forward-addr` servers in configured order instead of by round trip time, so that a later
server is a pure fallback.

- Upstream request: [NLnetLabs/unbound#1402](https://github.com/NLnetLabs/unbound/issues/1402)
- Change under test: [`585c58014`](https://github.com/abhiapsunde/unbound/commit/585c58014) on
  [abhiapsunde/unbound `forward-sequential`](https://github.com/abhiapsunde/unbound/tree/forward-sequential)

There are two parts. The end-to-end scenarios run real `unbound` processes against controllable
fake upstreams. The upstream suite runs Unbound's own `make longtest` in Debian, on the branch and
on its `master` base, for comparison.

## Results for 585c58014

### End-to-end scenarios

Two instances are built from the branch and forward to the same three upstreams (`up1` is the
primary, `up2` the first fallback, `up3` the last resort). `seq` has `forward-sequential: yes`.
`rtt` has the default selection and serves as a control. Every query uses a fresh name, so
nothing comes from cache. Each upstream answers with its own marker address, so every answer
shows which upstream produced it.

| Scenario | `seq` answered by | `rtt` (control) answered by | `seq` |
|---|---|---|---|
| Healthy, primary slower (150 ms) than fallbacks | up1: 30 | up1: 6, up2: 11, up3: 13 | PASS |
| Primary slow but alive (800 ms) | up1: 13, up2: 2 ¹ | up2: 6, up3: 9 | PASS |
| Primary drops one packet | up1: 14, up2: 1 | spread | PASS |
| Primary drops two packets | up1: 13, up2: 2 | spread | PASS |
| Primary down (no answer) | up2: 20 ² | up2: 8, up3: 12 | PASS |
| Primary and first fallback down | up3: 14, client timeout: 1 ³ | up3: 15 | FAIL |
| Primary answers SERVFAIL | up2: 15 | spread | PASS |
| Primary answers REFUSED | up2: 15 | spread | PASS |
| Primary answers garbage | up2: 15 ² | spread | PASS |
| Primary answers with a mismatched query id | up2: 15 ² | spread | PASS |
| Primary recovers after an outage | back on up1 after 28 s ⁴ | n/a | PASS |

1. An upstream slower than the initial 376 ms timeout loses its first queries to the fallback
   until its RTT is known. After that, all queries go to the primary.
2. The first three queries cost 0.4, 0.8 and 1.5 s while the primary times out. After three
   timeouts in a row it is passed over, and queries take about 5 ms. The last resort is never
   queried. All answers arrive within `discard-timeout`.
3. Known limitation. With two dead servers ahead of the live one, the third query waits for two
   backed-off timeouts in a row (1.5 s + 1.5 s). That exceeds `discard-timeout` (1.9 s), so its
   reply is dropped. A real client gets the cached answer on its retry. Later queries go straight
   to `up3`.
4. With `infra-host-ttl: 30` in the test config. The default is 900 s.

Packet captures agree. Outbound queries from `seq`: up1 123, up2 112, up3 15. From `rtt`:
up1 30, up2 74, up3 91.

Artifacts, in [`results/585c58014`](results/585c58014):
- `results.json`: per-scenario answers, upstream counters and latencies.
- `unbound-{seq,rtt}.log`: verbosity 4 logs, including server selection.
- `unbound-{seq,rtt}.pcap`: outbound traffic.
- `upstreams.log`: every query each fake upstream received.
- `run.log`: console output.

### Upstream test suite (`make longtest`)

This was run in Debian bookworm with [`Dockerfile.tdir`](Dockerfile.tdir), once for the branch
and once for its base commit on `master` (`d648eac68`).

| | Branch | master |
|---|---|---|
| `.tdir` passed | 78 | 78 |
| `.tdir` skipped (tool or feature not available) | 32 | 32 |
| `.tdir` failed | 7 | 7 |

The `.tdir` outcomes are identical for the branch and `master` (`diff` of the summaries is
empty). The 7 failures come from the container environment, not from the change: tests that need
network namespaces (`acl_interface`, `proxy_protocol`), live internet (`root_anchor`,
`root_hints`, `root_zonemd`), and `fwd_compress_c00c` and `stat_values`. The tests closest to the
change pass: `fast_reload_most_options`, `fast_reload_fwd`, and every other `fwd_*` test.

`make longtest` builds the unit and replay tests but does not run them. `make test` on the
branch, on macOS arm64, passed: 1,143,545 unit checks, the testbound self-test, and all 493
`.rpl` replay tests, including the two new ones. `Dockerfile.tdir` now runs `make test` before
`make longtest`. Results of that run in Debian, for the branch and `master`, will be added here.

## Running it

You need git, Docker and an Unbound checkout. `UNBOUND_SRC` defaults to `../unbound`.

```sh
# end-to-end scenarios, about 2 minutes; artifacts land in ./out
UNBOUND_SRC=~/src/unbound ./run.sh forward-sequential

# upstream's own long test suite in Debian, about 6 minutes
UNBOUND_SRC=~/src/unbound ./longtest.sh forward-sequential
```

### How the e2e harness works

- [`fakedns.py`](fakedns.py) is a stdlib-only fake upstream. You switch its behaviour at runtime
  over HTTP: `ok` (with an optional delay), `drop`, `dropn` (drop the next N queries),
  `servfail`, `refused`, `garbage` and `wrongid`. It logs every query it receives.
- [`runner.py`](runner.py) runs the scenarios. Before each one, it flushes the infra cache,
  request list and message cache on both instances (via `unbound-control`) and resets the upstream
  counters. It then checks which upstream answered each query and how long it took.
- [`compose.yaml`](compose.yaml) sets up a fixed-address network: `172.28.0.11` to `.13` for the
  upstreams, `.2` for `seq`, `.3` for `rtt` and `.100` for the runner. `tcpdump` runs in each
  unbound container.
- [`conf/`](conf) holds the unbound configs. Both instances share `base.conf` and differ only in
  `forward-sequential`.
