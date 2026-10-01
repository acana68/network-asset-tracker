"""Online/offline status.

A device is offline if it hasn't been seen in the N hours before the most
recent reliable scan of its subnet. Measuring from the last scan instead of
the clock means a PC that was asleep for a night doesn't make everything look
offline, and an unreliable scan (see db.scan_is_reliable) never marks anything
offline, because its subnet keeps being measured from the last good scan.

device_statuses() only reads, and copes with databases that haven't been
upgraded yet, so the read-only dashboard can use it too.
"""

from datetime import datetime, timedelta

from . import db

DEFAULT_HOURS = 2
ONLINE, OFFLINE = "online", "offline"


def offline_hours(cfg, override=None):
    hours = override if override is not None else cfg.get("offline_after_hours", DEFAULT_HOURS)
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise SystemExit(f"offline_after_hours must be a number of hours, not {hours!r}")
    if hours <= 0:
        raise SystemExit("offline_after_hours must be more than 0")
    return hours


def device_statuses(conn, hours):
    """{device_id: 'online' | 'offline' | None}. None = its subnet has no reliable scan."""
    if "reliable" in db.columns(conn, "scans"):
        latest = dict(conn.execute(
            """SELECT subnet, MAX(started_at) FROM scans
               WHERE finished_at IS NOT NULL AND COALESCE(reliable, 1) = 1 GROUP BY subnet"""
        ).fetchall())
    else:
        # Not upgraded yet (and maybe opened read-only): judge the scans in memory
        scans = conn.execute(
            "SELECT id, subnet, started_at, devices_found FROM scans "
            "WHERE finished_at IS NOT NULL ORDER BY id"
        ).fetchall()
        flags, latest = db.judge_scans(scans), {}
        for scan_id, subnet, started_at, _ in scans:
            if flags[scan_id]:
                latest[subnet] = max(latest.get(subnet, started_at), started_at)
    rows = conn.execute(
        """SELECT d.id, d.last_seen,
                  (SELECT sc.subnet FROM sightings s JOIN scans sc ON sc.id = s.scan_id
                   WHERE s.device_id = d.id ORDER BY s.id DESC LIMIT 1) AS subnet
           FROM devices d"""
    ).fetchall()
    out = {}
    for device_id, last_seen, subnet in rows:
        ref = latest.get(subnet)
        if ref is None or not last_seen:
            out[device_id] = None
            continue
        cutoff = datetime.fromisoformat(ref) - timedelta(hours=hours)
        out[device_id] = OFFLINE if datetime.fromisoformat(last_seen) < cutoff else ONLINE
    return out


def refresh(conn, hours):
    """Store every device's current status, except changes to watched devices.

    Those are returned instead (as dicts) so the caller can alert first and
    then save them with save(). A device's first status is stored silently.
    """
    current = device_statuses(conn, hours)
    held, ts = [], datetime.now().isoformat(timespec="seconds")
    for r in conn.execute(
        "SELECT id, mac, nickname, vendor, hostname, watched, status, last_seen FROM devices"
    ).fetchall():
        new = current.get(r["id"])
        if new is None or new == r["status"]:
            continue
        if r["watched"] and r["status"] is not None:
            held.append(_change(conn, r, new))
        else:
            conn.execute("UPDATE devices SET status = ?, status_changed_at = ? WHERE id = ?",
                         (new, ts, r["id"]))
    conn.commit()
    return held


def save(conn, changes):
    ts = datetime.now().isoformat(timespec="seconds")
    for c in changes:
        conn.execute("UPDATE devices SET status = ?, status_changed_at = ? WHERE id = ?",
                     (c["status"], ts, c["id"]))
    conn.commit()


def _change(conn, row, new):
    sightings = conn.execute(
        "SELECT ip, seen_at FROM sightings WHERE device_id = ? ORDER BY id DESC LIMIT 2",
        (row["id"],),
    ).fetchall()
    change = {"id": row["id"], "mac": row["mac"], "status": new,
              "name": row["nickname"] or row["vendor"] or row["hostname"] or row["mac"],
              "ip": sightings[0]["ip"] if sightings else None,
              "last_seen": row["last_seen"], "gap": None}
    if new == ONLINE and len(sightings) == 2:
        # How long it was gone: from the sighting before this one to this one
        change["gap"] = (datetime.fromisoformat(sightings[0]["seen_at"])
                         - datetime.fromisoformat(sightings[1]["seen_at"]))
    return change
