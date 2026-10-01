"""SQLite storage for devices, scans, and sightings.

Three tables:
  devices   - one row per unique MAC address (the "asset")
  scans     - one row per scan run
  sightings - one row per device seen in a scan (gives you history over time)
"""

import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mac           TEXT NOT NULL UNIQUE,
    vendor        TEXT,
    device_type   TEXT,
    hostname      TEXT,
    is_random_mac INTEGER NOT NULL DEFAULT 0,
    nickname      TEXT,
    trusted       INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    subnet        TEXT NOT NULL,
    devices_found INTEGER
);

CREATE TABLE IF NOT EXISTS sightings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    scan_id   INTEGER NOT NULL REFERENCES scans(id),
    ip        TEXT NOT NULL,
    seen_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sightings_device ON sightings(device_id);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect(path="assets.db"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def start_scan(conn, subnet):
    cur = conn.execute(
        "INSERT INTO scans (started_at, subnet) VALUES (?, ?)", (now(), subnet)
    )
    conn.commit()
    return cur.lastrowid


def finish_scan(conn, scan_id, devices_found):
    conn.execute(
        "UPDATE scans SET finished_at = ?, devices_found = ? WHERE id = ?",
        (now(), devices_found, scan_id),
    )
    conn.commit()


def record_device(conn, scan_id, dev):
    """Insert or update a device and log the sighting. Returns True if the device is new."""
    ts = now()
    row = conn.execute("SELECT id FROM devices WHERE mac = ?", (dev["mac"],)).fetchone()

    if row is None:
        cur = conn.execute(
            """INSERT INTO devices
               (mac, vendor, device_type, hostname, is_random_mac, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                dev["mac"],
                dev.get("vendor"),
                dev.get("device_type"),
                dev.get("hostname"),
                int(dev.get("is_random_mac", False)),
                ts,
                ts,
            ),
        )
        device_id = cur.lastrowid
        is_new = True
    else:
        device_id = row["id"]
        # Keep the old hostname/vendor if this scan couldn't resolve one.
        # device_type is always refreshed so improved rules apply to known devices.
        conn.execute(
            """UPDATE devices SET last_seen = ?,
                   hostname = COALESCE(?, hostname),
                   vendor = COALESCE(?, vendor),
                   device_type = ?
               WHERE id = ?""",
            (ts, dev.get("hostname"), dev.get("vendor"), dev.get("device_type"), device_id),
        )
        is_new = False

    conn.execute(
        "INSERT INTO sightings (device_id, scan_id, ip, seen_at) VALUES (?, ?, ?, ?)",
        (device_id, scan_id, dev["ip"], ts),
    )
    conn.commit()
    return is_new


def set_trusted(conn, mac, nickname=None):
    cur = conn.execute(
        "UPDATE devices SET trusted = 1, nickname = COALESCE(?, nickname) WHERE mac = ?",
        (nickname, mac.lower()),
    )
    conn.commit()
    return cur.rowcount > 0


def all_devices(conn):
    return conn.execute(
        """SELECT d.*, (SELECT ip FROM sightings s WHERE s.device_id = d.id
                         ORDER BY s.id DESC LIMIT 1) AS last_ip
           FROM devices d ORDER BY d.trusted, d.last_seen DESC"""
    ).fetchall()
