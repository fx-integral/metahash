#!/usr/bin/env python3
# neurons/background_payment_miner.py — payments + background retries

import asyncio
import time
from typing import Dict, List, Optional

import bittensor as bt

from metahash.miner.models import WinInvoice
from metahash.miner.mixins import MinerMixins

from metahash.base.miner import BaseMinerNeuron
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.utils.pretty_logs import pretty
from metahash.utils.wallet_utils import transfer_alpha
from metahash.config import PLANCK, PAYMENT_TICK_SLEEP_SECONDS


class BackgroundPaymentMiner(BaseMinerNeuron, MinerMixins):
    """
    Adds:
      - Payment attempt logic with duplicate-guard.
      - Background retry daemon (runs on axon loop).
      - NEW: configurable payment sources (origin hotkeys), via CLI:
          • --payment.validators <hk1> <hk2> ...
              Round-robin pool used as fallback if no subnet-specific mapping.
          • --payment.map <sid:hotkey> <sid:hotkey> ...
              Explicit subnet_id → hotkey mapping (takes precedence).

    Expects the host to initialize:
      - _pay_lock, _state_lock, _async_subtensor, _loop, _payment_retry_task, _retry_interval_s
      - _wins, _treasuries, _already_bid
    """

    # ---------------------- background task lifecycle ----------------------

    async def _ensure_background_tasks(self):
        """
        Ensures the payment retry daemon is running on the axon loop.
        Call this from inside async handlers once the loop exists.
        """
        await self._ensure_async_subtensor()
        self._ensure_payment_source_config()

        if getattr(self, "_loop", None) is None:
            return  # defensive

        if getattr(self, "_payment_retry_task", None) is None or self._payment_retry_task.done():
            self._payment_retry_task = asyncio.create_task(self._payment_retry_daemon(), name="payment_retry_daemon")
            pretty.log("[cyan]Started payment retry daemon.[/cyan]")

    def _cancel_background_tasks(self):
        task = getattr(self, "_payment_retry_task", None)
        if task and not task.done():
            task.cancel()

    def __del__(self):
        try:
            self._cancel_background_tasks()
        except Exception:
            pass

    # ---------------------- payment source selection (NEW) ----------------------

    def _ensure_payment_source_config(self):
        """Load CLI-provided payment sources exactly once."""
        if getattr(self, "_pay_cfg_initialized", False):
            return

        self._pay_rr_index: int = 0
        self._pay_pool: List[str] = []
        self._pay_map: Dict[int, str] = {}

        # Nested config style like other flags (e.g., blacklist.force_validator_permit)
        pay_cfg = getattr(self.config, "payment", None)

        # --payment.validators hk1 hk2 ...
        try:
            pool = list(getattr(pay_cfg, "validators", [])) if pay_cfg is not None else []
            self._pay_pool = [hk.strip() for hk in pool if isinstance(hk, str) and hk.strip()]
        except Exception:
            self._pay_pool = []

        # --payment.map sid:hk sid:hk ...
        try:
            pairs = list(getattr(pay_cfg, "map", [])) if pay_cfg is not None else []
            for item in pairs:
                if not isinstance(item, str):
                    continue
                if ":" not in item:
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
        except Exception:
            self._pay_map = {}

        # Log a quick summary to avoid confusion at runtime
        if self._pay_map:
            pretty.log(f"[green]Payment map loaded[/green]: {len(self._pay_map)} subnet-specific origin hotkey(s).")
        if self._pay_pool:
            pretty.log(f"[green]Payment pool loaded[/green]: {len(self._pay_pool)} round-robin origin hotkey(s).")
        if not self._pay_map and not self._pay_pool:
            pretty.log("[yellow]No --payment.map or --payment.validators provided; using local wallet hotkey as origin.[/yellow]")

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

    # ---------------------- payments ----------------------

    async def _attempt_payment(self, inv: WinInvoice):
        """
        Attempts the payment if we are in the allowed block window.
        FIX: prevent duplicate spend by re-checking inv.paid after acquiring the lock.
        Also re-check window under the lock to avoid racing with short windows.
        NEW: choose origin hotkey from CLI configuration (map → rr pool → local).
        """
        # Quick pre-checks (no lock)
        if inv.paid:
            return

        blk = await self._get_current_block()
        if inv.pay_window_start_block and (blk <= 0 or blk < inv.pay_window_start_block):
            inv.last_response = f"not yet in window (blk {blk} < start {inv.pay_window_start_block})"
            return
        if inv.pay_window_end_block and blk > inv.pay_window_end_block:
            inv.last_response = f"window over (blk {blk} > end {inv.pay_window_end_block})"
            return

        async with self._pay_lock:
            # Re-check under the lock to avoid duplicate transfers
            if inv.paid:
                inv.last_response = "already paid"
                return

            # Re-check the window with a fresh block height
            blk2 = await self._get_current_block()
            if inv.pay_window_start_block and (blk2 <= 0 or blk2 < inv.pay_window_start_block):
                inv.last_response = f"not yet in window (blk {blk2} < start {inv.pay_window_start_block})"
                return
            if inv.pay_window_end_block and blk2 > inv.pay_window_end_block:
                inv.last_response = f"window over (blk {blk2} > end {inv.pay_window_end_block})"
                return

            # Decide origin hotkey (where α is staked)
            origin_hotkey = self._origin_hotkey_for_invoice(inv)

            try:
                ok = await transfer_alpha(
                    subtensor=self._async_subtensor,
                    wallet=self.wallet,
                    hotkey_ss58=origin_hotkey,  # <<<<<<<<<<<<< CHANGED: origin source
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
            inv.last_attempt_ts = time.time()
            inv.last_response = str(resp)[:300]

            if ok:
                inv.paid = True
                pretty.log(
                    f"[green]Payment OK[/green] "
                    f"{inv.amount_rao/PLANCK:.4f} α → {inv.treasury_coldkey[:8]}… "
                    f"(src_hotkey={origin_hotkey[:8]}… sid={inv.subnet_id} "
                    f"seen_e={inv.epoch_seen}, pay_e={inv.pay_epoch_index}, inv={inv.invoice_id}, partial={inv.was_partial})"
                )
            else:
                pretty.log(
                    f"[red]Payment FAILED[/red] resp={resp} "
                    f"(src_hotkey={origin_hotkey[:8]}… sid={inv.subnet_id} "
                    f"seen_e={inv.epoch_seen}, pay_e={inv.pay_epoch_index}, inv={inv.invoice_id}, partial={inv.was_partial})"
                )

        await self._async_save_state()

    async def _retry_unpaid_invoices(self):
        pend = [w for w in self._wins if not w.paid]
        if not pend:
            return
        pretty.log(f"[cyan]Retrying {len(pend)} unpaid invoice(s) at epoch head / daemon tick…[/cyan]")
        for w in pend:
            await self._attempt_payment(w)

    # ---------------------- aggregation & payment retry daemon ----------------------

    async def _payment_retry_tick(self):
        """
        One pass:
          - ensures AsyncSubtensor is ready,
          - retries unpaid invoices (if window allows),
          - prints status tables and aggregate per-subnet summary,
          - persists state.
        """
        await self._ensure_async_subtensor()
        self._ensure_payment_source_config()
        await self._retry_unpaid_invoices()
        self._status_tables()
        self._log_aggregate_summary()
        await self._async_save_state()

    async def _payment_retry_daemon(self):
        """
        Runs forever on the neuron's asyncio loop.
        Never dies on tick exceptions.
        """
        while True:
            try:
                await self._payment_retry_tick()
            except asyncio.CancelledError:
                pretty.log("[grey]Payment retry daemon cancelled (shutdown).[/grey]")
                raise
            except Exception as e:
                clog.warning(f"[daemon] unexpected error on tick: {e}", color="yellow")
            # keep cadence short so we don't miss short windows (e.g. 10 blocks)
            await asyncio.sleep(getattr(self, "_retry_interval_s", PAYMENT_TICK_SLEEP_SECONDS))
