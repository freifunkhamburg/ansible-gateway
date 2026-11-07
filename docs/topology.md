# Component and interface topology

Figures for how the gateway's software fits together and how its Linux
network interfaces relate to client traffic, DNS and the uplink. All
figures are [Mermaid](https://mermaid.js.org/) blocks: they render in
Gitea, GitHub, GitLab and VS Code's markdown preview, and are plain text
in a diff.

Everything drawn here is taken from the rendered configuration in this
repository (templates and inventory), not from a live host. The values
shown are gw05's, the reference host; other gateways get the same shape
with their own addresses.

**Figure index**

| # | Figure | Answers |
|---|---|---|
| 1 | Software components | which daemon does what, and how they depend on each other |
| 2 | Linux interfaces and addressing | which NIC/device carries which address, and how mesh devices stack |
| 3 | Client traffic to the Internet | the forward and return packet path, including NAT and policy routing |
| 4 | Inter-gateway fabric | how VXLAN/batman ties gateways together |
| 5 | DNS resolution path | client → kresd → PowerDNS → community, and where synthesis happens |
| 6 | Routing decision | the `ip rule`/table-42 policy, and the two fwmark values |
| 7 | Apply order | role order as a dependency graph |

## 1. Software components

```mermaid
flowchart TB
  CLIENT["mesh clients<br/>DHCP / SLAAC"]
  COMM["community DNS<br/>srv01.hamburg.freifunk.net (dns01.ffhh)"]
  UPSTREAM["upstream router<br/>transit peers"]
  INET["Internet"]

  subgraph SERVICES["Client-facing services"]
    KEA4["kea-dhcp4"]
    KEA6["kea-dhcp6"]
    KRESD["kresd (Knot Resolver)<br/>127.0.0.1:53 + region gw addrs"]
    PDNS["pdns_server (PowerDNS Lua2)<br/>127.0.0.2:53"]
    FETCH["ffhh-dns-fetch<br/>dig AXFR, 15 min timer"]
    CHRONY["chrony"]
    RESPOND["respondd (mesh-announce)"]
  end

  subgraph TRANSPORT["Mesh transport"]
    FASTD["fastd instances<br/>vpn-hhsued-05 ... vpn-hhost-10"]
    BATMAN["batman-adv (module + batctl)<br/>bat-hhsued ... bat-hhost"]
    VXLAN["VXLAN mesh-hhsued ... mesh-hhost"]
  end

  subgraph ROUTING["Routing and NAT"]
    BIRD["BIRD 3<br/>kernel sync, OSPFv2/v3, BGP,<br/>static natips, RADV"]
    NF["netfilter<br/>rules.v4 / rules.v6"]
  end

  subgraph DATA["Data inputs"]
    STATIC["dhcp-static checkout<br/>(Kea reservations)"]
    ZDATA["ffhh-zones-data.lua<br/>ffhh + 112.10.in-addr.arpa"]
  end

  subgraph OPS["Operations"]
    VNSTAT["vnstat"]
    F2B["fail2ban"]
    CHK["check-gateway cron"]
    SSHD["sshd (allowlisted)"]
  end

  CLIENT -- "DHCPv4" --> KEA4
  CLIENT -- "DHCPv6" --> KEA6
  CLIENT -- "DNS" --> KRESD
  CLIENT -- "NTP" --> CHRONY
  CLIENT -- "respondd multicast ff02::2:1001" --> RESPOND
  CLIENT -- "fastd UDP 10003-10010" --> FASTD

  FASTD -- "TAP hardif" --> BATMAN
  VXLAN -- "hardif" --> BATMAN
  KEA4 --> BATMAN
  KEA6 --> BATMAN
  RESPOND --> BATMAN
  STATIC -- "includes" --> KEA4

  BATMAN -- "export to kernel table 42" --> BIRD
  BIRD -- "table 42 + main" --> NF
  NF -- "SNAT to the published /32 pool" --> UPSTREAM
  UPSTREAM --- INET

  KRESD -- "ffhh, 112.10.in-addr.arpa,<br/>region /64 and clients zones" --> PDNS
  KRESD -- "everything else<br/>NO_0X20 + DNSSEC_INSECURE" --> COMM
  FETCH -- "AXFR ffhh + 112.10" --> COMM
  FETCH -- "atomic rewrite" --> ZDATA
  ZDATA -- "reloaded in-process" --> PDNS

  CHRONY -- "NTP over intra-as" --> UPSTREAM
  VNSTAT -. "counts NICs and bat interfaces" .-> BATMAN
  F2B -. "filter sshd" .-> SSHD
  CHK -. "batctl gw mode" .-> BATMAN
```

**Reading it.** Two planes run side by side: *data* (fastd → batman-adv →
BIRD → netfilter → uplink) and *control/answering* (Kea, kresd/PowerDNS,
chrony, respondd). The DNS chain is deliberately split: `kresd` is the only
thing clients see; PowerDNS is a loopback-only authoritative server for the
zones this gateway owns, fed by `ffhh-dns-fetch` and by its own Lua
synthesis. `respondd` and Kea bind to the region bat interfaces, so they only
answer on the client-facing side.

## 2. Linux interfaces and addressing

```mermaid
flowchart LR
  subgraph TRANSIT["Transit side"]
    UP["upstream router<br/>100.64.112.225 / 193.96.224.210"]
    PEERS["other ffhh gateways<br/>VXLAN peers"]
    NET["Internet"]
  end

  subgraph HOST["gw05"]
    IAS["intra-as<br/>193.96.224.213/29<br/>2a03:2267:ffff:d00::5000/64"]
    UPL["uplink<br/>100.64.112.226/29<br/>IPv6 link-local only"]
    DUM["dummy0<br/>193.96.224.24-31/32<br/>published SNAT pool"]
    LOOP["lo<br/>127.0.0.1 / ::1"]

    subgraph REGION["Per served region"]
      BAT["bat-hhsued ... bat-hhost<br/>v4 10.112.64.255/19 ... 10.112.224.255/19<br/>v6 2a03:2267:1::50/64 ... 2a03:2267:4::50/64"]
      TAP["vpn-hhsued-05 ... vpn-hhost-10<br/>8 TAP devices, one per fastd instance"]
      VX["mesh-hhsued ... mesh-hhost<br/>VXLAN id 101-104, dstport 4789"]
    end

    subgraph SOCKS["Loopback-only sockets"]
      KPORT["kresd :53<br/>127.0.0.1 + region gw addrs"]
      PPORT["pdns_server :53<br/>127.0.0.2 only"]
    end
  end

  NET --- UP
  UP --- IAS
  UP --- UPL
  IAS --- PEERS
  IAS -- "VXLAN underlay (local = intra-as addr)" --> VX
  VX -- "batctl interface add" --> BAT
  TAP -- "batctl interface add" --> BAT
  BAT -- "10.112/16 + region /64s" --> UPL
  DUM -. "SNAT source addresses" .-> UPL
  LOOP --> KPORT
  LOOP --> PPORT
```

**Reading it.** Three kinds of device on the host:

- **NICs.** `intra-as` carries the public `/29` and the IPv6 transit block;
  `uplink` carries the transit `/29` plus link-local IPv6 only (no SLAAC, no
  RA-provided default — v6 egress is routed by OSPFv3). `dummy0` is a dummy
  device that exists only to hold the published SNAT `/32`s; it has no IPv6.
  `lo` carries the loopback-only DNS sockets.
- **Mesh devices.** `bat-<region>` is the batman-adv soft interface that
  clients see: it holds the gateway's region address (region network `+250+N`,
  region prefix `+N*0x10`) and is the interface Kea, RADV and respondd use.
  `vpn-<region>-NN` are the fastd TAP hardifs; `mesh-<region>` is the VXLAN
  hardif to the other gateways. Both are attached to `bat-<region>` with
  `batctl`.
