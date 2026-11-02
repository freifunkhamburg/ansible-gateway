# NTP servers advertised to mesh clients

The Gluon site.conf servers `1.ntp.services.ffhh`, `2.ntp.services.ffhh`,
`3.ntp.services.ffhh` are advertised to clients on the mesh by the gateway's
Kea servers. Which transports can carry them:

| transport | can it carry the names? | what is announced |
| --- | --- | --- |
| DHCPv4 (option 42, RFC 2132) | **no** — the option is a list of IPv4 addresses, and the DHCP name cannot be carried (Kea rejects a hostname in option 42) | the three servers' IPv4 addresses, per region subnet |
| DHCPv6 (option 56, RFC 5908) | names possible in principle, but Kea's FQDN suboption is broken (see below) | the three servers' IPv6 addresses |
| Router Advertisement (RA) | **no** — RFC 6106/8106 RA options are RDNSS/DNSSL only; there is no NTP option in an RA | nothing (DNS-only RDNSS is already announced) |

## Where the three servers are

`1./2./3.ntp.services.ffhh` are DNS aliases for the **gw01/gw02/gw03** index
addresses of the client's region (`roles/served_regions` numbering):

| IPv6 name | IPv4 alias | gateway | address inside the client's region |
| --- | --- | --- | --- |
| `1.ntp.services.ffhh` | `v4.1.ntp.services.ffhh` | gw01 | `2a03:2267:<region>::10` / `10.112.<prefix>.251` |
| `2.ntp.services.ffhh` | `v4.2.ntp.services.ffhh` | gw02 | `2a03:2267:<region>::20` / `10.112.<prefix>.252` |
| `3.ntp.services.ffhh` | `v4.3.ntp.services.ffhh` | gw03 | `2a03:2267:<region>::30` / `10.112.<prefix>.253` |

The un-prefixed names are IPv6-only. The `v4.*` aliases carry the `A`
records (see `nodeffhh.conf` below), so IPv4-only clients can resolve the
servers by name.

The managed gateway runs `chrony` (the renderable alternative is `ntpsec`).
The daemon listens on `:123` on the bat interfaces, so the gateway is a real
NTP server, not just DNS data. `allow fe80::/10` lets clients that query the
gateway through its IPv6 link-local address be served; the mesh aggregates
alone would reject those link-local sources.

## DHCPv4 — option 42 (`ntp-servers`)

Rendered per subnet by `roles/kea/templates/kea-dhcp4.conf.j2` from
`ntp_v4`, which `roles/served_regions` composes as
`host_in(251/252/253)` of the region's `v4_net`:

```
10.112.64.0/19   -> 10.112.64.251, 10.112.64.252, 10.112.64.253
10.112.96.0/19   -> 10.112.96.251, 10.112.96.252, 10.112.96.253
10.112.192.0/19  -> 10.112.192.251, 10.112.192.252, 10.112.192.253
10.112.224.0/19  -> 10.112.224.251, 10.112.224.252, 10.112.224.253
```

The FQDNs cannot be put in option 42: it is a plain IPv4 address list and
Kea rejects a hostname there. DHCPv4 therefore announces exactly the same
addresses the `v4.*` aliases resolve to. Announcing the *names* over DHCPv4
is not possible — clients that need names use the `v4.*` aliases via DNS.

Verified in the running server via the Kea control socket (`config-get`):
`code: 42`, `name: ntp-servers`, `data: "10.112.64.251,10.112.64.252,10.112.64.253"`.

## DHCPv6 — option 56 (`OPTION_NTP_SERVER`)

RFC 5908 defines suboption 1 (IPv6 address) and suboption 2 (FQDN).

**Kea 3.0.5 truncates the FQDN suboption to 16 bytes** — tested on gw05: a
21-byte DNS-wire FQDN `1.ntp.services.ffhh` came back from the wire as the
first 16 bytes, and plain-text FQDN was truncated to 16 characters. Kea's
built-in option-56 definition cannot be overridden (`unable to override
definition of option '56' in standard option space 'dhcp6'`), so the FQDN
suboption is unusable.

The role therefore emits raw hex: three suboption-1 records (code `0001`,
length `0010`, 16 bytes), which Kea splits into three option-56 entries:

```
2a03:2267:1::10   (gw01, sued)
2a03:2267:2::20   (gw02, west)
2a03:2267:3::30   (gw03, nowe)
```

These are the gateways' *home-region* addresses. A DHCPv6 server cannot vary
its option by client interface (the client sends from a link-local address, so
no `subnet6` match), and the name→region mapping lives in DNS. Clients reach
these three addresses through their default route, so the servers are correct
for every region even though the address is not region-local.

Also required: `always-send: true`. Without it Kea sends **no** option-data in
an INFORMATION-REQUEST reply — verified against the live server, which
previously returned only client-id/server-id (so the DHCPv6 DNS option was not
being announced either). The role now flags the DNS option, the refresh
interval and the NTP option with `always-send`.

Verified live on gw05 (`bat-hhwest`, port 546 -> `ff02::1:2:547`):

```
opt 23 len 16 -> 2a032267000100000000000000000050   (DNS)
opt 32 len 4  -> 00000e10                           (refresh 3600)
opt 56 len 20 -> sub 1, 2a03:2267:1::10
opt 56 len 20 -> sub 1, 2a03:2267:2::20
opt 56 len 20 -> sub 1, 2a03:2267:3::30
```

## RA — not possible

Router Advertisements carry only the options defined for ND/RA. RFC 6106 /
RFC 8106 define RDNSS and DNSSL; there is no NTP server option. BIRD's `radv`
protocol has no NTP knob. So NTP cannot be announced via RA — clients that
need it must use DHCPv4/6 or the Gluon site.conf (`ntp_servers`, which the
firmware applies directly).

The RA config still announces RDNSS (`2a03:2267:<region>::<gw host>`), which is
DNS, not NTP.

## DNS: `nodeffhh.conf` (IPv4 aliases)

The NTP name data lives in `roles/services/files/unbound/nodeffhh.conf`
(the file maintains the gw05 version, taken over from the live
`/etc/unbound/unbound.conf.d/nodeffhh.conf`). Besides the existing per-region
`AAAA` data it now has a `redirect` zone for each `v4.N.ntp.services.ffhh`
with tags `ntpNv4-<region>`, whose `A` data is the same region-specific
address DHCPv4 announces:

```
10.112.64.0/19   v4.1 -> 10.112.64.251   v4.2 -> .252   v4.3 -> .253
10.112.96.0/19   v4.1 -> 10.112.96.251   v4.2 -> .252   v4.3 -> .253
10.112.192.0/19  v4.1 -> 10.112.192.251  v4.2 -> .252   v4.3 -> .253
10.112.224.0/19  v4.1 -> 10.112.224.251  v4.2 -> .252   v4.3 -> .253
```

The `v4` tags are attached to both the IPv4 and IPv6 netblocks, so IPv6
clients get the IPv4 aliases too. Verified live on gw05 (`dig` from the
sued/west/hhost gateway and client addresses): every region resolves its own
three addresses, and the un-prefixed names still have no `A` record.

## Sources

`roles/served_regions` (ntp_v4 composition), `roles/kea` templates,
`roles/services/files/unbound/nodeffhh.conf`, and
`kea_ntp_server_names` / `kea_ntp_server_names_v4` / `kea_ntp_v6_addresses` in
`inventories/group_vars/all/main.yml`.
