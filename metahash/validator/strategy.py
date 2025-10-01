# metahash/validator/strategy.py
from __future__ import annotations

from pathlib import Path
from collections import defaultdict
from typing import Dict, Optional, Tuple

import bittensor as bt

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None


def load_subnet_weights(path: str | Path = "weights.yml") -> Tuple[defaultdict[int, float], float]:
    """
    Load subnet weights from a YAML file with shape:

        default: 1.0
        36: 1.0
        62: 0.0
        73: 1.0

    Rules:
      - Keys are subnet IDs (ints).
      - Values are floats >= 0.0 (0.0 = off; can exceed 1.0 to upweight).
      - 'default' (optional): fallback for subnets not listed; defaults to 1.0.

    Returns:
      (weights mapping (defaultdict), default_value)
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
    Subnet weighting strategy that hot‑reloads YAML on change.

    Exposes:
      • weight_for(netuid) -> float
      • compute_weights_bps(...) -> Dict[int, int]  # subnet_id -> 0..10_000
      • default_value -> float
    """

    def __init__(self, path: str | Path = "weights.yml", algorithm_path: str | None = None):
        # algorithm_path is accepted for compatibility; not used here.
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
        """Return the current float weight for a given subnet id (>=0)."""
        self._reload_if_changed()
        try:
            nid = int(netuid)
        except Exception:
            nid = netuid
        return float(self._weights[nid])

    def compute_weights_bps(self, *, netuid: int | None = None, metagraph=None, active_uids=None) -> Dict[int, int]:
        """
        Compute subnet weights in basis points (0..10_000) from YAML.

        Notes:
          - We only emit explicit mappings found in YAML (plus any
            subnets that have a non-default explicitly listed).
          - Consumers may use `default_value` separately as a fallback.
        """
        self._reload_if_changed()
        out: Dict[int, int] = {}
        # Materialize all explicitly defined keys in the table (including those equal to default)
        # so operators can see them in diagnostics.
        for sid, fval in self._weights.items():
            try:
                bp = int(round(max(0.0, float(fval)) * 10_000.0))
            except Exception:
                bp = 0
            out[int(sid)] = max(0, min(10_000, bp))
        return out

    @property
    def default_value(self) -> float:
        """Return the current default value from YAML (or 1.0 if not set)."""
        self._reload_if_changed()
        return self._default_val
