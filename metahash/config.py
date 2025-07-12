from __future__ import annotations
from decimal import Decimal
import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────── 1.  NETWORK‑LEVEL CONSTANTS  ──────────────────── #
TREASURY_COLDKEY: str = "5GW6xj5wUpLBz7jCNp38FzkdS6DfeFdUTuvUjcn6uKH5krsn"
STARTING_AUCTIONS_BLOCK = int(os.getenv("STARTING_ACTIONS_BLOCK","5931900"))
AUCTION_DELAY_BLOCKS: int = 50
FORBIDDEN_ALPHA_SUBNETS: list[int] = [73]
FORCE_BURN_WEIGHTS = False
DEFAULT_BITTENSOR_NETWORK: str = os.getenv("BITTENSOR_NETWORK","finney")
PLANCK = 10**9
TESTING = False

# ─────────────────── 2.  BOND‑CURVE DESIGN TARGETS  ────────────────── #
P_S_PAR: float = 1.0                       # spot‑parity (TAO ⇄ SN‑73)
D_START: float = 0.1                      # 10 % apex discount
D_TAIL_TARGET: float = 0.2               # 10 % tail discount just to start for simplicity
GAMMA_TARGET: float = 1.18                 # ≈ 15 % average discount
GAMMA_TOL: float = 0.02
BETA_NUDGE: float = 0.05
D_TAIL_TOL: float = 0.01
R_MIN_NUDGE: float = 0.10

# ─────────────── 3.  EPOCH EMISSION & AUCTION BUDGET  ──────────────── #
ALPHA_EMITTED_PER_EPOCH: int = int(round(360 * 0.41))   # 148 α
AUCTION_BUDGET_PCT: float = 1                           # 100% not burned on SN‑73
BAG_SN73: int = int(round(ALPHA_EMITTED_PER_EPOCH * AUCTION_BUDGET_PCT))

# ──────────────────── 4.  ECONOMIC SWITCHES & DELAYS ────────────────── #
ADJUST_BOND_CURVE: bool = False

# ──────────────────── 5.  PRICE / SLIPPAGE CONSTANTS ────────────────── #
DECIMALS: int = 10**9
K_SLIP: Decimal = Decimal("1.0")
CAP_SLIP: float = 1.0
SLIP_TOLERANCE: Decimal = Decimal("0.001")     
SAMPLE_POINTS: int = 8  # Sampling interval use for avg price and avg depth

# ─────────────────────── 6.  EVENT‑SCAN RPC TUNING ──────────────────── #
MAX_CHUNK: int = 512
FINALITY_LAG: int = 1
LOG_EVERY: int = 5_000

# ───────────────────────── 7.  ORACLE SAMPLING  ─────────────────────── #

DEFAULT_NETUID: int = 73
MAX_CONCURRENCY = 5  # Blocks requested in parallel

# ───────────────────────── 7.  Testing  ─────────────────────── #

TEST_TREASURY = "5DLULtxCS9vA3pZRgSTd9gkvGrUwgNje2TNQeLibo9wUWjSS"
