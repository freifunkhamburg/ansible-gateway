# Login motd: periodic checks, read-only at login

gw05 shows the gateway status and the batman health report on every
interactive login. The checks are **not** run at login: a cron job rebuilds
`/etc/motd.tail` every 5 minutes and PAM only reads that file.

## Pieces

| path | role |
| --- | --- |
| `/usr/local/bin/motd-refresh` | rebuilds `/etc/motd.tail` (check output + batman health) |
| `/etc/cron.d/check` | `*/5 * * * * root /usr/local/bin/motd-refresh` |
| `/usr/local/sbin/batman-health` | `tools/batman-health.py` (copied, `--motd` mode) |
| `/root/check.sh`, `/root/check-sanitized.sh` | the gateway check scripts |
| `/etc/pam.d/login`, `/etc/pam.d/sshd` | extra `pam_motd.so motd=/etc/motd.tail noupdate` line |

`check-gateway` runs from cron with a minimal `PATH`, so the script sets
`PATH` itself and skips/logs a failure instead of treating an empty batctl
result as a state change.

`/etc/motd.tail` is generated atomically (`mktemp` + `mv`), mode `0644`; the
cron job's own output is discarded. `pam_motd` already prints
`/run/motd.dynamic` and `/etc/motd` (the Debian text); the added line appends
the generated part after them.

The cron interval replaces the `0,30` schedule: the user asked for
"every 5 min or so", and a stale motd after 30 minutes was possible.

## The check script fix (the errors it used to print)

Before the change `check.sh` emitted, for every DHCP line:

```
(standard_in) 1: syntax error
... DHCP: 1
2
1
... of 1791 leases assigned (%)  (standard_in) 1: syntax error
check.sh: line 63: [: -eq: unary operator expected
invalid
```

Two separate defects:

1. **Kea 3 statistics are lists, not scalars.** `kea-dhcp4-query <id>` used
   `jq '.[] | .[$id]? | .[] | .[0]'`, which returns *every* historical sample
   in the statistic (newest first), one number per line. Kea 1.x on the old
   gateways kept a single value, so the helper worked there and
   printed `55`; on gw05 it printed 16 lines. The helper now takes only the
   newest sample: `.arguments[$id] | .[0][0] // 0`.
2. **`bc` + `[ ... -eq 1 ]` is not empty-safe.** With the multi-line value,
   `echo 'scale=2;' $used/$total*100 | bc` fails with "syntax error" and the
   test gets an empty operand → `[: -eq: unary operator expected`, and the
   empty comparison falls through to `invalid`. Percentages are now computed
   with `awk` (`ratio`, `verdict`), which treats empty operands as 0 and has
   no syntax-error mode.

Also hardened while keeping the output format: the disk check reads
`df -P <gw_check_hdd_device>` instead of `df | grep <device>`, and the numeric
`fastd` instance count test no longer emits a unary-operator error if the
count is empty.

After the fix, `check-sanitized.sh` prints the full table with every line
`valid` and no stray diagnostics, and `check.sh` still exits 1 if any check is
`invalid` (the motd generator ignores that exit code, so cron always refreshes).

## Batman health in the motd

`batman-health --motd` prints a compact block: one summary line, one line per
mesh (hardifs / originators / gateways) and only `WARN`/`ERROR` findings.
`INFO` findings are dropped to keep the login screen short.

This is separate from the periodic record: `/etc/cron.d/batman-health` runs
`batman-health-report` every 15 minutes and writes
`/var/lib/batman-health/{state.json,report.json,history.log}` plus the syslog
alert on `ERROR`/collection failure (see `docs/batman-health.md`). The motd
job stays at 5 minutes because a login should show a recent status; it does
not write a history entry and does not alert.

## Software update status in the motd

Components that are **not** plain Debian apt packages — `batctl`,
`batman-adv`, `fastd`, `knot-resolver`, the respondd checkout and the Kea LTS
pin — are checked daily against upstream by
`/etc/cron.d/upstream-versions` → `/usr/local/bin/upstream-versions-report`.
The wrapper caches the result in `/var/lib/upstream-versions/summary.txt`, and
`motd-refresh` appends that one line:

```
SOFTWARE UPDATES: all 6 components current (checked 2026-11-02 13:20)
SOFTWARE UPDATES: behind upstream — fastd 23->v24 (checked 2026-11-02 13:20)
SOFTWARE UPDATES: check failed — ... (checked 2026-11-02 13:20)
```

It is a *hint*, not an alert: a component behind upstream is normal between
playbook runs, and `fastd`/`knot_build` rebuild on the next `gateway.yml` run.
See `docs/upstream-version-checks.md`.

The line is only as good as the cached result. `upstream-versions-report`
validates the cache before formatting: if `state.json` is missing or
unparseable it writes `SOFTWARE UPDATES: check failed — cached result
unreadable`, never `all 0 components current` (the bug that showed 0 while the
catalog had 6 entries).

## Verification

- `printf 'exit\n' | ssh -tt …` shows the block after the Debian banner/motd
  (interactive login only; command-mode sessions get no motd, which is normal).
- `sudo /root/check.sh | grep -iE 'syntax|unary'` → no output.
- `/usr/local/sbin/batman-health --motd` → `BATMAN HEALTH: OK — 0 error(s)`.
- `/etc/motd.tail` mtime advances every 5 minutes.