- **Sockets.** `kresd` listens on `127.0.0.1`/`::1` and on each region's
  gateway address (the DHCP-advertised resolver). PowerDNS listens only on
  `127.0.0.2`, so it is never reachable from the mesh — kresd forwards to it
  and no firewall rule is needed.

## 3. Client traffic to the Internet

```mermaid
sequenceDiagram
  autonumber
  participant C as Mesh client (10.112.65.37)
  participant F as fastd (vpn-hhsued-05)
  participant B as bat-hhsued
  participant R as routing (ip rule + table 42)
  participant N as netfilter (FORWARD + nat)
  participant U as uplink (100.64.112.226)
  participant W as upstream router
  participant I as Internet

  C->>F: UDP 10003-10010 (salsa2012+umac)
  F->>B: decapsulated frame onto the TAP hardif
  B->>R: packet in via bat-hhsued
  Note over R: pref 31000, iif bat-hhsued, lookup table 42
  R->>N: forward, MSS clamped to 1240
  Note over N: POSTROUTING SNAT<br/>10.112.0.0/16 to 193.96.224.24-31
  N->>U: source rewritten to a published /32
  U->>W: transit /29
  W->>I: Internet

  I->>W: return packet to the published /32
  W->>U: route 193.96.224.0/24 (BIRD natips)
  U->>R: packet in via uplink
  Note over R: pref 31000, iif uplink, lookup table 42
  R->>B: back into the region interface
  B->>F: batman-adv picks the hardif to the client
  F->>C: encrypted tunnel frame
```

