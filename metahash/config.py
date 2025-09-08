"""
metahash/config.py — global constants
(v2.3: single-pass normalized-reputation allocator, early wins, pretty logs)
"""

from __future__ import annotations
import os
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()

# ╭─────────────────────────── ENVIRONMENT ────────────────────────────╮
METAHASH_SUBNET_ID = 73
DEFAULT_BITTENSOR_NETWORK: str = os.getenv("BITTENSOR_NETWORK", "finney")
TESTING: bool = os.getenv("TESTING", "false").lower() == "true"
START_V3_BLOCK: int = int(os.getenv("START_V3_BLOCK", "0"))

# Operational flags
FORCE_BURN_WEIGHTS: bool = os.getenv("FORCE_BURN_WEIGHTS", "false").lower() == "true"

# Monetary base unit (α planck)
PLANCK: int = 10**9
# ╰────────────────────────────────────────────────────────────────────╯

# ╭───────────────────────────── IPFS and Commitments ────────────────────────────────╮
ALLOW_INLINE_FALLBACK: bool = True
RAW_BYTES_CEILING = 120

# ╭───────────────────────────── EPOCH ────────────────────────────────╮
EPOCH_LENGTH_OVERRIDE: int = int(os.getenv("EPOCH_LENGTH_OVERRIDE", "20"))
# ╰────────────────────────────────────────────────────────────────────╯


# ╭───────────────────────────── AUCTION ──────────────────────────────╮
AUCTION_BUDGET_ALPHA: float = float(os.getenv("AUCTION_BUDGET_ALPHA", "148.0"))
S_MIN_ALPHA_MINER: float = float(os.getenv("S_MIN_ALPHA_MINER", "5.0"))
S_MIN_MASTER_VALIDATOR: float = float(os.getenv("S_MIN_MASTER_VALIDATOR", "10000"))
MAX_BIDS_PER_MINER: int = int(os.getenv("MAX_BIDS_PER_MINER", "5"))
FORBIDDEN_ALPHA_SUBNETS: list[int] = [73]
STRATEGY_PATH: str = os.getenv("STRATEGY_PATH", "weights.yml")
AUCTION_START_TIMEOUT = 30
# ╰────────────────────────────────────────────────────────────────────╯


# ╭─────────────────────────── REPUTATION ─────────────────────────────╮
REPUTATION_ENABLED: bool = os.getenv("REPUTATION_ENABLED", "true").lower() == "true"
REPUTATION_BETA: float = float(os.getenv("REPUTATION_BETA", "0.5"))
REPUTATION_MIX_GAMMA: float = float(os.getenv("REPUTATION_MIX_GAMMA", "0.7"))
REPUTATION_BASELINE_CAP_FRAC: float = float(os.getenv("REPUTATION_BASELINE_CAP_FRAC", "0.02"))
REPUTATION_MAX_CAP_FRAC: float = float(os.getenv("REPUTATION_MAX_CAP_FRAC", "0.30"))
# ╰────────────────────────────────────────────────────────────────────╯


# ╭────────────────────── SLIPPAGE ──────────────────────╮
DECIMALS: int = 10**9
K_SLIP: Decimal = Decimal("1.0")
CAP_SLIP: float = 1.0
SLIP_TOLERANCE: Decimal = Decimal("0.001")
SAMPLE_POINTS: int = 8
# ╰──────────────────────────────────────────────────────╯


# ╭────────────────────── ORACLE / SCANNER KNOBS ──────────────────────╮
MAX_CHUNK: int = int(os.getenv("ALPHA_SCAN_CHUNK", "512"))
MAX_CONCURRENCY: int = int(os.getenv("ALPHA_SCAN_CONCURRENCY", "8"))
POST_PAYMENT_CHECK_DELAY_BLOCKS: int = int(os.getenv("POST_PAYMENT_CHECK_DELAY_BLOCKS", "0"))
# ╰────────────────────────────────────────────────────────────────────╯


# ╭──────────────────── PENALTIES & JAIL ──────────────────────────────╮
JAIL_EPOCHS_PARTIAL: int = int(os.getenv("JAIL_EPOCHS_PARTIAL", "2"))
JAIL_EPOCHS_NO_PAY: int = int(os.getenv("JAIL_EPOCHS_NO_PAY", "8"))
# ╰────────────────────────────────────────────────────────────────────╯


# ╭──────────────────────────── LOGGING (pretty) ──────────────────────╮
PRETTY_LOGS: bool = os.getenv("PRETTY_LOGS", "true").lower() == "true"
LOG_TOP_N: int = int(os.getenv("LOG_TOP_N", "12"))
MASK_SS58: bool = os.getenv("MASK_SS58", "true").lower() == "true"
# ╰────────────────────────────────────────────────────────────────────╯


# ╭────────────── Miner payment re-submit guard ──────────────╮
PAYMENT_RETRY_COOLDOWN_BLOCKS: int = int(os.getenv("PAYMENT_RETRY_COOLDOWN_BLOCKS", "6"))
PAYMENT_TICK_SLEEP_SECONDS = 5 * 12  # 5 blocks
# ╰───────────────────────────────────────────────────────────╯


# ╭─────────────────────────── IPFS ─────────────────────────╮
DEFAULT_API_URL: str = os.getenv("IPFS_API_URL", "https://ipfs.infura.io:5001/api/v0").rstrip("/")
DEFAULT_GATEWAYS: list[str] = [
    gw.strip().rstrip("/")
    for gw in (
        os.getenv("IPFS_GATEWAYS", "https://ipfs.io/ipfs,https://cloudflare-ipfs.com/ipfs").split(",")
    )
    if gw.strip()
]
IPFS_CMD: str = os.getenv("IPFS_CMD", "ipfs")
# ╰──────────────────────────────────────────────────────────╯
