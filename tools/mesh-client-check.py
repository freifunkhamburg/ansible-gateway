#!/usr/bin/env python3
"""mesh-client-check.py — probe a gateway's client-facing services the way a
client attached to the mesh would see them.

Tests (one set per served region / bat-<region> interface):

  v4 uplink   ICMP echo to an internet target sourced from the region address,
              plus a source-bound HTTP request to an "what is my IP" endpoint;
              the observed egress address must fall inside the SNAT range.
  v6 uplink   ICMPv6 echo from the region address to an internet target, and
              the mesh routing table's v6 default (RADV-provided next hop).
  DNS         recursive query to the gateway's own region address (v4 and v6),
              verifying an answer and the RA (recursion available) flag.
  NTP         SNTP exchange with the gateway on port 123 (v4 and v6),
              verifying stratum and offset against the local clock.
  RA          Router Solicitation (ICMPv6 133) on the bat interface, collecting
              Router Advertisements (134): lifetime > 0, PIO with L/A flags.
  DHCPv6      INFORMATION-REQUEST (type 11) to ff02::1:2 with an ORO, parsing
              the REPLY: dns-servers (23) and information-refresh-time (32).

Raw sockets (RA, DHCPv6) need root. Run on the gateway (gateway mode, the
gateway's own region addresses are used as the "client" source) or on a mesh
host with --gateway-v4/--gateway-v6 (client mode; the host's own addresses
are the source).

Examples:
  sudo ./mesh-client-check.py --snat-range 193.96.224.24-193.96.224.31
  sudo ./mesh-client-check.py --interface bat-hhsued --json
  ./mesh-client-check.py --gateway-v4 10.112.64.255 --gateway-v6 2a03:2267:1::50

Exit status: 0 = all pass (skips allowed), 2 = at least one failure.
"""

import argparse
import http.client
import json
import os
import random
import socket
import struct
import subprocess
import sys
import time
from urllib.parse import urlsplit

# --- constants ----------------------------------------------------------
NTP_PORT = 123
SNTP_EPOCH = 2208988800          # 1900 -> 1970
IPPROTO_ICMPV6 = 58
MSG_RS = 133
MSG_RA = 134
# Linux/Python values (they differ from the RFC numbering used elsewhere)
IPV6_MULTICAST_IF = socket.IPV6_MULTICAST_IF
IPV6_MULTICAST_HOPS = socket.IPV6_MULTICAST_HOPS
IPV6_RECVPKTINFO = socket.IPV6_RECVPKTINFO
MSG_INFO_REQUEST = 11
MSG_REPLY = 7
OPT_CLIENTID = 1
OPT_SERVERID = 2
OPT_ORO = 6
OPT_DNS = 23
OPT_INFO_REFRESH = 32

PASS, FAIL, SKIP, WARN = "pass", "fail", "skip", "warn"


