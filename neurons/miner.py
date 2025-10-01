#!/usr/bin/env python3
# neurons/miner.py — Event-driven bidder v2.8.2


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
from metahash.treasuries import VALIDATOR_TREASURIES


class Miner(BaseMinerNeuron, MinerMixins):
    def __init__(self, config=None):
        super().__init__(config=config)

        # Local persistent state
        self._state_file = Path("miner_state.json")

        # SECURITY: Treasuries are pinned to the local allowlist. Do *not* mutate
        # from network input or persisted state. We overwrite any restored version
        # right after load to avoid stale/poisoned data.
        self._treasuries: Dict[str, str] = {}

        # Remember if we have already bid to a validator in an epoch
        # validator_key -> list of (epoch, subnet, alpha, discount_bps)
        self._already_bid: Dict[str, List[Tuple[int, int, float, int]]] = {}
        self._wins: List[WinInvoice] = []

        # Guards
        self._pay_lock: asyncio.Lock = asyncio.Lock()    # prevent concurrent tx on-chain
        self._state_lock: asyncio.Lock = asyncio.Lock()  # serialize state writes

        # Async subtensor bound to axon loop
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None

        # Per-invoice tasks (no global daemon)
        self._payment_tasks: Dict[str, asyncio.Task] = {}

        # Discount mode: default False (use effective-discount transformation)
        # Will be finalized by _build_lines_from_config()
        self._bids_raw_discount: bool = False

        # Optional: start fresh
        if getattr(self.config, "fresh", False):
            self._wipe_state()
            pretty.log("[magenta]Fresh start requested: cleared local miner state.[/magenta]")

        # Load persisted state (wins, etc.). Immediately re-pin treasuries.
        self._load_state_file()
        self._treasuries = dict(VALIDATOR_TREASURIES)

        # Build bid lines from CLI/config
        self.lines: List[BidLine] = self._build_lines_from_config()

        pretty.banner(
            "Miner started",
            f"uid={self.uid} | hotkey={self.wallet.hotkey.ss58_address} | lines={len(self.lines)} | epoch(e)={getattr(self, 'epoch_index', 0)}",
            style="bold magenta",
        )
        self._log_cfg_summary()
        pretty.kv_panel(
            "[magenta]Local treasuries loaded[/magenta]",
            [("allowlisted_validators", len(self._treasuries))],
            style="bold magenta",
        )

        unlock_wallet(wallet=self.wallet)

        # Payment source / retry config
        self._pay_cfg_initialized: bool = False
        self._pay_rr_index: int = 0
        self._pay_pool: List[str] = []
        self._pay_map: Dict[int, str] = {}
        self._pay_start_safety_blocks: int = 0
        self._retry_every_blocks: int = 2
        self._retry_max_attempts: int = 12

        # === EAGER scheduling of unpaid invoices (immediate, sync path) ===
        try:
            self._ensure_payment_config()
            # Ensure we can pay as soon as possible (async subtensor will be made by workers)
            self._schedule_unpaid_pending()
            pretty.log("[green]Startup: eagerly scheduled unpaid invoices (no waiting).[/green]")
        except Exception as _e:
            pretty.log(f"[yellow]Eager scheduling skipped:[/yellow] {_e}")

        # --- Also schedule the background resume + watchdog (idempotent) ---
        self._safe_create_task(self._resume_pending_payments(), name="resume_pending_payments")
        self._safe_create_task(self._pending_payments_watchdog(), name="payments_watchdog")

    # ---------------------- small task helpers ----------------------

    def _safe_create_task(self, coro, *, name: Optional[str] = None):
        """
        Schedule `coro` even if we're still starting up and no event loop exists yet.
        Avoids deprecated get_event_loop() warning and guarantees task creation.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        loop.call_soon(loop.create_task, coro, name=name)

    async def _resume_pending_payments(self):
        """Run once at startup to (re)schedule unpaid invoices again (harmless if already scheduled)."""
        try:
            self._ensure_payment_config()
            self._schedule_unpaid_pending()
            pretty.log("[green]Startup: resumed scheduling of unpaid invoices.[/green]")
        except Exception as e:
            pretty.log(f"[yellow]Startup resume failed:[/yellow] {e}")

    async def _pending_payments_watchdog(self):
        """
        Very light watchdog: every ~1 block, re-run the scheduler to catch any
        invoices that might have been added/merged after startup.
        This is idempotent because _schedule_payment() checks for existing tasks.
        """
        try:
            while True:
                self._schedule_unpaid_pending()
                await asyncio.sleep(max(0.5, float(BLOCKTIME)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            pretty.log(f"[yellow]Payments watchdog error:[/yellow] {e}")

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

    # ---------------------- allowlist helpers ----------------------

    def _lookup_local_treasury(
        self,
        caller_hot: Optional[str],
        vkey: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Return (treasury_coldkey, matched_validator_key) if the validator is allowlisted,
        else (None, None). We try the caller's hotkey first, then vkey fallback.

        NOTE: `vkey` is what self._validator_key(uid, caller_hot) returns; depending on
        upstream, it may be the hotkey or some derived key. The allowlist uses hotkeys.
        """
        # Prefer direct hotkey match
        if isinstance(caller_hot, str) and caller_hot:
            ck = self._treasuries.get(caller_hot)
            if ck:
                return ck, caller_hot

        # Fallback to whatever `vkey` is, in case your allowlist chose that string
        if isinstance(vkey, str) and vkey:
            ck = self._treasuries.get(vkey)
            if ck:
                return ck, vkey

        return None, None

    def _allowed_validator_note(self, caller_hot: Optional[str], vkey: str) -> str:
        key_preview = (caller_hot or vkey or "?")
        return f"validator not allowlisted: {key_preview}"

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
        """Schedule tasks for all unpaid invoices (idempotent), allowlist-enforced."""
        for w in self._wins:
            if w.paid:
                continue
            # Only schedule if validator is still allowlisted (safety on restarts)
            treasury_ck, _matched = self._lookup_local_treasury(None, w.validator_key)
            if treasury_ck:
                # Update the pinned treasury on the invoice to match local file
                w.treasury_coldkey = treasury_ck
                self._schedule_payment(w)
            else:
                pretty.kv_panel(
                    "[yellow]Skipping pending invoice[/yellow]",
                    [("inv", getattr(w, "invoice_id", "?")), ("reason", "validator not allowlisted")],
                    style="bold yellow",
                )

    # ---------------------- discount math (effective -> raw) ----------------------

    @staticmethod
    def _clamp_bps(x: int) -> int:
        return 0 if x < 0 else (10_000 if x > 10_000 else x)

    @staticmethod
    def _compute_raw_discount_bps(effective_factor_bps: int, weight_bps: int) -> int:
        """
        Given:
          - effective_factor_bps in [0..10_000], representing:  effective_factor = weight * (1 - raw_discount)
          - weight_bps in [0..10_000], representing the validator's subnet weight

        Solve for raw_discount (bps) to send to the validator.

        raw_discount = 1 - (effective_factor / weight)   [clamped to 0..1]
        """
        w_bps = Miner._clamp_bps(int(weight_bps or 0))
        ef_bps = Miner._clamp_bps(int(effective_factor_bps or 0))

        if w_bps <= 0:
            # No weight → product will be 0 regardless. Best we can do is not self-penalize.
            return 0

        w = w_bps / 10_000.0
        ef = ef_bps / 10_000.0

        raw = 1.0 - (ef / w)
        if raw < 0.0:
            raw = 0.0
        elif raw > 1.0:
            raw = 1.0
        return int(round(raw * 10_000))

    # ---------------------- auction handlers ----------------------

    async def auctionstart_forward(self, synapse: AuctionStartSynapse) -> AuctionStartSynapse:
        await self._ensure_async_subtensor()
        self._ensure_payment_config()

        uid, caller_hot = self._resolve_caller(synapse)
        vkey = self._validator_key(uid, caller_hot)
        epoch = int(synapse.epoch_index)

        # SECURITY: Never trust synapse.treasury_coldkey. We only use local allowlist.
        treasury_ck, matched_key = self._lookup_local_treasury(caller_hot, vkey)
        if not treasury_ck:
            # Do not bid for unknown validators
            note = self._allowed_validator_note(caller_hot, vkey)
            pretty.kv_panel(
                "[red]AuctionStart ignored[/red]",
                [("validator", f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})"),
                 ("reason", "not allowlisted"),
                 ("note", note)],
                style="bold red"
            )
            synapse.ack = True  # we answered, but with no bids
            synapse.bids = []
            synapse.bids_sent = 0
            synapse.note = note
            return synapse

        budget_alpha = float(getattr(synapse, "auction_budget_alpha", 0.0) or 0.0)
        min_stake_alpha = float(getattr(synapse, "min_stake_alpha", S_MIN_ALPHA_MINER) or S_MIN_ALPHA_MINER)

        # Weights (bps) sent by validator for this auction
        weights_bps_raw = getattr(synapse, "weights_bps", {}) or {}
        weights_bps: Dict[int, int] = {}
        try:
            for k, v in list(weights_bps_raw.items()):
                try:
                    kk = int(k)
                except Exception:
                    kk = k
                try:
                    vv = int(v)
                except Exception:
                    vv = 0
                weights_bps[int(kk)] = self._clamp_bps(vv)
        except Exception:
            weights_bps = {}

        pretty.kv_panel(
            "[cyan]AuctionStart[/cyan]",
            [
                ("validator", f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})"),
                ("epoch (e)", epoch),
                ("budget α", f"{budget_alpha:.3f}"),
                ("min_stake", f"{min_stake_alpha:.3f} α"),
                ("weights", f"{len(weights_bps)} subnet(s)"),
                ("discount_mode", "RAW (pass-through)" if self._bids_raw_discount else "EFFECTIVE (weight-adjusted)"),
                ("timeline", f"bid now (e) → pay in (e+1={epoch+1}) → weights from e in (e+2={epoch+2})"),
                ("treasury_src", "LOCAL allowlist (pinned)"),
            ],
            style="bold cyan",
        )

        # (Re)schedule any pending unpaid invoices we may have persisted (allowlist will filter)
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

            # Determine the "raw" discount to send (possibly transformed)
            subnet_id = int(ln.subnet_id)
            weight_bps = weights_bps.get(subnet_id, 10_000)  # default to 1.0 if absent

            if self._bids_raw_discount:
                send_disc_bps = int(ln.discount_bps)
                eff_factor_bps = int(round(weight_bps * (1.0 - (send_disc_bps / 10_000.0))))
                mode_note = "raw"
            else:
                # ln.discount_bps is interpreted as the EFFECTIVE multiplier (weight * (1 - raw_discount))
                send_disc_bps = self._compute_raw_discount_bps(ln.discount_bps, weight_bps)
                eff_factor_bps = int(round(weight_bps * (1.0 - (send_disc_bps / 10_000.0))))
                mode_note = "effective→raw"

            # Skip duplicates for this validator in this epoch with the *actual* discount we will send
            if self._has_bid(vkey, epoch, subnet_id, ln.alpha, send_disc_bps):
                continue

            if budget_alpha > 0 and ln.alpha > budget_alpha:
                pretty.log(
                    f"[grey58]Note:[/grey58] line α {ln.alpha:.4f} exceeds validator budget α {budget_alpha:.4f}; may be partially filled."
                )

            bid_id = self._make_bid_id(vkey, epoch, subnet_id, ln.alpha, send_disc_bps)
            out_bids.append({
                "subnet_id": subnet_id,
                "alpha": float(ln.alpha),
                "discount_bps": int(send_disc_bps),
                "bid_id": bid_id,
            })
            self._remember_bid(vkey, epoch, subnet_id, ln.alpha, send_disc_bps)
            sent += 1

            # Log: show cfg discount, sent (raw) discount, weight, and implied effective factor
            rows_sent.append([
                subnet_id,
                f"{ln.alpha:.4f} α",
                f"{ln.discount_bps} bps" + (" (cfg)" if not self._bids_raw_discount else ""),
                f"{send_disc_bps} bps",
                f"{weight_bps} w_bps",
                f"{eff_factor_bps} eff_bps",
                bid_id,
                epoch,
                mode_note,
            ])

        if sent == 0:
            pretty.log("[grey]No bids were added (all lines either invalid or already added).[/grey]")
        else:
            pretty.table(
                "[yellow]Bids Sent[/yellow]",
                ["Subnet", "Alpha", "Disc(cfg)", "Disc(send)", "Weight", "Eff", "BidID", "Epoch", "Mode"],
                rows_sent,
            )

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

        # SECURITY: Use local allowlist only (ignore synapse-provided treasury).
        treasury_ck, matched_key = self._lookup_local_treasury(caller_hot, vkey)
        if not treasury_ck:
            note = self._allowed_validator_note(caller_hot, vkey)
            pretty.kv_panel(
                "[red]Win ignored[/red]",
                [("validator", f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})"),
                 ("reason", "not allowlisted"),
                 ("note", note)],
                style="bold red",
            )
            synapse.ack = False
            synapse.payment_attempted = False
            synapse.payment_ok = False
            synapse.attempts = 0
            synapse.last_response = note
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
            treasury_coldkey=treasury_ck,  # <- pinned local value
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
                # SECURITY: ensure treasury is local-pinned even after merge
                w.treasury_coldkey = treasury_ck
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
                ("treasury_src", "LOCAL allowlist (pinned)"),
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
            _t.sleep(12)
