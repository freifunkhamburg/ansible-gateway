# Inventory model and review decisions

The inventory stores inputs only. This file documents the model and the
decisions behind it — what each tier holds, what was removed, and which values
are derived — rather than enumerating every field; the role reference is in
[roles.md](roles.md) and the field-level facts are in the inventory files
themselves.

## Tiers

| Tier | File | Holds |
| --- | --- | --- |
| Shared mesh | `group_vars/all/main.yml` | gateway registry, aggregates, AS, releases, forward-host list, NTP upstreams, Kea NTP options, resolver defaults/paths |
| Gateway | `host_vars/<gw>.yml` | ordinal, served regions, address plan, pools, allowlist, admin keys, NIC MACs, resolver flags |
| Region | `region_vars/<region>.yml` | subnet, prefix, numbering, tunnel ports, Kea block, Gluon domain, NBMA neighbours |

Rule: if a second gateway would repeat the same value, it is shared or a
protocol constant; if it would differ, it is gateway or region data; if it can
be computed from those, it is derived.

## host_vars decisions

### `gw_transit_net` — removed, derived
`uplink_v4` is already an address/prefix in the transit `/29`
(`100.64.112.226/29`). `roles/regions` derives
`gw_transit_net = uplink_v4 | net_only` → `100.64.112.224/29`. The network is
never stated twice.

### `dummy_v4_addresses` — removed, derived
`gw_public_snat_range` (`193.96.224.24-193.96.224.31`) is the only input.
`roles/regions` expands it with the `snat_hosts` filter into the eight `/32`s
used on `dummy0`, in BIRD's unreachable routes and in the published-address
policy. The range must stay inside one `/24`; the filter asserts that.

### `gw_forward_hosts_group1/group2` — consolidated
These are **not** OSPF-learned routes: they are static `natips` routes for
community service `/32`s reached through a specific mesh hop, and the firewall
mirrors them. The two groups existed only to preserve a reference file's rule
ordering, which is irrelevant on a fresh host. They are now one shared list,
`mesh_forward_hosts`, in `group_vars/all` (dest/via/label). OSPF does not
supply the `via` hop, so the topology fact must be stated.

### `gw_scraper_v6` — removed
It was the IPv6 source allowed to the 9000–9999 monitoring range (the ffhh
scraper address). External scraping/monitoring is not part of this design, so
the allow rule is gone; the range remains dropped by default.

### `gw_kea_exporter_source_v4` — removed
No exporters are foreseen. The kea-exporter accept rule and the
`kea-dhcp4-clean.conf` output are gone.

### `gw_ntp_*` — generalised
The ntpd-specific `server -4 …` and full `restrict …` lines were per-host
copies of upstream peers. They are replaced by the shared
`ntp_upstream_servers` list in `group_vars/all`. The daemon resolves A/AAAA
itself. Mesh access is derived from `mesh_v4_aggregate` / `mesh_v6_aggregate`
in the templates; no `gw_ntp_restrict_mesh_*` variable is needed.

### `bird_local_as` — moved to group_vars
AS 49009 is identical on every gateway, so it is shared, not host data.

### `gw_ospf_v4_neighbors` / `gw_ospf_v6_neighbors` — moved to region_vars
They are the OSPF neighbours on the NBMA region (`hhwest`): the two edge hosts
cloud/speicher. That is a property of the region, not of a gateway. They now
live in `region_vars/hhwest.yml` and are selected by `roles/regions` for the
gateway that serves the NBMA region. Gateways not serving it get an empty list.

### `builds` — removed
The batman/batctl release is one shared value (`batman_release: v2026.4`) in
`group_vars/all`, and `roles/batman_build` owns the repository URLs, build
directory and module path. No per-host build tree is stored. Go, ICVPN scripts,
Checkmk and Kea builds were removed from the build role.

### `gw_expect_gateway_v4` — derived
It is exactly the transit peer: `uplink_v4_peer`. `roles/regions` sets
`gw_expect_gateway_v4` from it.

### `gw_expect_batman_version` — derived
It is `batman_release` without the `v` prefix. The check script now parses
`batctl -v` with `awk` and compares the exact version.

