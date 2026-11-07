# fastd on gw05 (Debian 13 gateway)

`roles/fastd` builds fastd from the newest upstream release, renders
`common.conf`, the per-instance `fastd.conf` and the secret files, installs
the units and starts `fastd@<instance>` when secrets are supplied. It is part
of the single `playbooks/gateway.yml` run.

## Version policy — newest upstream release, always

- The role resolves the newest release tag from
  `https://github.com/NeoRaider/fastd` (`git ls-remote`, filtered to
  `v<number>`, sorted with `sort -V`).
- If the installed `/usr/bin/fastd` reports a different release, it clones that
  tag and builds it with meson: `meson setup --buildtype=release --prefix=/usr`,
  `ninja`, `meson install`.
- If the installed version already matches, nothing is rebuilt (idempotent,
  `changed=0` on a second run).
- `fastd_tag` can pin a release for reproducibility; empty means
  "newest".
- Build dependencies installed by the role: `meson ninja-build pkg-config bison
  git libsodium-dev libuecc-dev libcap-dev libjson-c-dev libssl-dev libmnl-dev`.
  These are what fastd 23's meson setup actually requires (each one was hit as
  a missing dependency during the first build).

The last build resolved **v23**, and Debian 13's package is `23-2` — the same
upstream release; the role builds from source, so the "latest" rule holds even
when a distro package lags later.

## Instance layout

Instance number = `gw_index + 3*(slot-1)`; the name is `<region><NN>` and the
TAP is `vpn-<region>-<NN>`. Ports are region facts (10003 … 10010).

| region | slot 0 | slot 1 |
|---|---|---|
| hhsued | `hhsued05`, port 10003, `vpn-hhsued-05` | `hhsued08`, port 10004, `vpn-hhsued-08` |
| hhwest | `hhwest05`, 10005 | `hhwest08`, 10006 |
| hhnowe | `hhnowe05`, 10007 | `hhnowe08`, 10008 |
| hhost | `hhost05`, 10009 | `hhost08`, 10010 |

That is **8 instances**, asserted against the derived
`gw_expect_fastd_instances` (`roles/regions`; 8 = four regions × two slots) —
the same two-slot-per-region count.

Tunnel MAC suffix stays the expected scheme (mac_prefix + instance number:
`de:ad:be:ef:50:15`, `…:18`, etc. for gw05).

## Configuration shape (configuration parity)

`/etc/fastd/common.conf`:
```
log to syslog level error;
method "salsa2012+umac";
method "null+salsa2012+umac";
hide ip addresses yes;
hide mac addresses yes;
mtu 1312;
secure handshakes yes;
drop capabilities force;
user "fastd";
group "fastd";
packet mark 0x6;
```



### Key scope

`fastd_key_scope: "instance"` (the default) is **one keypair per instance**:
eight keys, rendered as `/etc/fastd/secret-<region><NN>.conf` and included by
the matching `fastd.conf`. This gives every region/port its
own public key, which is what the operator asked for when generating keys for
all instances. It is a deliberate departure from the suffix-shared
layout.



## Secrets (now supplied via an encrypted vault)

`fastd_secrets` lives in the private `leo/ffhh-gw-vault` Gitea repo as `vault.yml`,
AES-encrypted with `ansible-vault`; it is symlinked to
`inventories/group_vars/all/vault.yml` (git-ignored).
The plaintext is never committed; the local password
file is `.vault_pass` (git-ignored). The role:

- renders the secret files,
- asserts that every instance (or suffix, depending on key scope) has a secret,
- starts the instances only when `fastd_start=true` and the vault is
  supplied,
- renders peer identities from `fastd_peers`,
- sets the expected tunnel MACs (`de:ad:be:ef:50:15`, `…:18`, …) on the TAPs.

Run it with the vault:

```sh
ansible-playbook playbooks/gateway-fastd.yml \
  -e fastd_start=true --vault-password-file .vault_pass
```

## Peer identities

`/var/lib/fastd/ffhh-peers` now holds one local test peer:

```
# thisisatestonly
key "2dd233274eb5da968135c3faeadabf42678c32a52923bdcc2efc76f2a8da04ee";
```

The file is named exactly `thisisatestonly`, so fastd reports the peer name as
`thisisatestonly` (the status socket confirms it, `connection: null` until a
node connects). It is rendered from the `fastd_peers` list in the role
defaults, so it is reproducible.

The real repository (`git@git.hamburg.freifunk.net:ffhh-peers`) is still
operator-provisioned: `update-fastd-keys` *pulls* it every 5 minutes (the cron
is installed), but it does not clone it. Cloning into the existing non-empty
directory needs a `git init` + remote add, or the local file moved aside first.

