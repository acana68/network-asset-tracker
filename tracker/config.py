"""Optional settings file, so scheduled runs don't need long command lines.

config.json (next to assets.db) looks like:

    {"subnets": ["192.168.1.0/24", "192.168.4.0/22"], "timeout": 2}

Every key is optional, and command-line arguments always win over the file.
"""

import json
import os

DEFAULT_PATH = "config.json"


def load(path=None):
    """Read the config. A missing default file is fine; a missing --config file is an error."""
    if path is None:
        path = DEFAULT_PATH
        if not os.path.exists(path):
            return {}
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"Config file not found: {path}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"Config file {path} isn't valid JSON: {e}")

    # Allow a single subnet as a plain string
    if isinstance(cfg.get("subnets"), str):
        cfg["subnets"] = [cfg["subnets"]]
    return cfg
