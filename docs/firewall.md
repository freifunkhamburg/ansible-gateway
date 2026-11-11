# Firewall (`roles/firewall`)

The gateway runs netfilter through `iptables` (nft-backed) and
`iptables-persistent`. Both families are **default-deny**: `INPUT` and
`FORWARD` are `DROP`, every listener has a named rule, and a rate-limited `LOG`
rule sits last so dropped traffic is visible in the journal instead of silent.

The rendered rulesets are written to `/etc/iptables/rules.v4` and
`/etc/iptables/rules.v6` (validated with `iptables-restore --test` before they
replace the file) and restored live only when the rendered file changed.

## Inputs

| variable | where | meaning |
|---|---|---|
| `gw_ssh_allowlist` | `host_vars` | IPv4 prefixes admitted to `sshd` |
| `gw_ssh_allowlist6` | `host_vars` (default `[]`) | extra IPv6 prefixes admitted to `sshd`; the mesh aggregate is always admitted |
| `mesh_v4_aggregate` / `mesh_v6_aggregate` | `group_vars/all` | the mesh client aggregates |
| `freifunk_v6_aggregates` | `group_vars/all` | clients of peer communities reachable through fastd |
| `mesh_forward_hosts` | `group_vars/all` | published `/32` services that may be reached from the uplink |
| `gw_public_net`, `gw_public_snat_range`, `gw_transit_net` | `host_vars` | gateway identity used for the fastd/SNAT rules |

Port ranges, the MSS clamp values, the fwmark and the hashlimit rates are
policy and stay literal in the templates.

## What is admitted

| port | source | v4 | v6 |
|---|---|---|---|
| tcp/22 | `gw_ssh_allowlist` / mesh + `gw_ssh_allowlist6` | direct `ACCEPT` rules | direct `ACCEPT` rules |
| udp/67 | `0.0.0.0` and mesh | DHCPv4 server | — |
| udp/547 | link-local (`fe80::/10`) and mesh | — | DHCPv6 server |
| udp+tcp/53 | mesh v4; mesh v6 + Freifunk peer aggregates | client resolver | client resolver |
| udp/10000–10010 | public (not mesh, not the intra-as `/24`) | fastd | fastd |
| udp/4789 | `intra-as` only | VXLAN | VXLAN |
| udp/1001 | mesh | respondd | respondd |
| udp/123 | public, hashlimit per source | NTP | NTP |
| tcp/80 | loopback, `intra-as`, mesh; on v6 also the public region addresses | status page | status page |
| ip proto 89 | `uplink` interface and mesh aggregate | OSPFv2 | OSPFv3 |
| tcp/179 | mesh | — | BGP |

Everything else is dropped, including udp/5000–5010, tcp/161 and
tcp/9000–9999.

### OSPF is matched by interface, not by source address

OSPF is a multicast, non-stateful protocol: BIRD sends/receives Hellos to
`224.0.0.5` (OSPFv2) and `ff02::5` (OSPFv3). Two consequences shape the rules:

- The uplink rule is `-i {{ uplink_if }} -p ospf`, not a source match. The
  upstream router speaks from the transit link (`100.64.112.225`), which is
  outside the mesh aggregate; a source-only rule silently breaks the IPv4
  adjacency.
- The OSPF rules sit **before** the `--ctstate INVALID -j DROP` rule.
  Multicast/broadcast packets can be classified `INVALID` by conntrack, and an
  INVALID check placed first would drop OSPF even though a later rule accepts
  it.

The bat-interface side is matched by the mesh aggregate (`10.112.0.0/16` on
v4, `2a03:2267::/32` on v6), because those adjacencies use the gateway mesh
addresses.

### Status page is deliberately public on IPv6

The region `/64`s are announced upstream, so `http://[2a03:2267:1::50]/` is
reachable from the internet by design (`docs/status-web.md`). On IPv4 the page
is **not** exposed: it is served only on loopback, the intra-as address and the
mesh.

### The resolver is not an open resolver