# --- helpers ------------------------------------------------------------
def sh(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:  # noqa: BLE001
        return 127, "", str(e)


def load_ifaces():
    rc, out, _ = sh(["ip", "-j", "addr", "show"])
    if rc != 0:
        return {}
    result = {}
    for entry in json.loads(out):
        name = entry.get("ifname")
        if not name:
            continue
        v4 = [a["local"] for a in entry.get("addr_info", [])
              if a.get("family") == "inet" and a.get("scope") == "global"]
        v6 = [a["local"] for a in entry.get("addr_info", [])
              if a.get("family") == "inet6" and a.get("scope") == "global"]
        result[name] = {"v4": v4, "v6": v6}
    return result


def prefix64(addr):
    return addr.split("/")[0]


def ping(target, family, source=None, count=2, timeout=4):
    binary = "ping" if family == 4 else "ping6"
    cmd = [binary, "-c", str(count), "-W", str(timeout)]
    if source:
        cmd += ["-I", source]
    cmd.append(target)
    rc, out, err = sh(cmd, timeout=timeout * count + 3)
    ok = rc == 0 and "0% packet loss" in out
    return ok, (out + err).strip()


def egress_ip(url, source):
    """Fetch a 'what is my IP' endpoint with a fixed source address."""
    u = urlsplit(url)
    if u.scheme != "http":
        return None, "only http:// endpoints are supported"
    conn = http.client.HTTPConnection(
        u.hostname, u.port or 80, timeout=8, source_address=(source, 0))
    try:
        conn.request("GET", u.path or "/")
        body = conn.getresponse().read().decode("utf-8", "replace").strip()
        return body.split()[0] if body else None, "empty response"
    except Exception as e:  # noqa: BLE001
        return None, str(e)
    finally:
        conn.close()


def in_range(ip, rng):
    try:
        start, end = rng.split("-")
        a = int("".join(f"{int(o):02d}" for o in start.split(".")))
        b = int("".join(f"{int(o):02d}" for o in end.split(".")))
        c = int("".join(f"{int(o):02d}" for o in ip.split(".")))
        return a <= c <= b
    except Exception:  # noqa: BLE001
        return False


def sntp(host, family, source=None, timeout=5):
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        if source:
            sock.bind((source, 0))
        req = bytes([0x23, 0x00, 0x00, 0x00] + [0] * 44)  # LI=0 VN=4 mode=3
        t1 = time.time()
        sock.sendto(req, (host, NTP_PORT))
        data, _ = sock.recvfrom(512)
        t4 = time.time()
    finally:
        sock.close()
    if len(data) < 48:
        return None, f"short response ({len(data)} bytes)"
    stratum = data[1]
    root_delay = struct.unpack("!I", data[4:8])[0] / 65536.0
    root_disp = struct.unpack("!I", data[8:12])[0] / 65536.0
    sec, frac = struct.unpack("!II", data[40:48])
    tx = sec + frac / 2 ** 32 - SNTP_EPOCH
    offset = tx - (t1 + t4) / 2.0
    return {"stratum": stratum, "offset": offset,
            "root_delay": root_delay, "root_dispersion": root_disp}, "ok"


def dns_query(server, name, family, source=None, timeout=5):
    tid = random.randint(0, 0xFFFF)
    header = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b""
    for label in name.rstrip(".").split("."):
        qname += bytes([len(label)]) + label.encode()
    qname += b"\x00"
    msg = header + qname + struct.pack("!HH", 1, 1)  # A, IN
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        if source:
            sock.bind((source, 0))
        sock.sendto(msg, (server, 53))
        data, _ = sock.recvfrom(2048)
    finally:
        sock.close()
    rid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    rcode = flags & 0xF
    ra = bool(flags & 0x0080)
    return {"rcode": rcode, "ra": ra, "answers": an, "authority": ns,
            "id_match": rid == tid}, "ok"


def parse_ra(data):
    hop_limit = data[4]
    flags = data[5]
    lifetime = struct.unpack("!H", data[6:8])[0]
    prefixes = []
    mtu = None
    rdnss = []
    i = 16
    while i + 2 <= len(data):
        typ = data[i]
        length = data[i + 1] * 8 if data[i + 1] else 8
        if length == 0 or i + length > len(data):
            break
        if typ == 3 and length >= 32:  # prefix information
            prefixes.append({
                "prefix": socket.inet_ntop(socket.AF_INET6, data[i + 16:i + 32]),
                "length": data[i + 2],
                "flags": data[i + 3],
            })
        elif typ == 5 and length >= 8:  # MTU
            mtu = struct.unpack("!I", data[i + 4:i + 8])[0]
        elif typ == 25 and length >= 24:  # RDNSS (RFC 6106)
            for j in range(i + 8, i + length, 16):
                rdnss.append(socket.inet_ntop(socket.AF_INET6, data[j:j + 16]))
        i += length
    return {
        "hop_limit": hop_limit,
        "flags": flags,
        "lifetime": lifetime,
        "prefixes": prefixes,
        "mtu": mtu,
        "rdnss": rdnss,
    }


def ra_probe(ifname, timeout=6):
    idx = socket.if_nametoindex(ifname)
    sock = socket.socket(socket.AF_INET6, socket.SOCK_RAW, IPPROTO_ICMPV6)
    try:
        sock.setsockopt(socket.IPPROTO_IPV6, IPV6_MULTICAST_IF,
                        struct.pack("I", idx))
        sock.setsockopt(socket.IPPROTO_IPV6, IPV6_MULTICAST_HOPS, 255)
        sock.setsockopt(socket.IPPROTO_IPV6, IPV6_RECVPKTINFO, 1)
        sock.settimeout(1.0)
        sock.sendto(struct.pack("!BBH", MSG_RS, 0, 0), ("ff02::2", 0))
        ras = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _anc, _flags, addr = sock.recvmsg(4096)
            except socket.timeout:
                continue
            if data[0] == MSG_RA:
                ras[addr[0]] = parse_ra(data)
    finally:
        sock.close()
    return ras


def dhcp6_info(ifname, timeout=6):
    idx = socket.if_nametoindex(ifname)
    mac = open(f"/sys/class/net/{ifname}/address").read().strip()
    hw = bytes(int(x, 16) for x in mac.split(":"))
    duid = struct.pack("!H", 3) + struct.pack("!H", 1) + hw  # DUID-LL
    client_id = struct.pack("!HH", OPT_CLIENTID, len(duid)) + duid
    oro = struct.pack("!HH", OPT_ORO, 4) + struct.pack("!HH", OPT_DNS, OPT_INFO_REFRESH)
    txid = os.urandom(3)
    msg = bytes([MSG_INFO_REQUEST]) + txid + client_id + oro

    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, IPV6_MULTICAST_IF,
                        struct.pack("I", idx))
        sock.setsockopt(socket.IPPROTO_IPV6, IPV6_MULTICAST_HOPS, 1)
        try:
            sock.bind(("::", 546))
        except OSError:
            sock.bind(("::", 0))
        sock.settimeout(1.0)
        sock.sendto(msg, ("ff02::1:2", 547))
        replies = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            if data[0] != MSG_REPLY or data[1:4] != txid:
                continue
            opts = {}
            i = 4
            while i + 4 <= len(data):
                code, length = struct.unpack("!HH", data[i:i + 4])
                opts[code] = data[i + 4:i + 4 + length]
                i += 4 + length
            dns = []
            if OPT_DNS in opts:
                dns = [socket.inet_ntop(socket.AF_INET6, opts[OPT_DNS][j:j + 16])
                       for j in range(0, len(opts[OPT_DNS]), 16)]
            refresh = (struct.unpack("!I", opts[OPT_INFO_REFRESH])[0]
                       if OPT_INFO_REFRESH in opts else None)
            replies.append({
                "source": addr[0],
                "dns": dns,
                "refresh": refresh,
                "has_server_id": OPT_SERVERID in opts,
            })
    finally:
        sock.close()
    return replies


