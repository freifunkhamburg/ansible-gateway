# Knot Resolver test instance (port 5353)

A mirror of the unbound ffhh DNS setup, implemented with Knot Resolver
**6.5.1** built from upstream source, listening on **port 5353** as a
test instance. The client-facing resolver is now Knot Resolver on
**port 53** (`roles/knot`), so this instance is only used for
side-by-side comparison when the unbound backend is selected
(`dns_backend: unbound`). Nothing in the client path uses port 5353.

Implemented by `roles/knot_test` and `playbooks/gateway-knot-test.yml`;
the comparison harness is `tools/knot-test-compare.py`, installed as
`/usr/local/bin/knot-test-compare`.

## Version policy and build

- Debian 13: `knot-resolver` 5.7.5-1 — too old for "most recent".
- The last build resolved **v6.5.1**; the role resolves the newest release with
  `git ls-remote --tags --refs`, keeps only `vX.Y.Z` tags, and takes `sort -V`
  tail. Pin with `-e knot_test_tag=v6.5.1` if a release should not move.
- Source: `https://github.com/CZ-NIC/knot-resolver` into
  `/var/lib/knot-resolver-test/src`, `--depth 1`, plus
  `git submodule update --init --recursive` (the policy module needs
  `lua-aho-corasick`; without it `meson setup` fails).
- Build: `meson setup build --buildtype=release --prefix=/usr/local
  --libdir=lib -Dmanaged_ta=disabled`, `ninja -C build`, `meson install`.
  Binary lands at `/usr/local/sbin/kresd`, libraries in `/usr/local/lib`
  (`/usr/local/lib` is already on `ld.so.conf`).
  `--libdir=lib` is deliberate: the default multiarch libdir would put a
  second `libkres.so.9` under `/usr/local/lib/x86_64-linux-gnu` and invite
  ambiguity.
- Build dependencies (Debian 13 packages): `build-essential meson
  ninja-build pkg-config git python3 luajit libluajit-5.1-dev libknot-dev
  libuv1-dev libgnutls28-dev liblmdb-dev libnghttp2-dev libcap-ng-dev
  libsystemd-dev`. `libknot-dev` here is 3.4.6-2, above the resolver's
  `>=3.3` requirement.
- The distro packages (`knot-resolver`, `knot-resolver-module-http`) are
  purged: their `libkres.so.9` would otherwise be loaded instead of the built
  one (`undefined symbol: KR_RULE_OPTS_DEFAULT`).
- `-Dmanaged_ta=disabled`: RFC 5011 trust-anchor updates need a writable
  keyfile directory, and the instance runs as `knot-resolver`. The installed
  `root.keys` is used read-only.
- Rebuild is conditional: the role reads `kresd --version` and rebuilds only
  when it does not match the desired tag (or the source tree is missing), so
  re-running the playbook is `changed=0`.
- Knot Resolver 6 also ships a Python manager (`kresctl`), but the daemon
  still accepts the classic Lua config with `-c`; the role uses that, so the
  existing `nodeffhh.conf` semantics map unchanged. The Lua API used
  (`policy.suffix`, `policy.FORWARD`, `policy.FLAGS`, `view.rule_src`,
  `kres.parse_rdata`, `req:ensure_answer`, `kr_pkt_make_auth_header`) is the
  same in 6.5.1.

## Mapping: unbound -> kresd

