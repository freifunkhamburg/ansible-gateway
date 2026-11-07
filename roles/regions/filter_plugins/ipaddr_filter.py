"""Filters used by roles/regions to derive addressing from an ordinal.

Ansible core alone ships no ipaddr filter (that lives in ansible.netcommon /
community.general, neither installed here), so this uses stdlib `ipaddress`.

The mesh uses one numbering scheme:
the BGP peer addresses follow the same ordinal scheme.

  gateway ordinal N (gw01 -> 1, gw02 -> 2, gw03 -> 3)

  bat IPv4 host   = region network + (250 + N)     -> .251, .252, .253
  bat IPv6 host   = region prefix  + (N * 0x10)    -> ::10, ::20, ::30
  bat MAC prefix   = de:ad:be:ef:<N*0x10>:<region_index>
  link-local       = EUI-64 of that MAC + "0"

Only the IPv4 host octet and the MAC field are decimal; the IPv6 host is the
hex-formatted ordinal (0x10, 0x20, 0x30), which is why it reads "::10".
"""

from ipaddress import ip_address, ip_network


def in_subnet(value, subnet):
    """True if `value` is an address inside `subnet`."""
    try:
        return ip_address(str(value)) in ip_network(str(subnet), strict=False)
    except ValueError:
        return False


def host_in(net, offset):
    """<network address + offset>/<same prefix length>."""
    n = ip_network(str(net), strict=False)
    return f"{n.network_address + int(offset)}/{n.prefixlen}"


def addr_only(value):
    """Strip the prefix length from an address."""
    return str(value).split("/", 1)[0]


def net_only(value):
    """The network/prefix of an address-with-prefix, e.g. 100.64.112.226/29
    -> 100.64.112.224/29."""
    n = ip_network(str(value), strict=False)
    return f"{n.network_address}/{n.prefixlen}"


def site48(value):
    """The /48 site aggregate containing a /64 prefix."""
    n = ip_network(str(value), strict=False)
    return str(n.supernet(new_prefix=48))


def netmask(value):
    """Dotted netmask of a CIDR network, e.g. 10.112.0.0/16 -> 255.255.0.0."""
    return str(ip_network(str(value), strict=False).netmask)


def reverse_zone(value):
    """Reverse-DNS zone name for a CIDR network.

    10.112.0.0/16      -> 112.10.in-addr.arpa
    2a03:2267::/32     -> 7.6.2.2.3.0.a.2.ip6.arpa
    """
    n = ip_network(str(value), strict=False)
    if n.version == 4:
        octets = str(n.network_address).split('.')
        keep = (n.prefixlen + 7) // 8
        return '.'.join(reversed(octets[:keep])) + '.in-addr.arpa'
    nibbles = (n.prefixlen + 3) // 4
    digits = f"{int(n.network_address):032x}"
    return '.'.join(reversed(digits[:nibbles])) + '.ip6.arpa'


def v6_ptr_owner(address, prefix):
    """PTR owner name of `address` relative to the reverse zone of `prefix`.

    v6_ptr_owner('2a03:2267:1::10', '2a03:2267:1::/64')
      -> '0.1.0.0.0.0.0.0.0.0.0.0.0.0.0.0'   (16 host nibbles, reversed)
    """
    n = ip_network(str(prefix), strict=False)
    if n.version != 6 or n.prefixlen % 4 != 0:
        raise ValueError(f"v6_ptr_owner needs a /4n IPv6 prefix: {prefix!r}")
    addr = ip_address(str(address))
    host_bits = (128 - n.prefixlen) // 4
    offset = int(addr) - int(n.network_address)
    if offset < 0:
        raise ValueError(f"{address!r} is outside {prefix!r}")
    digits = f"{offset:0{host_bits}x}"
    return '.'.join(reversed(digits))


def v6_synth_ptr(address, synth_prefix="ipv6-", origin="clients.hamburg.freifunk.net"):
    """The synthesized PTR rdata for an IPv6 client address.

    The community scheme (served by roles/powerdns inside the mesh and by
    the community DNS outside it) uses the interface part as 16 hex
    digits, so with origin 'sued.clients.hamburg.freifunk.net'
    2a03:2267:1::5 -> 'ipv6-0000000000000005.sued.clients.hamburg.freifunk.net.'
    """
    host = int(ip_address(str(address))) & ((1 << 64) - 1)
    return f"{synth_prefix}{host:016x}.{origin}."