# --- result bookkeeping --------------------------------------------------
def result(results, name, status, detail):
    results.append({"test": name, "status": status, "detail": detail})


def bird_check(results):
    """Gateway-mode: RADV must be up and exporting the default route."""
    for binary in ("birdc", "/usr/sbin/birdc"):
        rc, out, err = sh([binary, "show", "protocols"], timeout=10)
        if rc == 0:
            break
    else:
        result(results, "gateway radv", SKIP, f"birdc unavailable: {err.strip()}")
        return
    lines = [l for l in out.splitlines() if l.strip()]
    radv = [l for l in lines if "RAdv" in l]
    up = [l for l in radv if " up " in l]
    result(results, "gateway radv", PASS if up else FAIL,
           f"{radv[0].strip() if radv else 'no RAdv protocol found'}")

    if up:
        rc, out, _ = sh([binary, "show", "protocols", "all", "radv1"], timeout=10)
        exported = None
        for line in out.splitlines():
            if line.strip().startswith("Routes:"):
                m = line.split("exported")[0].split(",")
                try:
                    exported = int(m[-1].strip())
                except ValueError:
                    pass
        result(results, "gateway radv default route",
               PASS if exported == 1 else FAIL,
               f"default route exported={exported} (expected 1)")

    # uplink transit (gateway mode)
    rc, out, _ = sh(["ip", "-j", "addr", "show", "uplink"])
    v4 = []
    if rc == 0:
        for entry in json.loads(out):
            v4 += [a["local"] for a in entry.get("addr_info", [])
                   if a.get("family") == "inet"]
    result(results, "gateway uplink v4 address",
           PASS if any(ip.startswith("100.64.") for ip in v4) else FAIL,
           f"uplink v4: {', '.join(v4) or 'none'}")

    rc, out, _ = sh(["ip", "route", "show", "table", "42"])
    ok4 = any(l.startswith("default") for l in out.splitlines())
    result(results, "gateway uplink v4 transit route",
           PASS if ok4 else FAIL,
           "default route in table 42" if ok4 else "no default route in table 42")

    rc, out, _ = sh(["ip", "-6", "route", "show", "table", "42"])
    ok6 = any(l.startswith("default") for l in out.splitlines())
    next_hop = next((l.split()[2] for l in out.splitlines() if l.startswith("default")), None)
    result(results, "gateway uplink v6 transit route",
           PASS if ok6 else FAIL,
           f"default via {next_hop} in table 42" if ok6 else "no v6 default route in table 42")


