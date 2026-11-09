# Role reference

`playbooks/gateway.yml` runs these roles in order (see
[architecture.md](architecture.md) and figure 7 in [topology.md](topology.md)).
This file says what each role owns and where its inputs come from; the
topic-specific docs linked from the [README](../README.md) explain the parts
with non-obvious behaviour.

| Role | Owns | Key files / outputs | Inputs |
|---|---|---|---|
| `base` | Debian 13 assertion, kernel sysctl policy, journald retention, auditd identity rules | `01-gateway.conf.j2` (`/etc/sysctl.d`), `50-journald-limits.conf.j2`, `audit-rules.j2` | `journald_max_use` / `_max_file_size` / `_retention`, `audit_enabled` |
| `regions` | Loads region tables and **derives** everything downstream: served-region addresses/MACs, transit network, published `/32`s, peers, expectations, Kea subnet table | no templates | `group_vars`, `host_vars`, `region_vars` |
| `network` | NIC rename by MAC (udev), `/etc/network/interfaces`, `dummy0` published addresses, `intra-as` policy extras, uplink address, table 42 route | `70-persistent-net.rules.j2`, `interfaces.j2`, `interface-loopback.j2` | `gw_nic_names`, `gw_nic_rename[_reboot]`, `intra_as_*`, `uplink_*`, `gw_public_*` |
| `mesh` | Builds batman-adv/batctl (via `include_role: batman_build`), renders the ethtool-disable helper and one interface file per served region | `interface-region.j2`, `ethtool-disable.j2` | `gw_regions`, region `vxlan`/`bat_if` facts |
| `batman_build` | Builds the pinned batman-adv module and `batctl` from source, loads the module | `tasks/batman_adv.yml`, `tasks/batctl.yml` | `batman_release`, build dirs, `batctl_path` |
| `data_sync` | Clones/updates the externally provisioned `dhcp-static` repository used for Kea reservations, installs its post-merge hook | `post-merge.j2` | `dhcp_static_*` settings |
| `kea` | ISC Kea packages, DHCPv4/v6 config per region subnet, static includes, reservation pull cron, unit start | `kea-dhcp4.conf.j2`, `kea-dhcp6.conf.j2`, query helpers | `kea_*` in `group_vars`/`host_vars`, `kea_subnet4` |
| `bird` | BIRD 3 package, `bird.conf` (OSPF, BGP, static natips routes, RADV), config validation, reload handler | `bird.conf.j2` | derived peers/prefixes, `mesh_forward_hosts` |
| `services` | Resolver and time backends, package set, `resolv.conf`, vnstat, fail2ban, staged-config guard | `unbound.conf.j2`, `chrony.conf.j2`, `ntpsec.conf.j2`, `resolv.conf.j2` | `dns_backend`, `ntp_backend`, `ntp_upstream_servers`, `fail2ban_*` |
| `powerdns` | Authoritative PowerDNS (Lua2 backend) on `127.0.0.2`, the `ffhh-dns-fetch` script + timer, the synthesized zone Lua | `pdns.conf.j2`, `ffhh-synth.lua.j2`, `ffhh-dns-fetch.*` | `ffhh_dns_enabled`, `ffhh_dns_authoritative_v4/_v6`, `ffhh_dns_axfr_*`, `served_regions` |
| `knot` | `knot-resolver` user/workdir, `kresd.conf`, unit, start + probes; **meta dependency** on `knot_build` | `kresd.conf.j2`, unit template | `served_regions`, `unbound_access_control`, `ffhh_dns_*` |
| `knot_build` | Builds the newest Knot Resolver from source into `/usr/local`, purges the distro package | `tasks/build.yml` | `knot_tag` (empty = newest) |
| `knot_test` | Optional comparison instance on port 5353 (`playbooks/gateway-knot-test.yml`); not part of the client path | `kresd-test.conf.j2`, `kresd-test.service.j2`, `regions.json.j2` | `knot_test_*` |
| `fastd` | Builds the newest fastd, renders `common.conf` + one config/secret per instance, persists and attaches TAPs, starts units | `common.conf.j2`, `fastd.conf.j2`, `secret.conf.j2`, `fastd@.service.j2` | vault secrets, `fastd_peers`, `fastd_key_scope`, derived instances |
| `ssh` | SSH hardening drop-in (validated with `sshd -t`), administrator keys, key-only assertion | `ffhh-hardening.conf.j2` | `ffhh_ssh_authorized_keys`, `ssh_ciphers`/`_macs`/`_kex` |
| `operations` | Health and operator scripts, cron jobs, vnstat interface policy, motd | `check-gateway.j2`, `check.sh.j2`, `check-sanitized.sh.j2`, `motd-refresh.j2`, `batman-health-report.j2`, `upstream-versions-report.j2`, `update-fastd-keys.j2`, `cron/*.j2` | `gw_check_*`, `mesh_forward_hosts`, paths from `group_vars` |
| `mesh_announce` | mesh-announce (renamed respondd) checkout at the pinned commit, single `respondd.conf`, unit, domain verification | `respondd.conf.j2`, unit template | `gw_regions` + region `gluon_domain`, `mesh_announce_commit` |
| `status_web` | static mesh-facing status page (light, rrdtool + lighttpd bound to the region addresses only), minute sampling + 5-minute render cron | see `docs/status-web.md` | `status_web_*`, `served_regions` |
| `firewall` | netfilter packages, rendered persistent `rules.v4`/`rules.v6`, restore-on-change | `rules.v4.j2`, `rules.v6.j2` | `gw_ssh_allowlist`, derived addresses/NAT |

## Gating

Four switches decide which branch of a role actually runs; the role itself
handles the gate, so running the playbook is always safe:

- `dns_backend` (`knot` | `unbound`) — gates the real work inside `knot` and
  `knot_build`; with `unbound` kresd is stopped and the distro package purged
  (a playbook-level `when:` would not skip the `meta` dependency).
- `ntp_backend` (`chrony` | `ntpsec`) — selects the time daemon in `services`;
  the other package is purged and its unit stopped. See
  [ntpsec-vs-chrony.md](ntpsec-vs-chrony.md).
- `ffhh_dns_enabled` — runs the `powerdns` role's server at all; its two
  `ffhh_dns_authoritative_v*` flags decide which reverse zones it claims. See
  [knot-resolver.md](knot-resolver.md).
- `fastd_start` — renders fastd configuration without starting instances when
  no secrets are supplied.

Optional playbooks are verification only: they install/run tools
(`gateway-reverse-check.yml`, `gateway-client-check.yml`,
`gateway-check-upstreams.yml`) or a comparison instance
(`gateway-knot-test.yml`). They are not part of the production run.

## Where inputs live

The inventory stores inputs only; anything computable is derived in `regions`.
Tiering, the derivation list and the decisions behind it are in
[inventory.md](inventory.md). Shared constants belong to `group_vars/all`,
gateway identity to `host_vars/<gw>.yml`, per-region facts to
`region_vars/<region>.yml`.
