# ========================================================================== #
# metahash/protocol.py                                                       #
# New synapses for SN‑73 batch‑auction                                       #
# ========================================================================== #
from __future__ import annotations

from typing import Optional, Dict, List
from bittensor import Synapse


class StartRegistrationsSynapse(Synapse):       # already existed
    current_verified_coldkeys: List[str]
    verified_coldkeys_to_register: Optional[List[str]] = None
    signatures: Optional[List[str]] = None


class FinishRegistrationsSynapse(Synapse):      # already existed
    successfully_verified_coldkeys: Optional[List[str]] = None
    non_successfully_verified_coldkeys: Optional[List[str]] = None
    errors: Optional[List[str]] = None


# ───────────────────────────────  NEW  ──────────────────────────────── #

class BidSynapse(Synapse):
    """
    Miner → Validator

    A single bid packet.  All numbers are *inclusive* (e.g. 1 TAO = 1e9 RAW).
    """
    subnet_id: int
    alpha: float                  # α being offered
    discount_bps: int             # negative spread in basis‑points (1/10 000)
    # Populated by validator in the AckSynapse echo
    accepted: Optional[bool] = None
    error: Optional[str] = None


class AckSynapse(Synapse):
    """
    Validator → Miner

    Mirrors BidSynapse with the decision embedded.
    """
    subnet_id: int
    alpha: float
    discount_bps: int
    accepted: bool
    error: Optional[str] = None


class WinSynapse(Synapse):
    """
    Validator → Miner (only for winning bids)

    Communicates clearing result and full payment instructions.
    """
    subnet_id: int
    alpha: float                  # α that must be paid (may be ≤ original bid)
    clearing_discount_bps: int    # d*   (same for all winners)
    alpha_sink: str               # cold‑key address (single‑use sink)
    pay_deadline_block: int       # inclusive
