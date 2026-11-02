# Knot Resolver (client-facing DNS, port 53)

Knot Resolver is the Debian 13 gateway's client-facing resolver. The unbound
setup stays in the repository but is de-selected; switch back with
`dns_backend: unbound` in `inventories/group_vars/all/main.yml`.

## Roles and playbook

- `roles/knot_build` — resolves the newest upstream tag
  (`git ls-remote --tags --refs` → keep `vX.Y.Z` → `sort -V | tail -1`,
  currently **6.5.1**), checks out with submodules, builds with `meson`/`ninja`
  under `--prefix=/usr/local --libdir=lib`, installs into
  `/usr/local/sbin/kresd` and `/usr/local/lib/knot-resolver`, and purges the
  distro `knot-resolver` package. Skips rebuild when the installed version and
  runtime tree already match.
- `roles/knot` — creates the `knot-resolver` user, the workdir
  `/var/lib/knot-resolver`, renders `/etc/knot-resolver/kresd.conf` and
  `/etc/systemd/system/knot-resolver.service`, and starts the unit.
- Both roles are gated on the backend inside their `tasks/main.yml`
  (`include_tasks` only when `dns_backend == "knot"`), not by the playbook.
  `playbooks/gateway.yml` lists them unconditionally because the gate is
  the role's own business; a role skipped with `when:` would still run its
  `meta` dependencies (the Ansible pitfall this avoids).
  With `dns_backend: unbound`, `roles/knot` only stops/disables kresd and
  `roles/knot_build` only purges the distro package — no build dependencies
  are installed and nothing listens on port 53. The rendered config and unit
  stay on disk, so switching back is one line plus one run.

## Config mapping (unbound → kresd)

| unbound | kresd |
|---|---|
| access-control allow/refuse | policy rule inspecting `req.qsource.addr`, allow list first, `policy.REFUSE` for everything else |
| local-zone redirect + access-control-tag-data | per-region authoritative answers, matched on client subnet |
| `domain-insecure: "ffhh"` | `policy.FLAGS({'DNSSEC_INSECURE'})` |
| forward-zone ffhh / mesh reverse zones | `policy.FORWARD(...)` + `DNSSEC_INSECURE` |
| chroot, outgoing-interface pinning | not replicated (deliberate) |

`node.ffhh`, `knoten.ffhh`, `router.ffhh` and the NTP names are answered only
for the local region subnets; untagged clients fall through to the community
DNS, exactly like unbound's tagged local zones.

### Mesh reverse DNS (IPv4 and IPv6)

Legacy unbound only special-cased the IPv4 mesh reverse zone: `ffhh.conf` had
`local-zone: "112.10.in-addr.arpa" nodefault` plus a `forward-zone` to the
community DNS. There was **no** IPv6 reverse configuration at all. IPv6 PTRs
were resolved by ordinary recursion from the root:

```
ip6.arpa
  -> 0.a.2.ip6.arpa                       RIPE (insecure delegation)
  -> 7.6.2.2.3.0.a.2.ip6.arpa             NS: dns01 + named.ohrensessel
  -> 0.0.0.0.<region>.0.0.0.7.6.2.2.3.0.a.2.ip6.arpa
                                          NS: srv03.hamburg.freifunk.net
  -> srv03 answers the PTR authoritatively (AA, TTL 3600)
```

`0.a.2.ip6.arpa` holds no DS for `7.6...ip6.arpa` (DS query -> NOERROR with
AD=1 and no DS), so the /32 is an insecure delegation and unbound answers the
unsigned PTRs without validation. Client addresses handed out by RA/SLAAC in
the region `/64` are not in the gateway inventory; the community DNS derives
the name from the address itself: `ipv6-<16 nibbles>.<region>.clients.hamburg.freifunk.net`.

kresd cannot follow that delegation: `srv03.hamburg.freifunk.net` is an
auth-only PTR responder (it returns NXDOMAIN for `NS`/`SOA` at the zone apex),
so kresd reports `LAME_DELEGATION` and `SERVFAIL`s. The reverse zones are
therefore forwarded to the community DNS and marked `DNSSEC_INSECURE`.

The explicit `NO_0X20` flag is required on these zones: kresd randomises the
case of the QNAME it sends upstream (`...A.2.IP6.aRpA.`). The community DNS
forwards that mixed-case name to a case-sensitive backend, which returns
`NXDOMAIN` unless the name is already cached. `NO_0X20` keeps the forward
queries lower-case, as unbound sent them.

The gateway PTRs (`2a03:2267:R::N*0x10` -> `gwNN-new.<region>.hamburg.freifunk.net`)
come from the community DNS, not from local inventory. A stale negative entry
can still make gw05 diverge from a legacy gateway, so the knot role probes
`gw2`'s primary-region PTR after start and, if it returns nothing, stops
kresd, removes `data.mdb` and retries once.

Two limits of the upstream data, both identical to the legacy unbound gateway:

- **No IPv4 client PTRs upstream.** The community DNS only serves gateway
  names in `112.10.in-addr.arpa`; a client address in the mesh `/16` answers
  `NXDOMAIN`. The gateway forwards the zone; it can additionally synthesise
  PTRs from its own Kea leases, but that is **opt-in** because it exposes
  which addresses are currently leased (see below).
