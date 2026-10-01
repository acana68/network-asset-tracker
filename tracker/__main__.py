"""Command-line entry point.

Usage:
    python -m tracker scan                # scan your network, flag new devices
    python -m tracker scan --subnet 192.168.1.0/24 192.168.4.0/22
    python -m tracker list                # every device ever seen, online/offline
    python -m tracker trust <mac> --name "Work laptop"
    python -m tracker watch <mac>         # alert when it goes offline / comes back
    python -m tracker unwatch <mac>
    python -m tracker list --offline-after 4     # offline = not seen in 4 hours
    python -m tracker report              # summary stats
    python -m tracker update-vendors      # refresh the manufacturer database
    python -m tracker test-alert          # send a test Discord message
    python -m tracker --log logs/scan.log scan   # also append output to a log file
    python -m tracker demo                # fake data in demo.db, for screenshots
"""

import argparse
import os
import sys
from pathlib import Path

from . import alerts, config, db, demo, status
from .classify import classify

MAX_LOG_BYTES = 1_000_000  # rotate to scan.log.1 past ~1 MB


class _Tee:
    """Write to the console and a log file at the same time."""

    def __init__(self, *streams):
        # pythonw (used by the scheduled task) has no console, so stdout is None
        self.streams = [s for s in streams if s is not None]

    def write(self, text):
        for s in self.streams:
            s.write(text)

    def flush(self):
        for s in self.streams:
            s.flush()

    def isatty(self):
        return False


def start_log(path, command):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
        path.replace(path.with_name(path.name + ".1"))
    log = open(path, "a", encoding="utf-8", buffering=1)  # line-buffered
    log.write(f"\n===== {db.now()}  {command} =====\n")
    sys.stdout = _Tee(sys.stdout, log)
    sys.stderr = _Tee(sys.stderr, log)  # so crashes end up in the log too


def cmd_scan(args):
    from .scanner import arp_scan, gateway_ip, guess_subnet, lookup_hostname, own_macs

    conn = db.connect(args.db)
    cfg = config.load(args.config)
    hours = status.offline_hours(cfg, args.offline_after)

    if demo.is_demo(conn):
        # Never mix real devices into a demo database: replay fake ones instead
        subnets = demo.SUBNETS

        def discover(subnet):
            return demo.fake_scan(conn, subnet)
    else:
        subnets = args.subnet or cfg.get("subnets") or [guess_subnet()]
        timeout = args.timeout if args.timeout is not None else cfg.get("timeout", 2)
        my_macs = own_macs()

        def discover(subnet):
            gateway = gateway_ip(subnet)
            results = arp_scan(subnet, timeout=timeout)
            for dev in results:
                dev["hostname"] = None if args.no_hostnames else lookup_hostname(dev["ip"])
                dev.update(classify(dev["mac"], dev["hostname"],
                                    is_gateway=dev["ip"] == gateway,
                                    is_self=dev["mac"] in my_macs))
            return results

    run_scans(conn, subnets, discover, hours)


def run_scans(conn, subnets, discover, hours):
    """Scan each subnet with discover(subnet), update statuses, and send alerts."""
    new_devices, unreliable = [], []
    for subnet in subnets:
        print(f"Scanning {subnet} ...")

        scan_id = db.start_scan(conn, subnet)
        results = discover(subnet)

        for dev in results:
            if db.record_device(conn, scan_id, dev):
                new_devices.append(dev)

        started = conn.execute("SELECT started_at FROM scans WHERE id = ?", (scan_id,)).fetchone()[0]
        reliable, typical = db.scan_is_reliable(conn, subnet, scan_id, started, len(results))
        db.finish_scan(conn, scan_id, len(results), reliable)
        print(f"Found {len(results)} devices on {subnet}.")
        if not reliable:
            print(f"WARNING: {subnet} usually has ~{round(typical)} devices, so this scan looks "
                  "unreliable. No device on this subnet will be marked offline.", file=sys.stderr)
            if db.quality_alert_due(conn, subnet):
                unreliable.append((scan_id, subnet, len(results), typical))

    if new_devices:
        print(f"\n[!] {len(new_devices)} NEW device(s):")
        for d in new_devices:
            print(f"  {d['mac']}  {d['ip']:<15}  {d['vendor'] or '-':<25}  {d['device_type']}")
        print("\nIf you recognize one, mark it trusted:  python -m tracker trust <mac> --name \"...\"")
    else:
        print("No new devices.")

    # Watched devices that went offline or came back since the last scan
    changes = status.refresh(conn, hours)
    if changes:
        print("\n[!] Watched device status changed:")
        for c in changes:
            print("  " + alerts.status_line(c))

    # New devices are always untrusted, since trust is set by hand afterwards
    try:
        if alerts.send_new_devices(new_devices):
            print("Sent Discord alert.")
    except alerts.AlertError as e:
        print(f"Discord alert failed: {e}", file=sys.stderr)

    # One alert per unreliable stretch; retried next scan if it couldn't be sent
    for scan_id, subnet, found, typical in unreliable:
        try:
            if alerts.send_unreliable_scan(subnet, found, typical):
                db.mark_quality_alert(conn, scan_id)
                print(f"Sent Discord alert about the scan of {subnet}.")
        except alerts.AlertError as e:
            print(f"Discord alert failed: {e}", file=sys.stderr)

    # Statuses are saved only once the alert went out (or there's no webhook to send
    # it to), so a failed alert is retried on the next scan instead of being lost
    if changes:
        try:
            if alerts.send_status_changes(changes):
                print("Sent Discord alert for watched devices.")
            status.save(conn, changes)
        except alerts.AlertError as e:
            print(f"Discord alert failed: {e}. It will be retried on the next scan.", file=sys.stderr)


