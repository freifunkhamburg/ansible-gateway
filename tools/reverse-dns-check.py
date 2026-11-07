#!/usr/bin/env python3
"""reverse-dns-check.py — verify the gateway's mesh reverse DNS answers.

Checks, per served region:

  gateway PTRs   the gw01/gw02/gw03 addresses of the region must resolve to
                 gwNN-new.<region>.hamburg.freifunk.net.
  client PTR     one address that is (almost) certainly not in the resolver
                 cache must resolve to the synthesized community name
                 ipv6-<16 hex interface part>.<region>.clients.hamburg
                 .freifunk.net (roles/powerdns answers the served regions,
                 the community DNS the rest; both use this naming).
                 The address is derived from the current time, so the check
                 is a real regression test, not a cache hit.

  forward record the synthesized name must resolve back to the address
                 (A / AAAA through the same resolver).

Informational probes (never fail the run):

  own gateway     the gateway's own address inside each region. Upstream
                  naming for ordinals 1-3 is gwNN-new.<region>...; ordinals
                  that are not community gateways fall back to the client
                  naming pattern.
  v4 client       a client address in the region's IPv4 subnet: answered by
                  roles/powerdns as <4 hex host part>.<region>.ffhh (the
                  host part inside the mesh /16, in the ffhh zone).

Examples:
  sudo ./reverse-dns-check.py \\
      --region "hhsued|2a03:2267:1::/64|1|10.112.64.0/19" \\
      --region "hhwest|2a03:2267:2::/64|2|10.112.96.0/19" \\
      --gateway-index 5

Exit status: 0 = all checks pass, 2 = at least one check failed.
"""

import argparse
import ipaddress
import json
import subprocess
import sys
import time

PASS, FAIL, INFO = "pass", "fail", "info"


def dig_reverse(addr, server, port, timeout=5):
    """Return (rcode, [PTR target...]) for a -x query."""
    cmd = [
        "dig",
        "+time=" + str(timeout),
        "+tries=1",
        "-x",
        addr,
        "@" + server,
        "-p",
        str(port),
        "+noall",
        "+comments",
        "+answer",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 6)
    except Exception as e:  # noqa: BLE001
        return "error", [], str(e)

    rcode = "no-answer"
    out = []
    for line in p.stdout.splitlines():
        if "status:" in line:
            rcode = line.split("status:")[1].strip().split()[0].rstrip(",").upper()
        elif "IN" in line and "PTR" in line:
            out.append(line.split()[-1])
    if rcode == "no-answer":
        return "error", [], p.stderr.strip() or "no reply"
    return rcode, out, ""


def dig_forward(name, rdtype, server, port, timeout=5):
    """Return the first rdata of an A/AAAA query for `name`, or ''."""
    cmd = [
        "dig", "+short", "+time=" + str(timeout), "+tries=1",
        rdtype, name, "@" + server, "-p", str(port),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 6)
    except Exception:  # noqa: BLE001
        return ""
    lines = [ln.strip() for ln in p.stdout.splitlines() if ln.strip()]
    return lines[0] if lines else ""


def norm(name):
    return name.strip().lower().rstrip(".")


def ip_equal(a, b):
    """Compare two IP texts canonically (both may or may not use ::)."""
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return norm(a) == norm(b)


class Result:
    def __init__(self, kind, label, addr, expected, state, actual, note=""):
        self.kind = kind
        self.label = label
        self.addr = addr
        self.expected = expected
        self.state = state
        self.actual = actual
        self.note = note

    def as_dict(self):
        return {
            "kind": self.kind,
            "label": self.label,
            "addr": self.addr,
            "expected": self.expected,
            "state": self.state,
            "actual": self.actual,
            "note": self.note,
        }


def check(res, server, port):
    rcode, answers, err = dig_reverse(res.addr, server, port)
    if err:
        res.state = FAIL
        res.actual = "error: " + err
        return res
    if rcode != "NOERROR" or not answers:
        res.state = FAIL
        res.actual = rcode if not answers else ", ".join(answers)
        return res
    if norm(answers[0]) != norm(res.expected):
        res.state = FAIL
        res.actual = answers[0]
        return res
    res.state = PASS
    res.actual = answers[0]
    return res


