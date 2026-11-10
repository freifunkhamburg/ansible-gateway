

## What is shown

| line | source |
|---|---|
| gateway name | `ffhh_host_name` (rendered into the page) |
| CPU load 1/5/15 min (+ core count) | `/proc/loadavg`, `nproc` |
| memory used / total | `/proc/meminfo` (`MemTotal - MemAvailable`) |
| uplink throughput (last 5 min) | last complete `vnstat` 5-minute bucket for the uplink NIC |
| VPN connections | `fastd-query connections` summed over every status socket in `/run/fastd/` |
| DHCP leases | active (state `0`) rows of Kea's `kea-leases4.csv` via the stdlib `csv` parser; no IPv6 row, DHCPv6 is stateless here (information only) |

Four RRD graphs cover the last 24 h: uplink **down** (Mbit/s), uplink
**up** (kbit/s), CPU load, and memory MiB. Down and up are separate charts
because the uplink is asymmetric (~35 Mbit/s vs ~12 kbit/s): on a shared
axis the upload line is invisible. Each chart auto-scales to its own data,
and starts at the oldest sample available instead of always spanning a full
24 h — a freshly deployed gateway would otherwise show only a thin sliver at
the right edge of an empty axis. The page links itself by every served
region address so mesh users can share the URL of their own region.

## Who can reach it

The listeners are the four region addresses per family plus the gateway's
intra-as addresses. IPv4 region addresses (`10.112.x.255`) are private mesh
space, so they are reachable from the mesh only. The intra-as address
`193.96.224.213` is in the published `/24`, so it is reachable from the
internet as well.

The region **IPv6** addresses (`2a03:2267:1..4::50`) are the interesting
case: their `/64`s are announced upstream (OSPF external / BGP), so the page
is deliberately reachable from the public internet on its mesh addresses,
e.g. `http://[2a03:2267:1::50]/`. `roles/firewall` admits TCP/80 to exactly
those destinations and drops TCP/80 to every other address; the intra-as
`/64` (`2a03:2267:ffff:d00::/64`) is *not* routed from the internet, so its
listener only serves intra-as peers.

## How it works

* `/usr/local/bin/gw-status sample` (cron, every minute) diffs the
  kernel counters and appends the **rate** (bit/s) to the RRD files at a
  60 s step (minute data 24 h, 30-minute consolidated data 8 weeks). The
  data sets are `GAUGE`, not `COUNTER`: the delta is computed in the
  script, so a missed run or a counter reset produces a zero instead of a
  spike, and vnstat buckets can be written into the same archive.
* On every run `sample` also verifies the RRD schema. When the schema is
  stale (older `COUNTER` files) or the history is thin, it rebuilds the
  archives and **backfills the last 24 h from vnstat's 5-minute buckets** —
  vnstat keeps about 48 h of them, so a fresh deploy gets a populated chart
  immediately instead of an empty one. The rebuild is skipped when vnstat
  has nothing to import, so it cannot thrash every minute.
* The headline throughput is the last **complete** vnstat bucket (not the
  instantaneous 60 s delta): it matches the page's five-minute refresh
  cadence, is steadier, and is exactly the value the chart is built from.
* `/usr/local/bin/gw-status render` (cron, every five minutes) redraws
  the four PNGs and rewrites `index.html`. The page carries
  `<meta http-equiv="refresh" content="300">` — the browser reload is
  plain HTML, there is no `<script>` anywhere.


Both cron jobs run as root: the fastd status sockets are root-only and so
are Kea's lease files. Only the rendered page directory (`/var/www/
gw-status`) is world-readable, which is exactly what lighttpd serves.

## Failure behaviour

The page is a snapshot; if sampling or rendering breaks, it goes stale and
its "generated" timestamp stops advancing. Each chart command also exits
non-stop on the first RRDtool error, so the playbook run fails rather than
publishing a broken page; RRDtool diagnostics are appended to
`/var/log/gw-status.log`.

One RRDtool quirk is worth spelling out, because it produced charts that
looked empty: a malformed colour argument is **not** an error — RRDtool
silently omits the series, writes the PNG with axes and legend only, and
still exits 0. Colour tokens are therefore written as
`AREA:rx#3a9d3a:"down "`, never `AREA:rx#"#3a9d3a":"down "` (the latter
concatenates to a double `#` in the shell). A second quirk: `GPRINT` takes
`LAST`, not `CURRENT` — `CURRENT` makes the whole command fail with
"bad CF for DEF/CDEF", which with a swallowed exit status left the previous
PNG in place.
Nothing in the chain talks to any other host, and nothing is exposed
beyond static files on the listener addresses.
