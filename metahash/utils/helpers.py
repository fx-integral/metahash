# neurons/utils/helpers.py
from __future__ import annotations

import json
from ipaddress import ip_address, IPv4Address, IPv6Address
from typing import Optional, Any, Dict
from pathlib import Path
from collections import defaultdict
import yaml
import bittensor as bt


def load_weights(path: Path | str = "weights.yml") -> defaultdict[int, float]:
    """
    Load simple on/off (0/1) weights from a YAML file shaped like:

        0: 0
        1: 1
        2: 1
        ...
        73: 0
        136: 1  # Testnet
        348: 1  # Testnet

    - Keys: subnet IDs (ints)
    - Values: floats in [0, 1] (typically 0 or 1)

    Returns:
        defaultdict[int, float]: mapping with default 0 for missing SIDs.
    """
    p = Path(path)
    if not p.exists():
        bt.logging.error(f"strategy file not found at {p} – using all zeros (disabled).")
        return defaultdict(lambda: 0.0)

    try:
        raw = yaml.safe_load(p.read_text()) or {}
        if not isinstance(raw, dict):
            bt.logging.error(f"strategy load failed: YAML is not a mapping at {p} – using all zeros.")
            return defaultdict(lambda: 0.0)

        table: Dict[int, float] = {}
        for k, v in raw.items():
            try:
                sid = int(k)
                val = float(v)
                # Clamp defensively into [0,1] to avoid accidental >1 values.
                if val < 0.0:
                    val = 0.0
                elif val > 1.0:
                    val = 1.0
                table[sid] = val
            except Exception:
                # Skip malformed entries silently.
                continue

        bt.logging.info(f"strategy loaded from {p} • entries={len(table)} • domain=[0..1]")
        return defaultdict(lambda: 0.0, table)

    except Exception as e:
        bt.logging.error(f"strategy load failed from {p}: {e} – using all zeros.")
        return defaultdict(lambda: 0.0)


def safe_json_loads(val: Any):
    if isinstance(val, (bytes, bytearray)):
        try:
            val = val.decode("utf-8", errors="ignore")
        except Exception:
            return None
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    if isinstance(val, (dict, list)):
        return val
    return None


def safe_int(x) -> Optional[int]:
    try:
        if isinstance(x, int):
            return int(x)
        if isinstance(x, float):
            return int(x)
        if isinstance(x, str):
            return int(x.strip())
    except Exception:
        return None
    return None


def ip_is_public(host: str) -> bool:
    """Return True if host is a routable public IP (ipv4/ipv6). Hostnames return True."""
    if not host:
        return False
    try:
        ip = ip_address(host)
        if isinstance(ip, (IPv4Address, IPv6Address)):
            if ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
                return False
        return True
    except ValueError:
        # host is likely a DNS name → allow (cannot verify here)
        return True
