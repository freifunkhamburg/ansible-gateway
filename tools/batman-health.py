#!/usr/bin/env python3
"""batman-health.py — consistency checks for a batman-adv mesh.

The point is to catch the failures that a plain `batctl originators` glance
does not show: duplicated MAC addresses, a node announced as a gateway without
being reachable, conflicting gateway announcements, stale/dead originators and
inconsistent mesh settings across a gateway's several bat interfaces.

Checks (each finding has a stable code):

  hardif   H1 duplicate hardif MAC within a mesh
           H2 hardif MAC equals the mesh MAC
           H3 one hardif attached to several meshes
           H4 inactive (dropped) hardif
           H5 suspicious TAP/VPN MAC (not in the expected prefix range)
  mesh     M1 two meshes with the same MAC
           M2 batman version differs between meshes
           M3 routing algorithm differs between meshes
  orig     O1 duplicated originator path (same path listed twice, or two
           rows claiming to be the best path; multipath rows are normal)
           O2 same originator MAC on two meshes
           O3 best-path tq below threshold (link about to flap)
           O4 originator last-seen beyond the per-mesh limit
           O5 originator MAC reused as a hardif MAC elsewhere
           O6 originator MAC equals another mesh's MAC
  gw       G1 duplicated gateway announcement (conflicting bandwidth)
           G2 gateway without a matching originator (spurious announcement)
           G3 gateway announcing zero bandwidth
           G4 gateway next hop not a known originator/neighbour
           G6 implausible bandwidth (above the uplink)
           G10 gateway announced but its route tq is below threshold
           G7 own MAC announced as gateway while gw_mode is not server
           G8 several gateways on one mesh (informational)
           G9 gateway bandwidth asymmetry
  neigh    N1 neighbour reachable via several hardifs (informational)
           N2 stale neighbour
           N3 neighbour MAC not among the originators

Severity: ERROR (exit 1), WARN, INFO (exit 0 unless --strict).

Data source
  live    batctl netlink JSON (`*_json` commands) — batctl >= 2020;
          falls back to parsing the text tables for older batctl.
  dump    --dump FILE saves the collected state; --from-dump FILE analyses a
          saved state offline (also used by --self-test).

The debug tables need root: run on the gateway itself, e.g.

    sudo ./tools/batman-health.py
    sudo ./tools/batman-health.py --meshes bat-hhsued,bat-hhwest --json
    sudo ./tools/batman-health.py --dump /tmp/batman.json
    ./tools/batman-health.py --from-dump /tmp/batman.json --self-check
    ./tools/batman-health.py --self-test

"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict

# --- constants ----------------------------------------------------------
MAC_RE = re.compile(r"(?i)\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b")
TQ_RE = re.compile(r"\((\d+\.\d+)\)")
SEEN_RE = re.compile(r"(\d+\.\d+)\s*s")
DEFAULT_MESH_PREFIX = "bat-"
# batman-adv purges originators/neighbours after BATADV_PURGE_TIMEOUT
# (net/batman-adv/main.h: 200000 jiffies, i.e. 200 s on HZ=1000).
PURGE_MS = 200000
SEVERITY_ORDER = {"INFO": 0, "WARN": 1, "ERROR": 2}


def norm_mac(value):
    """Normalise a MAC to lowercase colon form."""
    if value is None:
        return None
    value = value.strip().lower().replace("-", ":")
    return value


def mac_ok(value):
    return value is not None and MAC_RE.fullmatch(value) is not None


class Finding:
    __slots__ = ("level", "code", "mesh", "message")

    def __init__(self, level, code, mesh, message):
        self.level = level
        self.code = code
        self.mesh = mesh
        self.message = message

    def as_dict(self):
        return {"level": self.level, "code": self.code,
                "mesh": self.mesh, "message": self.message}

    def __str__(self):
        scope = f"[{self.mesh}]" if self.mesh else "[all]"
        return f"{self.level:5s} {self.code:3s} {scope} {self.message}"


# --- collection ---------------------------------------------------------

def find_batctl(explicit=None):
    if explicit:
        return explicit
    for candidate in ("/usr/local/sbin/batctl", "/usr/sbin/batctl",
                      "/usr/bin/batctl"):
        if os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("batctl")
    if found:
        return found
    raise SystemExit("batctl not found; pass --batctl /path/to/batctl")


def detect_meshes(mesh_prefix):
    names = sorted(n for n in os.listdir("/sys/class/net")
                   if n.startswith(mesh_prefix))
    return names


def run_batctl(batctl, mesh, command, timeout=10):
    """Run a batctl command for one mesh."""
    argv = [batctl, "meshif", mesh, command]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as exc:
        return None, str(exc)
    if proc.returncode != 0:
        return None, proc.stderr.strip() or f"exit {proc.returncode}"
    return proc.stdout, None


def try_json(batctl, mesh, query):
    out, err = run_batctl(batctl, mesh, query)
    if out is None:
        return None, err
    text = out.strip()
    if not text or text[0] not in "[{":
        return None, "no JSON support"
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, "unparsable JSON"


def parse_text_originators(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("originator", "mac")):
            continue
        macs = MAC_RE.findall(line)
        if len(macs) < 2:
            continue
        seen = SEEN_RE.search(line)
        tq = TQ_RE.search(line)
        ifname = line.split()[-1]
        rows.append({
            "orig_address": norm_mac(macs[0]),
            "neigh_address": norm_mac(macs[1]),
            "hard_ifname": ifname,
            "last_seen_msecs": int(float(seen.group(1)) * 1000) if seen else None,
            "tq": int(float(tq.group(1)) * 100) if tq else None,
        })
    return rows


def parse_text_gateways(text):
    rows = []
    for line in text.splitlines():
        macs = MAC_RE.findall(line)
        if len(macs) < 2:
            continue
        # "router (hardif): gw-mac (tq) netmask"
        hardif = None
        m = re.search(r"\(([^)]+)\)", line)
        if m and not MAC_RE.match(m.group(1)):
            hardif = m.group(1).strip()
        tq = TQ_RE.search(line)
        rows.append({
            "orig_address": norm_mac(macs[1]),
            "router": norm_mac(macs[0]),
            "hard_ifname": hardif,
            "tq": int(float(tq.group(1)) * 100) if tq else None,
            "bandwidth_down": None,
            "bandwidth_up": None,
        })
    return rows


def parse_text_neighbors(text):
    rows = []
    for line in text.splitlines():
        macs = MAC_RE.findall(line)
        if not macs:
            continue
        seen = SEEN_RE.search(line)
        rows.append({
            "neigh_address": norm_mac(macs[0]),
            "hard_ifname": line.split()[-1],
            "last_seen_msecs": int(float(seen.group(1)) * 1000) if seen else None,
        })
    return rows


def collect_mesh(batctl, mesh):
    data = {"mesh": {}, "hardifs": [], "originators": [], "gateways": [],
            "neighbors": [], "source": "json"}
    notes = []

    for key, query in (("mesh", "mj"), ("hardifs", "hj"),
                       ("originators", "oj"), ("gateways", "gwj"),
                       ("neighbors", "nj")):
        payload, err = try_json(batctl, mesh, query)
        if payload is None:
            notes.append(f"{key}: {err}")
            continue
        if key == "mesh":
            data["mesh"] = payload if isinstance(payload, dict) else {}
        elif isinstance(payload, list):
            data[key] = payload

    if notes:
        data["source"] = "text"
        data["notes"] = notes
        out, _ = run_batctl(batctl, mesh, "o")
        if out:
            data["originators"] = parse_text_originators(out)
        out, _ = run_batctl(batctl, mesh, "gwl")
        if out:
            data["gateways"] = parse_text_gateways(out)
        out, _ = run_batctl(batctl, mesh, "n")
        if out:
            data["neighbors"] = parse_text_neighbors(out)

    return data


def collect(batctl, meshes):
    return {"meshes": {m: collect_mesh(batctl, m) for m in meshes},
            "batctl": batctl, "generation": "live"}


# --- checks -------------------------------------------------------------

def check(data, opts):
    findings = []
    meshes = data["meshes"]

    def add(level, code, mesh, message):
        findings.append(Finding(level, code, mesh, message))

    # H1/H2/H4/H5 + mesh registry ---------------------------------------
    all_mesh_macs = {}
    hardif_owner = defaultdict(list)     # mac -> [(mesh, ifname)]
    ifname_owner = defaultdict(list)     # ifname -> [mesh]
    for mesh, mdata in meshes.items():
        mesh_mac = norm_mac(mdata.get("mesh", {}).get("mesh_address"))
        if mac_ok(mesh_mac):
            all_mesh_macs.setdefault(mesh_mac, []).append(mesh)
        seen_hard = defaultdict(list)
        for hardif in mdata.get("hardifs", []):
            mac = norm_mac(hardif.get("hard_address"))
            ifname = hardif.get("hard_ifname")
            if mac:
                seen_hard[mac].append(ifname)
                hardif_owner[mac].append((mesh, ifname))
            if ifname:
                ifname_owner[ifname].append(mesh)
            if mac and mesh_mac and mac == mesh_mac:
                add("ERROR", "H2", mesh,
                    f"hardif {ifname} MAC {mac} equals the mesh MAC")
            if hardif.get("active") is False:
                add("WARN", "H4", mesh,
                    f"hardif {ifname} ({mac}) is inactive/dropped")
            if mac and opts.expect_prefix and ifname and \
                    ifname.startswith(opts.ifname_prefix):
                if not mac.startswith(opts.expect_prefix):
                    add("WARN", "H5", mesh,
                        f"{ifname} MAC {mac} is outside the expected prefix "
                        f"{opts.expect_prefix}")
        for mac, ifnames in seen_hard.items():
            if len(ifnames) > 1:
                add("ERROR", "H1", mesh,
                    f"hardif MAC {mac} appears on {len(ifnames)} hardifs "
                    f"({', '.join(sorted(ifnames))})")

    for mac, owners in hardif_owner.items():
        where = {(m, i) for m, i in owners}
        if len(where) > 1 and len({m for m, _ in where}) > 1:
            add("ERROR", "H1", None,
                f"hardif MAC {mac} is used in several meshes: "
                + ", ".join(sorted(f"{m}/{i}" for m, i in where)))
    for ifname, msh in ifname_owner.items():
        if len(set(msh)) > 1:
            add("ERROR", "H3", None,
                f"device {ifname} is a hardif of {len(set(msh))} meshes "
                f"({', '.join(sorted(set(msh)))})")

    # M1/M2/M3 ----------------------------------------------------------
    for mac, msh in all_mesh_macs.items():
        if len(msh) > 1:
            add("ERROR", "M1", None,
                f"meshes {', '.join(sorted(msh))} share MAC {mac}")
    versions = {(m, d.get("mesh", {}).get("version"))
                for m, d in meshes.items() if d.get("mesh", {}).get("version")}
    if len({v for _, v in versions}) > 1:
        grouped = defaultdict(list)
        for mesh, version in versions:
            grouped[version].append(mesh)
        add("WARN", "M2", None,
            "batman version differs between meshes: "
            + "; ".join(f"{'/'.join(sorted(v))} = {ver}"
                        for ver, v in sorted(grouped.items())))
    algos = {(m, d.get("mesh", {}).get("algo_name"))
             for m, d in meshes.items() if d.get("mesh", {}).get("algo_name")}
    if len({a for _, a in algos}) > 1:
        add("WARN", "M3", None,
            "routing algorithm differs between meshes: "
            + ", ".join(sorted(f"{m}={a}" for m, a in algos)))

    # O1..O6 ------------------------------------------------------------
    # The originator dump emits one row per (originator, neighbour) path, so
    # several rows for one MAC are normal multipath. A duplicate is only
    # suspicious when the same path is listed twice or when more than one row
    # claims to be the best path.
    orig_mesh = defaultdict(list)
    for mesh, mdata in meshes.items():
        own = norm_mac(mdata.get("mesh", {}).get("mesh_address"))
        interval = int(mdata.get("mesh", {}).get("orig_interval") or 1000)
        stale_limit = max(opts.stale_ms, 3 * interval)
        pairs = defaultdict(int)
        best_count = defaultdict(int)
        for origin in mdata.get("originators", []):
            mac = norm_mac(origin.get("orig_address"))
            if not mac:
                continue
            pairs[(mac, norm_mac(origin.get("neigh_address")))] += 1
            if origin.get("best"):
                best_count[mac] += 1
            # the local node's own originator entry (its mesh MAC) is normal
            if mac != own:
                orig_mesh[mac].append(mesh)
            tq = origin.get("tq")
            if origin.get("best") and tq is not None and tq < opts.tq_min:
                add("WARN", "O3", mesh,
                    f"best path to originator {mac} has tq={tq} below "
                    f"{opts.tq_min}")
            seen = origin.get("last_seen_msecs")
            if seen is not None and seen > stale_limit:
                level = "ERROR" if seen > PURGE_MS else "WARN"
                add(level, "O4", mesh,
                    f"originator {mac} via {origin.get('hard_ifname')} last "
                    f"seen {seen} ms ago (limit {stale_limit} ms)")
        for (mac, neigh), count in pairs.items():
            if count > 1:
                add("ERROR", "O1", mesh,
                    f"originator {mac} has {count} identical entries via "
                    f"neighbour {neigh}")
        for mac, count in best_count.items():
            if count > 1:
                add("ERROR", "O1", mesh,
                    f"originator {mac} has {count} entries flagged best "
                    "(conflicting best path)")
    for mac, msh in orig_mesh.items():
        if len(set(msh)) > 1:
            add("WARN", "O2", None,
                f"originator {mac} appears on meshes "
                + ", ".join(sorted(set(msh))))
        owners = {m for m, _ in hardif_owner.get(mac, [])}
        if owners:
            add("ERROR", "O5", None,
                f"originator MAC {mac} is also a hardif MAC in "
                + ", ".join(sorted(owners)))
        foreign = set(all_mesh_macs.get(mac, [])) - set(orig_mesh[mac])
        if foreign:
            add("ERROR", "O6", None,
                f"originator MAC {mac} equals the mesh MAC of "
                + ", ".join(sorted(foreign)))

    # G1..G10 -----------------------------------------------------------
    # `router` in a gateway row is the *next-hop neighbour* MAC used to reach
    # the gateway (bat_iv_ogm.c / bat_v.c put router->addr there), not the
    # gateway's own router MAC. Two gateways sharing one next hop is normal.
    for mesh, mdata in meshes.items():
        gateways = mdata.get("gateways", [])
        origs = {norm_mac(o.get("orig_address"))
                 for o in mdata.get("originators", [])}
        origs.discard(None)
        neighbors = {norm_mac(n.get("neigh_address"))
                     for n in mdata.get("neighbors", [])}
        neighbors.discard(None)
        own_mac = norm_mac(mdata.get("mesh", {}).get("mesh_address"))
        gw_mode = mdata.get("mesh", {}).get("gw_mode")
        known = set(origs) | set(neighbors)
        if own_mac:
            known.add(own_mac)

        counts = defaultdict(int)
        for gw in gateways:
            mac = norm_mac(gw.get("orig_address"))
            router = norm_mac(gw.get("router"))
            if mac:
                counts[mac] += 1
            if mac and mac not in known:
                add("WARN", "G2", mesh,
                    f"gateway {mac} announced but has no originator entry "
                    "(spurious announcement / dead node)")
            if router and router not in known:
                add("WARN", "G4", mesh,
                    f"gateway {mac} next-hop MAC {router} is neither an "
                    "originator nor a neighbour")
            down = gw.get("bandwidth_down")
            up = gw.get("bandwidth_up")
            if down == 0 or up == 0:
                add("ERROR", "G3", mesh,
                    f"gateway {mac} announces zero bandwidth "
                    f"(down={down}, up={up})")
            if down and down > opts.max_bw:
                add("WARN", "G6", mesh,
                    f"gateway {mac} announces down={down} Mbit, above the "
                    f"expected uplink {opts.max_bw} Mbit")
            if down and up and down / max(up, 1) > opts.max_ratio:
                add("INFO", "G9", mesh,
                    f"gateway {mac} bandwidth asymmetry down={down}/up={up}")
            tq = gw.get("tq")
            if tq is not None and tq < opts.tq_min:
                add("WARN", "G10", mesh,
                    f"gateway {mac} announced but the route to it has "
                    f"tq={tq} below {opts.tq_min}")
            if own_mac and mac == own_mac and gw_mode != "server":
                add("ERROR", "G7", mesh,
                    f"own MAC {mac} announced as a gateway while gw_mode is "
                    f"{gw_mode!r} (likely a duplicate MAC)")
        for mac, count in counts.items():
            if count > 1:
                seen_bw = sorted({(g.get("bandwidth_down"), g.get("bandwidth_up"))
                                  for g in gateways
                                  if norm_mac(g.get("orig_address")) == mac})
                add("ERROR", "G1", mesh,
                    f"gateway {mac} announced {count} times, bandwidths "
                    f"{seen_bw} (conflicting announcements)")
        if len(counts) > 1:
            add("INFO", "G8", mesh,
                f"{len(counts)} gateways announced on this mesh")

    # N1..N3 ------------------------------------------------------------
    for mesh, mdata in meshes.items():
        origs = {norm_mac(o.get("orig_address"))
                 for o in mdata.get("originators", [])}
        origs.discard(None)
        own = norm_mac(mdata.get("mesh", {}).get("mesh_address"))
        interval = int(mdata.get("mesh", {}).get("orig_interval") or 1000)
        stale_limit = max(opts.stale_ms, 3 * interval)
        per_neigh = defaultdict(set)
        for neigh in mdata.get("neighbors", []):
            mac = norm_mac(neigh.get("neigh_address"))
            if not mac:
                continue
            per_neigh[mac].add(neigh.get("hard_ifname"))
            seen = neigh.get("last_seen_msecs")
            if seen is not None and seen > stale_limit:
                add("WARN", "N2", mesh,
                    f"neighbour {mac} last seen {seen} ms ago "
                    f"on {neigh.get('hard_ifname')} (limit {stale_limit} ms)")
            if mac not in origs and mac != own:
                add("WARN", "N3", mesh,
                    f"neighbour {mac} is not among the originators")
        for mac, ifnames in per_neigh.items():
            names = sorted(i for i in ifnames if i)
            if len(names) > 1:
                # a neighbour reachable via several of our interfaces is normal
                add("INFO", "N1", mesh,
                    f"neighbour {mac} is reachable via {len(names)} interfaces "
                    f"({', '.join(names)})")

    unique = {}
    for finding in findings:
        key = (finding.code, finding.mesh or "", finding.message)
        unique.setdefault(key, finding)
    findings = list(unique.values())
    findings.sort(key=lambda f: (-SEVERITY_ORDER[f.level], f.code, f.mesh or ""))
    return findings


# --- fixtures -----------------------------------------------------------

GOOD = {
    "meshes": {
        "bat-hhsued": {
            "mesh": {"version": "2026.3", "algo_name": "BATMAN_IV",
                     "mesh_address": "de:ad:be:ef:50:10", "gw_mode": "server",
                     "orig_interval": 1000},
            "hardifs": [
                {"hard_ifname": "mesh-hhsued", "hard_address": "de:ad:be:ef:50:1f",
                 "active": True},
                {"hard_ifname": "vpn-hhsued-05", "hard_address": "de:ad:be:ef:50:15",
                 "active": True},
            ],
            # one row per (originator, neighbour) path: multipath is normal
            "originators": [
                {"orig_address": "de:ad:be:ef:50:10", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "tq": 255, "last_seen_msecs": 0,
                 "best": True},
                {"orig_address": "de:ad:be:ef:60:15", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "tq": 98, "last_seen_msecs": 120,
                 "best": True},
                {"orig_address": "de:ad:be:ef:60:15", "neigh_address": "de:ad:be:ef:50:15",
                 "hard_ifname": "vpn-hhsued-05", "tq": 90, "last_seen_msecs": 300},
                {"orig_address": "de:ad:be:ef:20:12", "neigh_address": "de:ad:be:ef:20:1f",
                 "hard_ifname": "mesh-hhsued", "tq": 225, "last_seen_msecs": 200,
                 "best": True},
                {"orig_address": "de:ad:be:ef:20:1f", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "tq": 225, "last_seen_msecs": 150,
                 "best": True},
            ],
            "gateways": [
                {"orig_address": "de:ad:be:ef:50:10", "router": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "bandwidth_down": 100,
                 "bandwidth_up": 20, "tq": 255},
                {"orig_address": "de:ad:be:ef:20:12", "router": "de:ad:be:ef:20:1f",
                 "hard_ifname": "mesh-hhsued", "bandwidth_down": 100,
                 "bandwidth_up": 20, "tq": 225},
            ],
            "neighbors": [
                {"neigh_address": "de:ad:be:ef:20:1f", "hard_ifname": "mesh-hhsued",
                 "last_seen_msecs": 100},
                {"neigh_address": "de:ad:be:ef:60:15", "hard_ifname": "vpn-hhsued-05",
                 "last_seen_msecs": 100},
            ],
        },
    },
}

BROKEN = {
    "meshes": {
        "bat-hhsued": {
            "mesh": {"version": "2026.3", "algo_name": "BATMAN_IV",
                     "mesh_address": "de:ad:be:ef:50:10", "gw_mode": "server",
                     "orig_interval": 1000},
            "hardifs": [
                {"hard_ifname": "mesh-hhsued", "hard_address": "de:ad:be:ef:50:1f",
                 "active": True},
                {"hard_ifname": "vpn-hhsued-05", "hard_address": "de:ad:be:ef:50:15",
                 "active": True},
                {"hard_ifname": "vpn-hhsued-08", "hard_address": "de:ad:be:ef:50:15",
                 "active": True},
                {"hard_ifname": "vpn-hhsued-09", "hard_address": "ca:fe:00:00:00:99",
                 "active": False},
                {"hard_ifname": "vpn-hhsued-10", "hard_address": "de:ad:be:ef:50:10",
                 "active": True},
            ],
            "originators": [
                {"orig_address": "de:ad:be:ef:50:10", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "tq": 255, "last_seen_msecs": 0,
                 "best": True},
                {"orig_address": "de:ad:be:ef:50:15", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "vpn-hhsued-05", "tq": 5, "last_seen_msecs": 120,
                 "best": True},
                {"orig_address": "de:ad:be:ef:50:15", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "vpn-hhsued-08", "tq": 98, "last_seen_msecs": 9500},
                {"orig_address": "de:ad:be:ef:50:15", "neigh_address": "aa:bb:cc:dd:ee:01",
                 "hard_ifname": "vpn-hhsued-08", "tq": 90, "last_seen_msecs": 250000},
                {"orig_address": "de:ad:be:ef:50:77", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "vpn-hhsued-05", "tq": 90, "last_seen_msecs": 100,
                 "best": True},
                {"orig_address": "de:ad:be:ef:50:77", "neigh_address": "de:ad:be:ef:50:15",
                 "hard_ifname": "vpn-hhsued-05", "tq": 80, "last_seen_msecs": 200,
                 "best": True},
                {"orig_address": "de:ad:be:ef:50:60", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "tq": 90, "last_seen_msecs": 100,
                 "best": True},
                {"orig_address": "de:ad:be:ef:70:11", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "tq": 90, "last_seen_msecs": 100,
                 "best": True},
                {"orig_address": "ca:fe:00:00:00:99", "neigh_address": "de:ad:be:ef:50:15",
                 "hard_ifname": "vpn-hhsued-08", "tq": 90, "last_seen_msecs": 200,
                 "best": True},
            ],
            "gateways": [
                {"orig_address": "de:ad:be:ef:50:10", "router": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "bandwidth_down": 100,
                 "bandwidth_up": 20, "tq": 255},
                {"orig_address": "de:ad:be:ef:50:70", "router": "de:ad:be:ef:50:10",
                 "hard_ifname": "vpn-hhsued-05", "bandwidth_down": 0,
                 "bandwidth_up": 0, "tq": 225},
                {"orig_address": "de:ad:be:ef:50:70", "router": "de:ad:be:ef:50:10",
                 "hard_ifname": "vpn-hhsued-05", "bandwidth_down": 5000,
                 "bandwidth_up": 20, "tq": 225},
                {"orig_address": "de:ad:be:ef:50:80", "router": "aa:bb:cc:dd:ee:03",
                 "hard_ifname": "mesh-hhsued", "bandwidth_down": 100,
                 "bandwidth_up": 20, "tq": 10},
            ],
            "neighbors": [
                {"neigh_address": "aa:bb:cc:dd:ee:01", "hard_ifname": "vpn-hhsued-05",
                 "last_seen_msecs": 100},
                {"neigh_address": "aa:bb:cc:dd:ee:01", "hard_ifname": "vpn-hhsued-08",
                 "last_seen_msecs": 6000},
                {"neigh_address": "aa:bb:cc:dd:ee:02", "hard_ifname": "mesh-hhsued",
                 "last_seen_msecs": 100},
            ],
        },
        "bat-hhwest": {
            "mesh": {"version": "2024.2", "algo_name": "BATMAN_V",
                     "mesh_address": "de:ad:be:ef:50:10", "gw_mode": "off",
                     "orig_interval": 1000},
            "hardifs": [
                {"hard_ifname": "mesh-hhsued", "hard_address": "de:ad:be:ef:50:1f",
                 "active": True},
            ],
            "originators": [
                {"orig_address": "de:ad:be:ef:50:60", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "tq": 90, "last_seen_msecs": 200,
                 "best": True},
                {"orig_address": "de:ad:be:ef:70:11", "neigh_address": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "tq": 90, "last_seen_msecs": 200,
                 "best": True},
            ],
            "gateways": [
                {"orig_address": "de:ad:be:ef:50:10", "router": "de:ad:be:ef:50:10",
                 "hard_ifname": "mesh-hhsued", "bandwidth_down": 100,
                 "bandwidth_up": 20, "tq": 225},
            ],
            "neighbors": [],
        },
        "bat-hhnowe": {
            "mesh": {"version": "2026.3", "algo_name": "BATMAN_IV",
                     "mesh_address": "de:ad:be:ef:50:60", "gw_mode": "off",
                     "orig_interval": 1000},
            "hardifs": [],
            "originators": [],
            "gateways": [],
            "neighbors": [],
        },
    },
}

EXPECTED_BROKEN = {"H1", "H2", "H3", "H4", "H5", "M1", "M2", "M3",
                   "O1", "O2", "O3", "O4", "O5", "O6",
                   "G1", "G2", "G3", "G4", "G6", "G7", "G9", "G10",
                   "N1", "N2", "N3"}


def self_test(opts):
    good = check(GOOD, opts)
    broken = check(BROKEN, opts)
    codes = {f.code for f in broken}
    failures = []

    if any(f.level != "INFO" for f in good):
        failures.append("GOOD fixture produced WARN/ERROR findings: "
                        + ", ".join(str(f) for f in good))
    missing = EXPECTED_BROKEN - codes
    if missing:
        failures.append("BROKEN fixture did not trigger: "
                        + ", ".join(sorted(missing)))

    print(f"self-test: clean fixture -> {len(good)} findings")
    print(f"self-test: broken fixture -> {len(broken)} findings, codes "
          + ", ".join(sorted(codes)))
    for f in broken:
        print("   ", f)
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1
    print("self-test: OK")
    return 0


# --- reporting ----------------------------------------------------------

def report(findings, data, opts, as_json):
    counts = defaultdict(int)
    for f in findings:
        counts[f.level] += 1

    if opts.motd:
        # Compact block for /etc/motd.tail: one line per mesh plus the
        # problems only (INFO findings are omitted).
        verdict = "OK" if counts.get("ERROR", 0) == 0 else "FAILING"
        print(f"BATMAN HEALTH: {verdict} — {counts.get('ERROR', 0)} error(s), "
              f"{counts.get('WARN', 0)} warning(s)")
        for mesh, mdata in sorted(data["meshes"].items()):
            print(f"  {mesh}: {len(mdata.get('hardifs', []))} hardifs, "
                  f"{len(mdata.get('originators', []))} originators, "
                  f"{len(mdata.get('gateways', []))} gateways")
        problems = [f for f in findings if f.level != "INFO"]
        if not problems:
            print("  no problems found")
        else:
            for f in problems:
                print(f"  {f}")
        return

    if as_json:
        print(json.dumps({
            "ok": not any(f.level == "ERROR" for f in findings),
            "counts": dict(counts),
            "meshes": sorted(data["meshes"]),
            "source": data.get("generation", "live"),
            "findings": [f.as_dict() for f in findings],
        }, indent=2))
        return

    print(f"mesh interfaces: {', '.join(sorted(data['meshes']))}")
    for mesh, mdata in sorted(data["meshes"].items()):
        md = mdata.get("mesh", {})
        print(f"  {mesh}: version={md.get('version')} algo={md.get('algo_name')} "
              f"gw_mode={md.get('gw_mode')} mac={md.get('mesh_address')} "
              f"hardifs={len(mdata.get('hardifs', []))} "
              f"originators={len(mdata.get('originators', []))} "
              f"gateways={len(mdata.get('gateways', []))}")
        if mdata.get("notes"):
            print(f"    degraded to text parsing: {', '.join(mdata['notes'])}")
    print()
    if not findings:
        print("no problems found")
    else:
        for f in findings:
            print(f)
    print(f"\nsummary: {counts.get('ERROR', 0)} error(s), "
          f"{counts.get('WARN', 0)} warning(s), {counts.get('INFO', 0)} info")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Consistency checks for a batman-adv mesh.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit: 0 clean (or only WARN/INFO), 1 ERROR found, 2 collection failed.")
    parser.add_argument("--batctl", default=None, help="path to batctl")
    parser.add_argument("--meshes", default="",
                        help="comma separated mesh interfaces (default: all bat-*)")
    parser.add_argument("--mesh-prefix", default=DEFAULT_MESH_PREFIX,
                        help="prefix used to auto-detect mesh interfaces")
    parser.add_argument("--tq-min", type=int, default=20,
                        help="originator TQ warning threshold (percent, default 20)")
    parser.add_argument("--stale-ms", type=int, default=3000,
                        help="last-seen warning threshold in ms (default 3000)")
    parser.add_argument("--max-bw", type=int, default=1000,
                        help="plausible gateway download bandwidth in Mbit (default 1000)")
    parser.add_argument("--max-ratio", type=float, default=10.0,
                        help="max down/up bandwidth ratio before INFO (default 10)")
    parser.add_argument("--expect-prefix", default="de:ad:be:ef",
                        help="expected TAP MAC prefix (default de:ad:be:ef)")
    parser.add_argument("--ifname-prefix", default="vpn-",
                        help="hardif name prefix that must match --expect-prefix")
    parser.add_argument("--dump", default=None, help="write the collected state to FILE")
    parser.add_argument("--from-dump", default=None, help="analyse a saved state instead of the live mesh")
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--motd", action="store_true",
                        help="compact block for /etc/motd.tail (problems only)")
    parser.add_argument("--strict", action="store_true", help="exit 1 also on warnings")
    parser.add_argument("--self-test", action="store_true", help="run the bundled fixtures")
    args = parser.parse_args(argv)

    opts = argparse.Namespace(**vars(args))

    if args.self_test:
        return self_test(opts)

    if args.from_dump:
        try:
            with open(args.from_dump) as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"cannot read dump {args.from_dump}: {exc}", file=sys.stderr)
            return 2
        data.setdefault("generation", "dump")
    else:
        batctl = find_batctl(args.batctl)
        if os.geteuid() != 0:
            print("warning: the batman debug tables need root; results will be "
                  "empty or degraded", file=sys.stderr)
        meshes = [m.strip() for m in args.meshes.split(",") if m.strip()]
        if not meshes:
            meshes = detect_meshes(args.mesh_prefix)
        if not meshes:
            print("no batman mesh interfaces found", file=sys.stderr)
            return 2
        data = collect(batctl, meshes)
        if args.dump:
            with open(args.dump, "w") as handle:
                json.dump(data, handle, indent=2)

    findings = check(data, opts)
    report(findings, data, opts, args.json)

    if any(f.level == "ERROR" for f in findings):
        return 1
    if args.strict and any(f.level == "WARN" for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