def info(res, server, port):
    rcode, answers, err = dig_reverse(res.addr, server, port)
    if err:
        res.state = INFO
        res.actual = "error: " + err
    elif not answers:
        res.state = INFO
        res.actual = rcode + " (no PTR)"
    else:
        res.state = INFO
        res.actual = ", ".join(answers)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=53)
    ap.add_argument("--gateway-index", type=int, default=None,
                    help="ordinal of this gateway (own-address probes)")
    ap.add_argument("--region", action="append", default=[],
                    metavar="ID|PREFIX|INDEX|V4NET",
                    help="served region, e.g. 'hhsued|2a03:2267:1::/64|1|10.112.64.0/19'")
    ap.add_argument("--check", action="append", default=[],
                    metavar="ADDR=EXPECTED",
                    help="extra address/expected-PTR pair")
    ap.add_argument("--info", action="append", default=[],
                    metavar="ADDR",
                    help="extra informational probe")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--seed", type=int, default=None,
                    help="seed for the fresh client probe (default: now)")
    args = ap.parse_args()

    seed = args.seed if args.seed is not None else int(time.time())
    results = []

    for spec in args.region:
        parts = spec.split("|")
        if len(parts) < 3:
            ap.error("--region expects ID|PREFIX|INDEX[|V4NET], got %r" % spec)
        rid, prefix, index = parts[0], parts[1], int(parts[2])
        v4net = parts[3] if len(parts) > 3 else None
        label = rid[2:] if rid.startswith("hh") else rid
        net = ipaddress.ip_network(prefix, strict=False)

        # Community gateways gw01/gw02/gw03 have real PTR records.
        for n in (1, 2, 3):
            addr = str(net.network_address + n * 0x10)
            expected = "gw%02d-new.%s.hamburg.freifunk.net." % (n, label)
            results.append(check(Result("gateway", label, addr, expected, None, None),
                                 args.server, args.port))

        # Fresh client address: derived from the run time so it is not
        # cached.  Synthesized by roles/powerdns (and upstream) as the
        # 16-hex interface part under the region clients domain.
        offset = 0x100000 + (seed % 0xFFFF) + index * 0x1000
        addr = str(net.network_address + offset)
        expected = "ipv6-%016x.%s.clients.hamburg.freifunk.net." % (
            int(ipaddress.ip_address(addr)) & ((1 << 64) - 1), label)
        res = check(Result("client", label, addr, expected, None, None),
                    args.server, args.port)
        results.append(res)
        if res.state == PASS:
            # The synthesized name must resolve back to the address.
            back = dig_forward(norm(expected), "AAAA", args.server, args.port)
            if not back or not ip_equal(back, addr):
                res.state = FAIL
                res.actual = "%s but %s -> %r" % (res.actual, norm(expected),
                                                  back or "no AAAA")

        if args.gateway_index is not None:
            own = str(net.network_address + args.gateway_index * 0x10)
            if args.gateway_index <= 3:
                expected = "gw%02d-new.%s.hamburg.freifunk.net." % (args.gateway_index, label)
            else:
                expected = "ipv6-%016x.%s.clients.hamburg.freifunk.net." % (
                    args.gateway_index * 0x10, label)
            results.append(info(Result("own", label, own, expected, None, None),
                                args.server, args.port))

        # IPv4 client address: roles/powerdns synthesizes the host part of
        # the /16 aggregate as 4 hex digits; check the round trip too.
        if v4net:
            net4 = ipaddress.ip_network(v4net, strict=False)
            addr4 = str(net4.network_address + 0x100 + (seed % 512))
            ia4 = ipaddress.ip_address(addr4)
            expected4 = "%04x.%s.ffhh." % (int(ia4) & 0xFFFF, label)
            res4 = check(Result("v4-client", label, addr4, expected4, None, None),
                         args.server, args.port)
            if res4.state == PASS:
                back = dig_forward(norm(expected4), "A", args.server, args.port)
                if not back or not ip_equal(back, addr4):
                    res4.state = FAIL
                    res4.actual = "%s but %s -> %r" % (
                        res4.actual, norm(expected4), back or "no A")
            results.append(res4)

    for spec in args.check:
        if "=" not in spec:
            ap.error("--check expects ADDR=EXPECTED, got %r" % spec)
        addr, expected = spec.split("=", 1)
        results.append(check(Result("extra", "-", addr, expected, None, None),
                             args.server, args.port))

    for addr in args.info:
        results.append(info(Result("extra", "-", addr, None, None, None),
                            args.server, args.port))

    failures = [r for r in results if r.state == FAIL]
    informational = sum(1 for r in results if r.state == INFO)
    summary = {
        "checks": len(results) - informational,
        "passed": sum(1 for r in results if r.state == PASS),
        "failed": len(failures),
        "informational": informational,
        "exit": 2 if failures else 0,
    }

    if args.json:
        print(json.dumps({"results": [r.as_dict() for r in results],
                          "summary": summary}, indent=2))
    else:
        print("reverse DNS via %s port %s" % (args.server, args.port))
        for r in results:
            print("[%-4s] %-8s %-8s %-34s %s" % (
                r.state, r.kind, r.label, r.addr, r.actual))
        print("summary: %d check(s), %d failed, %d informational" % (
            summary["checks"], summary["failed"], summary["informational"]))
    return summary["exit"]


if __name__ == "__main__":
    sys.exit(main())