# --- main ----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interface", action="append", default=None,
                    help="bat-<region> interface(s); default: all bat-* with a global v4")
    ap.add_argument("--gateway-v4", help="gateway address (client mode / override)")
    ap.add_argument("--gateway-v6", help="gateway IPv6 address (client mode / override)")
    ap.add_argument("--snat-range", help="e.g. 193.96.224.24-193.96.224.31")
    ap.add_argument("--echo-url", default="http://ipv4.icanhazip.com",
                    help="'what is my IP' endpoint (http only)")
    ap.add_argument("--v4-target", default="1.1.1.1")
    ap.add_argument("--v6-target", default="2606:4700:4700::1111")
    ap.add_argument("--dns-name", default="freifunk.net")
    ap.add_argument("--timeout", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-raw", action="store_true",
                    help="skip RA/DHCPv6 (not root)")
    args = ap.parse_args()

    ifaces = load_ifaces()
    if args.interface:
        chosen = args.interface
    else:
        chosen = [n for n, a in sorted(ifaces.items())
                  if n.startswith("bat-") and a["v4"]]
        if not chosen and args.gateway_v4:
            # client mode: use the mesh NIC (any global address in 10.112.0.0/16)
            chosen = [n for n, a in sorted(ifaces.items())
                      if any(ip.startswith("10.112.") for ip in a["v4"])]
    if not chosen:
        print("no bat-* interface with a v4 address found "
              "(on a client: pass --interface and --gateway-v4)", file=sys.stderr)
        return 2

    results = []
    raw_allowed = os.geteuid() == 0 and not args.skip_raw

    for ifname in chosen:
        info = ifaces.get(ifname, {"v4": [], "v6": []})
        gw_v4 = args.gateway_v4 or (info["v4"][0] if info["v4"] else None)
        gw_v6 = args.gateway_v6 or (prefix64(info["v6"][0]) if info["v6"] else None)
        if not gw_v4 or not gw_v6:
            result(results, f"addresses ({ifname})", SKIP,
                   "no v4 and/or v6 global address on the interface")
            continue

        tag = ifname

        # --- v4 uplink --------------------------------------------------
        ok, out = ping(args.v4_target, 4, source=gw_v4)
        result(results, f"v4 uplink ping ({tag})",
               PASS if ok else FAIL, f"target {args.v4_target}: {out.splitlines()[-1] if out else 'no output'}")
        ip, err = egress_ip(args.echo_url, gw_v4)
        if ip:
            range_ok = True if not args.snat_range else in_range(ip, args.snat_range)
            detail = f"egress address {ip}"
            if args.snat_range:
                detail += f" (SNAT range {args.snat_range}: {'inside' if range_ok else 'OUTSIDE'})"
            result(results, f"v4 SNAT ({tag})", PASS if range_ok else FAIL, detail)
        else:
            result(results, f"v4 SNAT ({tag})", FAIL, f"egress probe failed: {err}")

        # --- v6 uplink --------------------------------------------------
        ok, out = ping(args.v6_target, 6, source=gw_v6)
        detail = f"target {args.v6_target}: {out.splitlines()[-1] if out else 'no output'}"
        result(results, f"v6 uplink ping ({tag})", PASS if ok else FAIL, detail)

        # --- DNS --------------------------------------------------------
        for fam, addr in ((4, gw_v4), (6, gw_v6)):
            family = socket.AF_INET if fam == 4 else socket.AF_INET6
            try:
                r, err = dns_query(addr, args.dns_name, family, source=addr)
            except Exception as e:  # noqa: BLE001
                result(results, f"dns v{fam} ({tag})", FAIL, str(e))
                continue
            ok = r["rcode"] == 0 and r["ra"] and (r["answers"] or r["authority"]) and r["id_match"]
            result(results, f"dns v{fam} ({tag})", PASS if ok else FAIL,
                   f"{args.dns_name} via {addr}: rcode={r['rcode']} RA={r['ra']} "
                   f"answers={r['answers']} authority={r['authority']}")

        # --- NTP --------------------------------------------------------
        for fam, addr in ((4, gw_v4), (6, gw_v6)):
            family = socket.AF_INET if fam == 4 else socket.AF_INET6
            try:
                r, err = sntp(addr, family, source=addr)
            except Exception as e:  # noqa: BLE001
                result(results, f"ntp v{fam} ({tag})", FAIL, str(e))
                continue
            if r is None:
                result(results, f"ntp v{fam} ({tag})", FAIL, err)
                continue
            ok = 1 <= r["stratum"] <= 15 and abs(r["offset"]) <= 2.0
            result(results, f"ntp v{fam} ({tag})", PASS if ok else FAIL,
                   f"{addr}: stratum={r['stratum']} offset={r['offset'] * 1000:.1f} ms "
                   f"root_disp={r['root_dispersion'] * 1000:.1f} ms")

        # --- RA ---------------------------------------------------------
        if not raw_allowed:
            result(results, f"ra ({tag})", SKIP, "not root (raw ICMPv6 needs CAP_NET_RAW)")
            result(results, f"dhcpv6 ({tag})", SKIP, "not root")
            continue
        try:
            ras = ra_probe(ifname, timeout=args.timeout)
        except PermissionError as e:
            result(results, f"ra ({tag})", SKIP, str(e))
            ras = {}
        except Exception as e:  # noqa: BLE001
            result(results, f"ra ({tag})", FAIL, str(e))
            ras = None
        if ras is not None:
            if ras:
                good = [src for src, ra in ras.items()
                        if ra["lifetime"] > 0 and ra["rdnss"] and any(
                            p["length"] == 64 and (p["flags"] & 0x80) and (p["flags"] & 0x40)
                            for p in ra["prefixes"])]
                prefixes = sorted({f"{p['prefix']}/{p['length']}"
                                   for ra in ras.values() for p in ra["prefixes"]})
                dns_ns = sorted({a for ra in ras.values() for a in ra["rdnss"]})
                detail = (f"{len(ras)} RA(s) from {len(ras)} router(s); "
                          f"prefixes {prefixes or 'none'}; rdnss {dns_ns or 'none'}; "
                          f"valid routers (default lifetime + PIO + RDNSS): {len(good)}")
                result(results, f"ra ({tag})", PASS if good else FAIL, detail)
            else:
                # In gateway mode the probe uses the gateway's own bat
                # interface; its own multicast is not delivered to its raw
                # socket. RADV is verified separately via birdc, so an empty
                # result here is a skip, not a failure.
                if args.gateway_v4:
                    result(results, f"ra ({tag})", FAIL,
                           "no Router Advertisement received after RS")
                else:
                    result(results, f"ra ({tag})", SKIP,
                           "no RA on the gateway's own interface; see gateway radv")

        # --- DHCPv6 -----------------------------------------------------
        try:
            replies = dhcp6_info(ifname, timeout=args.timeout)
        except PermissionError as e:
            result(results, f"dhcpv6 ({tag})", SKIP, str(e))
            continue
        except Exception as e:  # noqa: BLE001
            result(results, f"dhcpv6 ({tag})", FAIL, str(e))
            continue
        with_dns = [r for r in replies if r["dns"]]
        if not replies:
            result(results, f"dhcpv6 ({tag})", FAIL, "no REPLY received")
        elif not with_dns:
            result(results, f"dhcpv6 ({tag})", FAIL,
                   f"{len(replies)} reply(ies), none carried DNS servers")
        else:
            detail = "; ".join(
                f"{r['source']} -> dns {r['dns']}, refresh {r['refresh']} "
                f"(server-id {'ok' if r['has_server_id'] else 'missing'})"
                for r in with_dns)
            result(results, f"dhcpv6 ({tag})", PASS, detail)

    if not args.gateway_v4:
        bird_check(results)

    # --- summary ------------------------------------------------------
    counts = {k: sum(1 for r in results if r["status"] == k)
              for k in (PASS, WARN, FAIL, SKIP)}
    failed = counts[FAIL] > 0
    if args.json:
        print(json.dumps({"results": results, "summary": counts,
                          "exit": 2 if failed else 0}, indent=2))
    else:
        for r in results:
            mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}[r["status"]]
            print(f"{mark:4} {r['test']:32} {r['detail']}")
        print(f"\n{counts['pass']} pass, {counts['fail']} fail, "
              f"{counts['skip']} skip, {counts['warn']} warn")
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
