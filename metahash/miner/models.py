#!/usr/bin/env python3
# neurons/miner_models.py — shared dataclasses for miner components

from dataclasses import dataclass


@dataclass
class BidLine:
    subnet_id: int
    alpha: float
    discount_bps: int  # 0..10000


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