def cmd_test_alert(args):
    if not alerts.webhook_url():
        print(f"{alerts.ENV_VAR} is not set, so there's nowhere to send alerts. See the README.")
        sys.exit(1)
    # Against a demo database, send a real-looking new-device alert listing its fake devices
    devices = []
    if os.path.exists(args.db):
        conn = db.connect(args.db)
        if demo.is_demo(conn):
            devices = demo.alert_devices(conn)
    if devices:
        messages = alerts.format_devices(devices)
        offline = demo.status_alert_sample(conn)
        if offline:
            messages += alerts.format_status_changes(offline)
    else:
        messages = ["Test alert from the network asset tracker. Alerts are working."]
    try:
        for message in messages:
            alerts.post(message)
    except alerts.AlertError as e:
        print(f"Test alert failed: {e}")
        sys.exit(1)
    if devices:
        print(f"Sent a sample new-device alert with {len(devices)} demo device(s). Check your Discord channel.")
    else:
        print("Test alert sent. Check your Discord channel.")


def cmd_demo(args):
    devices, scans = demo.build(args.out)
    print(f"Created {args.out}: {devices} fake devices, {scans} scans over the last {demo.DAYS} days.")
    print(f"Use it with --db, e.g.  python -m tracker --db {args.out} list")


def cmd_list(args):
    conn = db.connect(args.db)
    rows = db.all_devices(conn)
    if not rows:
        print("No devices yet. Run a scan first.")
        return
    hours = status.offline_hours(config.load(args.config), args.offline_after)
    statuses = status.device_statuses(conn, hours)
    print(f"{'MAC':<18} {'LAST IP':<15} {'NAME / VENDOR':<28} {'TYPE':<34} "
          f"{'TRUSTED':<8} {'WATCHED':<8} {'STATUS':<8} LAST SEEN")
    for r in rows:
        label = r["nickname"] or r["vendor"] or r["hostname"] or "-"
        trusted = "yes" if r["trusted"] else "NO"
        watched = "*" if r["watched"] else ""
        state = {status.ONLINE: "online", status.OFFLINE: "OFFLINE"}.get(statuses.get(r["id"]), "-")
        print(f"{r['mac']:<18} {r['last_ip'] or '-':<15} {label[:27]:<28} "
              f"{(r['device_type'] or '-')[:33]:<34} {trusted:<8} {watched:<8} {state:<8} "
              f"{r['last_seen']}")
    print(f"\nOFFLINE = not seen in the {hours:g} hours before its subnet's latest scan.  "
          f"* = watched")


def _device_name(conn, mac):
    row = conn.execute("SELECT nickname, vendor, hostname FROM devices WHERE mac = ?",
                       (mac.lower(),)).fetchone()
    return row and (row["nickname"] or row["vendor"] or row["hostname"] or mac)


def cmd_watch(args):
    conn = db.connect(args.db)
    name = _device_name(conn, args.mac)
    if name is None:
        print(f"No device with MAC {args.mac} found.")
        sys.exit(1)
    # Store its current status before watching, so an existing condition doesn't alert
    hours = status.offline_hours(config.load(args.config), args.offline_after)
    status.refresh(conn, hours)
    db.set_watched(conn, args.mac, True)
    device_id = conn.execute("SELECT id FROM devices WHERE mac = ?", (args.mac.lower(),)).fetchone()[0]
    now = status.device_statuses(conn, hours).get(device_id)
    print(f"Watching {name} ({args.mac.lower()}): currently {now or 'unknown'}.")
    print("You'll get a Discord alert when it goes offline or comes back online.")
    if not alerts.webhook_url():
        print(f"Note: {alerts.ENV_VAR} isn't set, so no alerts will actually be sent.")


