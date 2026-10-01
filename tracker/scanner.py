"""Network discovery using ARP.

ARP (Address Resolution Protocol) is how devices on a local network map an
IP address to a hardware (MAC) address. We broadcast "who has 192.168.1.x?"
to every address in the subnet, and every live device replies with its MAC.
This works even on devices that block ping, because they have to answer ARP
to stay on the network at all.

Note: on Windows the computer running the scan usually DOES show up in its
own results (Npcap sees its own replies). We match it against this
computer's adapter MACs and label it "This computer". On Linux/Mac it often
won't appear at all.
"""

import ipaddress
import socket


def guess_subnet():
    """Find this machine's local IP and assume a /24 home network around it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect doesn't send any packets; it just picks the outbound interface
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    finally:
        s.close()
    return str(ipaddress.ip_network(f"{local_ip}/24", strict=False))


def gateway_ip(subnet):
    """Assume the router is the first host address, e.g. 192.168.1.1 (or 192.168.4.1 for eero)."""
    return str(next(ipaddress.ip_network(subnet, strict=False).hosts()))


def own_macs():
    """MAC addresses of this computer's network adapters (lowercase)."""
    from scapy.all import conf

    macs = set()
    for iface in conf.ifaces.values():
        mac = (iface.mac or "").lower().replace("-", ":")
        if mac and mac != "00:00:00:00:00:00":
            macs.add(mac)
    return macs


def arp_scan(subnet, timeout=2):
    """Broadcast ARP requests across the subnet. Returns a list of {mac, ip}."""
    # Imported here so the rest of the app (list, report) works without scapy installed
    from scapy.all import ARP, Ether, srp

    packet = Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=subnet)
    # iface_hint sends out the adapter that routes to this subnet, not just the
    # default one, so a second subnet on a second adapter gets scanned too
    answered, _ = srp(packet, timeout=timeout, verbose=False, iface_hint=gateway_ip(subnet))

    found = {}
    for _, reply in answered:
        found[reply.hwsrc.lower()] = reply.psrc  # dedupe by MAC
    return [{"mac": mac, "ip": ip} for mac, ip in found.items()]


def lookup_hostname(ip):
    """Reverse DNS lookup. Many home devices won't have one, which is fine."""
    try:
        return socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        return None
