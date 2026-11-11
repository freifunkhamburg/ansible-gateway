# AGENTS.md — ansible-gateway

Project-specific rules. Read before doing anything here.

## 0. This repo is the development home

`ansible-gateway` (Gitea `leo/ansible-gateway`) is the single active repo for
ffhh gateway automation, replacing `leo/ffhh-gw-ansible` (archived; kept only
as history reference — never push there).

- History is secret-free **by construction**: secrets live in `leo/ffhh-gw-vault`,
  never here, not in any commit.
- Remotes: `origin` = Gitea (default for all pushes); `github` =
  `freifunkhamburg/ansible-gateway` (public mirror) — push there **only when
  explicitly asked** (pre-push hook requires `ALLOW_GITHUB_PUSH=1`).
  The mirror carries **the same history as `origin main`** — every commit,
  not a collapsed snapshot — but **only `main`**: feature branches are
  work-in-progress and are never published. Secrets are not published
  either; history is secret-free by construction (§6), so no filtering
  beyond that is needed.
- Run ansible via `.venv-ansible` (symlink to `/projects/.toolchain/ansible-venv`,
  ansible-core 2.21).
- Vault: `inventories/group_vars/all/vault.yml` and `.vault_pass` are operator
  symlinks (git-ignored) to `../ffhh-gw-vault/vault.yml` and
  `/projects/.ssh/ffhh_vault_pass`; `ansible.cfg` points at `.vault_pass`, so
  no `--ask-vault-pass` is needed.

## 1. Scope

This repository provisions **homogeneous Debian 13 gateways**. There is one
configuration for every gateway. Do not add per-gateway configuration variants,
template dialects, or compatibility paths for older hosts.

`gw01`–`gw04` are inventory entries kept for address/pool reservations and peer
derivation. Do not apply the automation to them; `gw05` is the managed host.

## 2. Branch workflow: verify on gw05 before merging

`main` is the deployable state of the gateways: every commit on it is proven,
and applying `main` to gw05 must always be safe. Therefore a branch that can
change gateway state is merged only after it has run against the live host:

1. `git checkout feature/<topic>`
2. Apply the branch, not a simulated run:
   `.venv-ansible/bin/ansible-playbook playbooks/gateway.yml -l gw05`
3. Confirm the services the change touches are still up — at minimum
   `knot-resolver`, `isc-kea-dhcp4-server`, `isc-kea-dhcp6-server`, `bird`
   and all `fastd@*` instances — plus a targeted probe (`mesh-client-check`,
   `dig`, `birdc`).
4. Record the outcome in the commit trailer `Tested-on: gw05 (…)`.
5. **Ask the operator for confirmation before merging into `main` and wait
   for it explicitly.** The operator may want to run their own tests against
   gw05 first. Never merge or push a state-changing branch to `main`
   without that go-ahead in this conversation.
6. Only after confirmation: merge the branch into `main` as one commit
   (`git merge --squash`, branch may be deleted afterwards) and push
   `origin main`. Re-run `main` on gw05 afterwards and require `changed=0`:
   that proves the merged commit is exactly the tested state.

`--check --diff` alone is not the gate: check mode creates no directories and
starts no services, so it hides ordering and permission bugs.

gw05 is the only managed host (§1), so verifying a branch means applying it to
production. That does not shrink the blast radius — it guarantees `main` never
carries untested code and that a failure is attributable to a branch rather
than to `main`. For changes that can take a service down, state the rollback
and install artifacts before any config that references them.

Small, obvious fixes (docs, comments, typos) may go straight to `main`.
Publish to GitHub only on explicit request (see §0). The mirror tracks
`origin main` commit-for-commit (features excluded), so after publishing,
`git log main` and `git log github/main` are the same list. Because the
public mirror must never inherit an unpublished branch, publish with
`git push github main` (fast-forward) and only rewrite it deliberately —
with `--force-with-lease` against the SHA you inspected — when catching up
a mirror that is behind or diverged.

## 3. One run

`playbooks/gateway.yml` is the production entry point. It is idempotent and
brings a fresh host to its full state. Do not reintroduce numbered step
playbooks; role order encodes dependencies. Use `--check --diff` first.

## 4. Inventory rule

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

## 5. Connection facts

- `ssh -i /projects/.ssh/id_ed25519_ffhh_gw leo@193.96.224.213` for gw05.
- `leo` needs a password for full sudo; Ansible `become` needs
  `--ask-become-pass` unless passwordless sudo is available.
- `~/.ssh` is not persistent here; the key lives in `/projects/.ssh`.
- Do not probe hosts with raw TCP scans or bypass the egress proxy.

## 6. Secrets and destructive changes

- Operator secrets (fastd keys) come from an ansible-vault kept in the private
  `leo/ffhh-gw-vault` repo (symlinked to `inventories/group_vars/all/vault.yml`,
  git-ignored); never commit them. This repo must stay secret-free in all history.
  Never add a GitHub remote to, or push, `ffhh-gw-vault`.
- The firewall is applied when its rendered ruleset changes. Confirm the SSH
  allowlist contains the control path before applying.
- BIRD reloads, service restarts and interface renames are expected parts of
  the run; keep every task idempotent.

## 7. Network access

The workspace uses an allowlist proxy. Source downloads and data clones happen
on the target, not on the controller. If controller-side access is needed and
is refused, stop that step rather than trying alternative routes.

## 8. Commit messages

Conventional Commits (https://www.conventionalcommits.org/en/v1.0.0/):

```
<type>(<scope>): <subject>

<body>

<trailers>
```

- Types: `feat`, `fix`, `chore`, `ci`, `docs`, `test`, `refactor`.
  Use a `BREAKING CHANGE:` footer when a role variable changes meaning.
- Scope is the module, never the host: `repo`, `ci`, `base`, `knot`, `kea`,
  `firewall`, `client-check`, `agents`, `inventory`.
- Subject: lowercase, imperative, no trailing period, at most 72 characters.
- Body: what changed and why in one short paragraph, wrapped at 72. It is not
  a changelog of the diff; call out consequences and defects fixed.
- Trailers: `Tested-on: gw05 (…)` (see §2) and `Refs: #<issue>`.
- Do not mention "squash" or the branch name in the message: consolidation is a
  merge-tool detail, not part of the change. Merged branches are deleted.

Example:

```
fix(base): create the journald drop-in directory before writing it

The directory does not exist on a stock trixie install, so the first
non-check-mode apply failed. Check mode hid the gap because it creates no
directories.

Tested-on: gw05
```
