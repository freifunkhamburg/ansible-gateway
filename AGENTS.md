# AGENTS.md — ffhh-gw-ansible

Project-specific rules. Read before doing anything here.

## 1. Scope

This repository provisions **homogeneous Debian 13 gateways**. There is one
configuration for every gateway. Do not add per-gateway configuration variants,
template dialects, or compatibility paths for older hosts.

`gw01`–`gw04` are inventory entries kept for address/pool reservations and peer
derivation. Do not apply the automation to them; `gw05` is the managed host.

## 2. One run

`playbooks/gateway.yml` is the production entry point. It is idempotent and
brings a fresh host to its full state. Do not reintroduce numbered step
playbooks; role order encodes dependencies. Use `--check --diff` first.

## 3. Inventory rule

The inventory stores **inputs only**. Derived values belong in
`roles/regions`, not in host/group vars: region addresses, BGP/VXLAN peers,
transit network, published `/32`s, OSPF neighbours, NTP addresses, respondd
domains and check expectations. Do not add a variable that repeats information
already present in another variable.

Shared values (AS number, mesh aggregates, releases, forward-host list, NTP
upstreams, Kea NTP options) belong in `group_vars/all`. A value that differs
per gateway (address plan, pools, allowlists, admin keys, NIC MACs) belongs in
`host_vars`. A value that differs per region (subnet, numbering, tunnel ports,
Kea block, Gluon domain) belongs in `region_vars`.

## 4. Connection facts

- `ssh -i /projects/.ssh/id_ed25519_ffhh_gw leo@193.96.224.213` for gw05.
- `leo` needs a password for full sudo; Ansible `become` needs
  `--ask-become-pass` unless passwordless sudo is available.
- `~/.ssh` is not persistent here; the key lives in `/projects/.ssh`.
- Do not probe hosts with raw TCP scans or bypass the egress proxy.

## 5. Secrets and destructive changes

- Operator secrets (fastd keys) come from an ansible-vault kept in the private
  `leo/ffhh-gw-vault` repo (symlinked to `inventories/group_vars/all/vault.yml`,
  git-ignored); never commit them. This repo must stay secret-free in all history.
- The firewall is applied when its rendered ruleset changes. Confirm the SSH
  allowlist contains the control path before applying.
- BIRD reloads, service restarts and interface renames are expected parts of
  the run; keep every task idempotent.

## 6. Network access

The workspace uses an allowlist proxy. Source downloads and data clones happen
on the target, not on the controller. If controller-side access is needed and
is refused, stop that step rather than trying alternative routes.
