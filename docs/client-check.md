# Client-view service check (gw05)

`tools/mesh-client-check.py` probes a gateway's client-facing services the way
a host attached to the mesh would see them. It is wired into
`playbooks/gateway-client-check.yml` (installs it as
`/usr/local/bin/mesh-client-check` and runs it with `gw_public_snat_range`).

```sh
ansible-playbook playbooks/gateway-client-check.yml
# or on the gateway directly:
sudo ./tools/mesh-client-check.py --snat-range 193.96.224.24-193.96.224.31
sudo ./tools/mesh-client-check.py --interface bat-hhsued --json
```

On a real client (no `bat-*` interface) use client mode:

```sh
./tools/mesh-client-check.py --interface eth0 \
  --gateway-v4 10.112.64.255 --gateway-v6 2a03:2267:1::50
```

Exit status: `0` all pass (skips allowed), `2` at least one failure.
`--json` emits `{"results": [...], "summary": {...}, "exit": ...}`.

## What is asserted, per served region

| test | probe | pass condition |
|---|---|---|
| `v4 uplink ping` | ICMP echo to `--v4-target` (default `1.1.1.1`), source = region address | 0% loss |
| `v4 SNAT` | HTTP GET to `--echo-url` with the region address as source | returned address inside `--snat-range` |
| `v6 uplink ping` | ICMPv6 echo to `--v6-target` (default `2606:4700:4700::1111`) | 0% loss |
| `dns v4` / `dns v6` | recursive query (`--dns-name`) to the gateway's region address | rcode 0, RA flag, answer/authority present |
| `ntp v4` / `ntp v6` | SNTP request to port 123 on the region address | stratum 1-15, offset within 2 s |
| `ra` | Router Solicitation (ICMPv6 133) on the bat interface, 6 s window | at least one RA with router lifetime > 0, a /64 PIO with L+A, and RDNSS |
| `dhcpv6` | INFORMATION-REQUEST (type 11) with an ORO to `ff02::1:2`, parsing the REPLY | DNS servers (23) and information-refresh-time (32) present |

Gateway mode (run on the gateway, no `--gateway-v4`) additionally checks
`birdc` RADV state, that RADV exports exactly one default route, the uplink
transit address (`100.64.x`) and the v4/v6 default routes in kernel table 42.

Reverse DNS is **not** covered here: the `dns v4`/`dns v6` checks only resolve
one forward name. PTRs are checked by `playbooks/gateway-reverse-check.yml`
(`tools/reverse-dns-check.py`), see `docs/knot-resolver.md`.

## Findings from the live run (gw05, 2026-11)

- **Multicast floods across the mesh.** A client on one region interface
  receives RAs and DHCPv6 replies from *every* gateway: on `bat-hhost` the RA
  prefixes are `2a03:2267:1::/64`, `2::/64` and `4::/64`; DHCPv6
  replies came from link-locals `...:1040`, `...:2040`, `...:3040`, `...:5040`
  with DNS `2a03:2267:1::{10,20,30,50}`. This matches the mesh design (BIRD
  `radv` per bat interface, flooded by batman-adv), so it is not a regression —
  but it means a client can SLAAC an address from another region's prefix and
  receives several resolvers. Worth a conscious decision.
- **The gateway cannot see its own RA through a raw socket.** gw05 does emit
  RAs (`tcpdump` on `bat-hhsued` shows source `fe80::...:5010` to `ff02::1`),
  but its own multicast is not delivered to its raw ICMPv6 socket. Gateway mode
  therefore verifies RADV via `birdc` (`radv1 up`, 1 exported default route)
  instead of relying on neighbour advertisements.
- **DHCPv6 DNS is global in the config** — gw05 advertises `2a03:2267:1::50`
  (its own primary-region resolver) and `2a03:2267:1::10` (another gateway's sued
  resolver) on every interface, i.e. two resolvers for redundancy; both are
  reachable over the mesh from all four regions.
- **NTP** is reachable from the mesh (chrony binds wildcard); offset ~0.1 ms,
  stratum 2.
- **SNAT** works: source-bound traffic from region addresses exits as
  `193.96.224.25/.27/.30/.31`, inside `193.96.224.24-193.96.224.31`.

## Limitations

- The gateway-mode traffic is *locally generated* with a region source, not
  forwarded through batman-adv; it exercises the same routing table, iptables
  SNAT and uplink next hop, but not forwarding itself. For the true client
  path run the script from an AP/client host with `--gateway-v4/--gateway-v6`.
- Uplink reachability uses ICMP; if a transit firewall drops echo, ping fails
  while the SNAT/TCP probe still proves reachability.
- DHCPv4 lease acquisition is not covered (only the DHCPv6 information
  service); Kea's v4 pools are checked structurally by the playbooks instead.
- Raw ICMPv6/UDP 546 need root (`--skip-raw` downgrades RA/DHCPv6 to SKIP).
- **The `ra` test on the gateway is a SKIP, not a pass**: the gateway's own
  multicast delivery to a raw ICMPv6 socket is not guaranteed. Gateway mode
  verifies RADV structurally (`birdc`: `radv1 up`, one exported default).
