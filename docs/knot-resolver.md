# Knot Resolver (client-facing DNS, port 53)

Knot Resolver is the Debian 13 gateway's client-facing resolver. The unbound
setup stays in the repository but is de-selected; switch back with
`dns_backend: unbound` in `inventories/group_vars/all/main.yml`.

## Roles and playbook

- `roles/knot_build` — resolves the newest upstream tag
  (`git ls-remote --tags --refs` → keep `vX.Y.Z` → `sort -V | tail -1`; the
  last build resolved **6.5.1**), checks out with submodules, builds with `meson`/`ninja`
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
so kresd reports `LAME_DELEGATION` and `SERVFAIL`s. Hence the IPv6 reverse
space is not resolved by recursion here:

- the served regions' `/64` zones are answered by the local PowerDNS
  (`roles/powerdns`, `ffhh_dns_authoritative_v6`) as authoritative zones;
- every other `/64` (and the rest of the `/32`) is forwarded to the community
  DNS with `NO_0X20` and `DNSSEC_INSECURE`.

The explicit `NO_0X20` flag is required on the zones still forwarded upstream:
kresd randomises the case of the QNAME it sends upstream
(`...A.2.IP6.aRpA.`). The community DNS forwards that mixed-case name to a
case-sensitive backend, which returns `NXDOMAIN` unless the name is already
cached. `NO_0X20` keeps those forward queries lower-case, as unbound sent
them. Queries to the local PowerDNS do not need it -- that backend lowercases
the QNAME itself.

The gateway PTRs (`2a03:2267:R::N*0x10` -> `gwNN-new.<region>.hamburg.freifunk.net`)
are the same names locally and upstream: for served regions the local
PowerDNS generates them from the inventory (`ffhh_dns_v6_gateway_ordinals`),
for other regions they come from the community DNS. A stale negative entry
can still make gw05 diverge, so the knot role probes the primary region's
gateway PTR after start and, if it returns nothing, stops kresd, removes
`data.mdb` and retries once.

Two limits of the upstream data, both identical to the legacy unbound gateway:

- **No IPv4 client PTRs upstream.** The community DNS only serves gateway
  names in `112.10.in-addr.arpa`; a client address in the mesh `/16` answers
  `NXDOMAIN`. The gateway answers the zone from a local authoritative
  PowerDNS instance (`roles/powerdns`, optional via `ffhh_dns_enabled`) that
  serves a transferred copy of those records plus synthesis. This is
  necessary because kresd ships the RFC 6303 empty zone `10.in-addr.arpa`,
  which shadows our subdomain `112.10.in-addr.arpa` and would answer
  `NXDOMAIN` before kresd even asks upstream -- including for `gw05.ffhh`.
  The zone is attached to kresd's rules DB with
  `policy.rule_forward_add(..., {auth = true, dnssec = false})`, not with
  `policy.FORWARD`, for the same reason. Addresses without a static record
  are synthesized locally (see the next sections), so no address in the mesh
  `/16` answers `NXDOMAIN` anymore.
- **Only ordinals 1-3 have gateway names.** Upstream, `2a03:2267:R::10/::20/::30`
  resolve to `gwNN-new.<region>.hamburg.freifunk.net` and every region's `::1`
  to `knoten.ffhh`; everything else answers the hex client pattern
  `ipv6-<16 nibbles>.<region>.clients.hamburg.freifunk.net`. The served
  regions' /64 reverse zones are local zones in PowerDNS (see the next
  section): those static names come from the inventory and every other
  address answers the same hex client pattern, synthesized.

Case sensitivity: `NO_0X20` is applied to the parts of the reverse zones that
are still forwarded upstream. Queries for `ffhh` are forwarded to the local
PowerDNS unmodified (or upstream when `ffhh_dns_enabled` is off); the community
DNS answers them case-insensitively (verified: `NODE.FFHH` returns the same
CNAME as `node.ffhh`). That is an upstream property, not something kresd
guarantees; if the upstream backend or its forwarding path changes, `ffhh`
names could break the same way the reverse zone did.


### Local mesh DNS: PowerDNS with a Lua backend (`roles/powerdns`)

The resolution chain is drawn as figure 5 in [topology.md](topology.md); this
section describes the server side of it.

