#!/usr/bin/env python3
# neurons/miner_models.py — shared dataclasses for miner components

from dataclasses import dataclass
from metahash.miner.logging import (
    MinerPhase, LogLevel, miner_logger, 
    log_init, log_auction, log_commitments, log_settlement
)


@dataclass
class BidLine:
    subnet_id: int
    alpha: float
    discount_bps: int  # 0..10000
    
    def __post_init__(self):
        """Log bid line creation for debugging purposes."""
        log_auction(LogLevel.DEBUG, "BidLine created", "models", {
            "subnet_id": self.subnet_id,
            "alpha": self.alpha,
            "discount_bps": self.discount_bps
        })


@dataclass
class WinInvoice:
    # identity
    invoice_id: str = ""                      # <- derived
    validator_key: str = ""                   # hotkey if known else "uid:<n>"
    treasury_coldkey: str = ""
    # bid / allocation
    subnet_id: int = 0
    alpha: float = 0.0                        # accepted α (what we'll pay against)
    discount_bps: int = 0
    alpha_requested: float = 0.0              # what we originally asked
    was_partial: bool = False                 # partial fill?
    # payment window (epoch e+1)
    pay_window_start_block: int = 0
    pay_window_end_block: int = 0
    pay_epoch_index: int = 0
    # amount to pay (NOTE: discount DOES NOT reduce payment)
    amount_rao: int = 0
    # context
    epoch_seen: int = 0
    # payment progress
    paid: bool = False
    pay_attempts: int = 0
    last_attempt_ts: float = 0.0
    last_response: str = ""
    
    def __post_init__(self):
        """Log win invoice creation for debugging purposes."""
        log_commitments(LogLevel.DEBUG, "WinInvoice created", "models", {
            "invoice_id": self.invoice_id,
            "validator_key": self.validator_key[:8] + "…" if self.validator_key else "unknown",
            "subnet_id": self.subnet_id,
            "alpha": self.alpha,
            "amount_rao": self.amount_rao,
            "paid": self.paid
        })
