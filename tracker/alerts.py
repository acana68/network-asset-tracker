"""Discord alerts: new devices, watched devices going offline or coming back,
and scans that look unreliable.

The webhook URL is read from the DISCORD_WEBHOOK_URL environment variable so
it never ends up in the code or the repo. Anyone with the URL can post to
your channel, so treat it like a password.

Uses only the standard library (urllib), so no extra dependencies.
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime

ENV_VAR = "DISCORD_WEBHOOK_URL"
MAX_LEN = 2000  # Discord's limit per message


class AlertError(Exception):
    pass


def webhook_url():
    return os.environ.get(ENV_VAR, "").strip() or None


def post(content, url=None):
    """Send one message. Raises AlertError if Discord rejects it."""
    url = url or webhook_url()
    body = json.dumps({
        "content": content,
        "allowed_mentions": {"parse": []},  # never ping @everyone etc.
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        # Discord blocks urllib's default User-Agent
        "User-Agent": "network-asset-tracker/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except urllib.error.HTTPError as e:
        raise AlertError(f"Discord returned HTTP {e.code} {e.reason}") from None
    except (urllib.error.URLError, OSError) as e:
        raise AlertError(f"could not reach Discord: {getattr(e, 'reason', e)}") from None


def format_devices(devices):
    """Build one or more messages listing the devices, each under Discord's length limit."""
    header = f"**[!] {len(devices)} new device(s) on the network**"
    lines = [f"{d['mac']}  {d['ip']:<15}  {(d.get('vendor') or '-')[:25]:<25}  {d.get('device_type') or '-'}"
             for d in devices]
    return _chunked(header, lines)


def _duration(delta):
    minutes = int(delta.total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    return f"{days}d {hours}h" if days else f"{hours}h {minutes:02d}m"


def status_line(c):
    """One line for a watched device that went offline or came back (see status.refresh)."""
    head = f"{'OFFLINE' if c['status'] == 'offline' else 'ONLINE':<8} {c['name'][:24]:<24}  {c['mac']}  {c['ip'] or '-':<15}"
    if c["status"] == "offline":
        return f"{head}  last seen {datetime.fromisoformat(c['last_seen']):%b %d %H:%M}"
    return head + (f"  back after {_duration(c['gap'])}" if c.get("gap") else "  back online")


def format_status_changes(changes):
    gone = sum(c["status"] == "offline" for c in changes)
    parts = []
    if gone:
        parts.append(f"{gone} went offline")
    if len(changes) - gone:
        parts.append(f"{len(changes) - gone} back online")
    header = f"**[!] Watched devices: {', '.join(parts)}**"
    return _chunked(header, [status_line(c) for c in changes])


def unreliable_scan_message(subnet, found, typical):
    devices = "device" if found == 1 else "devices"
    return (f"**[!] Scan of {subnet} found {found} {devices} (usually ~{round(typical)}).** "
            "Possible connectivity issue on the scanning machine. "
            "No devices on this subnet will be marked offline until scans look normal again.")


def _chunked(header, lines):
    """Header plus lines in code blocks, split to stay under Discord's length limit."""
    messages, chunk = [], []
    for line in lines:
        # +8 for the ``` fences and newlines, + header on the first message
        size = sum(len(l) + 1 for l in chunk) + len(line) + 8 + (len(header) + 1 if not messages else 0)
        if chunk and size > MAX_LEN:
            messages.append(chunk)
            chunk = []
        chunk.append(line)
    messages.append(chunk)

    out = []
    for i, chunk in enumerate(messages):
        text = "```\n" + "\n".join(chunk) + "\n```"
        out.append(f"{header}\n{text}" if i == 0 else text)
    return out


def send_new_devices(devices):
    """Alert about new devices. Returns False without doing anything if no webhook is set."""
    if not devices or not webhook_url():
        return False
    for message in format_devices(devices):
        post(message)
    return True


def send_status_changes(changes):
    """Alert about watched devices going offline or coming back. False if no webhook is set."""
    if not changes or not webhook_url():
        return False
    for message in format_status_changes(changes):
        post(message)
    return True


def send_unreliable_scan(subnet, found, typical):
    if not webhook_url():
        return False
    post(unreliable_scan_message(subnet, found, typical))
    return True
