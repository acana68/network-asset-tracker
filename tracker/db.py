"""SQLite storage for devices, scans, and sightings.

Three tables:
  devices   - one row per unique MAC address (the "asset")
  scans     - one row per scan run
  sightings - one row per device seen in a scan (gives you history over time)

Databases from older versions are upgraded in place by connect().
"""

import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

# Data-quality guard: a scan finding less than half the usual number of devices
# is unreliable. "Usual" is the median of the subnet's reliable scans in the
# previous day; with fewer than MIN_HISTORY of them, or a usual count below
# MIN_TYPICAL, every scan counts as reliable.
HISTORY = timedelta(hours=24)
MIN_HISTORY = 3
MIN_TYPICAL = 4

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
    last_seen     TEXT NOT NULL,
    watched       INTEGER NOT NULL DEFAULT 0,  -- alert when it goes offline / comes back
    status        TEXT,                        -- last stored 'online' / 'offline'
    status_changed_at TEXT
);

CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    subnet        TEXT NOT NULL,
    devices_found INTEGER,
    reliable      INTEGER,                     -- 0 = found far fewer devices than usual
    quality_alert INTEGER NOT NULL DEFAULT 0   -- 1 = a Discord alert was sent about it
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
    _upgrade(conn)
    return conn


def columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _upgrade(conn):
    """Add columns that older databases don't have yet."""
    have = columns(conn, "devices")
    for col, ddl in [("watched", "INTEGER NOT NULL DEFAULT 0"),
                     ("status", "TEXT"), ("status_changed_at", "TEXT")]:
        if col not in have:
            conn.execute(f"ALTER TABLE devices ADD COLUMN {col} {ddl}")
    have = columns(conn, "scans")
    if "quality_alert" not in have:
        conn.execute("ALTER TABLE scans ADD COLUMN quality_alert INTEGER NOT NULL DEFAULT 0")
    if "reliable" not in have:
        conn.execute("ALTER TABLE scans ADD COLUMN reliable INTEGER")
        # Judge existing scans by the same rule, so an ongoing outage isn't
        # mistaken for the new normal
        flags = judge_scans(conn.execute(
            "SELECT id, subnet, started_at, devices_found FROM scans "
            "WHERE finished_at IS NOT NULL ORDER BY id"
        ).fetchall())
        conn.executemany("UPDATE scans SET reliable = ? WHERE id = ?",
                         [(int(ok), scan_id) for scan_id, ok in flags.items()])
    conn.commit()


def start_scan(conn, subnet):
    cur = conn.execute(
        "INSERT INTO scans (started_at, subnet) VALUES (?, ?)", (now(), subnet)
    )
    conn.commit()
    return cur.lastrowid


def finish_scan(conn, scan_id, devices_found, reliable=True):
    conn.execute(
        "UPDATE scans SET finished_at = ?, devices_found = ?, reliable = ? WHERE id = ?",
        (now(), devices_found, int(reliable), scan_id),
    )
    conn.commit()


def _judge(found, recent):
    """(reliable, typical) for a scan, given the counts of the subnet's recent reliable scans."""
    if len(recent) < MIN_HISTORY:
        return True, None
    typical = statistics.median(recent)
    if typical < MIN_TYPICAL:
        return True, typical
    return found >= typical / 2, typical


def scan_is_reliable(conn, subnet, scan_id, started_at, devices_found):
    """Returns (reliable, typical count or None) for a scan that just finished."""
    since = (datetime.fromisoformat(started_at) - HISTORY).isoformat(timespec="seconds")
    recent = [r[0] for r in conn.execute(
        """SELECT devices_found FROM scans
           WHERE subnet = ? AND id < ? AND started_at >= ? AND finished_at IS NOT NULL
                 AND COALESCE(reliable, 1) = 1""",
        (subnet, scan_id, since),
    )]
    return _judge(devices_found, recent)


def judge_scans(scans):
    """{scan id: reliable} for (id, subnet, started_at, devices_found) rows, oldest first.

    The same rule as scan_is_reliable, worked out in memory: used to upgrade old
    databases, and by the read-only dashboard before a database is upgraded.
    """
    history, flags = defaultdict(list), {}
    for scan_id, subnet, started_at, found in scans:
        started = datetime.fromisoformat(started_at)
        recent = [n for t, n in history[subnet] if t >= started - HISTORY]
        flags[scan_id] = _judge(found, recent)[0]
        if flags[scan_id]:
            history[subnet].append((started, found))
    return flags


def quality_alert_due(conn, subnet):
    """True unless an alert was already sent since the subnet's last reliable scan."""
    return conn.execute(
        """SELECT 1 FROM scans WHERE subnet = ? AND quality_alert = 1 AND id >
               COALESCE((SELECT MAX(id) FROM scans WHERE subnet = ? AND reliable = 1), 0)""",
        (subnet, subnet),
    ).fetchone() is None


def mark_quality_alert(conn, scan_id):
    conn.execute("UPDATE scans SET quality_alert = 1 WHERE id = ?", (scan_id,))
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


def set_watched(conn, mac, watched):
    cur = conn.execute("UPDATE devices SET watched = ? WHERE mac = ?", (int(watched), mac.lower()))
    conn.commit()
    return cur.rowcount > 0


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