| Zone | Served by | Content |
|---|---|---|
| `ffhh` | transferred + synthesis | community A/AAAA/CNAME; `<4 hex>.<region>.ffhh` synthesized |
| `112.10.in-addr.arpa` | transferred + synthesis | community PTRs; `<4 hex>.<region>.ffhh` synthesized |
| `<reversed /64>.ip6.arpa` | gateway only | static gateway names + synthesized `ipv6-...` |
| `<region>.clients.hamburg.freifunk.net` | gateway only | synthesized `ipv6-...` + reverse lookup |


`ffhh_dns_enabled` (default `false`, `true` on gw05) runs PowerDNS
with the Lua2 backend (`pdns-server`, `pdns-backend-lua2`) on `127.0.0.2:53`
-- loopback only, Knot Resolver forwards to it, so clients never reach it and
no firewall port is needed. It is authoritative-only: no recursion and no
forwarding are configured, so it answers its own zones and refuses the rest.
It serves four kinds of zones (`/etc/powerdns/ffhh-synth.lua`, generated from
`served_regions`):

- `ffhh` and `112.10.in-addr.arpa` -- **transferred** from the community
  (AXFR from srv01/dns01). Everything in them is answered verbatim
  (A/AAAA/CNAME; CNAMEs are chained by PowerDNS), and transferred records
  always win over synthesis.

  The fetch machinery is `ffhh-dns-fetch.service` (oneshot, also run once
  before the first PowerDNS start) plus `ffhh-dns-fetch.timer` (first run 1
  min after boot, then every `ffhh_dns_fetch_interval`, default 15 min). The
  script AXFRs `ffhh_dns_axfr_zones` from `ffhh_dns_axfr_masters` (srv01),
  keeps a JSON cache beside the data file and replaces `ffhh_dns_data`
  (`/var/lib/powerdns/ffhh-zones-data.lua`) atomically; a failed transfer
  keeps the last good content. A change to the Lua script or `pdns.conf`
  needs a pdns restart -- the zone *data* file does not (the Lua backend
  reloads it every `ffhh_dns_reload_secs`, default 5 s).
- Gaps in those zones are synthesized in the community hex naming:

  ```
  10.112.65.37   <-> 4125.sued.ffhh
                     (host part of the /16 aggregate, 4 hex, region label)
  ```

- one local zone per served region /64 (`<reversed>.ip6.arpa`) -- static
  names generated from the inventory (`::1` -> `knoten.ffhh`,
  `::10/::20/::30` -> `gwNN-new.<region>.hamburg.freifunk.net`, ordinals
  1-3 exist upstream) plus synthesis for the rest of the region. The /64
  reverse zones are delegated to srv03, which refuses AXFR, so they cannot
  be transferred; if the community renames a gateway name, the inventory
  list must be updated.
- one zone per served region `<label>.clients.hamburg.freifunk.net` --
  synthesis of the IPv6 client names:

  ```
  2a03:2267:1::5 <-> ipv6-0000000000000005.sued.clients.hamburg.freifunk.net
                     (interface part, 16 hex digits)
  ```

Forward synthesis accepts only names whose hex decodes back into the region
it is queried in (a `...sued...` name never answers for a west address),
and transferred records always win over synthesis.

### Authority over the reverse zones (`ffhh_dns_authoritative*`)

`ffhh_dns_authoritative_v4` and `ffhh_dns_authoritative_v6` (defaults derive
from `ffhh_dns_authoritative: true`; gw05 sets both explicitly) decide
whether the gateway is authoritative for the mesh reverse zones:

- **true** -- the Lua backend claims the zone (`dns_get_domaininfo` returns
  `kind = NATIVE`, so PowerDNS answers with the AA bit and its own SOA/NS) and
  kresd queries the local server as an authoritative one
  (`rule_forward_add(..., {auth = true})`). No DNSSEC validation, no
  recursion. This is what gw05 runs for both families.
  The AA bit is on PowerDNS' reply: kresd still answers the client as a
  resolver (`qr rd ra`), so a client-side check must query
  `127.0.0.2` directly to observe the authority claim.