**Reading it.** Inbound mesh traffic enters on the transit side — peers dial
the `intra-as` address (fastd binds `any:<port>`) — is decapsulated onto a
TAP, and lands in `bat-<region>`. From there the policy rules send it to **table 42** (the mesh
table BIRD populates), netfilter clamps MSS and rewrites the source to one of
the published `193.96.224.24-31`, and it leaves via `uplink`. Return traffic
for those `/32`s comes back from the upstream router; whichever transit NIC
it arrives on, BIRD also syncs the mesh routes into `main`
(`kernel_main4/6`), so forwarding can reach the region either way, and
batman-adv selects the tunnel back to the client. `dummy0` holds the `/32`s
so the host owns them as local addresses.

Two marks matter: fastd sets **fwmark `0x6`** on its own tunnel packets so
they leave over `main` (the transit route), and netfilter sets **fwmark
`0x1`** on traffic from `uplink`/`bat-*` (the gateway-control path), which
`check-gateway` ensures is routed through table 42.

## 4. Inter-gateway fabric

```mermaid
flowchart LR
  subgraph G5["gw05"]
    B5["bat-hhsued"]
    V5["mesh-hhsued<br/>local 193.96.224.213"]
  end
  subgraph G1["gw01"]
    B1["bat-hhsued"]
    V1["mesh-hhsued<br/>local 193.96.224.x"]
  end
  subgraph G2["gw02"]
    B2["bat-hhsued"]
    V2["mesh-hhsued"]
  end
  B5 --- V5
  B1 --- V1
  B2 --- V2
  V5 -- "VXLAN 4789 over intra-as" --> V1
  V5 -- "VXLAN 4789 over intra-as" --> V2
  B5 -- "batman-adv OGMs across the fabric" --> B1
```

**Reading it.** Each served region has its own VXLAN segment. The underlay is
the gateways' `intra-as` addresses, and the peers come from the gateway
registry in `group_vars/all` — adding a gateway updates every VXLAN peer list
and every BGP neighbour automatically. batman-adv runs on top, so a client
attached to any gateway's region can reach any other gateway in the fabric.

## 5. DNS resolution path

```mermaid
flowchart LR
  C["client"]
  K["kresd :53<br/>client-facing resolver"]
  P["pdns_server :53<br/>127.0.0.2"]
  F["ffhh-dns-fetch"]
  D["ffhh-zones-data.lua<br/>generated Lua zone data"]
  COMM["community DNS srv01/srv03"]

  C -- "query" --> K
  K -- "ffhh, 112.10.in-addr.arpa,<br/>served region /64 + clients zones" --> P
  K -- "other regions / non-served names<br/>NO_0X20, DNSSEC_INSECURE" --> COMM
  F -- "AXFR: ffhh + 112.10.in-addr.arpa" --> COMM
  F -- "JSON cache to Lua data file" --> D
  D -- "reload every ~5 s" --> P
  P -- "transferred records verbatim" --> K
  P -- "authoritative (AA) when ffhh_dns_authoritative_v4/v6" --> K
  P -- "synthesis: HEX.region.ffhh,<br/>ipv6-HEX.region.clients..." --> K
```

**Reading it.** `kresd` never talks to the community for a zone this gateway
owns; it forwards it to the local PowerDNS, which answers from transferred
content first and synthesis second. Inside those zones, names that match
neither a transferred record nor a synthesized pattern get NODATA from the
local PowerDNS -- they are not passed upstream. What does go upstream is the
reverse space this gateway does not own (other regions' `/64`s, and the rest
of the `/32`), with `NO_0X20` because the upstream backend is case-sensitive
and `DNSSEC_INSECURE` because those answers are unsigned. The fetch timer is
the only moving part: zones are as fresh as the last successful AXFR, and a
failed transfer keeps the previous data.

## 6. Routing decision

