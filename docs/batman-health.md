# batman-health.py — consistency checks for the batman-adv mesh

`tools/batman-health.py` inspects a gateway's batman-adv tables and reports
the failures that a glance at `batctl originators` does not show.

Run it on the gateway itself — the debug tables come from batman netlink and
need root:

```sh
sudo ./tools/batman-health.py
sudo ./tools/batman-health.py --meshes bat-hhsued,bat-hhwest
sudo ./tools/batman-health.py --json
sudo ./tools/batman-health.py --dump /tmp/batman.json
./tools/batman-health.py --from-dump /tmp/batman.json
./tools/batman-health.py --self-test
```

Exit status: `0` clean (INFO/WARN allowed), `1` at least one ERROR
(`--strict` also exits 1 on WARN), `2` collection failed.

## What "data" means

The script normalises four batctl dumps per mesh:

| dump | batctl | JSON keys used |
| --- | --- | --- |
| mesh settings | `mj` | `version`, `algo_name`, `mesh_address`, `gw_mode`, `orig_interval` |
| hardifs | `hj` | `hard_ifname`, `hard_address`, `active` |
| originators | `oj` | `orig_address`, `neigh_address`, `hard_ifname`, `tq`, `last_seen_msecs`, `best` |
| gateways | `gwj` | `orig_address`, `router`, `bandwidth_down`, `bandwidth_up`, `tq` |
| neighbours | `nj` | `neigh_address`, `hard_ifname`, `last_seen_msecs` |

Two semantics matter and drive the checks:

- The originator dump emits **one row per (originator, neighbour) path**, so
  several rows for one MAC are normal multipath. Only a repeated
  `(originator, neighbour)` pair or more than one `best` row is a duplicate.
- In a gateway row, `router` is the **next-hop neighbour** MAC used to reach
  the gateway, not the gateway's own router MAC (see
  `net/batman-adv/bat_iv_ogm.c`, `bat_v.c`). Two gateways sharing one next hop
  is therefore normal, which is why there is no "shared router MAC" check.

## Checks

| code | level | meaning |
| --- | --- | --- |
| H1 | ERROR | same hardif MAC on two hardifs / in two meshes (duplicate MAC) |
| H2 | ERROR | hardif MAC equals the mesh MAC |
| H3 | ERROR | one device attached to several meshes |
| H4 | WARN | hardif is inactive/dropped |
| H5 | WARN | TAP/VPN hardif MAC outside the expected `de:ad:be:ef` prefix |
| M1 | ERROR | two meshes share one MAC |
| M2 | WARN | batman version differs between meshes |
| M3 | WARN | routing algorithm differs between meshes |
| O1 | ERROR | duplicate originator path / conflicting `best` rows |
| O2 | WARN | same originator MAC on two meshes |
| O3 | WARN | best path to an originator has TQ below `--tq-min` |
| O4 | WARN/ERROR | originator stale beyond `max(--stale-ms, 3 × orig_interval)`; ERROR past the 200 s purge timeout |
| O5 | ERROR | an originator MAC is also one of our hardif MACs |
| O6 | ERROR | an originator MAC equals another mesh's MAC |
| G1 | ERROR | the same gateway MAC announced twice with conflicting bandwidth |
| G2 | WARN | gateway announced but no originator entry (spurious / dead node) |
| G3 | ERROR | gateway announces zero bandwidth |
| G4 | WARN | gateway next hop is not a known originator/neighbour |
| G6 | WARN | bandwidth above `--max-bw` |
| G10 | WARN | gateway announced but route TQ below `--tq-min` |
| G7 | ERROR | our own MAC announced as a gateway while `gw_mode` is not `server` |
| G8 | INFO | number of gateways on the mesh |
| G9 | INFO | down/up bandwidth asymmetry above `--max-ratio` |
| N1 | INFO | neighbour reachable via several of our hardifs (normal) |
| N2 | WARN | stale neighbour |
| N3 | WARN | neighbour MAC not among the originators |

The stale limit is per mesh: batman emits OGMs every `orig_interval` and
purges after `BATADV_PURGE_TIMEOUT` (200 s), so a fixed 3 s threshold would
flag every node on gateways with a 5 s interval. gw05 runs with
`orig_interval = 5000`, hence a 15 s limit there.

## Live result on gw05 (2026-11-01)

```
bat-hhnowe : 118 originators, 4 gateways
bat-hhost  :   0 originators, 0 gateways (no peers attached)
bat-hhsued : 117 originators, 4 gateways
bat-hhwest : 231 originators, 4 gateways
summary: 0 error(s), 2 warning(s), 3 info
```

The only warnings were two originators last seen ~16 s and ~19 s ago (dead
paths still inside the purge timeout). No duplicate MAC, no conflicting or
spurious gateway announcement, no zero-bandwidth announcement.

The script targets the current batctl and does not include compatibility
paths for older batctl versions.

## Periodic record and alerting

The check is scheduled by `roles/operations`:

| path | role |
| --- | --- |
| `/usr/local/sbin/batman-health` | the checker (`tools/batman-health.py`) |
| `/usr/local/bin/batman-health-report` | wrapper: collect, record, alert |
| `/etc/cron.d/batman-health` | `*/15 * * * * root /usr/local/bin/batman-health-report` |
| `/var/lib/batman-health/state.json` | latest normalised dump |
| `/var/lib/batman-health/report.json` | latest findings (`--json`) |
| `/var/lib/batman-health/history.log` | one line per run, bounded to 288 lines |
| `/var/lib/batman-health/last-error.log` | stderr of the latest run |

The state directory is persistent (`/var/lib`), not `/run` tmpfs: a record is
only useful if it survives the boot that produced it, and the dump is ~190 kB
for four regions.

A history line is

```
2026-11-02T12:53:18+01:00 rc=0 errors=0 warnings=5 bat-hhnowe:117 bat-hhost:220 bat-hhsued:114 bat-hhwest:229
```

so it doubles as a trend of per-mesh originator counts (a shrinking mesh is
visible without parsing the JSON).

### What alerts

Only real failures go to syslog, via `logger -t batman-health`:

- `rc=1` (an `ERROR` finding, e.g. duplicate MAC, conflicting gateway
  announcement): one `daemon.warning` line with the failing codes and meshes,
  plus the dump path.
- `rc=2` (collection failed: no `batctl`, no `bat-*` interfaces): one
  `daemon.err` line with the checker's stderr.

`WARN` findings deliberately do **not** alert. On this mesh they are normal —
a path can have low TQ while a better path exists, and an originator last seen
20 s ago is still inside the 200 s purge timeout. Those are visible in the
motd and in `report.json`.

No pager/email exists: syslog (rsyslog → journal) is the alert channel, so an
operator reads `journalctl -t batman-health`. If a real alerting target is
added later, hook that tag.

On a collection failure the previous `report.json` is kept (the wrapper writes
to a temp file and only replaces it when non-empty), and the history line
records `errors=n/a warnings=n/a` so old counts are not presented as a fresh
result.

### Verification

```sh
systemctl status cron
cat /etc/cron.d/batman-health
sudo /usr/local/bin/batman-health-report
cat /var/lib/batman-health/history.log
jq .counts /var/lib/batman-health/report.json
journalctl -t batman-health -n 20
```

The playbook runs the reporter once and asserts that `report.json` parses and
that `history.log` is non-empty, so the wiring is verified on every run.
