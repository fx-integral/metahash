#!/usr/bin/env python3
# neurons/background_payment_miner.py — payments + background retries

import asyncio
import time

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

    # ---------------------- payments ----------------------

    async def _attempt_payment(self, inv: WinInvoice):
        """
        Attempts the payment if we are in the allowed block window.
        FIX: prevent duplicate spend by re-checking inv.paid after acquiring the lock.
        Also re-check window under the lock to avoid racing with short windows.
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

            try:
                print(inv)
                ok = await transfer_alpha(
                    subtensor=self._async_subtensor,
                    wallet=self.wallet,
                    hotkey_ss58=self.wallet.hotkey.ss58_address,
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
                    f"(seen_e={inv.epoch_seen}, pay_e={inv.pay_epoch_index}, inv={inv.invoice_id}, partial={inv.was_partial})"
                )
            else:
                pretty.log(
                    f"[red]Payment FAILED[/red] resp={resp} "
                    f"(seen_e={inv.epoch_seen}, pay_e={inv.pay_epoch_index}, inv={inv.invoice_id}, partial={inv.was_partial})"
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