### `mesh_announce_domains` — removed, derived
Each region declares its Gluon domain (`gluon_domain`) in `region_vars`;
`roles/regions` composes `mesh_announce_domains` from `gw_regions`. There is no
per-host list.

### `mesh_announce_remove_legacy` — removed
A fresh Debian 13 host has no older respondd units or old checkout. The role
only configures and starts the new service.

### `ffhh_dns_enabled` / `ffhh_dns_authoritative_v4|_v6` — gateway identity
These stay per host because they decide whether this gateway serves the mesh
DNS locally and which reverse families it claims. The shared defaults live in
`roles/powerdns/defaults/main.yml` (both families derived from
`ffhh_dns_authoritative`); a host sets them only when it must differ. Adding
more per-host resolver data (peers, zones) would repeat registry facts and is
deliberately avoided.

## group_vars decisions

### `local_dc_if` / `local_ms_if` — removed
They are one gateway's local peering topology, not a shared constant, and the
homogeneous NIC model is `intra-as` + `uplink`. They are not part of the new
gateway state.

### `anycast_dns_if` / anycast DNS — removed
DNS anycast is de-scoped: no interface, listener or static route.

### `batctl_path` — kept once
It is a shared install path used by `batman_build` and the operational scripts;
it is not duplicated in a build section.

### `kea_dhcp4_clean_conf` — removed
It existed only for the kea exporter. The exporter and the clean config are
gone.

### exporters / SNMP / Checkmk / ICVPN — removed
Every firewall rule, package and build job for them was deleted. The
remaining ICVPN-adjacent text was deleted from the Knot Resolver config; no
ICVPN data or zone drop exists.

### NTP
`ntp_servers` (PTB only) and the per-host `gw_ntp_*` lists are replaced by one
shared `ntp_upstream_servers` list used by both the chrony and ntpsec
templates.

### `ffhh_apply_firewall`, `ffhh_populate_data`, `ffhh_build_binaries` — removed
These were opt-in flags from the reproduction workflow. On a fresh production
host the firewall must be applied, data must be cloned and binaries must be
built as part of the run. The firewall restores only when the rendered rules
change; data sync and the build roles are naturally idempotent.

### Older batctl/batman versions — removed
Only the pinned current release remains. No `v2018.4` values or older build
paths are in the inventory.

### `icvpn_scripts_dir` — removed
ICVPN metadata/scripts are not part of the design.

## region_vars decisions

### `fw_v6_alias` — removed
It encoded a older repeated-digit `/48` used only in an ip6tables FORWARD
rule, and it did not contain the region's actual prefix. On a fresh host the
forward rule uses the region's real `v6_prefix`; the guard/drop rule uses the
same prefix. Keeping a second, unrelated `/48` would create a rule that does
not match client addresses.

### `hop_penalty_last`, `pool_pad` — removed
They reproduced reference file ordering/JSON spacing only.

### `interface_file`, `ospf_index`, `dhcp6_index` — kept
They are stable naming/ordering inputs for interface files and the OSPF/DHCPv6
lists; they make rendered output deterministic.

### `bat_if`, `v6_site`, `vxlan_id`, Kea `subnet` — removed, derived
`bat_if` is `bat-<id>`, `v6_site` is the /48 containing `v6_prefix`, `vxlan_id`
is `100 + region_index`, and the Kea subnet is `v4_net`. No region file repeats
them.

## What `roles/regions` derives

```yaml
served_regions          # address, MAC, ntp_v4 per served region
gw_transit_net          # uplink_v4 network
dummy_v4_addresses      # expanded SNAT range
gw_ospf_v4_neighbors    # NBMA-region neighbours
bird_bgp_neighbors_v6   # registry peers
mesh_vxlan_peers        # registry peers
gw_expect_gateway_v4    # uplink_v4_peer
gw_expect_batman_version
gw_expect_fastd_instances
mesh_announce_domains
ospf_v4_networks
kea_subnet4
kea_dhcp6_interfaces
```

## Playbook model

The numbered step playbooks were build-out scaffolding, not a
production state machine. In production the host is brought to the final state
in one ordered run; splitting the run would leave intermediate states that are
not valid (e.g. Kea with no DHCP-static checkout, firewall before services).
`playbooks/gateway.yml` encodes that order. Individual roles can still be run
for debugging, but they are not the deployment interface.