| unbound (`nodeffhh.conf`, `ffhh.conf`) | Knot Resolver equivalent |
| --- | --- |
| `local-zone "<zone>" redirect` + `access-control-tag-data` per client subnet | one `policy.suffix()` rule per zone and per client subnet, answered authoritatively (AA) with TTL 3600 from a forged rtable; `view.rule_src()` selects the client subnet |
| unbound picks the longest matching local-zone, so `v4.N.ntp.services.ffhh` beats its parent `N.ntp.services.ffhh` | rules are registered in explicit order: `v4.1/2/3` before `1/2/3.ntp.services.ffhh` |
| a name below a `redirect` zone gets the apex data | `policy.suffix` matches subdomains, so `foo.node.ffhh` answers `node.ffhh`'s data |
| a qtype the zone does not hold gets authoritative NODATA | `NOERROR`, empty answer, no SOA (matching unbound's local-zone behaviour) |
| clients without a tag fall through to `forward-zone "ffhh"` | the local rules only match their subnets; untagged clients hit the FORWARD rule |
| `domain-insecure: "ffhh"` | `policy.FLAGS({'DNSSEC_INSECURE'})` on the ffhh zone; without it the forwarded CNAME `node.ffhh -> knoten.ffhh` fails validation and kresd answers SERVFAIL |
| `forward-zone: "ffhh"` / `"112.10.in-addr.arpa"` -> `2a00:14b0:4200:3380:134::1` | `policy.suffix(policy.FORWARD(...), ...)` |
| local-zone-tag / access-control-tag: v4 netblock + v6 `/48` per region | `view.rule_src()` rules for `10.112.64/96/192/224.0/19` and `2a03:2267:{1,2,3,4}::/48` |
| the NTP v6 names answer `::10/::20/::30`; the v4 aliases answer `.251/.252/.253` | derived from the region tables: `host_in(16/32/48)` and `ntp_v4` |

## Verified

`knot-test-compare` runs 50 checks and currently reports `50 passed, 0
failed`:

- For every served region and its listening address: `node.ffhh`,
  `knoten.ffhh`, `router.ffhh` (A and AAAA), `1/2/3.ntp.services.ffhh` (AAAA),
  `v4.1/2/3.ntp.services.ffhh` (A) — unbound and kresd both match the expected
  region data and both set `aa`.
- `node.ffhh TXT` -> authoritative NODATA on both.
- `foo.node.ffhh A` -> the region apex address on both.
- Untagged loopback: `node.ffhh AAAA` and `1.ntp.services.ffhh AAAA` -> both
  forwarded to the community DNS (`knoten.ffhh.` CNAME + `2a03:2267::1`;
  `2a03:2267::301`), neither authoritative.

## Deliberate divergences

- **No access-control allow/refuse list.** The instance listens only on
  loopback and the gateway's own region addresses (port 5353); it does not
  listen on `uplink` or `intra-as`. The gateway firewall already restricts
  queries from outside the mesh, but the unbound allow-list is not reproduced
  in kresd.
- **No chroot, no `outgoing-interface` pinning.** Outgoing queries follow the
  OS routing tables, like the rest of the gateway.
- **In-memory cache, 50 MB** (`cache.current_storage = nil`; no LMDB file
  and no size-reconcile warnings), no prefetch.
- **Qname minimisation** is kresd's default; unbound is configured with it too.
- The kresd instance is not enabled at boot by default (test instance).

## Usage

```sh
ansible-playbook playbooks/gateway-knot-test.yml --check --diff
ansible-playbook playbooks/gateway-knot-test.yml
```

Compare manually:

```sh
dig +short node.ffhh AAAA @127.0.0.1 -p 5353          # forwarded, untagged
dig +short node.ffhh AAAA @10.112.64.255 -p 5353      # region data, AA
dig +short v4.1.ntp.services.ffhh A @10.112.64.255 -p 5353
```

Keep it across reboots with `-e knot_test_enable=true`; skip the comparison
with `-e knot_test_verify=false`.

## Removal

```sh
systemctl disable --now kresd-test.service
rm -f /etc/systemd/system/kresd-test.service /etc/knot-resolver/kresd-test.conf
rm -rf /var/lib/knot-resolver-test /usr/local/etc/knot-resolver
rm -rf /usr/local/sbin/kresd /usr/local/lib/libkres.so* /usr/local/lib/knot-resolver
ldconfig
# build dependencies stay installed; remove them if nothing else needs them
```

The distro `kresd.target`/`kresd@.service` units and packages are removed by
this role, so there is no distro instance listening anywhere.
