"""
metahash/config.py — global constants  (patched 2025‑07‑09)
"""

from __future__ import annotations
import os
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()

# ───────────────────────────  Network  ────────────────────────────── #
DEFAULT_BITTENSOR_NETWORK: str = os.getenv("BITTENSOR_NETWORK", "finney")
TREASURY_COLDKEY: str = "5GW6xj5wUpLBz7jCNp38FzkdS6DfeFdUTuvUjcn6uKH5krsn"
STARTING_AUCTIONS_BLOCK = int(os.getenv("STARTING_ACTIONS_BLOCK","5931900"))
AUCTION_DELAY_BLOCKS: int = 50
FORBIDDEN_ALPHA_SUBNETS: list[int] = [73]
FORCE_BURN_WEIGHTS = False
DEFAULT_BITTENSOR_NETWORK: str = os.getenv("BITTENSOR_NETWORK","finney")
PLANCK = 10**9
TESTING = False

# ───────────────────────────  Auction  ─────────────────────────────── #
AUCTION_BUDGET_ALPHA: float = 148.0                  # α sold each epoch
SOFT_QUOTA_LAMBDA: float = 100.0                  # λ penalty per α of stretch
MAX_BIDS_PER_MINER: int = 10                     # anti‑spam
PAYMENT_WINDOW_BLOCKS: int = 30                     # ~3 min on Finney
JAIL_BLOCKS: int = 4_800                  # ≈ 24 h
S_MIN_ALPHA: float = 5.0                    # stake needed to bid

# Strategy file (static – restart validator to reload)
STRATEGY_PATH: str = "weights_bps.yml"

# Per‑validator treasury addresses (hotkey → coldkey)
VALIDATOR_TREASURIES: dict[str, str] = {
    # "hotkey_ss58"          : "coldkey_ss58"
    # ----------------------------------------
    "5Ha…Foo": "5Dh…Bar",
    # add yours here
}


# ───────────────────────  Oracle / Scanner  ───────────────────────── #
MAX_CHUNK: int = 512           # event‑scan chunk size
FINALITY_LAG: int = 1           # skip last N blocks for finality
