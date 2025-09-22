# ==========================================================================
# metahash/protocol.py
#
# (v2.3 — miners include bids in AuctionStart; validators send EARLY wins.)
# ==========================================================================

from __future__ import annotations
from typing import Optional, Dict, List, Union
from bittensor import Synapse

# ─────────────────────── Trigger synapse (Validator → Miners) ───────────────────────


class AuctionStartSynapse(Synapse):
    """ Validator → all miners (once per epoch, i.e., current epoch 'e').
        Miner returns this *same* synapse with:
          - ack: bool
          - retries_attempted: int
          - bids: list of dicts {"subnet_id", "alpha", "discount_bps"} (+ optional extra keys)
          - bids_sent: int
          - note: optional string
    """
    # request fields (from Validator)
    epoch_index: int
    auction_start_block: int
    min_stake_alpha: float
    auction_budget_alpha: float
    weights_bps: Dict[int, int]
    treasury_coldkey: str
    # identity (for miner logs)
    validator_uid: Optional[int] = None
    validator_hotkey: Optional[str] = None

    # response fields (from Miner)
    ack: Optional[bool] = None
    retries_attempted: Optional[int] = None
    bids: Optional[List[Dict[str, Union[int, float, str]]]] = None
    bids_sent: Optional[int] = None
    note: Optional[str] = None


# ───────────────────── Win synapse (Validator → Miner) ─────────────────────
class WinSynapse(Synapse):
    """ Validator → Miner (per won bid line).
        Sent EARLY in epoch e, with explicit next-epoch window (e+1).
    """
    # request fields
    subnet_id: int
    alpha: float
    clearing_discount_bps: int
    pay_window_start_block: Optional[int] = None
    pay_window_end_block: Optional[int] = None
    pay_epoch_index: Optional[int] = None
    # validator identity
    validator_uid: Optional[int] = None
    validator_hotkey: Optional[str] = None
    # clarity
    requested_alpha: Optional[float] = None
    accepted_alpha: Optional[float] = None
    was_partially_accepted: Optional[bool] = None
    accepted_amount_rao: Optional[int] = None

    # response fields (from Miner)
    ack: Optional[bool] = None
    payment_attempted: Optional[bool] = None
    payment_ok: Optional[bool] = None
    attempts: Optional[int] = None
    last_response: Optional[str] = None


# (Optional future) Bid feedback synapse kept for compatibility (not used in single-pass)
class BidFeedbackSynapse(Synapse):
    epoch_index: int
    feedback: List[Dict[str, Union[int, float, str]]]
    validator_uid: Optional[int] = None
    validator_hotkey: Optional[str] = None
    ack: Optional[bool] = None
