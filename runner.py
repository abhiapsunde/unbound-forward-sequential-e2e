#!/usr/bin/env python3
"""End-to-end scenarios for forward-sequential.

Two unbound instances forward to the same three fake upstreams:
  seq  forward-sequential: yes   (the change under test)
  rtt  forward-sequential: no    (stock behaviour, as a control)
Each upstream answers with its own marker address, so every answer tells
which upstream produced it. Expectations are checked for seq only; rtt is
reported alongside for comparison.
"""
import json
import random
import socket
import statistics
import struct
import subprocess
import sys
import time
import urllib.request
from collections import Counter

UPSTREAMS = {"up1": "172.28.0.11", "up2": "172.28.0.12", "up3": "172.28.0.13"}
MARKERS = {"192.0.2.11": "up1", "192.0.2.12": "up2", "192.0.2.13": "up3"}
INSTANCES = {"seq": "172.28.0.2", "rtt": "172.28.0.3"}
RCODES = {0: "NOERROR", 2: "SERVFAIL", 3: "NXDOMAIN", 5: "REFUSED"}
QUERY_TIMEOUT = 20.0


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def http(up, path):
    with urllib.request.urlopen(f"http://{UPSTREAMS[up]}:8080{path}", timeout=5) as r:
        return json.loads(r.read())


def set_modes(modes):
    for up in UPSTREAMS:
        m = modes.get(up, "ok")
        mode, _, arg = m.partition(":")
        if mode == "dropn":
            http(up, f"/mode?m=dropn&n={arg}")
        else:
            http(up, f"/mode?m={mode}&delay={arg or 0}")


def upstream_counts(inst):
    """queries received by each upstream from this instance."""
    src = INSTANCES[inst]
    return {up: http(up, "/stats").get(src, {}).get("received", 0) for up in UPSTREAMS}


def control(inst, *cmd):
    subprocess.run(["unbound-control", "-c", "/etc/unbound/base.conf", "-s", f"{INSTANCES[inst]}@8953", *cmd],
                   check=True, capture_output=True)


def reset():
    for inst in INSTANCES:
        control(inst, "flush_infra", "all")
        control(inst, "flush_requestlist")
        control(inst, "flush_zone", ".")
    for up in UPSTREAMS:
        http(up, "/stats?reset=1")


