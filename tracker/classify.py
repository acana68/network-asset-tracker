"""Figure out who made a device and roughly what it is.

The first 3 bytes of a MAC address (the OUI) are assigned to a manufacturer,
so we can look up the vendor offline. From the vendor + hostname we make a
best guess at the device type. It's a guess, not a guarantee.
"""

_lookup = None

# (keywords to match in vendor/hostname, device type). First match wins, so
# more specific rules go first.
TYPE_RULES = [
    (["iphone", "android", "galaxy", "pixel"], "Phone"),
    (["ipad", "tablet"], "Tablet"),
    (["raspberry"], "Single-board computer"),
    (["espressif", "tuya", "wyze", "ring llc", "nest labs", "ecobee", "shelly",
      "sonoff", "kasa", "philips lighting", "signify", "honeywell", "trane",
      "chamberlain"],
     "IoT / smart home"),
    (["amazon"], "Smart speaker / Amazon device"),
    (["roku", "vizio", "sonos", "chromecast", "lg electronics", "tcl"],
     "TV / media"),
    (["hewlett", "canon", "epson", "brother", "xerox", "lexmark"], "Printer"),
    (["nintendo", "playstation", "sony interactive", "xbox"], "Game console"),
    # Commscope/Arris make both cable modems and ISP set-top boxes, so we
    # can't tell which from the vendor alone
    (["commscope", "arris"], "Cable TV box or modem"),
    (["netgear", "tp-link", "ubiquiti", "linksys", "eero", "cisco", "asustek",
      "sagemcom", "technicolor", "wnc corp", "actiontec"],
     "Network gear (router/AP/modem)"),
    # These make the Wi-Fi chips inside other brands' products, so the vendor
    # is the chip maker rather than the device brand
    (["microchip technology", "ampak", "murata", "realtek semiconductor"],
     "IoT device (Wi-Fi module maker)"),
    (["intel", "dell", "lenovo", "microsoft", "liteon", "azurewave",
      "realtek", "micro-star", "gigabyte"], "Computer"),
    (["apple"], "Apple device"),
    (["samsung"], "Samsung device"),
    (["google"], "Google device"),
]


def is_random_mac(mac):
    """Phones and laptops often use a randomized 'private' MAC for privacy.

    Those set the 'locally administered' bit: the second-lowest bit of the
    first byte. e.g. x2, x6, xA, xE as the second hex digit.
    """
    first_byte = int(mac.split(":")[0], 16)
    return bool(first_byte & 0x02)


def get_vendor(mac):
    global _lookup
    try:
        from mac_vendor_lookup import MacLookup
    except ImportError:
        return None
    if _lookup is None:
        _lookup = MacLookup()
    try:
        return _lookup.lookup(mac)
    except Exception:
        return None


def guess_type(vendor, hostname, random_mac):
    text = f"{vendor or ''} {hostname or ''}".lower()
    for keywords, device_type in TYPE_RULES:
        if any(k in text for k in keywords):
            return device_type
    # The manufacturer paid to keep its OUI registration anonymous
    if vendor == "Private":
        return "Unknown (vendor hidden)"
    if random_mac:
        return "Phone/tablet/laptop (private MAC)"
    return "Unknown"


def classify(mac, hostname=None, is_gateway=False, is_self=False):
    random_mac = is_random_mac(mac)
    # Randomized MACs aren't registered to anyone, so skip the lookup
    vendor = None if random_mac else get_vendor(mac)
    # What we know from the network itself beats a guess from the vendor
    if is_self:
        device_type = "This computer"
    elif is_gateway:
        device_type = "Router / gateway"
    else:
        device_type = guess_type(vendor, hostname, random_mac)
    return {
        "vendor": vendor,
        "is_random_mac": random_mac,
        "device_type": device_type,
    }
