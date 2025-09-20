#!/usr/bin/env python3
# neurons/miner.py — Event-driven bidder v2.7.1 (no daemon; per-invoice retries with clearer LastResp)

import asyncio
import time
from math import isfinite
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import bittensor as bt
from bittensor import Synapse, BLOCKTIME

from metahash.miner.models import BidLine, WinInvoice
from metahash.base.miner import BaseMinerNeuron
from metahash.miner.mixins import MinerMixins

from metahash.base.utils.logging import ColoredLogger as clog
from metahash.protocol import AuctionStartSynapse, WinSynapse
from metahash.config import (
    PLANCK,
    S_MIN_ALPHA_MINER,
)
from metahash.utils.pretty_logs import pretty
from metahash.utils.wallet_utils import unlock_wallet, transfer_alpha


class Miner(BaseMinerNeuron, MinerMixins):
    def __init__(self, config=None):
        super().__init__(config=config)

        # Local persistent state
        self._state_file = Path("miner_state.json")
        self._treasuries: Dict[str, str] = {}  # validator_key -> treasury coldkey
        self._already_bid: Dict[str, List[Tuple[int, int, float, int]]] = {}  # validator_key -> list of (epoch, subnet, alpha, discount_bps)
        self._wins: List[WinInvoice] = []

        # Guards
        self._pay_lock: asyncio.Lock = asyncio.Lock()    # prevent concurrent tx on-chain
        self._state_lock: asyncio.Lock = asyncio.Lock()  # serialize state writes

        # Async subtensor bound to axon loop
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Per-invoice tasks (no global daemon)
        self._payment_tasks: Dict[str, asyncio.Task] = {}

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

        # Payment source / retry config
        self._pay_cfg_initialized: bool = False
        self._pay_rr_index: int = 0
        self._pay_pool: List[str] = []
        self._pay_map: Dict[int, str] = {}
        self._pay_start_safety_blocks: int = 0
        self._retry_every_blocks: int = 2
        self._retry_max_attempts: int = 12

    # ---------------------- configuration ----------------------

    def _ensure_payment_config(self):
        """Load CLI-provided payment sources + retry knobs exactly once."""
        if self._pay_cfg_initialized:
            return

        pay_cfg = getattr(self.config, "payment", None)

        # --payment.validators hk1 hk2 ...
        try:
            pool = list(getattr(pay_cfg, "validators", [])) if pay_cfg is not None else []
            self._pay_pool = [hk.strip() for hk in pool if isinstance(hk, str) and hk.strip()]
            if self._pay_pool:
                pretty.log(f"[green]Payment pool loaded[/green]: {len(self._pay_pool)} round-robin origin hotkey(s).")
        except Exception:
            self._pay_pool = []

        # --payment.map sid:hk sid:hk ...
        try:
            pairs = list(getattr(pay_cfg, "map", [])) if pay_cfg is not None else []
            for item in pairs:
                if not isinstance(item, str) or ":" not in item:
                    continue
                sid_s, hk = item.split(":", 1)
                sid_s = sid_s.strip()
                hk = hk.strip()
                if not sid_s or not hk:
                    continue
                try:
                    sid = int(sid_s)
                except Exception:
                    continue
                self._pay_map[sid] = hk
            if self._pay_map:
                pretty.log(f"[green]Payment map loaded[/green]: {len(self._pay_map)} subnet-specific origin hotkey(s).")
        except Exception:
            self._pay_map = {}

        # --payment.start_safety_blocks N
        try:
            ssb = int(getattr(pay_cfg, "start_safety_blocks", 0) or 0) if pay_cfg is not None else 0
            self._pay_start_safety_blocks = max(0, ssb)
            if self._pay_start_safety_blocks:
                pretty.log(f"[green]Payment start safety[/green]: +{self._pay_start_safety_blocks} block(s) after window start.")
        except Exception:
            self._pay_start_safety_blocks = 0

        # --payment.retry_every_blocks N (default 2)
        try:
            reb = int(getattr(pay_cfg, "retry_every_blocks", 2) or 2) if pay_cfg is not None else 2
            self._retry_every_blocks = max(1, reb)
        except Exception:
            self._retry_every_blocks = 2

        # --payment.retry_max_attempts N (default 12)
        try:
            rma = int(getattr(pay_cfg, "retry_max_attempts", 12) or 12) if pay_cfg is not None else 12
            self._retry_max_attempts = max(1, rma)
        except Exception:
            self._retry_max_attempts = 12

        if not self._pay_map and not self._pay_pool and not self._pay_start_safety_blocks:
            pretty.log("[yellow]No --payment.map / --payment.validators / --payment.start_safety_blocks; defaults in effect.[/yellow]")

        self._pay_cfg_initialized = True

    def _pick_rr_hotkey(self) -> str:
        """Round-robin from the pool; if empty, fall back to local wallet hotkey."""
        if not self._pay_pool:
            return self.wallet.hotkey.ss58_address
        hk = self._pay_pool[self._pay_rr_index % len(self._pay_pool)]
        self._pay_rr_index += 1
        return hk

    def _origin_hotkey_for_invoice(self, inv: WinInvoice) -> str:
        """
        Decide which origin hotkey to use for this invoice:
          1) If subnet-specific mapping exists → use mapped hotkey.
          2) Else use round-robin pool (if any).
          3) Else use local wallet hotkey (default / legacy).
        """
        sid = getattr(inv, "subnet_id", None)
        if isinstance(sid, int) and sid in self._pay_map:
            return self._pay_map[sid]
        return self._pick_rr_hotkey()

    # ---------------------- per-invoice scheduler ----------------------

    def _schedule_payment(self, inv: WinInvoice):
        """Create a single async task to handle paying this invoice; idempotent."""
        if inv.paid:
            return
        t = self._payment_tasks.get(inv.invoice_id)
        if t and not t.done():
            return  # already scheduled

        # Update UX immediately
        inv.last_response = f"scheduled (wait ≥{self._pay_start_safety_blocks} blk)"
        asyncio.create_task(self._async_save_state())

        task = asyncio.create_task(self._payment_worker(inv), name=f"pay_{inv.invoice_id}")
        self._payment_tasks[inv.invoice_id] = task

        pretty.kv_panel(
            "[cyan]Payment scheduled[/cyan]",
            [
                ("invoice", inv.invoice_id),
                ("subnet", inv.subnet_id),
                ("α", f"{inv.amount_rao/PLANCK:.4f}"),
                ("window", f"[{inv.pay_window_start_block},{inv.pay_window_end_block or '?'}]"),
                ("safety", f"+{self._pay_start_safety_blocks} blk"),
                ("retry", f"every {self._retry_every_blocks} blk × {self._retry_max_attempts}"),
            ],
            style="bold cyan",
        )

    async def _payment_worker(self, inv: WinInvoice):
        """Try to pay inside the window with retries; exits when paid/expired/attempts exceeded."""
        try:
            await self._ensure_async_subtensor()

            start = int(inv.pay_window_start_block or 0)
            end = int(inv.pay_window_end_block or 0)
            allowed_start = start + int(self._pay_start_safety_blocks or 0)

            attempt = 0
            while True:
                blk = await self._get_current_block()

                # Too early
                if start and (blk <= 0 or blk < allowed_start):
                    inv.last_response = f"waiting (blk {blk} < start {allowed_start})"
                    await self._async_save_state()
                    sleep_blocks = max(1, allowed_start - blk)
                    sleep_s = max(0.5, min(10 * float(BLOCKTIME), sleep_blocks * float(BLOCKTIME)))
                    pretty.kv_panel(
                        "[blue]PAY wait[/blue]",
                        [("inv", inv.invoice_id), ("blk", blk), ("allowed_start", allowed_start), ("sleep", f"{sleep_s:.1f}s")],
                        style="bold blue",
                    )
                    await asyncio.sleep(sleep_s)
                    continue

                # Window over
                if end and blk > end:
                    inv.last_response = f"window over (blk {blk} > end {end})"
                    await self._async_save_state()
                    pretty.kv_panel(
                        "[yellow]PAY exit[/yellow]",
                        [("inv", inv.invoice_id), ("reason", "window over"), ("blk", blk), ("end", end)],
                        style="bold yellow",
                    )
                    break

                # Attempt payment (under lock)
                attempt += 1
                inv.last_attempt_ts = time.time()
                await self._async_save_state()
                pretty.kv_panel(
                    "[white]PAY attempt[/white]",
                    [("inv", inv.invoice_id), ("try", f"{attempt}/{self._retry_max_attempts}"), ("blk", blk), ("win", f"[{start},{end}]")],
                    style="bold white",
                )

                async with self._pay_lock:
                    if inv.paid:
                        inv.last_response = "already paid"
                        await self._async_save_state()
                        break

                    # Fresh height under the lock too
                    blk2 = await self._get_current_block()
                    if start and (blk2 <= 0 or blk2 < allowed_start):
                        inv.last_response = f"not yet in window (blk {blk2} < start {start}+{self._pay_start_safety_blocks})"
                        ok = False
                        resp = inv.last_response
                    elif end and blk2 > end:
                        inv.last_response = f"window over (blk {blk2} > end {end})"
                        ok = False
                        resp = inv.last_response
                    else:
                        origin_hotkey = self._origin_hotkey_for_invoice(inv)
                        try:
                            ok = await transfer_alpha(
                                subtensor=self._async_subtensor,
                                wallet=self.wallet,
                                hotkey_ss58=origin_hotkey,
                                origin_and_dest_netuid=inv.subnet_id,
                                dest_coldkey_ss58=inv.treasury_coldkey,
                                amount=bt.Balance.from_rao(inv.amount_rao),
                                wait_for_inclusion=True,
                                wait_for_finalization=False,
                            )
                            resp = "ok" if ok else "rejected"
                        except Exception as exc:
                            ok = False
                            resp = f"exception: {exc}"

                    inv.pay_attempts += 1
                    inv.last_response = str(resp)[:300]

                    if ok:
                        inv.paid = True
                        await self._async_save_state()
                        pretty.kv_panel(
                            "[green]Payment OK[/green]",
                            [
                                ("inv", inv.invoice_id),
                                ("α", f"{inv.amount_rao/PLANCK:.4f}"),
                                ("dst_treasury", inv.treasury_coldkey[:8] + "…"),
                                ("src_hotkey", origin_hotkey[:8] + "…"),
                                ("sid", inv.subnet_id),
                                ("pay_e", inv.pay_epoch_index),
                            ],
                            style="bold green",
                        )
                        self._status_tables()
                        self._log_aggregate_summary()
                        break
                    else:
                        await self._async_save_state()
                        pretty.kv_panel(
                            "[red]Payment FAILED[/red]",
                            [
                                ("inv", inv.invoice_id),
                                ("resp", inv.last_response[:80]),
                                ("sid", inv.subnet_id),
                                ("attempts", inv.pay_attempts),
                            ],
                            style="bold red",
                        )

                # Exit conditions
                if attempt >= int(self._retry_max_attempts or 1):
                    inv.last_response = f"max attempts ({attempt})"
                    await self._async_save_state()
                    pretty.kv_panel(
                        "[yellow]PAY exit[/yellow]",
                        [("inv", inv.invoice_id), ("reason", "max attempts"), ("attempts", attempt)],
                        style="bold yellow",
                    )
                    break

                # Sleep until next retry
                await asyncio.sleep(max(0.5, float(self._retry_every_blocks) * float(BLOCKTIME)))

        except asyncio.CancelledError:
            inv.last_response = "cancelled"
            await self._async_save_state()
            pretty.kv_panel("[grey]PAY cancelled[/grey]", [("inv", inv.invoice_id)], style="bold magenta")
            raise
        except Exception as e:
            inv.last_response = f"error: {e}"
            await self._async_save_state()
            pretty.kv_panel(
                "[yellow]PAY worker error[/yellow]",
                [("inv", getattr(inv, "invoice_id", "?")), ("err", str(e)[:120])],
                style="bold yellow",
            )
            return
        finally:
            # cleanup finished/cancelled task
            t = self._payment_tasks.get(inv.invoice_id)
            if t and t.done():
                self._payment_tasks.pop(inv.invoice_id, None)

    def _schedule_unpaid_pending(self):
        """Schedule tasks for all unpaid invoices (idempotent)."""
        for w in self._wins:
            if not w.paid:
                self._schedule_payment(w)

    # ---------------------- auction handlers ----------------------

    async def auctionstart_forward(self, synapse: AuctionStartSynapse) -> AuctionStartSynapse:
        await self._ensure_async_subtensor()
        self._ensure_payment_config()

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
            "[cyan]AuctionStart[/cyan]",
            [
                ("validator", f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})"),
                ("epoch (e)", epoch),
                ("budget α", f"{budget_alpha:.3f}"),
                ("min_stake", f"{min_stake_alpha:.3f} α"),
                ("timeline", f"bid now (e) → pay in (e+1={epoch+1}) → weights from e in (e+2={epoch+2})"),
            ],
            style="bold cyan",
        )

        # (Re)schedule any pending unpaid invoices we may have persisted
        self._schedule_unpaid_pending()

        # Stake gate
        my_stake = float(self.metagraph.stake[self.uid])
        if my_stake < min_stake_alpha:
            pretty.log(f"[yellow]Stake below S_MIN_ALPHA_MINER – not bidding to this validator (epoch {epoch}).[/yellow]")
            self._status_tables()
            synapse.ack = True
            synapse.bids = []
            synapse.bids_sent = 0
            synapse.note = "stake gate"
            return synapse

        # Build bids on received synapse
        out_bids = []
        sent = 0
        rows_sent = []
        for ln in self.lines:
            # Checks; DO NOT cap by validator budget.
            if not isfinite(ln.alpha) or ln.alpha <= 0:
                pretty.log(f"[yellow]Invalid alpha {ln.alpha} for subnet {ln.subnet_id} – skipping line.[/yellow]")
                continue
            if not (0 <= ln.discount_bps <= 10_000):
                pretty.log(f"[yellow]Invalid discount {ln.discount_bps} bps – skipping line.[/yellow]")
                continue
            if self._has_bid(vkey, epoch, ln.subnet_id, ln.alpha, ln.discount_bps):
                continue

            if budget_alpha > 0 and ln.alpha > budget_alpha:
                pretty.log(
                    f"[grey58]Note:[/grey58] line α {ln.alpha:.4f} exceeds validator budget α {budget_alpha:.4f}; may be partially filled."
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
        synapse.note = None
        return synapse

    async def win_forward(self, synapse: WinSynapse) -> WinSynapse:
        await self._ensure_async_subtensor()
        self._ensure_payment_config()

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

        # Window-stable invoice identity
        inv.invoice_id = self._make_invoice_id(
            vkey,
            inv.subnet_id,
            inv.alpha,
            inv.discount_bps,
            inv.amount_rao,
            inv.pay_window_start_block,
            inv.pay_window_end_block,
            inv.pay_epoch_index,
        )

        # idempotent merge by window identity of the allocation
        for w in self._wins:
            if (
                w.validator_key == inv.validator_key
                and w.subnet_id == inv.subnet_id
                and w.alpha == inv.alpha
                and w.discount_bps == inv.discount_bps
                and w.amount_rao == inv.amount_rao
                and w.pay_window_start_block == inv.pay_window_start_block
                and w.pay_window_end_block == inv.pay_window_end_block
            ):
                if inv.pay_window_end_block and not w.pay_window_end_block:
                    w.pay_window_end_block = inv.pay_window_end_block
                w.alpha_requested = inv.alpha_requested
                w.was_partial = inv.was_partial
                w.pay_epoch_index = inv.pay_epoch_index or w.pay_epoch_index
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

        # Schedule payment task (no daemon)
        self._schedule_payment(inv)
        self._status_tables()

        synapse.ack = True
        # Latest known status; actual payment occurs in the task.
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
