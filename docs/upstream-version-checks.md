# Upstream version checks for built components

Ansible has no built-in notion of "a newer upstream release exists". This repo
uses two patterns, and one external checker to make drift visible.

## What the repo does today

**1. Dynamic resolution at deploy time** — the role asks upstream for the
newest tag and rebuilds only if the installed version differs:

- `roles/fastd` (`fastd_tag`, empty = newest): `git ls-remote
  --tags --refs` → keep `v\d+` → `sort -V | tail -1`.
- `roles/knot_build` (`knot_tag`, empty = newest): `git ls-remote
  --tags --refs` → keep `vX.Y.Z` → `sort -V | tail -1`.

Running the playbook is therefore the update mechanism: the role compares
`<binary> --version` / the runtime tree with the desired tag and rebuilds only
when needed (`changed=0` on a current host).

**2. Pinned versions in inventory** — reproducible, but silent drift:

- `batman_release` — `v2026.4` in `group_vars/all` (one value for every
  gateway).
- `mesh_announce_commit` — `40be9a1`.

Nothing warns when upstream moves past a pin. That is what the checker is for.

Applying a pin bump: change `batman_release` and run the playbook. The role
re-checks out the tag and rebuilds both components; build stamps
(`.batctl-built-<release>`, `.batman-adv-built-<release>-<kernel>`) decide
whether a rebuild is needed, so the same release is never rebuilt and a
**kernel upgrade forces a module rebuild** even when the source tree is
unchanged. The new `batctl` is live immediately; the new kernel module is
installed under `updates/` and modprobe-resolves from then on, but the
running mesh keeps the old module until a mesh restart or reboot (the bat
interfaces hold it). The role prints
`running vs installed` in that case. Wire compatibility is checked via
`BATADV_COMPAT_VERSION` (still 15 across 2026.3 -> 2026.4), so gateways can
be upgraded one at a time.

**3. Package-managed** — Kea (ISC cloudsmith repo), BIRD, vnstat, fail2ban etc.
are updated by `apt upgrade`; upstream tags are irrelevant except as a hint
that the package repo lags.

BIRD is a plain apt package, not a source build: Debian 13 provides
`bird3` 3.1.7-0+deb13u1 (`bird` does not exist in Debian 13; `bird2` 2.17.5 is
the older alternative). `roles/bird` installs `bird3`, so it is
covered by apt and is not in the checker catalog.

**LTS series.** Some upstreams publish deliberate LTS lines. Kea's LTS is
**3.0.x** (3.0.5 at the last check on isc.org/kea); ISC also ships newer
non-LTS releases (3.1.x/3.2.x/3.3.x). Comparing a deliberate LTS pin with
"newest tag overall" reports the pin as outdated, so the catalog supports
`policy: lts` with an `lts_tag_regex`. Knot Resolver has no upstream LTS
designation, so it remains on `policy: latest` (newest vX.Y.Z tag).

## The checker

`tools/check-upstream-versions.py` reads `tools/upstream-versions.yml`, runs
`git ls-remote` for each entry, reads the local version, and prints a table.

It must run **on the target** (it needs the installed binaries and the target's
network); the workspace egress proxy is not involved.

```sh
python3 tools/check-upstream-versions.py
python3 tools/check-upstream-versions.py --json
python3 tools/check-upstream-versions.py --only batctl,knot-resolver
python3 tools/check-upstream-versions.py --no-network   # local versions only
```

Exit codes: `0` current, `1` at least one component behind upstream, `2` at
least one check failed (repo unreachable, version unreadable).

`--json` is **pure JSON** and is the contract the wrapper depends on:

```json
{
  "checked_at": "2026-11-02T12:57:19Z",
  "components": [ { "name": "fastd", "installed": "23", "upstream": "v23",
                    "status": "current", "note": "" }, ... ],
  "counts": { "total": 6, "current": 6, "outdated": 0, "differs": 0, "unknown": 0 }
}
```

Status values are `current`, `outdated`, `differs` (commit-pinned checkout
whose HEAD moved), `ahead (unreleased build?)` and `unknown` (check failed).
`--json` must not append the human summary: the wrapper caches it as
`state.json` and parses it with `jq`. An unparseable cache made the motd print
`all 0 components current`; the wrapper now reports `cached result unreadable`
instead, and the role asserts `.counts.total > 0`.

## Deployment and periodic check

`roles/operations` owns the installation (so the single production run
`playbooks/gateway.yml` sets it up):

| path | role |
| --- | --- |
| `/usr/local/bin/check-upstream-versions` | `tools/check-upstream-versions.py` |
| `/usr/local/share/check-upstream-versions.yml` | `tools/upstream-versions.yml` |
| `/usr/local/bin/upstream-versions-report` | wrapper: run, cache, alert |
| `/etc/cron.d/upstream-versions` | `17 6 * * * root /usr/local/bin/upstream-versions-report` (daily) |
| `/var/lib/upstream-versions/state.json` | latest `--json` result |
| `/var/lib/upstream-versions/summary.txt` | one line for the motd |
| `/var/lib/upstream-versions/last-error.log` | stderr of the latest run |

The catalog is found by default (next to the script, then
`/usr/local/share/check-upstream-versions.yml`), so the installed binary runs
without `--catalog`.

`playbooks/gateway-check-upstreams.yml` now only runs and reports the installed
checker; it fails if the checker is missing, and with
`-e upstream_check_fail_on_outdated=true` it fails when a component is behind
upstream. Use it as verification, not as installation.

### What the motd shows

`/usr/local/bin/motd-refresh` appends the cached summary; it never reaches the
network:

```
SOFTWARE UPDATES: all 6 components current (checked 2026-11-02 13:20)
SOFTWARE UPDATES: behind upstream — fastd 23->v24 (checked 2026-11-02 13:20)
SOFTWARE UPDATES: check failed — checkout unreachable (checked 2026-11-02 13:20)
```

Being behind upstream is the normal state between playbook runs (`fastd` and
`knot_build` rebuild at deploy time), so it is not syslog-alerted. Only a
failed check (rc=2, e.g. upstream unreachable) goes to syslog once a day:
`logger -t upstream-versions`. The timestamp in the line makes staleness
visible if the daily job stops.

## Adding a component

Append an entry to `tools/upstream-versions.yml`:

```yaml
  - name: mybin
    kind: tag                    # or: commit
    repo: "https://github.com/example/mybin.git"
    tag_regex: '^v[0-9]+\.[0-9]+\.[0-9]+$'
    # For an LTS pin instead of newest-release:
    # policy: lts
    # lts_tag_regex: '^v3\.0\.[0-9]+$'
    installed:
      command: "mybin --version"
      regex: 'mybin\s+([0-9]+\.[0-9]+\.[0-9]+)'
```

`installed` may instead be `file: <path>` or `git_dir: <checkout>` (for
commit-pinned checkouts).

For the two dynamic roles, prefer letting the role resolve the newest tag at
deploy time; the checker then just confirms whether the deployed host is
current.
