"""
metahash/config.py — global constants (v2 + reputation + pretty logs)
"""

from __future__ import annotations
import os
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()

# ╭─────────────────────────── ENVIRONMENT ────────────────────────────╮
DEFAULT_BITTENSOR_NETWORK: str = os.getenv("BITTENSOR_NETWORK", "finney")

# Gate v2 logic until chain reaches this height (validators idle before this)
START_V2_BLOCK: int = int(os.getenv("START_V2_BLOCK", "0"))

# Operational flags
TESTING: bool = os.getenv("TESTING", "false").lower() == "true"
FORCE_BURN_WEIGHTS: bool = os.getenv("FORCE_BURN_WEIGHTS", "false").lower() == "true"

# Monetary base unit (α planck)
PLANCK: int = 10**9
# ╰────────────────────────────────────────────────────────────────────╯


# ╭───────────────────────────── AUCTION ──────────────────────────────╮
# Global base budget (per epoch, system‑wide). Each **master** i gets:
# personal_budget_i = AUCTION_BUDGET_ALPHA * (stake_i / sum_master_stake)
AUCTION_BUDGET_ALPHA: float = float(os.getenv("AUCTION_BUDGET_ALPHA", "148.0"))

# Min stake for a *miner’s UID (coldkey owner)* to bid
S_MIN_ALPHA_MINER: float = float(os.getenv("S_MIN_ALPHA_MINER", "5.0"))

# Min stake for a *validator* to be considered a master
S_MIN_MASTER_VALIDATOR: float = float(os.getenv("S_MIN_MASTER_VALIDATOR", "10000"))

# Max distinct subnets a single coldkey may bid per epoch (per master)
MAX_BIDS_PER_MINER: int = int(os.getenv("MAX_BIDS_PER_MINER", "5"))

# Payment window length (blocks) from confirmation to deadline
PAYMENT_WINDOW_BLOCKS: int = int(os.getenv("PAYMENT_WINDOW_BLOCKS", "30"))

# Subnets on which incoming α transfers must be ignored for rewards calc
FORBIDDEN_ALPHA_SUBNETS: list[int] = [73]

# Strategy file (static – restart validator to reload)
STRATEGY_PATH: str = os.getenv("STRATEGY_PATH", "weights_bps.yml")
# ╰────────────────────────────────────────────────────────────────────╯


# ╭─────────────────────────── REPUTATION ─────────────────────────────╮
# Enable reputation-based caps at clearing time
REPUTATION_ENABLED: bool = os.getenv("REPUTATION_ENABLED", "true").lower() == "true"

# Exponential smoothing coefficient for reputation updates (0..1)
# Set high (e.g., 0.4–0.6) so miners can reach cap “in a couple of days”.
REPUTATION_BETA: float = float(os.getenv("REPUTATION_BETA", "0.5"))

# Baseline cap (fraction of master budget) for brand-new / zero-rep miners.
# Example 0.02 → at least 2% of a master's budget can be won if their bid is good.
REPUTATION_BASELINE_CAP_FRAC: float = float(os.getenv("REPUTATION_BASELINE_CAP_FRAC", "0.02"))

# Max cap (fraction of master budget) once reputation saturates.
# Example 0.30 → at most 30% of a master's budget can be won per auction by one coldkey.
REPUTATION_MAX_CAP_FRAC: float = float(os.getenv("REPUTATION_MAX_CAP_FRAC", "0.30"))
# ╰────────────────────────────────────────────────────────────────────╯


# ╭────────────────────── SLIPPAGE ──────────────────────╮
DECIMALS: int = 10**9
K_SLIP: Decimal = Decimal("1.0")
CAP_SLIP: float = 1.0
SLIP_TOLERANCE: Decimal = Decimal("0.001")     
SAMPLE_POINTS: int = 8  

# ╭────────────────────── ORACLE / SCANNER KNOBS ──────────────────────╮
# Event scan chunk / concurrency for alpha transfer scanner
MAX_CHUNK: int = int(os.getenv("ALPHA_SCAN_CHUNK", "512"))
MAX_CONCURRENCY: int = int(os.getenv("ALPHA_SCAN_CONCURRENCY", "8"))

# We use post‑deadline delay for settlement; keep FINALITY_LAG for compatibility
FINALITY_LAG: int = int(os.getenv("FINALITY_LAG", "0"))
# ╰────────────────────────────────────────────────────────────────────╯


# ╭──────────────────── PENALTIES & SETTLEMENT DELAYS ─────────────────╮
# Extra delay after payment window before settlement (blocks).
# With a full-epoch payment window (e+1), you can keep this at 0.
POST_PAYMENT_CHECK_DELAY_BLOCKS: int = int(os.getenv("POST_PAYMENT_CHECK_DELAY_BLOCKS", "0"))

# Haircut on partially‑paid contributions (bps), e.g. 4000 = −40%
PARTIAL_PAYMENT_PENALTY_BPS: int = int(os.getenv("PARTIAL_PAYMENT_PENALTY_BPS", "0"))

# Epoch‑based jail durations applied to the *coldkey* (not uid)
#   – If some lines covered but not all → PARTIAL jail
#   – If nothing paid (0 RAO)           → NO_PAY jail
JAIL_EPOCHS_PARTIAL: int = int(os.getenv("JAIL_EPOCHS_PARTIAL", "2"))
JAIL_EPOCHS_NO_PAY: int = int(os.getenv("JAIL_EPOCHS_NO_PAY", "8"))
# ╰────────────────────────────────────────────────────────────────────╯


# ╭──────────────────────────── LOGGING (pretty) ──────────────────────╮
# Turn on Rich-based pretty logs if available
PRETTY_LOGS: bool = os.getenv("PRETTY_LOGS", "true").lower() == "true"

# Show at most this many rows in tables (caps, reps, winners, offenders)
LOG_TOP_N: int = int(os.getenv("LOG_TOP_N", "12"))

# Mask addresses in logs (e.g., 5Gw6…krsn)
MASK_SS58: bool = os.getenv("MASK_SS58", "true").lower() == "true"
# ╰────────────────────────────────────────────────────────────────────╯