def cmd_unwatch(args):
    conn = db.connect(args.db)
    name = _device_name(conn, args.mac)
    if name is None:
        print(f"No device with MAC {args.mac} found.")
        sys.exit(1)
    db.set_watched(conn, args.mac, False)
    print(f"Stopped watching {name} ({args.mac.lower()}).")


def cmd_trust(args):
    conn = db.connect(args.db)
    if db.set_trusted(conn, args.mac, args.name):
        print(f"Marked {args.mac} as trusted.")
    else:
        print(f"No device with MAC {args.mac} found.")
        sys.exit(1)


def cmd_report(args):
    conn = db.connect(args.db)
    total = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    untrusted = conn.execute("SELECT COUNT(*) FROM devices WHERE trusted = 0").fetchone()[0]
    random_macs = conn.execute("SELECT COUNT(*) FROM devices WHERE is_random_mac = 1").fetchone()[0]
    scans = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]

    print(f"Scans run:          {scans}")
    print(f"Devices tracked:    {total}")
    print(f"Untrusted devices:  {untrusted}")
    print(f"Private/random MAC: {random_macs}")

    print("\nDevices by type:")
    for row in conn.execute(
        "SELECT device_type, COUNT(*) AS n FROM devices GROUP BY device_type ORDER BY n DESC"
    ):
        print(f"  {row['device_type']:<34} {row['n']}")

    print("\nMost frequently seen:")
    for row in conn.execute(
        """SELECT COALESCE(d.nickname, d.vendor, d.mac) AS label, COUNT(s.id) AS seen
           FROM devices d JOIN sightings s ON s.device_id = d.id
           GROUP BY d.id ORDER BY seen DESC LIMIT 5"""
    ):
        print(f"  {row['label'][:34]:<34} {row['seen']} scans")


def cmd_update_vendors(args):
    from mac_vendor_lookup import MacLookup
    print("Downloading latest manufacturer list ...")
    MacLookup().update_vendors()
    print("Done.")


def add_offline_after(parser):
    parser.add_argument("--offline-after", type=float, metavar="HOURS",
                        help="Offline = not seen in this many hours before the subnet's "
                             f"latest scan (default: from config.json, else {status.DEFAULT_HOURS})")


def main():
    parser = argparse.ArgumentParser(prog="tracker", description="Network asset tracker")
    parser.add_argument("--db", default="assets.db", help="SQLite database file")
    parser.add_argument("--config", help="Settings file (default: config.json, if it exists)")
    parser.add_argument("--log", help="Also append output to this file, e.g. logs/scan.log")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="Scan the network")
    p.add_argument("--subnet", nargs="+",
                   help="One or more, e.g. 192.168.1.0/24 192.168.4.0/22 "
                        "(default: from config.json, else auto-detected)")
    p.add_argument("--timeout", type=int,
                   help="Seconds to wait for replies (default: from config.json, else 2)")
    p.add_argument("--no-hostnames", action="store_true", help="Skip reverse DNS (faster)")
    add_offline_after(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("list", help="List all known devices")
    add_offline_after(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("watch", help="Alert when this device goes offline or comes back")
    p.add_argument("mac")
    add_offline_after(p)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("unwatch", help="Stop alerting about this device")
    p.add_argument("mac")
    p.set_defaults(func=cmd_unwatch)

    p = sub.add_parser("trust", help="Mark a device as known")
    p.add_argument("mac")
    p.add_argument("--name", help="A nickname, e.g. \"Living room TV\"")
    p.set_defaults(func=cmd_trust)

    sub.add_parser("report", help="Summary stats").set_defaults(func=cmd_report)
    sub.add_parser("update-vendors", help="Refresh vendor database").set_defaults(func=cmd_update_vendors)
    sub.add_parser("test-alert", help="Send a test Discord message "
                   "(a sample new-device alert if --db is a demo database)").set_defaults(func=cmd_test_alert)

    p = sub.add_parser("demo", help="Build a database of fake devices for screenshots")
    p.add_argument("--out", default=demo.DEFAULT_PATH,
                   help=f"File to create (default: {demo.DEFAULT_PATH}). Never assets.db.")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args()
    if args.log:
        start_log(args.log, args.command)
    args.func(args)


if __name__ == "__main__":
    main()
