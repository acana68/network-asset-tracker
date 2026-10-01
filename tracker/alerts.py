"""Discord alerts for new devices.

The webhook URL is read from the DISCORD_WEBHOOK_URL environment variable so
it never ends up in the code or the repo. Anyone with the URL can post to
your channel, so treat it like a password.

Uses only the standard library (urllib), so no extra dependencies.
"""

import json
import os
import urllib.error
import urllib.request

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