- **false** -- the zone is not claimed at all: `dns_lookup`/`dns_list` return
  nothing for it, `zone_set()` omits it, and kresd forwards that reverse zone
  to the community DNS instead. Useful for a host where the community should
  keep answering the reverse space.

The two flags are independent, so a host can be authoritative for v4 reverse
and not v6, or the other way round. `ffhh` and the `<region>.clients...`
forward zones are not affected: they are served whenever
`ffhh_dns_enabled` is on.

This replaces the earlier `roles/knotd` (authoritative Knot DNS +
`mod-synthrecord`) and the lease-based `knot_client_ptr_*` feature; both are
removed, and the powerdns role cleans their leftovers off hosts that had
them (`knot` package purged, client-ptr units and files removed).

kresd rules (`roles/knot/templates/kresd.conf.j2`):

- v4 reverse: `policy.rule_forward_add('112.10.in-addr.arpa', {auth = true,
  dnssec = false}, { { 127.0.0.2 } })` -- bypasses the RFC 6303 empty zone;
- v6 reverse: the same `rule_forward_add(..., {auth = true, dnssec = false})`
  for each served region /64, registered **before** the /32 forward to the
  community DNS, so regions this gateway does not serve still go upstream
  (with `NO_0X20`, which the case-sensitive upstream backend needs and the
  local one does not);
- clients zones: `policy.FORWARD(127.0.0.2)` + `DNSSEC_INSECURE` per served
  region. Upstream delegates `clients.hamburg.freifunk.net` to srv03, an
  unsigned responder whose answers fail DNSSEC validation through kresd
  (SERVFAIL), so serving these zones locally costs nothing that worked
  before;
- `ffhh`: forwarded to the local PowerDNS (transferred content + hex
  synthesis). The view rules (knoten/node/router per region) are registered
  before that forward, so the per-region answers keep winning.

Consequences to be aware of:

- ffhh answers are as fresh as the last transfer (`ffhh_dns_fetch_interval`)
  plus kresd's caching; between transfers upstream changes are not visible
  locally. A gateway that has never transferred anything (fresh install with
  the community down) answers only synthesized names until the first
  successful fetch.
- Inside the mesh, the region clients zones answer authoritatively (hex
  synthesis + SOA); names outside the served regions keep resolving upstream
  wherever that worked.
- Synthesized PTRs reveal address->name mapping for the whole mesh ranges to
  anyone the resolver ACL admits (this was accepted for gw05 when the
  lease-based feature was enabled; the synthesis covers more addresses).
- kresd caches negative answers, so synthesis changes need a resolver
  restart; the role's "Flush resolver negative cache" handler does it (the
  cache lives on tmpfs).

### Verification

`roles/knot` checks after start (the node probe guards unit/port):

- `node.ffhh AAAA` answers on `127.0.0.1` at all;
- the primary region's gateway PTR (via `knot_rev_probe_addr`), with one
  cache purge and retry if it is empty;
- one **fresh** client PTR per served region. The address is derived from the
  run time (`knot_probe_seed`, default the current epoch), so it is not in the
  cache; the assertion compares the answer with
  `ipv6-<16 hex>.<region>.clients.hamburg.freifunk.net` (local synthesis or
  upstream -- both use this naming). For upstream-served regions this is the
  regression test for `NO_0X20`: a cached answer would mask the defect;
- with `ffhh_dns_enabled`, the static `gw05.ffhh`-style IPv4 PTR through the
  kresd rules DB, which proves the `rule_forward_add` path to PowerDNS.

`roles/powerdns` additionally verifies (against `127.0.0.2`): the static and
synthesized PTRs and the round-trip A/AAAA of the synthesized names for each
family it is authoritative for, and -- for every family -- the reply's status:
`aa` when the zone is claimed (`NATIVE`), `REFUSED` when the flag is off.
That is what distinguishes "the parameter works" from "the zone silently
falls through".

`playbooks/gateway-reverse-check.yml` + `tools/reverse-dns-check.py` run the
same checks standalone, plus informational probes:

```sh
ansible-playbook playbooks/gateway-reverse-check.yml
# per region: gw01/02/03 PTRs (must pass), a fresh client PTR plus its
# forward record (must pass), the gateway's own address (informational)
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
