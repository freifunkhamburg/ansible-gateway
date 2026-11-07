# NTP: ntpsec vs chrony

The managed gateway (Debian 13): **chrony 4.6.1-3+deb13u2**
(`chrony.service`) is selected. It is installed and rendered by
`roles/services` from `templates/chrony.conf.j2`. **ntpsec** stays in
the repo (`templates/ntpsec.conf.j2`, package install branch) but is
de-selected: its unit is stopped and its package is purged on gw05.

Selection is one variable: `ntp_backend: chrony | ntpsec` in
`inventories/group_vars/all/main.yml`.

## Current config (chrony selected)

`roles/services/templates/chrony.conf.j2` renders, from the shared
`ntp_upstream_servers` list (`ntp0.iphh.net`, `ntp1.iphh.net`,
`ptbtime2.ptb.de`, `ptbtime3.ptb.de`):

- `server <name> iburst` per upstream. chrony resolves each name's A and
  AAAA records itself, so IPv6 upstream works (the ntpsec template pins
  `server -4`).
- `local stratum 10` -- the unsynchronised local-clock fallback, so the
  gateway can still serve time with the uplink down.
- `allow 10.112.0.0/16`, `allow 2a03:2267::/32`, `allow fe80::/10`,
  `allow 127.0.0.1`, `allow ::1` -- mesh-only service; upstream peers are
  not served.
- `ratelimit interval 3 burst 20 kod 2` and `clientloglimit 4194304` --
  rate-limited answers with per-client counters instead of opaque `kod`
  counters.
- `makestep 1.0 -1`, `maxslewrate 1000`, `maxupdateskew 100.0`.

The ntpsec template (`templates/ntpsec.conf.j2`, rendered only when
`ntp_backend: ntpsec`) keeps the classic shape: `server -4` peers,
`restrict -4/-6 default ignore`, and per-net `restrict ... kod notrap nomodify
nopeer noquery limited`. The per-host `gw_ntp_*` lists are gone; both
templates read the same shared `ntp_upstream_servers` and derive the mesh
nets from `mesh_v4_aggregate` / `mesh_v6_aggregate`.

## Live-state finding (fixed)

On gw05 the running daemon had been started at **2026-10-31 14:59:19**, two
minutes *before* the config was rendered at **15:01**. ntpd reads its config
only at start, so the daemon was still using the Debian default config
(`0.debian.pool.n` peers), not the intended iphh/PTB servers and mesh ACLs.
The Ansible handler had not been flushed in that run.

Fixed by restarting: peers are now `netview1.server` (= ntp0.iphh.net),
`ntp1.iphh.net`, `ptbtime2.ptb.de`, `ptbtime3.ptb.de`, plus `LOCAL(0)`.
That was the ntpsec daemon, before the switch to chrony.

The role now also guards this: a task compares the unit's start timestamp with
the config mtime and restarts when the daemon predates its config. That is
needed because neither ntpd nor kresd reloads its config on SIGHUP.

## Assessment: does chrony make sense?

**Yes, it is the better fit for this gateway — with a staged migration.**

| Aspect | ntpsec here | chrony 4.6 |
|---|---|---|
| Config reload | start-only | start-only, but `chronyc` gives live state |
| IPv6 upstream | `server -4` only; v6 peers unused | native; one `pool`/`server` per family |
| Convergence on flaky uplink | slower; step/slew control limited | `makestep`, `maxslewrate`, `minsources`, `maxdistance`; recovers faster after uplink flaps |
| VM/container clocks | refclock workaround | handles large initial offsets and drift better |
| Server ACL | `restrict ... kod ... limited` per net | `allow <net>` + `ratelimit` + `clientlog`; same effect, simpler |
| Monitoring | `ntpq -p` | `chronyc tracking`, `sources`, `clients` (per-client counters) |
| Security | hardened ntpd | NTS (Network Time Security) client/server support; `ntske` |
| Upstream activity | ntpsec 1.2.3 (2023-era) | chrony 4.6.1 (2025) |
| Package default | Debian 13 default | alternative, fully supported |

Why it matters here specifically:

1. **Uplink flaps.** `check-gateway` toggles mesh gateway mode based on
   Internet reachability; NTP must re-converge quickly afterwards. chrony's
   slew/step policy handles that better than ntpd's default.
2. **IPv6.** The gateway has a working egress v6 path and the mesh is
   IPv6-heavy; ntpsec's `-4`-only server lines mean no v6 upstream at all.
3. **Rate-limited server role.** chrony `allow` + `ratelimit` expresses the
   current `restrict ... limited` policy more directly and gives per-client
   logs instead of opaque `kod` counters.
4. **Observability.** `chronyc clients` shows which mesh clients are actually
   syncing — useful when debugging clients behind batman/VXLAN.

Reasons to stay on ntpsec:

- The mesh-facing behaviour is already proven; Kea advertises the NTP names,
  not this daemon's address directly.
- chrony's `allow` is per-subnet, and its `ratelimit` defaults differ from
  ntpsec's `limited`; the ACL semantics need re-validation.
- A migration needs a maintenance window: both daemons bind UDP/TCP 123, so
  they cannot run side by side.

**Decision taken:** chrony is the default. ntpsec is purged, not just
disabled; rollback is a one-line backend flip (`ntp_backend: ntpsec`) plus a
playbook run.

## Migration sketch

The role now does this itself: stop/disable the de-selected unit, purge its
package (including `/etc/ntpsec` and `/etc/cron.d/ntpsec`), install the
selected package, render the selected config, enable/start the selected unit.

Implemented `/etc/chrony/chrony.conf` (gw05):

```
driftfile /var/lib/chrony/chrony.drift
makestep 1.0 -1
maxslewrate 1000
maxupdateskew 100.0
logdir /var/log/chrony
log tracking statistics

# upstream sources (families resolved by chrony)
server ntp0.iphh.net iburst
server ntp1.iphh.net iburst
server ptbtime2.ptb.de iburst
server ptbtime3.ptb.de iburst

# fallback: keep serving on the local clock when unsynchronised
local stratum 10

# mesh ACL (replaces restrict ... kod limited)
allow 10.112.0.0/16
allow 2a03:2267::/32
allow fe80::/10
allow 127.0.0.1
allow ::1
ratelimit interval 3 burst 20 kod 2
clientloglimit 4194304
```

Validation:

```sh
systemctl restart chrony
chronyc tracking
chronyc sources -v
chronyc clients            # after some client traffic
# from a mesh client / locally:
chronyc -n -c 'ntpdata 10.112.64.200'  # or `chronyd -Q` style probe
```

Rollback: set `ntp_backend: ntpsec` and run `playbooks/gateway.yml`; the role
reinstalls ntpsec and purges chrony.

Do **not** run both: UDP 123 would be claimed by whichever starts first, and
the second would fail or silently share via SO_REUSEADDR only for some modes.

## Implementation note

The switch is implemented in `roles/services` (backend selection, package
install/purge, template choice). The stale-config guard (unit start timestamp
vs config mtime) applies to whichever daemon is selected, and to kresd.
Kea's DHCP options 42/56 advertise addresses, not daemon details, so they do
not change with the backend.

The migration sketch above is the state the role now produces; the remaining
manual step is only the first run that swaps packages on an existing host.