```mermaid
flowchart TD
  P["outgoing or forwarded packet"] --> M{"fwmark 0x6?<br/>fastd's own tunnel packets"}
  M -- yes --> MAIN["lookup main<br/>intra-as / uplink routes"]
  M -- no --> IN{"arrived on a bat region interface<br/>or on uplink?"}
  IN -- yes --> T42["lookup table 42 (mesh)<br/>BIRD kernel_mesh4/6"]
  IN -- "from a bat address" --> T42
  IN -- no --> CK{"fwmark 0x1?<br/>gateway-control traffic"}
  CK -- yes --> T42
  CK -- no --> DEF["main table defaults"]
```

**Reading it.** Policy routing is how the gateway keeps mesh and transit
traffic apart with one routing daemon. Table 42 is written by BIRD's
`kernel_mesh4`/`kernel_mesh6` protocols from the `mesh4`/`mesh6` tables. The
`ip rule` entries at preference 31000/31001 send traffic arriving on a mesh
interface into table 42 (31001 makes it unreachable while the interface is
down); `check-gateway` also installs a preference-32000 `fwmark 0x1` rule, and
the firewall marks packets arriving on `uplink` or `bat-*` with `0x1`, so
gateway-control traffic is looked up in table 42 too. The `0x6` rule is what
stops fastd's own packets from looping back into the mesh.

## 7. Apply order

```mermaid
flowchart LR
  base --> regions --> network --> mesh
  mesh --> data_sync --> kea
  kea --> bird --> services
  services --> powerdns --> knot
  knot --> fastd --> ssh --> operations --> mesh_announce --> firewall
  batman_build -. "include_role" .-> mesh
  knot_build -. "meta dependency" .-> knot
```

**Reading it.** This is a dependency order, not a staging sequence: one run
converges everything, and `--check --diff` shows what would change. `firewall`
is last because every address it needs must already be derived.

## Service and port map

| Service | Bind | Port / socket | Scope |
|---|---|---|---|
| kresd | `127.0.0.1`, `::1`, region `bat-*` addresses | 53 | mesh clients + host |
| pdns_server | `127.0.0.2` | 53 | loopback only (kresd forwards) |
| kea-dhcp4 / kea-dhcp6 | region `bat-*` interfaces | 67 / 547, unix control socket | mesh clients |
| fastd | all interfaces | UDP 10003-10010 (two slots per region) | mesh peers |
| VXLAN | `intra-as` underlay | UDP 4789 | gateway peers |
| respondd | region `bat-*` interfaces | multicast ff02::2:1001, port 1001 | mesh nodes |
| chrony | all interfaces, ACL = mesh aggregates + loopback | UDP 123 | mesh clients + host |
| sshd | all | TCP 22, allowlisted sources | operators |
| ffhh-dns-fetch | host | none (oneshot timer) | data refresh |

## Where to change what

| What you see in the figures | Template / variable |
|---|---|
| `intra-as`, `uplink`, `dummy0` addresses | `roles/network/templates/interfaces.j2`, `interface-loopback.j2`, `host_vars` |
| `bat-<region>`, `vpn-*-NN`, `mesh-<region>` | `roles/mesh/templates/interface-region.j2`, `region_vars`, `gw_index` |
| fastd instances, ports, packet mark | `roles/fastd/*`, `fastd_common_conf`, `tunnel_slots` |
| table 42, OSPF/BGP/RADV | `roles/bird/templates/bird.conf.j2` |
| SNAT, MSS clamp, SSH allowlist | `roles/firewall/templates/rules.v4.j2`, `rules.v6.j2`, `gw_ssh_allowlist` |
| kresd forwards | `roles/knot/templates/kresd.conf.j2` |
| PowerDNS zones and synthesis | `roles/powerdns/templates/ffhh-synth.lua.j2`, `defaults/main.yml` |
| Reverse-zone authority (v4 / v6) | `ffhh_dns_authoritative_v4`, `ffhh_dns_authoritative_v6` (Lua zone claim + kresd `{auth = true}`) |
| Transferred zone set | `ffhh_dns_axfr_zones`, `ffhh_dns_axfr_masters` |
| Kea subnets and pools | `roles/kea/templates/kea-dhcp*.conf.j2`, `gw_kea_pools` |
| respondd domains | `roles/mesh_announce/templates/respondd.conf.j2` |

If a template changes the topology, update the matching figure in this
file — the figures are only useful if they track the rendered config.