def query(inst, name):
    """returns (who, latency_ms); who is up1/up2/up3, an rcode, or TIMEOUT."""
    qid = random.randint(0, 65535)
    wire = b"".join(bytes([len(l)]) + l.encode() for l in name.rstrip(".").split(".")) + b"\0"
    pkt = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0) + wire + struct.pack("!HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(QUERY_TIMEOUT)
    t0 = time.monotonic()
    s.sendto(pkt, (INSTANCES[inst], 53))
    try:
        while True:
            data, _ = s.recvfrom(4096)
            if struct.unpack("!H", data[:2])[0] == qid:
                break
    except socket.timeout:
        return "TIMEOUT", QUERY_TIMEOUT * 1000
    finally:
        s.close()
    ms = (time.monotonic() - t0) * 1000
    rcode = struct.unpack("!H", data[2:4])[0] & 0xF
    ancount = struct.unpack("!H", data[6:8])[0]
    if rcode != 0 or ancount == 0:
        return RCODES.get(rcode, f"RCODE{rcode}"), ms
    ip = socket.inet_ntoa(data[-4:])
    return MARKERS.get(ip, ip), ms


def batch(inst, label, n, gap=0.05):
    results = []
    for i in range(n):
        name = f"{label}-{inst}-{i}-{random.randint(0, 1 << 30)}.example."
        results.append(query(inst, name))
        time.sleep(gap)
    return results


def summarize(results):
    who = Counter(w for w, _ in results)
    lat = [ms for _, ms in results]
    return {
        "answered_by": dict(who),
        "lat_p50_ms": round(statistics.median(lat)),
        "lat_max_ms": round(max(lat)),
        "lat_first5_ms": [round(x) for x in lat[:5]],
    }


def exactly(**want):
    return (lambda got: got == want), f"exactly {want}"


def slow_primary(n):
    # an upstream slower than the initial 376ms timeout loses the first
    # queries to the fallback until its rtt is learned; no timeouts, and
    # never the last resort
    return ((lambda got: got.get("up1", 0) >= n - 2 and set(got) <= {"up1", "up2"}),
            f"up1 >= {n - 2}, rest up2, no timeouts")


SCENARIOS = [
    # name, upstream modes, queries, (check, description) for seq, why
    ("healthy, primary slower (150ms)",
     {"up1": "ok:150"}, 30, exactly(up1=30),
     "primary used for everything even though fallbacks are faster"),
    ("primary slow but alive (800ms)",
     {"up1": "ok:800"}, 15, slow_primary(15),
     "slow is not down: no traffic leaks once the rtt is known"),
    ("primary drops one packet",
     {"up1": "dropn:1"}, 15, exactly(up1=14, up2=1),
     "a single loss fails over that query only, the next goes to the primary"),
    ("primary drops two packets",
     {"up1": "dropn:2"}, 15, exactly(up1=13, up2=2),
     "two losses in a row are not an outage either"),
    ("primary down (no answer)",
     {"up1": "drop"}, 20, exactly(up2=20),
     "fail over to the first fallback only, never the last resort, no client timeouts"),
    ("primary and first fallback down",
     {"up1": "drop", "up2": "drop"}, 15, exactly(up3=15),
     "fail over down the list to the last resort, no client timeouts"),
    ("primary answers SERVFAIL",
     {"up1": "servfail"}, 15, exactly(up2=15),
     "a SERVFAIL from the primary is retried on the fallback"),
    ("primary answers REFUSED",
     {"up1": "refused"}, 15, exactly(up2=15),
     "a REFUSED from the primary is retried on the fallback"),
    ("primary answers garbage",
     {"up1": "garbage"}, 15, exactly(up2=15),
     "an unparsable reply is treated as no reply"),
    ("primary answers with wrong query id",
     {"up1": "wrongid"}, 15, exactly(up2=15),
     "a spoofed-looking reply is ignored"),
]


def run_scenario(name, modes, n, expect, why):
    check, desc = expect
    log(f"=== {name}")
    reset()
    set_modes(modes)
    out = {"scenario": name, "modes": modes, "why": why, "expect_seq": desc}
    for inst in INSTANCES:
        # the drop-next-n counters are shared, re-arm them per instance
        set_modes(modes)
        res = batch(inst, name.split()[0] + str(abs(hash(name)) % 1000), n)
        out[inst] = summarize(res)
        out[inst]["upstream_received"] = upstream_counts(inst)
        log(f"  {inst}: answered_by={out[inst]['answered_by']} "
            f"received={out[inst]['upstream_received']} "
            f"p50={out[inst]['lat_p50_ms']}ms max={out[inst]['lat_max_ms']}ms "
            f"first5={out[inst]['lat_first5_ms']}")
    out["pass"] = check(out["seq"]["answered_by"])
    log(f"  seq {'PASS' if out['pass'] else 'FAIL'} (expected {desc})")
    return out


def run_recovery():
    name = "primary recovers after outage"
    log(f"=== {name}")
    reset()
    set_modes({"up1": "drop"})
    before = batch("seq", "rec-down", 10)
    log(f"  primary down: {summarize(before)['answered_by']}")
    # the primary must be passed over while down, else recovery is trivial
    down_ok = summarize(before)["answered_by"] == {"up2": 10}
    set_modes({})
    log("  primary restored, polling every 2s (infra-host-ttl is 30s)")
    t0 = time.monotonic()
    timeline = []
    back_after = None
    while time.monotonic() - t0 < 90:
        who, ms = query("seq", f"rec-up-{random.randint(0, 1 << 30)}.example.")
        timeline.append((round(time.monotonic() - t0, 1), who, round(ms)))
        if who == "up1":
            back_after = round(time.monotonic() - t0, 1)
            break
        time.sleep(2)
    log(f"  back on primary after: {back_after}s ({len(timeline)} polls)")
    after = batch("seq", "rec-after", 10)
    log(f"  after recovery: {summarize(after)['answered_by']}")
    ok = (down_ok and back_after is not None
          and summarize(after)["answered_by"] == {"up1": 10})
    log(f"  seq {'PASS' if ok else 'FAIL'}")
    return {"scenario": name, "why": "traffic returns to the primary once it answers again",
            "back_on_primary_after_s": back_after, "polls": timeline,
            "after": summarize(after), "pass": ok}


def main():
    results = [run_scenario(*s) for s in SCENARIOS]
    results.append(run_recovery())
    with open("/out/results.json", "w") as f:
        json.dump(results, f, indent=2)
    print()
    print(f"{'scenario':42} {'seq answered_by':22} {'rtt answered_by':28} result")
    for r in results:
        seq = r.get("seq", {}).get("answered_by", r.get("after", {}).get("answered_by"))
        rtt = r.get("rtt", {}).get("answered_by", "-")
        print(f"{r['scenario'][:42]:42} {str(seq)[:22]:22} {str(rtt)[:28]:28} "
              f"{'PASS' if r['pass'] else 'FAIL'}")
    failed = [r["scenario"] for r in results if not r["pass"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
