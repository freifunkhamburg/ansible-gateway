# ansible-gateway

Ansible automation for **Freifunk Hamburg (ffhh) mesh gateways**. This is the
single development home for the gateway configuration (successor of the
archived `ffhh-gw-ansible`).

Every gateway managed by this repository is a **Debian 13** host built to the
same configuration. The existing gateways are not modified by this automation;
they are retained in the inventory only as address reservation references.

## What the automation builds

A gateway is the exit/uplink router of a mesh built on **batman-adv**. Mesh
nodes reach it through **fastd** tunnels and the gateway-to-gateway fabric is
a VXLAN/batman mesh. It provides IPv4/IPv6 uplink, DHCP, DNS and NTP to mesh
clients.

The desired state includes:

- two physical NICs, `intra-as` (transit/peering) and `uplink` (transit),
  plus `dummy0` holding the published SNAT addresses;
- batman-adv/batctl built from the pinned upstream release, one bat/VXLAN
  interface per served region;
- fastd instances derived from the region table and the gateway ordinal;
- Kea DHCPv4/v6 with per-region subnets and a per-gateway pool range inside
each region `/19` (the first `/24` of the slot is kept for static
reservations);
- BIRD OSPF/BGP/static routing;
- Knot Resolver as the client-facing resolver (unbound stays renderable but
  de-selected);
- PowerDNS on loopback as the authoritative server for the zones the gateway
  owns (community zones transferred from srv01 + hex synthesis), which kresd
  forwards to;
- chrony as the time daemon (ntpsec stays renderable but de-selected);
- vnstat, fail2ban, respondd from upstream `mesh-announce`, SSH hardening;
- the netfilter ruleset, rendered and applied when it changes.

## Diagrams

`docs/topology.md` holds the figures and is the place to start when reading
this repository: software components, Linux interfaces and addressing, the
client-to-Internet packet path (including NAT and policy routing), the
inter-gateway VXLAN fabric, and the DNS resolution chain. They are Mermaid
blocks, so they render in Gitea and stay plain text in a diff.

## One run, not a step sequence

`playbooks/gateway.yml` is the production entry point. It brings a fresh
Debian 13 host to the full configured state in a single idempotent run; role
order encodes the dependencies (network → mesh → data → Kea → BIRD →
services → PowerDNS → kresd → fastd → SSH → operations → respondd →
firewall). The old per-step playbooks were build-out scaffolding and are
gone.

```sh
ansible-playbook playbooks/gateway.yml --check --diff
ansible-playbook playbooks/gateway.yml
```

fastd peer secrets are operator-supplied and kept out of this repo entirely
(in the private `leo/ffhh-gw-vault` repo, symlinked to
`inventories/group_vars/all/vault.yml` — git-ignored, with `.vault_pass`
symlinked to the decryption password; see `AGENTS.md`). Without them the role
renders the configuration but does not start instances; use
`playbooks/gateway-fastd.yml -e fastd_start=true` for the secrets-enabled run.

Optional verification playbooks:

- `playbooks/gateway-check-upstreams.yml` — installed components vs upstream
- `playbooks/gateway-client-check.yml` — client-view service probe
- `playbooks/gateway-reverse-check.yml` — mesh reverse DNS (gateway and
  client PTRs, including a fresh uncached client probe)
- `playbooks/gateway-knot-test.yml` — Knot Resolver comparison instance

## Inventory model

```
inventories/
  hosts.yml                 targets (env-overridable)
  group_vars/all/main.yml   shared mesh facts, gateway registry, releases
  host_vars/<gateway>.yml   gateway identity and address plan
  region_vars/<region>.yml  region subnets, numbering, tunnel ports, Kea blocks
```

Only inputs are stored. Everything that follows from them is derived in
`roles/regions`:

- the gateway's address/MAC inside each served region;
- BGP and VXLAN peers from the gateway registry;
- the uplink transit network from `uplink_v4` (a /29);
- the published `/32`s from `gw_public_snat_range`;
- the NBMA region's OSPF neighbours from `region_vars`;
- NTP server addresses, mesh-announce domains, Check expectations.

`gw01`–`gw04` are address reservation references; `gw05` is the managed
reference host. See `docs/inventory.md` for the variable-by-variable model.

## Layout

```
ansible-gateway/
  README.md  AGENTS.md  ansible.cfg
  playbooks/
    gateway.yml                full production run
    gateway-fastd.yml          secrets-enabled fastd run
    gateway-check-upstreams.yml
    gateway-client-check.yml
    gateway-reverse-check.yml
    gateway-knot-test.yml
  roles/
    base  regions  network  mesh  batman_build  data_sync
    kea  bird  services  powerdns  knot  knot_build  knot_test
    fastd  ssh  operations  mesh_announce  status_web  firewall
  inventories/                 see above
  docs/                        architecture and operational notes
  tools/                       checkers used by the playbooks
```

The automation downloads upstream source and data **on the target**, so the
target's own network is used.

## Documentation map

| Document | Covers |
|---|---|
| `docs/roles.md` | role-by-role reference: what each role owns, its inputs, gating |
| `docs/topology.md` | figures: components, interfaces, packet paths, DNS chain |
| `docs/architecture.md` | runtime model, role order, addressing, backends |
| `docs/inventory.md` | inventory model: tiers, removed variables, derived values |
| `docs/knot-resolver.md` | kresd build, policy rules, local PowerDNS DNS |
| `docs/fastd.md` | tunnel numbering, keys, MTU, packet mark |
| `docs/ntp.md`, `docs/ntpsec-vs-chrony.md` | time daemon choice |
| `docs/client-check.md` | client-view verification |
| `docs/batman-health.md` | batman-adv health records |
| `docs/respondd.md` | mesh-announce/respondd domains |
| `docs/motd.md` | operator-facing login banner |
| `docs/upstream-version-checks.md` | component-vs-upstream drift |
| `docs/knot-resolver-test.md` | comparison instance |