## TAP attachment to batman (configuration parity)

The old gateways create each fastd TAP in the **batman interface's `pre-up`
hooks**, not in the fastd config:

```
pre-up    /usr/bin/ip tuntap add dev vpn-hhsued-01 mode tap && /sbin/ip link set dev vpn-hhsued-01 address de:ad:be:ef:10:11 up
pre-up    /usr/local/sbin/batctl -m $IFACE if add vpn-hhsued-01
post-up   /usr/local/bin/ethtool-disable vpn-hhsued-01 || true
```

`ip tuntap add` creates a **persistent** TAP, so it survives fastd restarts with its
MAC intact; fastd then opens the existing interface instead of creating its
own. `roles/mesh` renders exactly this shape into
`/etc/network/interfaces.d/<region>` (modern syntax
`batctl meshif $IFACE interface add …`), and `roles/fastd` performs the
one-time conversion if a TAP was previously created by fastd itself
(`tap_persist`: stop instance → delete TAP → `ip tuntap add` → MAC/up →
attach → start), plus an idempotent live attach (`tap_attach`).

### Survives fastd restarts (verified)

The attachment is **not** made by fastd: fastd only opens an interface. The
TAP is created with `ip tuntap add` (persistent, `persist on`), brought up
and added to batman by the bat interface's hooks. A batman hardif stays
attached to its mesh independently of the fastd process, and a persistent TAP
keeps its MAC. Restarting all eight instances therefore leaves the TAPs
attached with their expected MACs — verified with
`sudo systemctl restart fastd@<region>{05,08}`: every instance came back
`active`, every TAP still `persist on` with the expected MAC, and every
`vpn-*` still `active` in its `batctl meshif … interface` list.

Boot ordering is also correct: `fastd@.service` has `After=network.target`,
and `network.target` has `After=networking.service`, so the
`/etc/network/interfaces.d` hooks (`ip tuntap add`, MAC, `batctl … interface add`) run
before fastd starts.

### The status socket directory is tmpfs — restored at boot

`/run/fastd` is on tmpfs, so it is empty after every reboot. The role creates
it during a run, but that does not happen at boot, and fastd then exits with
`unable to open status socket lock file: No such file or directory` — every
instance `failed`, all TAPs still present and attached, but no tunnels. The
role therefore also installs `/etc/tmpfiles.d/fastd.conf`:

```
d /run/fastd 0755 fastd fastd -
```

`systemd-tmpfiles-setup.service` runs in `sysinit.target`, i.e. before
`network.target`, so the directory (with its `fastd:fastd` ownership) exists
before any `fastd@*` unit starts. The role applies the same entry immediately
(`systemd-tmpfiles --create`) so the state is correct without a reboot, and
asserts the directory exists with the right owner. Verified on gw05 by
deleting `/run/fastd`, running the boot-time tmpfiles pass and starting all
eight instances: all `active`, sockets created in `/run/fastd`.

The path is `/run/fastd` (not the legacy `/var/run/fastd`, which systemd
rewrites with a warning at every boot).


The one caveat: if a TAP is deleted externally, fastd creates a fresh
non-persistent TAP (random MAC, not attached). `roles/fastd` converts
that back on the next play run (`tap_persist` + `tap_attach`),
and `ifup bat-<region>` also re-sets the MAC and re-adds it.

On gw05 all eight TAPs are now `persist on`, UP with the expected MACs, and
listed as `active` hardifs of their batman mesh:

```
bat-hhsued:  mesh-hhsued: active, vpn-hhsued-05: active, vpn-hhsued-08: active
...
```

With the one test peer connected, batman already sees a node via
`vpn-hhsued-05` (`[vpn-hhsued-05]` in the originator table).

## Not done yet

- **Real peer repository**: operator key still required (see above).

## Applied state on gw05 (2026-10-31)

`fastd v23` at `/usr/bin/fastd` (built from tag v23), the `fastd` system
user/group, `/etc/fastd/common.conf`, eight instance directories and configs,
`/lib/systemd/system/fastd@.service`, `/run/fastd` (owned by `fastd`, restored at boot by `/etc/tmpfiles.d/fastd.conf`),
eight running instances (`fastd@<region>05`, `@<region>08`), eight listening
UDP ports 10003–10010 on the two `intra-as` public addresses (16 sockets: one
v4 and one v6 per port, no wildcard), eight `vpn-*` TAPs with the expected
MACs, and `/var/lib/fastd/ffhh-peers/thisisatestonly`. Second play run:
`changed=0`.
