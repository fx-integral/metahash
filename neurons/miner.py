#!/usr/bin/env python3
# neurons/miner.py — Event-driven bidder v2.6 (files split, no α cap vs validator budget)
# Files:
#   - miner_models.py              (BidLine, WinInvoice)
#   - miner_mixins.py              (state I/O, helpers, chain info)
#   - background_payment_miner.py  (payments + background retries)
#
# Change in this build:
#   • Do NOT reject bids with alpha > auction budget (or AUCTION_BUDGET_ALPHA).
#     We warn but still submit; validator’s TAO-based clearing will partially fill.

import asyncio
from math import isfinite
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import bittensor as bt
from bittensor import Synapse

from metahash.miner.models import BidLine, WinInvoice
from metahash.miner.payments import BackgroundPaymentMiner

from metahash.base.utils.logging import ColoredLogger as clog
from metahash.protocol import AuctionStartSynapse, WinSynapse
from metahash.config import (
    PLANCK,
    S_MIN_ALPHA_MINER,
    PAYMENT_TICK_SLEEP_SECONDS,
)
from metahash.utils.pretty_logs import pretty
from metahash.utils.wallet_utils import unlock_wallet


class Miner(BackgroundPaymentMiner):
    def __init__(self, config=None):
        super().__init__(config=config)

        # Local persistent state
        self._state_file = Path("miner_state.json")
        self._treasuries: Dict[str, str] = {}  # validator_key -> treasury coldkey
        self._already_bid: Dict[str, List[Tuple[int, int, float, int]]] = {}  # validator_key -> list of (epoch, subnet, alpha, discount_bps)
        self._wins: List[WinInvoice] = []

        # Guards
        self._pay_lock: asyncio.Lock = asyncio.Lock()    # avoid concurrent tx + ws recv
        self._state_lock: asyncio.Lock = asyncio.Lock()  # serialize state writes

        # Async subtensor bound to axon loop
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None

        # Axon loop + background task
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._retry_interval_s: int = PAYMENT_TICK_SLEEP_SECONDS
        self._payment_retry_task: Optional[asyncio.Task] = None

        # Optional: start fresh
        if getattr(self.config, "fresh", False):
            self._wipe_state()
            pretty.log("[magenta]Fresh start requested: cleared local miner state.[/magenta]")

        self._load_state_file()
        self.lines: List[BidLine] = self._build_lines_from_config()

        pretty.banner(
            "Miner started",
            f"uid={self.uid} | hotkey={self.wallet.hotkey.ss58_address} | lines={len(self.lines)} | epoch(e)={getattr(self, 'epoch_index', 0)}",
            style="bold magenta",
        )
        self._log_cfg_summary()

        unlock_wallet(wallet=self.wallet)

    # ---------------------- auction handlers ----------------------

    async def auctionstart_forward(self, synapse: AuctionStartSynapse) -> AuctionStartSynapse:
        await self._ensure_background_tasks()

        uid, caller_hot = self._resolve_caller(synapse)
        vkey = self._validator_key(uid, caller_hot)
        epoch = int(synapse.epoch_index)

        # Remember treasury for future invoices
        try:
            if getattr(synapse, "treasury_coldkey", None):
                self._treasuries[vkey] = synapse.treasury_coldkey
                await self._async_save_state()
        except Exception:
            pass

        budget_alpha = float(getattr(synapse, "auction_budget_alpha", 0.0) or 0.0)
        min_stake_alpha = float(getattr(synapse, "min_stake_alpha", S_MIN_ALPHA_MINER) or S_MIN_ALPHA_MINER)

        pretty.kv_panel(
            "[cyan]AuctionStart received[/cyan]",
            [
                ("validator", f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})"),
                ("epoch (e)", epoch),
                ("budget α", f"{budget_alpha:.3f}"),
                ("min_stake", f"{min_stake_alpha:.3f} α"),
                ("timeline", f"bid now (e) → pay in (e+1={epoch+1}) → weights from e in (e+2={epoch+2})"),
            ],
            style="bold cyan",
        )

        # Retry unpaid invoices (only pays if in window)
        pre_unpaid = len([w for w in self._wins if not w.paid])
        await self._retry_unpaid_invoices()

        # Stake gate
        my_stake = float(self.metagraph.stake[self.uid])
        if my_stake < min_stake_alpha:
            pretty.log(f"[yellow]Stake below S_MIN_ALPHA_MINER – not bidding to this validator (epoch {epoch}).[/yellow]")
            self._status_tables()
            synapse.ack = True
            synapse.bids = []
            synapse.bids_sent = 0
            synapse.retries_attempted = pre_unpaid
            synapse.note = "stake gate"
            return synapse

        # Build bids directly on received synapse
        out_bids = []
        sent = 0
        rows_sent = []
        for ln in self.lines:
            # Basic sanity checks only; DO NOT cap by validator budget anymore.
            if not isfinite(ln.alpha) or ln.alpha <= 0:
                pretty.log(f"[yellow]Invalid alpha {ln.alpha} for subnet {ln.subnet_id} – skipping line.[/yellow]")
                continue
            if not (0 <= ln.discount_bps <= 10_000):
                pretty.log(f"[yellow]Invalid discount {ln.discount_bps} bps – skipping line.[/yellow]")
                continue
            if self._has_bid(vkey, epoch, ln.subnet_id, ln.alpha, ln.discount_bps):
                continue

            # Soft warning if the line α exceeds the announced budget — still send the bid.
            if budget_alpha > 0 and ln.alpha > budget_alpha:
                pretty.log(
                    f"[grey58]Note:[/grey58] line α {ln.alpha:.4f} exceeds validator’s announced budget α {budget_alpha:.4f}; "
                    f"it will be considered and may be partially filled by TAO-budgeted clearing."
                )

            bid_id = self._make_bid_id(vkey, epoch, ln.subnet_id, ln.alpha, ln.discount_bps)
            out_bids.append({
                "subnet_id": int(ln.subnet_id),
                "alpha": float(ln.alpha),
                "discount_bps": int(ln.discount_bps),
                "bid_id": bid_id,
            })
            self._remember_bid(vkey, epoch, ln.subnet_id, ln.alpha, ln.discount_bps)
            sent += 1
            rows_sent.append([ln.subnet_id, f"{ln.alpha:.4f} α", f"{ln.discount_bps} bps", bid_id, epoch])

        if sent == 0:
            pretty.log("[grey]No bids were added (all lines either invalid or already added).[/grey]")
        else:
            pretty.table("[yellow]Bids Sent[/yellow]", ["Subnet", "Alpha", "Discount", "BidID", "Epoch"], rows_sent)

        await self._async_save_state()
        self._status_tables()

        synapse.ack = True
        synapse.bids = out_bids
        synapse.bids_sent = sent
        synapse.retries_attempted = pre_unpaid
        synapse.note = None
        return synapse

    async def win_forward(self, synapse: WinSynapse) -> WinSynapse:
        await self._ensure_background_tasks()

        uid, caller_hot = self._resolve_caller(synapse)
        vkey = self._validator_key(uid, caller_hot)

        treasury_ck = self._treasuries.get(vkey, "")
        if not treasury_ck:
            pretty.log("[red]Treasury unknown for this validator – cannot pay.[/red]")
            synapse.ack = False
            synapse.payment_attempted = False
            synapse.payment_ok = False
            synapse.attempts = 0
            synapse.last_response = "treasury unknown"
            return synapse

        # Prefer explicit accepted_alpha if provided (new), fallback to alpha (compat)
        accepted_alpha = float(getattr(synapse, "accepted_alpha", None) or synapse.alpha)
        requested_alpha = float(getattr(synapse, "requested_alpha", None) or accepted_alpha)
        was_partial = bool(getattr(synapse, "was_partially_accepted", accepted_alpha < requested_alpha - 1e-12))

        # IMPORTANT: discount does NOT reduce payment.
        amount_rao = int(round(accepted_alpha * PLANCK))
        epoch_now = int(getattr(self, "epoch_index", 0))

        pay_start = int(getattr(synapse, "pay_window_start_block", 0) or 0)
        pay_end = int(getattr(synapse, "pay_window_end_block", 0) or 0)
        pay_ep = int(getattr(synapse, "pay_epoch_index", 0) or 0)
        clearing_bps = int(getattr(synapse, "clearing_discount_bps", 0) or 0)

        inv = WinInvoice(
            validator_key=vkey,
            treasury_coldkey=treasury_ck,
            subnet_id=int(synapse.subnet_id),
            alpha=float(accepted_alpha),
            alpha_requested=float(requested_alpha),
            was_partial=was_partial,
            discount_bps=clearing_bps,
            pay_window_start_block=pay_start,
            pay_window_end_block=pay_end,
            pay_epoch_index=pay_ep,
            amount_rao=amount_rao,
            epoch_seen=epoch_now,
        )

        inv.invoice_id = self._make_invoice_id(
            vkey, epoch_now, inv.subnet_id, inv.alpha, inv.discount_bps, inv.pay_epoch_index, inv.amount_rao
        )

        # idempotent merge by identity of the allocation / pay window
        for w in self._wins:
            if (
                w.validator_key == inv.validator_key
                and w.subnet_id == inv.subnet_id
                and w.alpha == inv.alpha
                and w.discount_bps == inv.discount_bps
                and w.pay_epoch_index == inv.pay_epoch_index
                and w.amount_rao == inv.amount_rao
            ):
                if inv.pay_window_end_block and not w.pay_window_end_block:
                    w.pay_window_end_block = inv.pay_window_end_block
                w.alpha_requested = inv.alpha_requested
                w.was_partial = inv.was_partial
                inv = w
                break
        else:
            self._wins.append(inv)

        await self._async_save_state()

        pretty.kv_panel(
            "[green]Win received[/green]",
            [
                ("validator", f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})"),
                ("epoch_now (e)", epoch_now),
                ("subnet", inv.subnet_id),
                ("accepted α", f"{inv.alpha:.4f}"),
                ("requested α", f"{inv.alpha_requested:.4f}"),
                ("partial", inv.was_partial),
                ("discount", f"{inv.discount_bps} bps"),
                ("pay_epoch (e+1)", inv.pay_epoch_index or (epoch_now + 1)),
                ("window", f"[{inv.pay_window_start_block}, {inv.pay_window_end_block or '?'}]"),
                ("amount_to_pay", f"{inv.amount_rao/PLANCK:.4f} α"),
                ("invoice_id", inv.invoice_id),
            ],
            style="bold green",
        )

        # Attempt payment ONLY when inside the block window
        await self._attempt_payment(inv)
        self._status_tables()

        synapse.ack = True
        synapse.payment_attempted = inv.pay_attempts > 0
        synapse.payment_ok = inv.paid
        synapse.attempts = inv.pay_attempts
        synapse.last_response = inv.last_response[:300] if inv.last_response else ""
        return synapse

    async def forward(self, synapse: Synapse):
        return synapse


if __name__ == "__main__":
    from metahash.bittensor_config import config

    with Miner(config=config(role="miner")) as m:
        import time as _t
        while True:
            clog.info("Miner running…", color="gray")
            _t.sleep(120)
