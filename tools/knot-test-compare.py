#!/usr/bin/env python3
"""Compare the Knot Resolver test instance against unbound for ffhh DNS data.

The expected answers are the per-subnet data unbound serves from
nodeffhh.conf. For every region we query both resolvers using the gateway's
own address in that region (so unbound's access-control-tag applies) and
compare with the expected values and the AA flag.

Untagged clients (loopback) are compared too: unbound forwards ffhh to the
community DNS, and the kresd instance must do the same.

This is a test harness, not part of the client-facing DNS path.
"""

import argparse
import json
import subprocess
import sys
import time

DIG_TIMEOUT = 8


def dig(addr, port, name, qtype, full=False):
    cmd = ["dig", qtype, name, "@" + addr, "-p", str(port)]
    if full:
        cmd += ["+noall", "+comments", "+answer"]
    else:
        cmd.append("+short")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=DIG_TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return None, str(exc)
    if p.returncode != 0:
        return None, p.stderr.strip() or "dig failed"
    return p.stdout, (p.stderr or "").strip()


def short(addr, port, name, qtype):
    out, err = dig(addr, port, name, qtype)
    if out is None:
        return None, err
    return "\n".join(l.strip() for l in out.splitlines() if l.strip()), err


def flags_and_answer(addr, port, name, qtype):
    out, err = dig(addr, port, name, qtype, full=True)
    if out is None:
        return None, None, err
    flags = ""
    answers = []
    for line in out.splitlines():
        if line.startswith(";; flags:"):
            flags = line
        elif line and not line.lstrip().startswith(";"):
            answers.append(line.strip())
    return flags, "\n".join(answers), err


def has_aa(flags):
    return flags is not None and " aa " in flags + " "


def wait_for(addr, port, name, qtype, tries=10, delay=1.0):
    for _ in range(tries):
        out, _ = short(addr, port, name, qtype)
        if out is not None:
            return True
        time.sleep(delay)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="/var/lib/knot-resolver-test/regions.json")
    ap.add_argument("--unbound-port", type=int, default=53)
    ap.add_argument("--knot-port", type=int, default=5353)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    with open(args.regions) as fh:
        regions = json.load(fh)

    failures = 0
    passes = 0

    def report(ok, kind, label, detail=""):
        nonlocal failures, passes
        if ok:
            passes += 1
        else:
            failures += 1
        if not args.quiet:
            print(("PASS " if ok else "FAIL ") + f"{kind:<10} {label:<42} {detail}")

    first = regions[0]
    if not wait_for(first["listen4"], args.knot_port, "node.ffhh", "AAAA"):
        print(f"FAIL startup: kresd on port {args.knot_port} did not answer")
        return 1

    for r in regions:
        checks = [
            ("node.ffhh", "A", r["gw4"]),
            ("node.ffhh", "AAAA", r["gw6"]),
            ("knoten.ffhh", "A", r["gw4"]),
            ("router.ffhh", "AAAA", r["gw6"]),
            ("1.ntp.services.ffhh", "AAAA", r["ntp6"][0]),
            ("2.ntp.services.ffhh", "AAAA", r["ntp6"][1]),
            ("3.ntp.services.ffhh", "AAAA", r["ntp6"][2]),
            ("v4.1.ntp.services.ffhh", "A", r["ntp4"][0]),
            ("v4.2.ntp.services.ffhh", "A", r["ntp4"][1]),
            ("v4.3.ntp.services.ffhh", "A", r["ntp4"][2]),
        ]
        for name, qtype, expected in checks:
            label = f"{r['id']} {name} {qtype} @{r['listen4']}"
            ub, ub_err = short(r["listen4"], args.unbound_port, name, qtype)
            kn, kn_err = short(r["listen4"], args.knot_port, name, qtype)
            ub_flags, ub_ans, _ = flags_and_answer(r["listen4"], args.unbound_port, name, qtype)
            kn_flags, kn_ans, _ = flags_and_answer(r["listen4"], args.knot_port, name, qtype)
            ok = (ub == expected and kn == expected
                  and has_aa(ub_flags) and has_aa(kn_flags))
            detail = f"expected={expected} unbound={ub!r} kresd={kn!r}"
            if ub_err or kn_err:
                detail += f" err={ub_err or kn_err}"
            report(ok, "tagged", label, detail)

        # authoritative NODATA for a type the zone does not hold
        label = f"{r['id']} node.ffhh TXT (NODATA)"
        ub_flags, ub_ans, _ = flags_and_answer(r["listen4"], args.unbound_port, "node.ffhh", "TXT")
        kn_flags, kn_ans, _ = flags_and_answer(r["listen4"], args.knot_port, "node.ffhh", "TXT")
        report(ub_ans == kn_ans == "", "tagged", label,
               f"unbound={ub_ans!r} kresd={kn_ans!r}")

        # suffix redirection: a name below the zone gets the apex data
        label = f"{r['id']} foo.node.ffhh A (redirect)"
        ub, ub_err = short(r["listen4"], args.unbound_port, "foo.node.ffhh", "A")
        kn, kn_err = short(r["listen4"], args.knot_port, "foo.node.ffhh", "A")
        report(ub == kn == r["gw4"], "tagged", label,
               f"expected={r['gw4']} unbound={ub!r} kresd={kn!r}")

        # (the v4.1/v4.2/v4.3 checks above prove the parent N.ntp zone does
        # not shadow the more specific v4.N zone)

    # Untagged loopback: unbound forwards ffhh to the community DNS; so must kresd.
    for name, qtype in (("node.ffhh", "AAAA"), ("1.ntp.services.ffhh", "AAAA")):
        label = f"loopback {name} {qtype} (forwarded)"
        ub, ub_err = short("127.0.0.1", args.unbound_port, name, qtype)
        kn, kn_err = short("127.0.0.1", args.knot_port, name, qtype)
        ub_flags, _, _ = flags_and_answer("127.0.0.1", args.unbound_port, name, qtype)
        kn_flags, _, _ = flags_and_answer("127.0.0.1", args.knot_port, name, qtype)
        ok = ub == kn and ub is not None and kn is not None \
            and not has_aa(ub_flags) and not has_aa(kn_flags)
        report(ok, "untagged", label, f"unbound={ub!r} kresd={kn!r}")

    print(f"\n{passes} passed, {failures} failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
