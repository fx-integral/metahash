# neurons/flags.py
from __future__ import annotations

import os
from metahash.config import TESTING


def _env_flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


# DRY‑RUN: suppress on-chain set_weights (default: True in TESTING)
DRYRUN_WEIGHTS: bool = _env_flag("METAHASH_DRYRUN_WEIGHTS", default=bool(TESTING))
# Allow fallback inline commit when IPFS fails
ALLOW_INLINE_FALLBACK: bool = _env_flag("METAHASH_ALLOW_INLINE_FALLBACK", default=True)
# Strict axon filtering (recommended)
STRICT_AXON_FILTER: bool = _env_flag("METAHASH_STRICT_AXON_FILTER", default=True)
# On-chain raw bytes ceiling for inline commitment blob (CID-only fits easily).
RAW_BYTES_CEILING: int = int(os.getenv("METAHASH_COMMIT_MAX_BYTES", "120"))

__all__ = [
    "DRYRUN_WEIGHTS",
    "ALLOW_INLINE_FALLBACK",
    "STRICT_AXON_FILTER",
    "RAW_BYTES_CEILING",
]