- **Only ordinals 1-3 have gateway names.** `2a03:2267:R::10/::20/::30`
  resolve to `gwNN-new.<region>.hamburg.freifunk.net`; addresses for ordinals
  4 and 5 (including gw05's own address `::50`) fall back to the client naming
  pattern `ipv6-<16 nibbles>.<region>.clients.hamburg.freifunk.net`.
  Upstream also maps `2a03:2267:4::1` to `knoten.ffhh.`

Case sensitivity: `NO_0X20` is applied only to the two mesh reverse zones.
Queries for the `ffhh` zone are forwarded as-is, and the community DNS answers
them case-insensitively (verified: `NODE.FFHH` returns the same CNAME as
`node.ffhh`). That is an upstream property, not something kresd guarantees;
if the upstream backend or its forwarding path changes, `ffhh` names could
break the same way the reverse zone did.


### IPv4 client PTR synthesis (opt-in)

Kea knows every currently leased v4 address, so the gateway can answer those
PTRs locally instead of forwarding them upstream:

```
dig -x 10.112.193.10 @127.0.0.1
10.193.112.10.in-addr.arpa. 3600 IN PTR ipv4-0a70c10a.nowe.clients.hamburg.freifunk.net.
```

The name follows the upstream IPv6 client pattern, with the 32-bit address in
8 hex nibbles instead of the 64-bit host part in 16:

```
10.112.65.37 -> ipv4-0a704125.sued.clients.hamburg.freifunk.net.
```

Mechanics:

- `knot_client_ptr_enabled` (default `false`) enables the feature on a gateway.
- `knot-client-ptr-export.service` + `.timer` copy Kea's memfile lease DB
  (`/var/lib/kea/kea-leases4.csv`, `0640 root:_kea`) into a `0644` file under
  `/var/lib/knot-resolver/` every minute, so the resolver user never needs
  access to Kea's data.
- `client-ptr.lua` is loaded by kresd, registers a `policy.suffix` rule for
  `112.10.in-addr.arpa` **before** the forwarding rule, and answers PTRs for
  leases whose `expire` is still in the future. Expired leases fall through to
  the upstream forward (NXDOMAIN), exactly as without the feature.
- Region labels come from `served_regions` (`hhsued` -> `sued`), so the
  name is `ipv4-<hex>.<region>.clients.hamburg.freifunk.net`.

Limits and caveats:

- Only leases known to **this** gateway are answered. A client asking another
  gateway's resolver can still get NXDOMAIN, so answers are resolver-local,
  not network-wide. That is inherent: no gateway has a global lease view.
- Static reservations from `update-dhcp-static` are not in the memfile lease
  DB until used; the synthesis therefore covers dynamic leases only.
- The feature reveals lease presence to anyone allowed by the resolver ACL.
  Keep it off unless that is an accepted policy decision.

### Verification

`roles/knot` checks two things after start:

- `gw2`'s primary-region gateway PTR (via `knot_rev_probe_addr`), with one
  cache purge and retry if it is empty;
- one **fresh** client PTR per served region. The address is derived from the
  run time (`knot_probe_seed`, default the current epoch), so it is not in the
  cache; the assertion compares the answer with
  `ipv6-<16 nibbles>.<region>.clients.hamburg.freifunk.net`. This is the
  regression test for `NO_0X20`: a cached answer would mask the defect.

`playbooks/gateway-reverse-check.yml` + `tools/reverse-dns-check.py` run the
same checks standalone, plus informational probes:

```sh
ansible-playbook playbooks/gateway-reverse-check.yml
# per region: gw01/02/03 PTRs (must pass), a fresh client PTR (must pass),
# the gateway's own address and one IPv4 client address (informational)
```

### Policy callback signature

`policy.add()` callbacks are called as `cb(request, query)` and must **return an
action** (e.g. `policy.REFUSE`); the engine then calls `action(state, request)`.
Writing `req.qsource` inside a `(state, req)`-shaped callback fails with
`'struct kr_query' has no member named 'qsource'` — that is the trap fixed in
the ACL rule.

## Cache

Knot Resolver 6 always uses LMDB; `cache.current_storage = nil` does **not**
disable persistence — `cache.size` then falls back to `lmdb://` relative to the
workdir. That produced a 96 MB `data.mdb` and, after a config bug was fixed,
stale aggressive-NSEC `NXDOMAIN` answers survived the restart.

The production config therefore points the cache at
`/run/knot-resolver` (the unit's `RuntimeDirectory`, tmpfs):
`cache.storage = '/run/knot-resolver'` before `cache.size = 100 * MB`. The
handler stops the unit, removes `/run/knot-resolver/data.mdb` and starts it
again, so a config/unit change cannot inherit stale negative entries.

## Operations

- kresd has no reload, so a
  config/unit change restarts the unit — a brief cache loss (the cache is in
  tmpfs and survives restarts until reboot).
- The stale-config guard in `roles/services` applies to the time daemon
  only; kresd's config is rendered before the unit starts in the same role, so
  no guard is needed there.
- Verify: `dig +short node.ffhh AAAA @127.0.0.1 -p 53`,
  `dig +short 1.ntp.services.ffhh AAAA @<region gw v4>`, and a gateway PTR:
  `dig +short -x 2a03:2267:1::20 @127.0.0.1` -> `gw02-new.sued.hamburg.freifunk.net.`
