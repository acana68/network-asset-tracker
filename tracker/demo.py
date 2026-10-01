"""Fake network data for screenshots, so the README never shows a real network.

    python -m tracker demo                        # (re)build demo.db
    python -m tracker --db demo.db scan           # simulated scan, no packets sent
    python -m tracker --db demo.db list
    python -m tracker --db demo.db report
    python -m tracker --db demo.db test-alert     # Discord alert listing demo devices

The MACs are made up, but start with real vendor prefixes (OUIs), so they get
the same vendor names and device types a real scan would give them.

A demo database is marked with a demo_info table. `scan` never does a real
ARP sweep into a marked database, and `demo` refuses to overwrite anything
that isn't one, or anything called assets.db.
"""

import ipaddress
import os
import random
import sqlite3
from datetime import datetime, timedelta

from . import db
from .classify import guess_type, is_random_mac
from .scanner import gateway_ip

DEFAULT_PATH = "demo.db"
MARKER_TABLE = "demo_info"

SUBNETS = ["192.168.1.0/24", "192.168.4.0/22"]
DAYS = 3
SCAN_EVERY = timedelta(minutes=30)  # same as the scheduled task
SEED = 7  # fixed, so every rebuild looks the same

# The "computer running the scans", labeled like a real scan would label it
THIS_COMPUTER = "f8:94:c2:3a:71:5e"

# Vendor strings are exactly what mac_vendor_lookup returns for each prefix.
# nickname = trusted. presence = chance of answering any one scan.
# window = (first, last) days ago the device was around.
DEVICES = [
    # 192.168.1.0/24
    dict(mac="f8:bb:bf:4e:29:30", ip="192.168.1.1", vendor="eero inc.",
         nickname="Mesh router (WAN side)"),
    dict(mac="3c:52:82:9d:14:c6", ip="192.168.1.23", vendor="Hewlett Packard",
         hostname="HP9D14C6", nickname="Office printer"),
    dict(mac="8c:79:f5:62:b0:1d", ip="192.168.1.40", vendor="Samsung Electronics Co.,Ltd",
         hostname="Samsung-TV", nickname="Living room TV", presence=0.85),
    dict(mac="24:0a:c4:7e:58:93", ip="192.168.1.57", vendor="Espressif Inc.",
         window=(2, 0)),
    # 192.168.4.0/22
    dict(mac="f8:bb:bf:4e:29:31", ip="192.168.4.1", vendor="eero inc.",
         nickname="Mesh router"),
    dict(mac=THIS_COMPUTER, ip="192.168.4.12", vendor="Intel Corporate",
         hostname="DESKTOP-OFFICE", nickname="Office desktop"),
    dict(mac="f0:18:98:c4:2e:7a", ip="192.168.4.20", vendor="Apple, Inc.",
         hostname="MacBook-Air", nickname="Work laptop", presence=0.7),
    dict(mac="88:66:5a:1f:d3:08", ip="192.168.4.31", vendor="Apple, Inc.",
         hostname="iPad", nickname="Kitchen iPad", presence=0.6),
    dict(mac="34:3e:a4:b8:06:5f", ip="192.168.4.44", vendor="Ring LLC",
         nickname="Front doorbell"),
    dict(mac="90:48:6c:2d:9a:e4", ip="192.168.4.45", vendor="Ring LLC",
         presence=0.95),
    dict(mac="5c:49:7d:a3:61:bc", ip="192.168.4.52", vendor="Samsung Electronics Co.,Ltd",
         hostname="Galaxy-S23", presence=0.55),
    dict(mac="a4:cf:12:05:e7:3b", ip="192.168.5.17", vendor="Espressif Inc.",
         hostname="ESP-05E73B"),
    # Randomized private MACs (phones/laptops), which come and go
    dict(mac="da:4f:91:0c:7e:22", ip="192.168.4.67", presence=0.6),
    dict(mac="6a:13:c8:b2:05:9f", ip="192.168.4.88", presence=0.5, window=(3, 1)),
    dict(mac="f6:2e:7d:41:a8:93", ip="192.168.5.103", presence=0.7, window=(1, 0)),
]

# Not in the history, so the first simulated scan has something new to report
NEWCOMERS = [
    dict(mac="ec:fa:bc:91:3d:46", ip="192.168.1.62", vendor="Espressif Inc."),
    dict(mac="8c:8d:28:6b:f0:17", ip="192.168.4.97", vendor="Intel Corporate",
         hostname="LAPTOP-GUEST"),
]


def is_demo(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (MARKER_TABLE,)
    ).fetchone() is not None


def _subnet_of(ip):
    return next(s for s in SUBNETS if ipaddress.ip_address(ip) in ipaddress.ip_network(s))


