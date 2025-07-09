# ========================================================================== #
# metahash/protocol.py                                                       #
# (only the new and changed parts are shown)                                 #
# ========================================================================== #
from __future__ import annotations
from typing import Optional, Dict, List
from bittensor import Synapse


# ─────────────────────── NEW trigger synapse ────────────────────────── #
class AuctionStartSynapse(Synapse):
    """
    Validator → all miners (once per epoch).

    Carries every detail a miner needs to decide whether and how to bid.
    """
    epoch_index: int
    auction_start_block: int          # block height when validator opens bidding
    min_stake_alpha: float            # S_MIN_ALPHA gate
    auction_budget_alpha: float       # constant 148 α
    weights_bps: Dict[int, int]       # subnet→bps map (operator strategy)
    treasury_coldkey: str             # destination cold‑key for α payments


# ───────────────────── existing synapses (unchanged) ────────────────── #
class BidSynapse(Synapse):
    subnet_id: int
    alpha: float
    discount_bps: int
    accepted: Optional[bool] = None
    error: Optional[str] = None


class AckSynapse(Synapse):
    subnet_id: int
    alpha: float
    discount_bps: int
    accepted: bool
    error: Optional[str] = None


class WinSynapse(Synapse):
    subnet_id: int
    alpha: float
    clearing_discount_bps: int
    pay_deadline_block: int
