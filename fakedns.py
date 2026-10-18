#!/usr/bin/env python3
"""Controllable fake upstream DNS server (stdlib only).

Answers every A query with its own marker address, so the client can tell
which upstream produced an answer. Behaviour is switched at runtime over HTTP:

  GET /mode?m=ok&delay=150   answer, after an optional delay in ms
  GET /mode?m=drop           never answer (simulates an outage)
  GET /mode?m=dropn&n=2      drop the next n queries, then answer
  GET /mode?m=servfail       answer SERVFAIL
  GET /mode?m=refused        answer REFUSED
  GET /mode?m=garbage        answer random bytes
  GET /mode?m=wrongid        answer with a mismatched query id
  GET /stats[?reset=1]       per-source counters, as json
"""
import json
import os
import random
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NAME = os.environ["NAME"]
ANSWER_IP = os.environ["ANSWER_IP"]

lock = threading.Lock()
state = {"mode": "ok", "delay": 0, "n": 0}
# stats[source_ip] = {"received": n, "answered": n}
stats = {}


def log(msg):
    ts = time.strftime("%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}"
    print(f"{ts} {NAME} {msg}", flush=True)


def parse_query(data):
    """Return (id, flags, qname, qtype, question_end, arcount) or None."""
    if len(data) < 12:
        return None
    qid, flags, qdcount, _, _, arcount = struct.unpack("!HHHHHH", data[:12])
    if qdcount != 1:
        return None
    pos, labels = 12, []
    while True:
        if pos >= len(data):
            return None
        ln = data[pos]
        pos += 1
        if ln == 0:
            break
        labels.append(data[pos:pos + ln].decode("ascii", "replace"))
        pos += ln
    if pos + 4 > len(data):
        return None
    qtype, _ = struct.unpack("!HH", data[pos:pos + 4])
    return qid, flags, ".".join(labels) + ".", qtype, pos + 4, arcount


def build_reply(data, q, rcode, qid=None):
    orig_id, flags, _, qtype, qend, arcount = q
    rd = flags & 0x0100
    out_flags = 0x8000 | rd | 0x0080 | rcode  # QR, RD copied, RA, rcode
    answer = b""
    ancount = 0
    if rcode == 0 and qtype == 1:
        answer = (b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4)
                  + socket.inet_aton(ANSWER_IP))
        ancount = 1
    opt = b""
    if arcount:
        opt = b"\x00" + struct.pack("!HHIH", 41, 1232, 0, 0)
    header = struct.pack("!HHHHHH", orig_id if qid is None else qid,
                         out_flags, 1, ancount, 0, 1 if opt else 0)
    return header + data[12:qend] + answer + opt


def handle(sock, data, addr):
    q = parse_query(data)
    if q is None:
        log(f"from {addr[0]} unparsable query, ignored")
        return
    with lock:
        mode, delay = state["mode"], state["delay"]
        if mode == "dropn":
            if state["n"] > 0:
                state["n"] -= 1
                mode = "drop"
            else:
                mode = "ok"
        st = stats.setdefault(addr[0], {"received": 0, "answered": 0})
        st["received"] += 1
    log(f"recv from {addr[0]} id={q[0]} {q[2]} type={q[3]} mode={mode}"
        + (f" delay={delay}ms" if delay else ""))
    if mode == "drop":
        return
    if delay:
        time.sleep(delay / 1000.0)
    if mode == "ok":
        reply = build_reply(data, q, 0)
    elif mode == "servfail":
        reply = build_reply(data, q, 2)
    elif mode == "refused":
        reply = build_reply(data, q, 5)
    elif mode == "garbage":
        reply = bytes(random.getrandbits(8) for _ in range(40))
    elif mode == "wrongid":
        reply = build_reply(data, q, 0, qid=(q[0] ^ 0x5a5a))
    else:
        return
    sock.sendto(reply, addr)
    with lock:
        stats[addr[0]]["answered"] += 1


def udp_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 53))
    log(f"listening udp/53, answers {ANSWER_IP}")
    while True:
        data, addr = sock.recvfrom(4096)
        threading.Thread(target=handle, args=(sock, data, addr), daemon=True).start()


class Control(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        args = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/mode":
            with lock:
                state["mode"] = args.get("m", "ok")
                state["delay"] = int(args.get("delay", 0))
                state["n"] = int(args.get("n", 0))
                body = dict(state)
            log(f"control: mode={body['mode']} delay={body['delay']}ms")
        elif url.path == "/stats":
            with lock:
                body = json.loads(json.dumps(stats))
                if args.get("reset"):
                    stats.clear()
        else:
            self.send_response(404)
            self.end_headers()
            return
        out = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=udp_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Control).serve_forever()
    sys.exit(0)
