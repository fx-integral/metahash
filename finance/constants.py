"""
finance/constants.py
--------------------
Shared configuration knobs used by the treasury finance tooling.
Keeping them in a dedicated module avoids circular imports and makes
unit testing easier because values can be patched in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from metahash.config import (
    METAHASH_SUBNET_ID,
    AUCTION_BUDGET_ALPHA,
    PLANCK,
)

# --- block cadence ----------------------------------------------------------------
SECONDS_PER_BLOCK: Final[int] = 12
BLOCKS_PER_DAY: Final[int] = 24 * 60 * 60 // SECONDS_PER_BLOCK  # 7200

# --- treasury policy ---------------------------------------------------------------
# Alpha received from miners is illiquid for two months.
LOCK_PERIOD_DAYS: Final[int] = 60
LOCK_PERIOD_BLOCKS: Final[int] = LOCK_PERIOD_DAYS * BLOCKS_PER_DAY

# Default history window used when backfilling transactions/commitments.
DEFAULT_HISTORY_DAYS: Final[int] = 120

# SN73 auction parameters.
BASE_SUBNET_ID: Final[int] = METAHASH_SUBNET_ID
BASE_AUCTION_EMISSION_ALPHA: Final[float] = AUCTION_BUDGET_ALPHA

# Numeric helpers -------------------------------------------------------------------

@dataclass(frozen=True)
class UnitScales:
    """Tiny helper for planck/alpha conversions."""

    planck: int = PLANCK

    @property
    def alpha_from_rao(self) -> float:
        return 1.0 / self.planck


UNITS: Final[UnitScales] = UnitScales()

