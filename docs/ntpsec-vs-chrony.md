# NTP: ntpsec vs chrony

The managed gateway (Debian 13): **chrony 4.6.1-3+deb13u2**
(`chrony.service`) is selected. It is installed and rendered by
`roles/services` from `templates/chrony.conf.j2`. **ntpsec** stays in
the repo (`templates/ntpsec.conf.j2`, package install branch) but is
de-selected: its unit is stopped and its package is purged on gw05.

Selection is one variable: `ntp_backend: chrony | ntpsec` in
`inventories/group_vars/all/main.yml`.

## What the current config does

- Upstream servers (`gw_ntp_server_lines`): `ntp0.iphh.net`, `ntp1.iphh.net`,
  `ptbtime2.ptb.de`, `ptbtime3.ptb.de` (IPv4 only: `server -4`).
- `server 127.127.1.0` + `fudge ... stratum 10`: local-clock fallback so the
  gateway can still serve time when the uplink is down.
- `restrict default ignore` for v4 and v6, then explicit allows for the
  upstream servers, loopback, and the mesh ranges (`10.112.0.0/16` and
  `2a03:2267::/32`) with `kod ... limited` — i.e. rate-limited answers, no
  monitoring/peer/mode-6 access.
- `disable monitor`.

## Live-state finding (fixed)

On gw05 the running daemon had been started at **2026-10-31 14:59:19**, two
minutes *before* the config was rendered at **15:01**. ntpd reads its config
only at start, so the daemon was still using the Debian default config
(`0.debian.pool.n` peers), not the intended iphh/PTB servers and mesh ACLs.
The Ansible handler had not been flushed in that run.

Fixed by restarting: peers are now `netview1.server` (= ntp0.iphh.net),
`ntp1.iphh.net`, `ptbtime2.ptb.de`, `ptbtime3.ptb.de`, plus `LOCAL(0)`.

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

If the switch is approved, the role should be parameterised (like the DNS
backend):

- `ntp_backend: ntpsec | chrony` in group vars.
- Package install and template selection branch on it.
- The stale-config guard (start timestamp vs config mtime) applies to both.
- Kea's DHCP options 42/56 already advertise addresses, not daemon details, so
  they do not change.
