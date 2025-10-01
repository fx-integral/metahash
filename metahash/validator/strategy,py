# metahash/validator/strategy.py
from __future__ import annotations

from pathlib import Path
from collections import defaultdict
from typing import Dict, Optional

import bittensor as bt

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None


def load_subnet_weights(path: str | Path = "weights.yml") -> tuple[defaultdict[int, float], float]:
    """
    Load subnet weights from a YAML file with shape:

        default: 1.0
        36: 1.0
        62: 0.0
        73: 1.0

    Rules:
      - Keys are subnet IDs (ints).
      - Values are floats (0.0 = off, 1.0 = on, or any positive float).
      - 'default' (optional): sets the fallback value for subnets not listed.
      - If 'default' is not provided, fallback is 1.0.

    Returns:
      (weights mapping, default_value)
    """
    p = Path(path)
    if not p.exists():
        bt.logging.warning(f"[strategy] file not found at {p}; using all=1.0 default.")
        return defaultdict(lambda: 1.0), 1.0

    if yaml is None:
        bt.logging.error("[strategy] pyyaml not installed; using all=1.0 default.")
        return defaultdict(lambda: 1.0), 1.0

    try:
        raw = yaml.safe_load(p.read_text()) or {}
        if not isinstance(raw, dict):
            bt.logging.error(f"[strategy] {p} must be a mapping; using all=1.0.")
            return defaultdict(lambda: 1.0), 1.0

        # Extract default value
        default_val = 1.0
        if "default" in raw:
            try:
                default_val = max(0.0, float(raw.pop("default")))
            except Exception:
                default_val = 1.0

        table: Dict[int, float] = {}
        for k, v in raw.items():
            try:
                sid = int(k)
                val = float(v)
                if val < 0.0:
                    val = 0.0
                table[sid] = val
            except Exception:
                continue

        bt.logging.info(f"[strategy] loaded from {p} • entries={len(table)} • default={default_val}")
        return defaultdict(lambda: default_val, table), default_val

    except Exception as e:
        bt.logging.error(f"[strategy] load failed from {p}: {e} – using all=1.0.")
        return defaultdict(lambda: 1.0), 1.0


class Strategy:
    """
    Strategy wrapper that auto-reloads YAML on change.
    Allows a 'default' key in the YAML to override the fallback value.
    """

    def __init__(self, path: str | Path = "weights.yml"):
        self.path = Path(path)
        self._weights, self._default_val = load_subnet_weights(self.path)
        self._mtime: Optional[float] = self._get_mtime()

    def _get_mtime(self) -> Optional[float]:
        try:
            return self.path.stat().st_mtime
        except Exception:
            return None

    def _reload_if_changed(self) -> None:
        mtime = self._get_mtime()
        if mtime is not None and mtime != self._mtime:
            self._weights, self._default_val = load_subnet_weights(self.path)
            self._mtime = mtime
            bt.logging.info(f"[strategy] reloaded: {self.path} (mtime={mtime})")

    def weight_for(self, netuid: int) -> float:
        """
        Returns the weight (float) for a given subnet.
        Defaults to the 'default' in YAML, or 1.0 if none is provided.
        """
        self._reload_if_changed()
        try:
            nid = int(netuid)
        except Exception:
            nid = netuid
        return float(self._weights[nid])

    @property
    def default_value(self) -> float:
        """Return the current default value from YAML (or 1.0 if not set)."""
        self._reload_if_changed()
        return self._default_val
