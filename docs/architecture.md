# Architecture

## Runtime model

A gateway is a Debian 13 host with two NICs:

- `intra-as` — the transit/peering interface (public `/29` plus an IPv6 peer
  block);
- `uplink` — the upstream interface with a `100.64.112.0/29`-style transit
  address.

The mesh is built on batman-adv. Every served region has a `bat-<region>`
device bridged over VXLAN to the other gateways (`mesh-<region>`), and fastd
tunnels carry the wider mesh transport. The gateway terminates client traffic
and NATs the mesh into its published block.

## Role order

1. `base` — Debian 13 assertion, sysctl.
2. `regions` — load region tables, derive addresses/peers/pools/expectations.
3. `network` — rename NICs, configure `intra-as`/`uplink`, create `dummy0`,
   persistent names.
4. `mesh` — build batman-adv/batctl, create and attach bat/VXLAN interfaces.
5. `data_sync` — clone `dhcp-static` so Kea reservations are available.
6. `kea` — DHCPv4/v6, per-region subnets, static includes.
7. `bird` — OSPF, BGP, static natips routes, RA.
8. `services` — DNS backend selection, chrony/ntpsec selection, vnstat,
   fail2ban.
9. `knot` — client-facing Knot Resolver (build dependency `knot_build`).
10. `fastd` — build and render instances; start only with secrets.
11. `ssh` — hardening and administrator keys.
12. `operations` — health scripts, cron (gateway/fastd/check/vnstat and the
    periodic batman-health record), vnstat interface policy, motd.
13. `mesh_announce` — respondd from upstream mesh-announce.
14. `firewall` — render and apply netfilter last.

This is a dependency order, not a series of deployment stages. The run is
idempotent: the build roles rebuild only when the pin or output is missing;
Kea, BIRD and the firewall change only when their inputs change.

## Addressing scheme

For gateway ordinal `N`:

- bat IPv4 host = region network + `250 + N`;
- bat IPv6 host = region prefix + `N * 0x10`;
- bat MAC = `de:ad:be:ef:<N*16>:<region_index>`;
- fastd instance = `gw_index + 3*(slot-1)`;
- Kea pool = assigned per gateway, disjoint `/21` blocks inside the region `/19`.

The registry in `group_vars/all` is the only place gateways are listed. Adding
a gateway means adding its `host_vars` file and one registry entry; peers and
VXLAN addresses update everywhere.

## DNS and time backends

`dns_backend` selects Knot Resolver (default) or the renderable unbound setup.
`ntp_backend` selects chrony (default) or renderable ntpsec. Both alternates
are kept in the repo so switching back is a one-line change, but only one is
installed/started.

The DNS gate is enforced inside the roles, not by the playbook: `roles/knot`
and `roles/knot_build` each `include_tasks` their real work only when
`dns_backend == "knot"`. With unbound selected, kresd is stopped/disabled and
the distro knot-resolver package is removed; no build dependencies are pulled
in. (A playbook-level `when:` on the role would not skip its `meta`
dependency, which is why the gate lives in the role.)

Kea logs at `WARN` for both DHCPv4 and DHCPv6 (`Dhcp4.loggers` /
`Dhcp6.loggers`), so packet-level `INFO` messages do not fill the journal.
The separate `kea-lfc` lease-file-cleanup helper still writes its own `INFO`
lines once per `lfc-interval`; Kea 3 does not expose a severity setting for
that helper.

## Sources and builds

Downloads happen on the target. `batman_build` builds the pinned batman-adv
module and batctl; `knot_build` builds the newest Knot Resolver release;
`fastd` builds the newest fastd release when the installed binary is older.
The release pins live in role defaults / `group_vars/all`, not in host_vars.

## Firewall

The ruleset is rendered to `/etc/iptables/rules.v4|v6` and restored when the
rendered file changes. The SSH allowlist comes from `host_vars`; the control
path must be included before applying. The IPv6 forward rule uses each
region's real `/64`, not an unrelated alias.