`kresd` listens on the region IPv4/IPv6 addresses. Two layers keep it mesh-only:
`unbound_access_control` in `group_vars/all` (rendered as a `REFUSE` ACL by
`roles/knot`) and the `53` rules above. A query from outside the mesh is
REFUSED by the resolver and dropped by the firewall. `roles/powerdns` listens
on `127.0.0.2` only, so it is never reachable from the wire.

## Forwarding

`FORWARD` is `DROP` as well. The clamp rule is placed **before** the `ACCEPT`
rules: `TCPMSS` is a non-terminating target, and an `ACCEPT` in front of it
would end the traversal and leave new connections unclamped — the IPv6 ruleset
previously had that ordering defect, so its clamp was dead code.

### IPv4

- return traffic of tracked connections (`ESTABLISHED,RELATED`) — client
  traffic is SNATed, so return packets are de-NATed and do carry state;
- mesh aggregate → uplink (SNAT to `gw_public_snat_range`);
- uplink → published `/32` services (`mesh_forward_hosts`);
- anything outside the mesh aggregate leaving via the uplink is dropped
  (anti-spoofing).

### IPv6 is deliberately stateless

Client IPv6 traffic is **not** NATed, so a flow can leave through one gateway
and return through another. Conntrack state therefore cannot be trusted:

- an `ESTABLISHED,RELATED` fast path does not match return packets on a gateway
  that never saw the outbound direction;
- an `--ctstate INVALID` drop **actively discards** exactly those asymmetric
  packets, and packets forwarded inbound-only are not `INVALID` at all — they
  simply never match an `ESTABLISHED` rule and fall through to the `DROP`
  policy.

`FORWARD` on IPv6 is thus decided purely by interface and source address:
clients → uplink, uplink → clients, bat ↔ bat (mesh-internal services), and
bat ↔ intra-as. No conntrack rule is used, and none should be added back.

### Interface wildcards: `bat-+`, not `bat-*`

iptables only treats a **trailing `+`** as a prefix match. `bat-*` is a literal
name that matches no real interface, so `--in-interface bat-*` /
`--out-interface bat-*` rules silently never fire. This was a real defect:
`-i uplink -o bat-*` matched nothing, which is why inbound packets to clients
and to published services were dropped, and the mangle `MARK` on bat traffic
never applied. All wildcards are now `bat-+`.

### NTP over IPv6 needs mesh-internal forwarding

DHCPv4 option 42 hands out the *region's own* NTP addresses (`s.ntp_v4`), which
sit in the client's own `/64` and are reached at L2. DHCPv6 option 56 instead
advertises one global list for every client
(`kea_ntp_v6_addresses`: `…:1::10`, `…:2::20`, `…:3::30`), so a client in
another region reaches its NTP server only if the gateway forwards bat → bat.
That path is the `-i bat-+ -s mesh_v6_aggregate -o bat-+` rule; without it NTP
works over IPv4 but not over IPv6.

## Conntrack

Stateful matching is used for **`INPUT` only** (traffic addressed to the gateway
itself): established flows are accepted before the per-port rules, so the
gateway's own conversations are cheap and the per-port rules only ever see new
connections. It is *not* used for `FORWARD` on IPv6 (see above), because
forwarded client traffic may be asymmetric.

On IPv6 the `raw` table NOTRACKs **only** the fastd and VXLAN ports; it used to
NOTRACK every packet, which made a default-deny policy impossible because no
flow could ever be observed as `ESTABLISHED`. Tunnel and VXLAN packets
therefore arrive as `NEW` and are matched by their own rules.

## fail2ban

`roles/services` installs the `sshd` jail with

```
action = iptables-multiport[name=sshd, port="ssh", actionstart_on_demand=false]
```

fail2ban 1.1.0's action carries an `[Init?family=inet6]` section, so it creates
`f2b-sshd` and its `INPUT` jump in **both** tables and picks the family from the
banned address. `actionstart_on_demand=false` makes that happen at daemon start
instead of at the first ban.

