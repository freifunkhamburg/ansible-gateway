# respondd: upstream mesh-announce

`ffnord-alfred-announce` was renamed to **mesh-announce** upstream. All four
Gluon domains are served by one process from **`/opt/mesh-announce`**
(upstream `master`, pinned commit `40be9a1`, 2023-02-18) with a
`respondd.conf` file and the `providers/` package.

Managed by `roles/mesh_announce`, part of the single `playbooks/gateway.yml`
run. The domain list is derived from `gw_regions` and each region's
`gluon_domain`; it is not stored per host.

## Domain names (from Gluon's `site-ffhh`)

| Gluon domain | prefix4 | ffhh region | bat interface | `BatmanInterface` |
| --- | --- | --- | --- | --- |
| `ffhh_sued` | 10.112.64.0/19 | `hhsued` | `bat-hhsued` | `bat-hhsued` |
| `ffhh_west` | 10.112.96.0/19 | `hhwest` | `bat-hhwest` | `bat-hhwest` |
| `ffhh_nowe` | 10.112.192.0/19 | `hhnowe` | `bat-hhnowe` | `bat-hhnowe` |
| `ffhh_ost` | 10.112.224.0/19 | `hhost` | `bat-hhost` | `bat-hhost` |

Config sections use the **Gluon domain name**, so `nodeinfo.system.domain_code`
matches what the Gluon nodes advertise; `BatmanInterface` overrides the
upstream default `bat-<domain>` with the ffhh gateway naming.

## Current state (all four domains migrated)

- `/opt/mesh-announce` — clean checkout, `master` / `40be9a1`.
- `/etc/mesh-announce/respondd.conf` — one file, `[Defaults]` + four domain
  sections. Port 1001, `ff02::2:1001` / `ff05::2:1001`.
- `/etc/systemd/system/mesh-announce.service` — ONE process:
  `respondd.py -f /etc/mesh-announce/respondd.conf -d /opt/mesh-announce/providers`.
  Enabled and active.
- **Removed**: `respondd@hhsued|hhwest|hhnowe|hhost` (stopped, disabled),
  `/etc/systemd/system/respondd@.service`, `/opt/ffnord-alfred-announce`,
  `/etc/cron.d/respondd-announce` (absent anyway). The intermediate per-region
  `mesh-announce@.service` and `respondd-<region>.conf` files from the first
  migration step are also gone.
- Runtime dependency: `python3-psutil` (for the `fastd.enabled` provider).

## Why one process

Upstream maps each request to a domain by the interface it arrived on
(`util.ifindex_to_iface` → `DomainRegistry.get_domain_by_interface`) and joins
the multicast group on every registered `bat-<region>` device. In the older
four-instance design every process bound `*:1001` and answered with its own
data, so a multi-interface query could be answered by an unrelated region. The
single process has no such race. There is deliberately **no `DefaultDomain`**:
requests on unregistered interfaces (e.g. unicast to `::1`) are ignored instead
of being answered with a random region's data.

## Verification (live, 2026-11-01)

Multicast `GET nodeinfo` from **another gateway over the mesh**, loopback off,
8 probes per interface:

```
bat-hhsued {('ffhh_sued', 'deadbeef5010'): 8}
bat-hhwest {('ffhh_west', 'deadbeef5020'): 8}
bat-hhnowe {('ffhh_nowe', 'deadbeef5030'): 8}
bat-hhost  {('ffhh_ost',  'deadbeef5040'): 8}
```

No cross-domain answers. Also confirmed:

- `ip -6 maddr show dev bat-*` lists `ff02::2:1001` and `ff05::2:1001` on all
  four devices (the single socket's memberships).
- site-local query (`ff05::2:1001`) on `bat-hhwest` answered `ffhh_west`.
- `GET statistics` / `GET neighbours` work; `neighbours` is populated (the
  older tree returned `null`).
- Re-running the playbook: `changed=0`.

Node identity is unchanged from the older output: `node_id`
`deadbeef5010`…`deadbeef5040` derived from the bat-interface MACs,
`hardware.nproc`, `software.fastd.version v23`, `software.batman-adv.version
2026.3`, `software.firmware {base Debian, release 13}`.

## Operational note: multicast membership is fixed at process start

The memberships are joined once when the process starts, for the bat devices
that exist at that moment (`if_nameindex()`). On normal boot this is fine
(`networking.service` is ordered before `network.target`, so the bat devices
are up first). If a bat device is created later — e.g. a manual
`ifup bat-hhwest` after the service is already running — restart
`mesh-announce.service` so the group is re-joined. The older design had the
same limitation.

## Not migrated

- **Alfred announcement** (`announce.sh`, services 158/159): no announce cron
  existed on gw05, so nothing was scheduled. If wanted, configure it in the
  same role with the new provider/config layout.
- `hardware.model`, `owner.contact`, `location.*`, `gateway.address` are not
  advertised because the config does not set `Hardware-Model`, `Contact`,
  `Latitude`, `Longitude`, `IPv4Gateway`. Set them to enable those providers.
