# metahash/miner/payments.py
from __future__ import annotations

import asyncio
import time
import inspect
from concurrent.futures import Future
from typing import Dict, List, Optional, Any, Coroutine

import bittensor as bt
from bittensor import BLOCKTIME

from metahash.config import PLANCK
from metahash.utils.pretty_logs import pretty
from metahash.utils.wallet_utils import transfer_alpha
from metahash.miner.logging import (
    MinerPhase, LogLevel, miner_logger, 
    log_init, log_auction, log_commitments, log_settlement
)

from metahash.protocol import WinSynapse
from metahash.miner.models import WinInvoice
from metahash.miner.state import StateStore
from metahash.miner.runtime import Runtime


def _as_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        if isinstance(x, bool):
            return int(x)
        if isinstance(x, int):
            return x
        if isinstance(x, float):
            return int(x)
        s = str(x).strip()
        return int(float(s))
    except Exception:
        return default


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        return float(s)
    except Exception:
        return default


class Payments:
    """
    Background loop + payment scheduling/worker with α balance pre-check.
    """

    def __init__(self, config, wallet, runtime: Runtime, state: StateStore):
        self.config = config
        self.wallet = wallet
        self.runtime = runtime
        self.state = state

        # Background asyncio loop (daemon)
        self._bg_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._bg_thread = None

        # Async primitives (created on loop)
        self._pay_lock: Optional[asyncio.Lock] = None
        self._tasks: Dict[str, Future[Any]] = {}

        # Payment config
        self._pay_cfg_initialized: bool = False
        self._pay_rr_index: int = 0
        self._pay_pool: List[str] = []
        self._pay_map: Dict[int, str] = {}
        self._pay_start_safety_blocks: int = 0
        self._retry_every_blocks: int = 2
        self._retry_max_attempts: int = 3
        

    # ---------------------- Background loop ----------------------

    def _run_bg_loop(self):
        asyncio.set_event_loop(self._bg_loop)
        self._pay_lock = asyncio.Lock()
        self._bg_loop.run_forever()

    def start_background_tasks(self):
        if self._bg_thread is None:
            import threading
            self._bg_thread = threading.Thread(target=self._run_bg_loop, name="miner-payments-loop", daemon=True)
            self._bg_thread.start()
            log_init(LogLevel.MEDIUM, "Background payment thread started", "payments")
        # Fresh-start safety: when --fresh is set, ensure no stale invoices remain
        # and skip auto-resume of pending payments for this boot.
        if bool(getattr(self.config, "fresh", False)):
            try:
                # Clear any loaded wins (payments) so miner truly starts fresh
                self.state.wins.clear()
                # Persist the cleared state
                self.submit(self.state.save_async())
                log_init(LogLevel.MEDIUM, "--fresh: cleared in-memory wins and skipped resume", "payments")
            except Exception:
                pass
        else:
            # resume unpaid + watchdog
            self.submit(self._resume_pending_payments())
        self.submit(self._pending_payments_watchdog())

    def shutdown_background(self):
        log_init(LogLevel.MEDIUM, "Shutting down background payment tasks", "payments")
        for fut in list(self._tasks.values()):
            try:
                fut.cancel()
            except Exception:
                pass
        try:
            if self._bg_loop and not self._bg_loop.is_closed():
                self._bg_loop.call_soon_threadsafe(self._bg_loop.stop)
            if self._bg_thread and self._bg_thread.is_alive():
                self._bg_thread.join(timeout=2)
        except Exception as e:
            log_init(LogLevel.HIGH, "Error during background task shutdown", "payments", {"error": str(e)})

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future[Any]:
        if not asyncio.iscoroutine(coro):
            raise TypeError("Expected coroutine in submit()")
        if not self._bg_loop or self._bg_loop.is_closed():
            raise RuntimeError("Background loop not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._bg_loop)

        def _log_exc(f: Future[Any]) -> None:
            try:
                f.result()
            except Exception as e:
                pretty.log(f"[yellow]Unhandled task exception[/yellow]: {e}")
        fut.add_done_callback(_log_exc)
        return fut

    # ---------------------- Payment config ----------------------

    def ensure_payment_config(self):
        if self._pay_cfg_initialized:
            return
        log_init(LogLevel.MEDIUM, "Loading payment configuration", "payments")
        pay_cfg = getattr(self.config, "payment", None)

        # --payment.validators hk1 hk2 ...
        try:
            pool = list(getattr(pay_cfg, "validators", [])) if pay_cfg is not None else []
            self._pay_pool = [hk.strip() for hk in pool if isinstance(hk, str) and hk.strip()]
        except Exception as e:
            self._pay_pool = []
            log_init(LogLevel.HIGH, "Failed to load payment pool", "payments", {"error": str(e)})

        # --payment.map sid:hk sid:hk ...
        try:
            pairs = list(getattr(pay_cfg, "map", [])) if pay_cfg is not None else []
            for item in pairs:
                if not isinstance(item, str) or ":" not in item:
                    continue
                sid_s, hk = item.split(":", 1)
                try:
                    sid = int(sid_s.strip())
                except Exception:
                    continue
                self._pay_map[sid] = hk.strip()
        except Exception as e:
            self._pay_map = {}
            log_init(LogLevel.HIGH, "Failed to load payment map", "payments", {"error": str(e)})

        # Start safety / retry knobs
        try:
            self._pay_start_safety_blocks = max(0, int(getattr(pay_cfg, "start_safety_blocks", 0) or 0))
        except Exception as e:
            self._pay_start_safety_blocks = 0
            log_init(LogLevel.HIGH, "Failed to load payment start safety", "payments", {"error": str(e)})

        try:
            self._retry_every_blocks = max(1, int(getattr(pay_cfg, "retry_every_blocks", 2) or 2))
        except Exception:
            self._retry_every_blocks = 2

        try:
            self._retry_max_attempts = max(1, int(getattr(pay_cfg, "retry_max_attempts", 3) or 3))
        except Exception:
            self._retry_max_attempts = 3

        # Default payment hotkeys from --miner.bids.validators if no explicit pool set
        if not self._pay_pool:
            try:
                cfg_miner = getattr(self.config, "miner", None)
                cfg_bids = getattr(cfg_miner, "bids", cfg_miner)
                bids_validators = [hk.strip() for hk in list(getattr(cfg_bids, "validators", []) or []) if isinstance(hk, str) and hk.strip()]
                if bids_validators:
                    self._pay_pool = bids_validators
            except Exception:
                pass

        if not self._pay_map and not self._pay_pool and not self._pay_start_safety_blocks:
            log_init(LogLevel.HIGH, "No payment configuration found - using defaults", "payments")

        self._pay_cfg_initialized = True
        log_init(LogLevel.MEDIUM, "Payment configuration loaded successfully", "payments", {
            "pool_size": len(self._pay_pool),
            "map_size": len(self._pay_map),
            "safety_blocks": self._pay_start_safety_blocks,
            "retry_every_blocks": self._retry_every_blocks,
            "retry_max_attempts": self._retry_max_attempts
        })

    def _pick_rr_hotkey(self) -> str:
        if not self._pay_pool:
            return self.wallet.hotkey.ss58_address
        hk = self._pay_pool[self._pay_rr_index % len(self._pay_pool)]
        self._pay_rr_index += 1
        return hk

    def _origin_hotkey_for_invoice(self, inv: WinInvoice) -> str:
        sid = getattr(inv, "subnet_id", None)
        if isinstance(sid, int) and sid in self._pay_map:
            return self._pay_map[sid]
        return self._pick_rr_hotkey()

    # ---------------------- Schedulers ----------------------

    async def _resume_pending_payments(self):
        try:
            self.ensure_payment_config()
            self.schedule_unpaid_pending()
        except Exception as e:
            log_init(LogLevel.HIGH, "Startup resume failed", "payments", {"error": str(e)})

    async def _pending_payments_watchdog(self):
        try:
            while True:
                self.schedule_unpaid_pending()
                await asyncio.sleep(max(0.5, float(BLOCKTIME)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_settlement(LogLevel.HIGH, "Payments watchdog error", "payments", {"error": str(e)})

    def schedule_unpaid_pending(self):
        """Idempotent: schedules only invoices that are not paid and not expired."""
        scheduled_count = 0
        skipped_count = 0
        for w in self.state.wins:
            if bool(getattr(w, "paid", False) or getattr(w, "expired", False)):
                continue
            treasury_ck = self.state.treasuries.get(w.validator_key) or self.state.treasuries.get(getattr(w, "validator_key", ""))
            if not treasury_ck:
                log_settlement(LogLevel.HIGH, "Skipping pending invoice - validator not allowlisted", "scheduling", {
                    "invoice_id": getattr(w, "invoice_id", "?"),
                    "validator_key": w.validator_key
                })
                miner_logger.phase_panel(
                    MinerPhase.SETTLEMENT, "Skipping Pending Invoice",
                    [("inv", getattr(w, "invoice_id", "?")), ("reason", "validator not allowlisted")],
                    LogLevel.HIGH
                )
                skipped_count += 1
                continue
            if w.treasury_coldkey != treasury_ck:
                w.treasury_coldkey = treasury_ck
            self._schedule_payment(w)
            scheduled_count += 1
        

    def _schedule_payment(self, inv: WinInvoice):
        tid = inv.invoice_id
        t = self._tasks.get(tid)
        if t and not t.done():
            return

        inv.last_response = f"scheduled (wait ≥{self._pay_start_safety_blocks} blk)"
        self.submit(self.state.save_async())

        fut = self.submit(self._payment_worker(inv))
        self._tasks[tid] = fut

        # Enhanced payment scheduling information
        log_settlement(LogLevel.MEDIUM, "Payment scheduled successfully", "scheduling", {
            "invoice_id": inv.invoice_id,
            "subnet_id": inv.subnet_id,
            "amount_alpha": inv.amount_rao/PLANCK
        })
        
        # Use the new clean payment summary method
        miner_logger.payment_summary(
            invoice_id=inv.invoice_id,
            subnet_id=inv.subnet_id,
            amount_alpha=inv.amount_rao/PLANCK,
            window=f"[{inv.pay_window_start_block},{inv.pay_window_end_block or '?'}]",
            safety_blocks=self._pay_start_safety_blocks,
            retry_every=self._retry_every_blocks,
            retry_max=self._retry_max_attempts,
            treasury=inv.treasury_coldkey
        )

    # ---------------------- Worker ----------------------

    async def _maybe_transfer_alpha(self, **kwargs) -> Any:
        try:
            result = transfer_alpha(**kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except TypeError:
            return await transfer_alpha(**kwargs)  # type: ignore[misc]

    async def _payment_worker(self, inv: WinInvoice):
        try:
            await self.runtime._ensure_async_subtensor()
            
            # Log network configuration for debugging
            network = getattr(self.runtime._async_subtensor, 'network', 'unknown')
            log_settlement(LogLevel.MEDIUM, "Payment worker network configuration", "worker", {
                "invoice_id": inv.invoice_id,
                "network": network,
                "subnet_id": inv.subnet_id
            })

            start = int(inv.pay_window_start_block or 0)
            end = int(inv.pay_window_end_block or 0)
            allowed_start = start + int(self._pay_start_safety_blocks or 0)

            log_settlement(LogLevel.MEDIUM, "Payment worker started", "worker", {
                "invoice_id": inv.invoice_id,
                "subnet_id": inv.subnet_id,
                "amount_alpha": inv.amount_rao/PLANCK,
                "window_start": start,
                "window_end": end,
                "allowed_start": allowed_start
            })

            attempt = 0
            block_fetch_failures = 0
            max_block_fetch_failures = 10
            worker_start_time = time.time()
            max_worker_runtime = 1800  # 30 minutes max runtime per worker (reduced from 1 hour)
            
            while True:
                # Check if worker has been running too long
                if time.time() - worker_start_time > max_worker_runtime:
                    inv.last_response = f"worker timeout after {max_worker_runtime}s"
                    inv.expired = True
                    await self.state.save_async()
                    log_settlement(LogLevel.HIGH, "Payment worker timeout", "worker", {
                        "invoice_id": inv.invoice_id,
                        "runtime_seconds": time.time() - worker_start_time
                    })
                    miner_logger.phase_panel(
                        MinerPhase.SETTLEMENT, "PAY Exit",
                        [("inv", inv.invoice_id), ("reason", "worker timeout"), ("runtime", f"{time.time() - worker_start_time:.1f}s")],
                        LogLevel.HIGH
                    )
                    break
                blk = await self.runtime.get_current_block()
                
                # Handle block fetch failures
                if blk <= 0:
                    block_fetch_failures += 1
                    if block_fetch_failures >= max_block_fetch_failures:
                        inv.last_response = f"failed to fetch current block after {max_block_fetch_failures} attempts"
                        inv.expired = True
                        await self.state.save_async()
                        log_settlement(LogLevel.HIGH, "Payment failed - cannot fetch current block", "worker", {
                            "invoice_id": inv.invoice_id,
                            "failures": block_fetch_failures
                        })
                        miner_logger.phase_panel(
                            MinerPhase.SETTLEMENT, "PAY Exit",
                            [("inv", inv.invoice_id), ("reason", "block fetch failed"), ("failures", block_fetch_failures)],
                            LogLevel.HIGH
                        )
                        break
                    
                    inv.last_response = f"block fetch failed ({block_fetch_failures}/{max_block_fetch_failures}), retrying..."
                    await self.state.save_async()
                    log_settlement(LogLevel.HIGH, "Block fetch failed, retrying", "worker", {
                        "invoice_id": inv.invoice_id,
                        "current_block": blk,
                        "failures": block_fetch_failures
                    })
                    await asyncio.sleep(max(0.5, float(BLOCKTIME) * 2))
                    continue
                
                # Reset block fetch failure counter on success
                block_fetch_failures = 0

                if start and blk < allowed_start:
                    inv.last_response = f"waiting (blk {blk} < start {allowed_start})"
                    await self.state.save_async()
                    sleep_blocks = max(1, allowed_start - blk)
                    sleep_s = max(0.5, min(10 * float(BLOCKTIME), sleep_blocks * float(BLOCKTIME)))
                    await asyncio.sleep(sleep_s)
                    continue

                if end and blk > end:
                    inv.last_response = f"window over (blk {blk} > end {end})"
                    inv.expired = True
                    await self.state.save_async()
                    log_settlement(LogLevel.HIGH, "Payment window expired", "worker", {
                        "invoice_id": inv.invoice_id,
                        "current_block": blk,
                        "window_end": end
                    })
                    miner_logger.phase_panel(
                        MinerPhase.SETTLEMENT, "PAY Exit",
                        [("inv", inv.invoice_id), ("reason", "window over"), ("blk", blk), ("end", end)],
                        LogLevel.HIGH
                    )
                    self.state.status_tables()
                    break

                attempt += 1
                inv.last_attempt_ts = time.time()
                await self.state.save_async()
                log_settlement(LogLevel.MEDIUM, "Payment attempt started", "worker", {
                    "invoice_id": inv.invoice_id,
                    "attempt": f"{attempt}/{self._retry_max_attempts}",
                    "current_block": blk,
                    "amount_alpha": inv.amount_rao/PLANCK
                })
                miner_logger.phase_panel(
                    MinerPhase.SETTLEMENT, "PAY Attempt",
                    [
                        ("invoice", inv.invoice_id),
                        ("attempt", f"{attempt}/{self._retry_max_attempts}"),
                        ("current_block", blk),
                        ("window", f"[{start},{end}]"),
                        ("subnet", f"SN-{inv.subnet_id}"),
                        ("amount", f"{inv.amount_rao/PLANCK:.4f} α"),
                    ],
                    LogLevel.MEDIUM
                )

                ok = False
                resp: Any = None
                tx_hash: Optional[str] = None
                paid_block: Optional[int] = None
                origin_hotkey = self._origin_hotkey_for_invoice(inv)

                need_alpha = float(inv.amount_rao) / float(PLANCK)
                bal_alpha = await self.runtime.get_alpha_balance(inv.subnet_id, origin_hotkey)
                if bal_alpha is not None and bal_alpha + 1e-12 < need_alpha:
                    inv.last_response = f"insufficient α on subnet {inv.subnet_id}: have {bal_alpha:.6f}, need {need_alpha:.6f}"
                    await self.state.save_async()
                    log_settlement(LogLevel.HIGH, "Insufficient balance for payment", "worker", {
                        "invoice_id": inv.invoice_id,
                        "subnet_id": inv.subnet_id,
                        "current_balance": bal_alpha,
                        "required_amount": need_alpha,
                        "deficit": need_alpha - bal_alpha
                    })
                    miner_logger.phase_panel(
                        MinerPhase.SETTLEMENT, "Insufficient Balance",
                        [
                            ("invoice", inv.invoice_id),
                            ("subnet", f"SN-{inv.subnet_id}"),
                            ("current_balance", f"{bal_alpha:.6f} α"),
                            ("required_amount", f"{need_alpha:.6f} α"),
                            ("deficit", f"{need_alpha - bal_alpha:.6f} α"),
                            ("hotkey", origin_hotkey[:8] + "…"),
                        ],
                        LogLevel.HIGH
                    )
                    await asyncio.sleep(max(0.5, float(BLOCKTIME) * float(self._retry_every_blocks)))
                    continue

                async with self.runtime._rpc_lock:  # type: ignore
                    blk2 = await self.runtime.get_current_block()
                    if start and (blk2 <= 0 or blk2 < allowed_start):
                        inv.last_response = f"not yet in window (blk {blk2} < start {start}+{self._pay_start_safety_blocks})"
                        ok = False
                        resp = inv.last_response
                    elif end and blk2 > end:
                        inv.last_response = f"window over (blk {blk2} > end {end})"
                        inv.expired = True
                        ok = False
                        resp = inv.last_response
                    elif inv.paid:
                        ok = True
                        resp = "already paid"
                    else:
                        try:
                            result = await self._maybe_transfer_alpha(
                                subtensor=self.runtime._async_subtensor,
                                wallet=self.wallet,
                                hotkey_ss58=origin_hotkey,
                                origin_and_dest_netuid=inv.subnet_id,
                                dest_coldkey_ss58=inv.treasury_coldkey,
                                amount=bt.Balance.from_rao(inv.amount_rao),
                                wait_for_inclusion=True,
                                wait_for_finalization=False,
                            )
                            if isinstance(result, bool):
                                ok = result
                            else:
                                ok = True
                                tx_hash = getattr(result, "extrinsic_hash", None) or getattr(result, "tx_hash", None) or getattr(result, "hash", None)
                                paid_block = getattr(result, "in_block", None) or getattr(result, "included_block", None)
                            resp = "ok" if ok else "rejected"
                        except Exception as exc:
                            ok = False
                            resp = f"exception: {exc}"

                inv.pay_attempts += 1
                inv.last_response = str(resp)[:300]

                if ok and inv.paid:
                    break

                if ok:
                    inv.paid = True
                    if tx_hash:
                        inv.tx_hash = tx_hash
                    if paid_block:
                        inv.paid_at_block = int(paid_block)
                    await self.state.save_async()
                    log_settlement(LogLevel.MEDIUM, "Payment successful", "worker", {
                        "invoice_id": inv.invoice_id,
                        "amount_alpha": inv.amount_rao/PLANCK,
                        "subnet_id": inv.subnet_id,
                        "attempts": inv.pay_attempts,
                        "tx_hash": tx_hash[:10] + "…" if tx_hash else None
                    })
                    # Use the new payment success summary method with green color
                    miner_logger.payment_success_summary(
                        invoice_id=inv.invoice_id,
                        amount_alpha=inv.amount_rao/PLANCK,
                        destination=inv.treasury_coldkey,
                        source=origin_hotkey,
                        subnet_id=inv.subnet_id,
                        pay_epoch=inv.pay_epoch_index,
                        tx_hash=getattr(inv, "tx_hash", None),
                        block=getattr(inv, "paid_at_block", None),
                        attempts=inv.pay_attempts
                    )
                    self.state.status_tables()
                    self.state.log_aggregate_summary()
                    break
                else:
                    await self.state.save_async()
                    log_settlement(LogLevel.HIGH, "Payment failed", "worker", {
                        "invoice_id": inv.invoice_id,
                        "response": inv.last_response[:80],
                        "subnet_id": inv.subnet_id,
                        "attempts": f"{inv.pay_attempts}/{self._retry_max_attempts}",
                        "amount_alpha": inv.amount_rao/PLANCK
                    })
                    # Use the new payment failed summary method with red color and cross icon
                    miner_logger.payment_failed_summary(
                        invoice_id=inv.invoice_id,
                        response=inv.last_response,
                        subnet_id=inv.subnet_id,
                        attempts=inv.pay_attempts,
                        max_attempts=self._retry_max_attempts,
                        amount_alpha=inv.amount_rao/PLANCK,
                        current_block=blk
                    )

                if attempt >= int(self._retry_max_attempts or 1):
                    inv.expired = True  # stop any future scheduling
                    # disable further bids on this subnet to avoid repeated wins without ability to pay
                    try:
                        self.state.disable_subnet(int(inv.subnet_id))
                    except Exception:
                        pass
                    inv.last_response = f"max attempts reached ({attempt}); marking invoice expired and disabling subnet {int(inv.subnet_id)}"
                    await self.state.save_async()
                    log_settlement(LogLevel.HIGH, "Payment max attempts reached", "worker", {
                        "invoice_id": inv.invoice_id,
                        "subnet_id": int(inv.subnet_id),
                        "attempts": attempt,
                        "action": "expired invoice; subnet disabled for bidding"
                    })
                    miner_logger.phase_panel(
                        MinerPhase.SETTLEMENT, "PAY Exit",
                        [("inv", inv.invoice_id), ("reason", "max attempts"), ("attempts", attempt), ("action", "expired+disable subnet")],
                        LogLevel.HIGH
                    )
                    break

                await asyncio.sleep(max(0.5, float(BLOCKTIME) * float(self._retry_every_blocks)))

        except asyncio.CancelledError:
            inv.last_response = "cancelled"
            await self.state.save_async()
            log_settlement(LogLevel.MEDIUM, "Payment worker cancelled", "worker", {
                "invoice_id": inv.invoice_id
            })
            miner_logger.phase_panel(
                MinerPhase.SETTLEMENT, "PAY Cancelled",
                [("inv", inv.invoice_id)],
                LogLevel.MEDIUM
            )
            raise
        except Exception as e:
            inv.last_response = f"error: {e}"
            await self.state.save_async()
            log_settlement(LogLevel.HIGH, "Payment worker error", "worker", {
                "invoice_id": getattr(inv, "invoice_id", "?"),
                "error": str(e)[:120]
            })
            miner_logger.phase_panel(
                MinerPhase.SETTLEMENT, "PAY Worker Error",
                [("inv", getattr(inv, "invoice_id", "?")), ("err", str(e)[:120])],
                LogLevel.HIGH
            )
            return
        finally:
            t = self._tasks.get(inv.invoice_id)
            try:
                if t and t.done():
                    self._tasks.pop(inv.invoice_id, None)
            except Exception:
                self._tasks.pop(inv.invoice_id, None)

    # ---------------------- Win handler ----------------------

    def _sanitize_win_synapse_numbers_inplace(self, syn: WinSynapse) -> None:
        for name in ("validator_uid", "subnet_id", "pay_window_start_block", "pay_window_end_block", "pay_epoch_index", "attempts"):
            if hasattr(syn, name):
                try:
                    setattr(syn, name, _as_int(getattr(syn, name)))
                except Exception:
                    pass
        for name in ("accepted_alpha", "requested_alpha", "alpha"):
            if hasattr(syn, name):
                try:
                    setattr(syn, name, _as_float(getattr(syn, name)))
                except Exception:
                    pass
        if hasattr(syn, "was_partially_accepted"):
            try:
                v = getattr(syn, "was_partially_accepted")
                setattr(syn, "was_partially_accepted", bool(v))
            except Exception:
                pass

    async def handle_win(self, synapse: WinSynapse) -> WinSynapse:
        """
        Non-blocking win handler that immediately returns response and schedules
        heavy processing (state saving, payment scheduling, logging) in background.
        """
        # Quick validation and basic processing
        self._sanitize_win_synapse_numbers_inplace(synapse)

        uid = getattr(synapse, "validator_uid", None)
        caller_hot = getattr(synapse, "validator_hotkey", None)
        if caller_hot is None:
            try:
                uid = int(getattr(getattr(synapse, "dendrite", None), "origin", None))
            except Exception:
                uid = uid
            if uid is not None and 0 <= uid < len(self.runtime.metagraph.axons):
                caller_hot = getattr(self.runtime.metagraph.axons[uid], "hotkey", None)
        vkey = caller_hot or (f"uid:{uid}" if uid is not None else "<unknown>")

        # Quick treasury validation
        treasury_ck = self.state.treasuries.get(caller_hot or "") or self.state.treasuries.get(vkey)
        if not treasury_ck:
            note = f"validator not allowlisted: {caller_hot or vkey or '?'}"
            synapse.ack = False
            synapse.payment_attempted = False
            synapse.payment_ok = False
            synapse.attempts = 0
            synapse.last_response = note
            return synapse

        # Extract basic win data
        accepted_alpha = _as_float(getattr(synapse, "accepted_alpha", None) or getattr(synapse, "alpha", 0.0))
        requested_alpha = _as_float(getattr(synapse, "requested_alpha", None) or accepted_alpha)
        was_partial = bool(getattr(synapse, "was_partially_accepted", accepted_alpha < requested_alpha - 1e-12))
        amount_rao = int(round(accepted_alpha * PLANCK))
        
        # Get current epoch from metagraph or use 0 as fallback
        try:
            epoch_now = int(getattr(self.runtime.metagraph, "current_epoch", 0) or 0)
        except Exception:
            epoch_now = 0

        pay_start = _as_int(getattr(synapse, "pay_window_start_block", 0))
        pay_end = _as_int(getattr(synapse, "pay_window_end_block", 0))
        pay_ep = _as_int(getattr(synapse, "pay_epoch_index", 0))
        clearing_bps = _as_int(getattr(synapse, "clearing_discount_bps", 0))

        # Create invoice
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

        import hashlib
        inv.invoice_id = hashlib.sha256(
            f"{vkey}|{inv.subnet_id}|{inv.alpha:.12f}|{inv.discount_bps}|{inv.amount_rao}|{inv.pay_window_start_block}|{inv.pay_window_end_block}|{inv.pay_epoch_index or 0}".encode("utf-8")
        ).hexdigest()[:12]

        # Check for existing invoice
        for w in self.state.wins:
            if (
                w.validator_key == inv.validator_key
                and w.subnet_id == inv.subnet_id
                and abs(w.alpha - inv.alpha) < 1e-12
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
                w.treasury_coldkey = treasury_ck
                inv = w
                break
        else:
            self.state.wins.append(inv)

        # Immediately return response without waiting for heavy processing
        synapse.ack = True
        synapse.payment_attempted = bool(inv.pay_attempts > 0)
        synapse.payment_ok = bool(inv.paid)
        synapse.attempts = int(inv.pay_attempts)
        synapse.last_response = inv.last_response[:300] if inv.last_response else ""

        # Schedule heavy processing in background (fire-and-forget)
        self.submit(self._process_win_background(inv, caller_hot, vkey, uid, epoch_now))
        
        return synapse

    async def _process_win_background(self, inv: WinInvoice, caller_hot: str, vkey: str, uid: int, epoch_now: int):
        """
        Background processing for win handling - includes async operations,
        state saving, payment scheduling, and logging.
        """
        try:
            # Ensure async subtensor is available
            await self.runtime._ensure_async_subtensor()
            self.ensure_payment_config()

            # Save state
            await self.state.save_async()

            # Log win notification
            log_commitments(LogLevel.MEDIUM, "Processing win notification", "win_handler", {
                "validator": vkey,
                "uid": uid,
                "subnet_id": inv.subnet_id,
                "accepted_alpha": inv.alpha
            })

            # Enhanced win notification with structured information
            log_commitments(LogLevel.MEDIUM, "Win received from allowlisted validator", "win_handler", {
                "validator": f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})",
                "subnet_id": inv.subnet_id,
                "accepted_alpha": inv.alpha,
                "invoice_id": inv.invoice_id
            })
            
            # Use the new clean win summary method
            miner_logger.win_summary(
                validator=f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})",
                epoch_now=epoch_now,
                subnet_id=inv.subnet_id,
                accepted_alpha=inv.alpha,
                requested_alpha=inv.alpha_requested,
                was_partial=inv.was_partial,
                discount_bps=inv.discount_bps,
                pay_epoch=inv.pay_epoch_index or (epoch_now + 1),
                window=f"[{inv.pay_window_start_block}, {inv.pay_window_end_block or '?'}]",
                amount_to_pay=inv.amount_rao/PLANCK,
                invoice_id=inv.invoice_id,
                treasury_src="LOCAL allowlist (pinned)"
            )
            
            # Schedule payment
            self._schedule_payment(inv)
            self.state.status_tables()

            log_commitments(LogLevel.MEDIUM, "Win processing completed", "win_handler", {
                "invoice_id": inv.invoice_id,
                "ack": True,
                "payment_scheduled": True
            })
            
        except Exception as e:
            log_commitments(LogLevel.HIGH, "Background win processing failed", "win_handler", {
                "invoice_id": inv.invoice_id,
                "error": str(e)
            })