def ip_normalize(value):
    """Canonical (compressed, lower-case) text form of an IP address.

    '2a03:2267:1:0:0:0:0:fefe' -> '2a03:2267:1::fefe'
    """
    return str(ip_address(str(value).strip()))


def snat_hosts(value):
    """Expand a published range ('193.96.224.24-193.96.224.31') into /32s.

    The range must stay inside one /24; that is how a gateway's published NAT
    block is defined on this mesh.
    """
    first, last = str(value).split("-", 1)
    a, b = ip_address(first), ip_address(last)
    if a.version != 4 or b.version != 4:
        raise ValueError(f"snat range must be IPv4: {value!r}")
    prefix = str(a).rsplit(".", 1)[0]
    if str(b).rsplit(".", 1)[0] != prefix:
        raise ValueError(f"snat range must be inside one /24: {value!r}")
    return [f"{prefix}.{i}/32" for i in range(int(str(a).split(".")[-1]),
                                             int(str(b).split(".")[-1]) + 1)]


def mac_prefix(gw_index, region_index):
    """de:ad:be:ef:<gw_index as hex, step 0x10>:<region index>."""
    return f"de:ad:be:ef:{int(gw_index) * 16:x}:{int(region_index)}"


def eui64(mac):
    """IPv6 link-local (EUI-64) address of a MAC, e.g. fe80::dcad:beff:feef:1010."""
    o = [int(x, 16) for x in str(mac).split(":")]
    if len(o) != 6:
        return ""
    o[0] ^= 0x02
    return "fe80::" + ":".join(
        [f"{o[0]:02x}{o[1]:02x}", f"{o[2]:02x}ff", f"fe{o[3]:02x}", f"{o[4]:02x}{o[5]:02x}"]
    )


def tunnel_slots(region, gw_index):
    """Expand a region's fastd peer slots into this gateway's tunnel list.

    The instance number is a gateway fact, not a region fact: it is
    gw_index + 3*(slot-1). Examples: gw01 -> 01/04, gw03 -> 03/06
    (so gw02 would be 02/05). The MAC suffix is that same number appended
    to the region MAC prefix: de:ad:be:ef:10:11 / :14 on gw01, :13 / :16 on
    gw03. Ports stay per region/slot.
    """
    out = []
    for i, slot in enumerate(region.get("tunnel_slots", [])):
        n = int(gw_index) + 3 * i
        suffix = f"{n:02d}"
        out.append(
            {
                "iface": f"vpn-{region['id']}-{suffix}",
                "mac_suffix": str(n),
            }
        )
    return out


def fastd_instances(region, gw_index):
    """Fastd instance names for this gateway, same numbering as the tunnels."""
    out = []
    for i, slot in enumerate(region.get("tunnel_slots", [])):
        n = int(gw_index) + 3 * i
        suffix = f"{n:02d}"
        out.append(
            {
                "name": f"{region['id']}{suffix}",
                "iface": f"vpn-{region['id']}-{suffix}",
                "port": slot["port"],
                "bat_if": region.get("bat_if"),
                "mac": f"{region.get('mac_prefix', '')}{n % 10}",
            }
        )
    return out


class FilterModule(object):
    def filters(self):
        return {
            "in_subnet": in_subnet,
            "host_in": host_in,
            "addr_only": addr_only,
            "net_only": net_only,
            "netmask": netmask,
            "reverse_zone": reverse_zone,
            "v6_ptr_owner": v6_ptr_owner,
            "v6_synth_ptr": v6_synth_ptr,
            "ip_normalize": ip_normalize,
            "site48": site48,
            "snat_hosts": snat_hosts,
            "mac_prefix": mac_prefix,
            "eui64": eui64,
            "tunnel_slots": tunnel_slots,
            "fastd_instances": fastd_instances,
        }