`iptables-restore` flushes custom chains, so every restore removes `f2b-sshd`
and its jump. The role restarts fail2ban after a restore, and fails the run if
the jump is missing afterwards — otherwise fail2ban would be running but unable
to ban.

## SSH rules are direct ACCEPT rules

There is deliberately no user-defined allowlist chain for `sshd`. With a
`DROP` policy, a chain that ends in `RETURN` for an allowed source falls back
into `INPUT`, continues past the jump and hits the policy drop — sshd is
closed even though the source was allowlisted. Each allowlist prefix therefore
renders its own `-A INPUT … -j ACCEPT` rule, followed by a rate-limited `LOG`
and a final `REJECT`.

## Lockout guard

Because the restore replaces the running ruleset, the role refuses to apply a
ruleset that does not admit the source address of the Ansible connection. It
reads `SSH_CONNECTION` from the target's environment, extracts the client
address, and checks it against the allowlist that is about to be installed
(IPv4 or IPv6 as appropriate). On failure the run stops with the old ruleset
still live and names the prefix to add.

This check covers *membership* only. A rendered rule can still fail to accept
an allowlisted source (that is exactly what the old returning `ssh-filter`
chain did), so the post-restore assertion checks for a real tcp/22 `ACCEPT`,
and the revert guard below covers the remaining case.

## Revert guard (dead-man switch)

A ruleset restore is never the last word:

1. Before the render, the role snapshots the **live** ruleset
   (`iptables-save` / `ip6tables-save`) into `/var/lib/ffhh-fw-guard/`
   (root-only, `0700`). The live state — not the rendered files — is the
   snapshot, because that is what is actually protecting the host.
2. When the rendered ruleset changed, a transient timer is armed:
   `systemd-run --on-active={{ fw_guard_seconds }} --unit=ffhh-fw-guard`.
3. The new ruleset is restored and `fail2ban` is restarted.
4. `playbooks/gateway.yml` then opens a **fresh** SSH connection to the gateway
   from the controller (`ControlMaster=no`, `ControlPath=none`) — a real new
   TCP handshake and key auth, not a reused Ansible session.
5. Only if that probe succeeds is the timer stopped, which is the confirmation.

If the play dies anywhere before step 5 — probe failure, a later role breaking
something, an interrupted run — the timer fires and
`/usr/local/sbin/ffhh-fw-revert-guard` restores the snapshot, rewrites
`/etc/iptables/rules.v4|v6` from the same snapshot (so a reboot cannot
resurrect unconfirmed rules) and restarts fail2ban. The revert is logged as
`ffhh-fw-guard` in the journal.

The window is `fw_guard_seconds` (default `300`, five minutes; override with
`-e fw_guard_seconds=600`). It bounds how long the gateway can be unreachable
before it reverts. When nothing changes, no timer is armed — the guard is idle
on idempotent runs.

## Verification

After the restore the role reads `iptables -S` / `ip6tables -S` and asserts:

- `INPUT` and `FORWARD` are `DROP` in both families;
- `iptables -S INPUT` contains a tcp/22 `-j ACCEPT` rule in each family
  (the lockout guard already proved the control source is one of them);
- `f2b-sshd` exists in both families.

The first version of this ruleset used a returning allowlist chain; with a
`DROP` policy that closed sshd from every source. The guard checks allowlist
*membership*, but membership alone is not enough — the rendered rule must
actually accept. That is why the allowlist is rendered as direct `ACCEPT`
rules and why the post-restore check looks for the ACCEPT, not merely the jump.

## Known trade-offs

- `gw_ssh_allowlist` contains `10.0.0.0/8`, i.e. admin SSH is also reachable from
  any private 10/8 host, not only the operator prefixes.
- `udp/5000–5010` is accepted from the mesh on IPv6 but dropped on IPv4; that
  asymmetry is historical and kept deliberately.
- The NTP limiter is per source address, so it bounds one client's rate, not the
  total.
