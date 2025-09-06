# ==========================================================================
#
# metahash/protocol.py
#
# (v2.2+ — miners include bids in AuctionStart; validators send EARLY wins with
#          payment window = epoch e+1; explicit accepted size so miners can
#          pay exactly what was accepted; backward compatible fields kept.)
#
# ==========================================================================

from __future__ import annotations
from typing import Optional, Dict, List, Union
from bittensor import Synapse


# ─────────────────────── Trigger synapse (Validator → Miners) ─────────────────────── #
class AuctionStartSynapse(Synapse):
    """
    Validator → all miners (once per epoch, i.e., current epoch 'e').
    Carries all details a miner needs to decide whether and how to bid.

    Miner returns this *same* synapse with:
      - ack: bool
      - retries_attempted: int (how many pending win payments retried)
      - bids: list of dicts with keys {"subnet_id", "alpha", "discount_bps"}.
              Extra fields like {"bid_id": "<string>"} are allowed for logging/tracing.
      - bids_sent: int (redundant but handy to log)
      - note: optional string
    """
    # request fields (from Validator)
    epoch_index: int
    auction_start_block: int      # block height when validator opens bidding (epoch e)
    min_stake_alpha: float        # S_MIN_ALPHA gate
    auction_budget_alpha: float   # e.g., 148 α (personal budget share)
    weights_bps: Dict[int, int]   # subnet→bps map (operator strategy)
    treasury_coldkey: str         # destination cold-key for α payments (master’s treasury)

    # NEW: identity of the validator (for robust miner logs)
    validator_uid: Optional[int] = None
    validator_hotkey: Optional[str] = None

    # response fields (filled by Miner)
    ack: Optional[bool] = None
    retries_attempted: Optional[int] = None

    # IMPORTANT: allow string values inside each bid dict (e.g., "bid_id")
    # Required numeric keys (validator relies on): subnet_id:int, alpha:float, discount_bps:int
    # Extra keys are tolerated and ignored by the validator.
    bids: Optional[List[Dict[str, Union[int, float, str]]]] = None

    bids_sent: Optional[int] = None
    note: Optional[str] = None


# ───────────────────── Win synapse (Validator → Miner) ───────────────────── #
class WinSynapse(Synapse):
    """
    Validator → Miner (per won bid line). Sent EARLY in epoch e,
    with explicit *next-epoch* payment window (epoch e+1).

    Settlement by validators will scan exactly the payment-epoch window recorded in commitments.

    Backward compatibility notes:
      - Field `alpha` continues to contain the *accepted* alpha (as before).
      - New fields make partial acceptance explicit without breaking older miners.
    """
    # request fields
    subnet_id: int
    alpha: float                        # accepted α (kept for backward compatibility)
    clearing_discount_bps: int

    # explicit payment window (epoch e+1)
    pay_window_start_block: Optional[int] = None
    pay_window_end_block: Optional[int] = None
    pay_epoch_index: Optional[int] = None

    # NEW: validator identity for miner-side logs
    validator_uid: Optional[int] = None
    validator_hotkey: Optional[str] = None

    # NEW (optional) clarity fields
    requested_alpha: Optional[float] = None        # α requested in the bid line
    accepted_alpha: Optional[float] = None         # α actually accepted (same as `alpha`)
    was_partially_accepted: Optional[bool] = None  # whether accepted < requested
    accepted_amount_rao: Optional[int] = None      # convenience (computed from accepted_alpha; no discount applied)

    # response fields (filled by Miner)
    ack: Optional[bool] = None
    payment_attempted: Optional[bool] = None
    payment_ok: Optional[bool] = None
    attempts: Optional[int] = None
    last_response: Optional[str] = None
