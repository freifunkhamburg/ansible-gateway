# Status web (roles/status_web)

A deliberately small status page for each gateway, reachable **only from
the mesh**, with no JavaScript and no application server: cron regenerates
one static HTML file plus three PNG charts, and lighttpd serves them from a
tiny file tree.

## What is shown

| line | source |
|---|---|
| gateway name | `ffhh_host_name` (rendered into the page) |
| CPU load 1/5/15 min (+ core count) | `/proc/loadavg`, `nproc` |
| memory used / total | `/proc/meminfo` (`MemTotal - MemAvailable`) |
| uplink throughput | `/sys/class/net/<uplink>/statistics/{rx,tx}_bytes` deltas |
| VPN connections | `fastd-query connections` summed over every status socket in `/run/fastd/` |
| DHCP leases v4 / v6 | active (state `0`) rows of Kea's `kea-leases{4,6}.csv` via the stdlib `csv` parser |

Three RRD graphs cover the last 24 h: uplink bit/s, load (with a
core-count reference line), and memory MiB. The page links itself by every
served region address so mesh users can share the URL of their own region.

## How it works

* `/usr/local/bin/gw-status sample` (cron, every minute) appends the
  counters to `/var/lib/gw-status/*.rrd` (60 s step; minute data 24 h,
  30-minute consolidated data 8 weeks) and writes the instantaneous
  uplink rate to `current`.
* `/usr/local/bin/gw-status render` (cron, every five minutes) redraws
  the three PNGs and rewrites `index.html`. The page carries
  `<meta http-equiv="refresh" content="300">` — the browser reload is
  plain HTML, there is no `<script>` anywhere.
* lighttpd is configured by this role alone (the shipped `conf-enabled`
  snippets are removed): the config contains **only** `$SERVER["socket"]`
  listeners on each served region's `v4_host`/`v6_host` address on port
  80 — there is no listener on intra-as, uplink or loopback at all.
* The firewall additionally drops TCP/80 whose source is outside the mesh
  aggregate (both families), so a future listener mistake cannot expose
  the page on the public side either.

Both cron jobs run as root: the fastd status sockets are root-only and so
are Kea's lease files. Only the rendered page directory (`/var/www/
gw-status`) is world-readable, which is exactly what lighttpd serves.

## Failure behaviour

The page is a snapshot; if sampling or rendering breaks, it goes stale and
its "generated" timestamp stops advancing. The charts render as far as the
data allows (`|| true`), so one bad value cannot blank the whole page.
Nothing in the chain talks to any other host, and nothing is exposed
beyond static files on the mesh addresses.