def _classified(spec):
    """A device dict shaped like a classified scan result."""
    mac, ip = spec["mac"], spec["ip"]
    vendor, hostname = spec.get("vendor"), spec.get("hostname")
    random_mac = is_random_mac(mac)
    if mac == THIS_COMPUTER:
        device_type = "This computer"
    elif ip == gateway_ip(_subnet_of(ip)):
        device_type = "Router / gateway"
    else:
        device_type = guess_type(vendor, hostname, random_mac)
    return {"mac": mac, "ip": ip, "vendor": vendor, "hostname": hostname,
            "is_random_mac": random_mac, "device_type": device_type}


def _check_replaceable(path):
    """Refuse to replace anything but an earlier demo database."""
    if os.path.basename(path).lower() == "assets.db":
        raise SystemExit("Refusing to write demo data to assets.db. Pick another name, e.g. demo.db.")
    if not os.path.exists(path):
        return
    try:
        conn = sqlite3.connect(path)
        try:
            ok = is_demo(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        ok = False
    if not ok:
        raise SystemExit(f"{path} exists and isn't a demo database, so it was left alone.")
    os.remove(path)


def build(path=DEFAULT_PATH):
    """Create a fresh demo database. Returns (devices, scans)."""
    _check_replaceable(path)
    rng = random.Random(SEED)
    ts = lambda t: t.isoformat(timespec="seconds")

    conn = db.connect(path)
    conn.execute(f"CREATE TABLE {MARKER_TABLE} (created_at TEXT NOT NULL)")
    conn.execute(f"INSERT INTO {MARKER_TABLE} VALUES (?)", (db.now(),))

    devices = []
    for spec in DEVICES:
        dev = _classified(spec)
        cur = conn.execute(
            """INSERT INTO devices (mac, vendor, device_type, hostname, is_random_mac,
                                    nickname, trusted, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, '', '')""",
            (dev["mac"], dev["vendor"], dev["device_type"], dev["hostname"],
             int(dev["is_random_mac"]), spec.get("nickname"), int(bool(spec.get("nickname")))),
        )
        devices.append((cur.lastrowid, spec, _subnet_of(spec["ip"])))

    # Scan both subnets every 30 minutes, the last round a few minutes ago
    end = datetime.now().replace(microsecond=0) - timedelta(minutes=4)
    t = end - timedelta(days=DAYS)
    scans = 0
    while t <= end:
        for i, subnet in enumerate(SUBNETS):
            started = t + timedelta(seconds=7 * i)
            seen = []
            for device_id, spec, dev_subnet in devices:
                first, last = spec.get("window", (DAYS, 0))
                if (dev_subnet == subnet
                        and end - timedelta(days=first) <= t <= end - timedelta(days=last)
                        and rng.random() < spec.get("presence", 1.0)):
                    seen.append((device_id, spec["ip"]))
            cur = conn.execute(
                "INSERT INTO scans (started_at, finished_at, subnet, devices_found) VALUES (?, ?, ?, ?)",
                (ts(started), ts(started + timedelta(seconds=rng.randint(3, 6))), subnet, len(seen)),
            )
            conn.executemany(
                "INSERT INTO sightings (device_id, scan_id, ip, seen_at) VALUES (?, ?, ?, ?)",
                [(device_id, cur.lastrowid, ip, ts(started + timedelta(seconds=2)))
                 for device_id, ip in seen],
            )
            scans += 1
        t += SCAN_EVERY

    conn.execute(
        """UPDATE devices SET
               first_seen = (SELECT MIN(seen_at) FROM sightings WHERE device_id = devices.id),
               last_seen  = (SELECT MAX(seen_at) FROM sightings WHERE device_id = devices.id)"""
    )
    conn.commit()
    conn.close()
    return len(devices), scans


def fake_scan(conn, subnet):
    """Stand-in for an ARP sweep: whoever answered the last scan, plus any newcomers not yet seen."""
    rows = conn.execute(
        """SELECT d.mac, s.ip, d.vendor, d.hostname, d.is_random_mac, d.device_type
           FROM sightings s JOIN devices d ON d.id = s.device_id
           WHERE s.scan_id = (SELECT MAX(id) FROM scans
                              WHERE subnet = ? AND finished_at IS NOT NULL)""",
        (subnet,),
    ).fetchall()
    results = [dict(r) for r in rows]
    known = {r["mac"] for r in conn.execute("SELECT mac FROM devices")}
    results += [_classified(spec) for spec in NEWCOMERS
                if spec["mac"] not in known and _subnet_of(spec["ip"]) == subnet]
    return results


def alert_devices(conn, limit=3):
    """The most recently arrived untrusted devices, shaped like a scan's new-device list."""
    rows = [r for r in db.all_devices(conn) if not r["trusted"]]
    rows.sort(key=lambda r: r["first_seen"], reverse=True)
    return [{"mac": r["mac"], "ip": r["last_ip"], "vendor": r["vendor"],
             "device_type": r["device_type"]} for r in rows[:limit]]
