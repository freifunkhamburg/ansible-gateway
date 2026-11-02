# ffhh-gw-ansible

Ansible automation for **Freifunk Hamburg (ffhh) mesh gateways**.

Every gateway managed by this repository is a **Debian 13** host built to the
same configuration. The existing gateways are not modified by this automation;
they are retained in the inventory only as address reservation references.

## What the automation builds

A gateway is the exit/uplink router of a mesh built on **batman-adv**. Mesh
nodes reach it through **fastd** tunnels and the gateway-to-gateway fabric is
a VXLAN/batman mesh. It provides IPv4/IPv6 uplink, DHCP, DNS and NTP to mesh
clients.

The desired state includes:

- two NICs named `intra-as` (uplink to the transit fabric) and `uplink`;
- batman-adv/batctl built from the pinned upstream release, one bat/VXLAN
  interface per served region;
- fastd instances derived from the region table and the gateway ordinal;
- Kea DHCPv4/v6 with per-region subnets and per-gateway `/21` pools;
- BIRD OSPF/BGP/static routing;
- Knot Resolver as the client-facing resolver (unbound stays renderable but
  de-selected);
- chrony as the time daemon (ntpsec stays renderable but de-selected);
- vnstat, fail2ban, respondd from upstream `mesh-announce`, SSH hardening;
- the netfilter ruleset, rendered and applied when it changes.

## One run, not a step sequence

`playbooks/gateway.yml` is the production entry point. It brings a fresh
Debian 13 host to the full configured state in a single idempotent run; role
order encodes the dependencies (network → mesh → data → Kea → BIRD → services
→ fastd → SSH → operations → respondd → firewall). The old per-step playbooks
were build-out scaffolding and are gone.

```sh
ansible-playbook playbooks/gateway.yml --check --diff
ansible-playbook playbooks/gateway.yml
```

fastd peer secrets are operator-supplied and kept out of this repo entirely
(in the private `leo/ffhh-gw-vault` repo, symlinked to
`inventories/group_vars/all/vault.yml` — git-ignored). Without them the role
renders the configuration but does not start instances; use
`playbooks/gateway-fastd.yml -e fastd_start=true --ask-vault-pass` for the
secrets-enabled run.

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
ffhh-gw-ansible/
  README.md  AGENTS.md  ansible.cfg
  playbooks/
    gateway.yml               full production run
    gateway-fastd.yml         secrets-enabled fastd run
    gateway-check-upstreams.yml
    gateway-client-check.yml
    gateway-knot-test.yml
  roles/
    base  regions  network  mesh  batman_build  data_sync
    kea  bird  services  knot  knot_build  knot_test
    fastd  ssh  operations  mesh_announce  firewall
  inventories/                see above
  docs/                       architecture and operational notes
  tools/                      checkers used by the playbooks
```

The automation downloads upstream source and data **on the target**, so the
target's own network is used.
