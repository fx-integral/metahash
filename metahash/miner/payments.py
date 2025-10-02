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
        self._retry_max_attempts: int = 12

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
        # resume unpaid + watchdog
        self.submit(self._resume_pending_payments())
        self.submit(self._pending_payments_watchdog())

    def shutdown_background(self):
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
        except Exception:
            pass

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
                try:
                    sid = int(sid_s.strip())
                except Exception:
                    continue
                self._pay_map[sid] = hk.strip()
            if self._pay_map:
                pretty.log(f"[green]Payment map loaded[/green]: {len(self._pay_map)} subnet-specific origin hotkey(s).")
        except Exception:
            self._pay_map = {}

        # Start safety / retry knobs
        try:
            self._pay_start_safety_blocks = max(0, int(getattr(pay_cfg, "start_safety_blocks", 0) or 0))
            if self._pay_start_safety_blocks:
                pretty.log(f"[green]Payment start safety[/green]: +{self._pay_start_safety_blocks} block(s) after window start.")
        except Exception:
            self._pay_start_safety_blocks = 0

        try:
            self._retry_every_blocks = max(1, int(getattr(pay_cfg, "retry_every_blocks", 2) or 2))
        except Exception:
            self._retry_every_blocks = 2

        try:
            self._retry_max_attempts = max(1, int(getattr(pay_cfg, "retry_max_attempts", 12) or 12))
        except Exception:
            self._retry_max_attempts = 12

        if not self._pay_map and not self._pay_pool and not self._pay_start_safety_blocks:
            pretty.log("[yellow]No --payment.map / --payment.validators / --payment.start_safety_blocks; defaults in effect.[/yellow]")

        self._pay_cfg_initialized = True

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
            pretty.log("[green]Startup: resumed scheduling of unpaid invoices.[/green]")
        except Exception as e:
            pretty.log(f"[yellow]Startup resume failed:[/yellow] {e}")

    async def _pending_payments_watchdog(self):
        try:
            while True:
                self.schedule_unpaid_pending()
                await asyncio.sleep(max(0.5, float(BLOCKTIME)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            pretty.log(f"[yellow]Payments watchdog error:[/yellow] {e}")

    def schedule_unpaid_pending(self):
        """Idempotent: schedules only invoices that are not paid and not expired."""
        for w in self.state.wins:
            if bool(getattr(w, "paid", False) or getattr(w, "expired", False)):
                continue
            treasury_ck = self.state.treasuries.get(w.validator_key) or self.state.treasuries.get(getattr(w, "validator_key", ""))
            if not treasury_ck:
                pretty.kv_panel(
                    "[yellow]Skipping pending invoice[/yellow]",
                    [("inv", getattr(w, "invoice_id", "?")), ("reason", "validator not allowlisted")],
                    style="bold yellow",
                )
                continue
            if w.treasury_coldkey != treasury_ck:
                w.treasury_coldkey = treasury_ck
            self._schedule_payment(w)

    def _schedule_payment(self, inv: WinInvoice):
        tid = inv.invoice_id
        t = self._tasks.get(tid)
        if t and not t.done():
            return

        inv.last_response = f"scheduled (wait ≥{self._pay_start_safety_blocks} blk)"
        self.submit(self.state.save_async())

        fut = self.submit(self._payment_worker(inv))
        self._tasks[tid] = fut

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

            start = int(inv.pay_window_start_block or 0)
            end = int(inv.pay_window_end_block or 0)
            allowed_start = start + int(self._pay_start_safety_blocks or 0)

            attempt = 0
            while True:
                blk = await self.runtime.get_current_block()

                if start and (blk <= 0 or blk < allowed_start):
                    inv.last_response = f"waiting (blk {blk} < start {allowed_start})"
                    await self.state.save_async()
                    sleep_blocks = max(1, allowed_start - blk)
                    sleep_s = max(0.5, min(10 * float(BLOCKTIME), sleep_blocks * float(BLOCKTIME)))
                    pretty.kv_panel(
                        "[blue]PAY wait[/blue]",
                        [("inv", inv.invoice_id), ("blk", blk), ("allowed_start", allowed_start), ("sleep", f"{sleep_s:.1f}s")],
                        style="bold blue",
                    )
                    await asyncio.sleep(sleep_s)
                    continue

                if end and blk > end:
                    inv.last_response = f"window over (blk {blk} > end {end})"
                    inv.expired = True
                    await self.state.save_async()
                    pretty.kv_panel(
                        "[yellow]PAY exit[/yellow]",
                        [("inv", inv.invoice_id), ("reason", "window over"), ("blk", blk), ("end", end)],
                        style="bold yellow",
                    )
                    self.state.status_tables()
                    break

                attempt += 1
                inv.last_attempt_ts = time.time()
                await self.state.save_async()
                pretty.kv_panel(
                    "[white]PAY attempt[/white]",
                    [("inv", inv.invoice_id), ("try", f"{attempt}/{self._retry_max_attempts}"), ("blk", blk), ("win", f"[{start},{end}]")],
                    style="bold white",
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
                    pretty.kv_panel(
                        "[red]PAY wait (insufficient α)[/red]",
                        [("inv", inv.invoice_id), ("subnet", inv.subnet_id), ("have α", f"{bal_alpha:.6f}"), ("need α", f"{need_alpha:.6f}")],
                        style="bold red",
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
                    pretty.kv_panel(
                        "[green]Payment OK[/green]",
                        [
                            ("inv", inv.invoice_id),
                            ("α", f"{inv.amount_rao/PLANCK:.4f}"),
                            ("dst_treasury", inv.treasury_coldkey[:8] + "…"),
                            ("src_hotkey", origin_hotkey[:8] + "…"),
                            ("sid", inv.subnet_id),
                            ("pay_e", inv.pay_epoch_index),
                            *([("tx", inv.tx_hash[:10] + "…")] if getattr(inv, "tx_hash", None) else []),
                            *([("in_block", inv.paid_at_block)] if getattr(inv, "paid_at_block", None) else []),
                        ],
                        style="bold green",
                    )
                    self.state.status_tables()
                    self.state.log_aggregate_summary()
                    break
                else:
                    await self.state.save_async()
                    pretty.kv_panel(
                        "[red]Payment FAILED[/red]",
                        [("inv", inv.invoice_id), ("resp", inv.last_response[:80]), ("sid", inv.subnet_id), ("attempts", inv.pay_attempts)],
                        style="bold red",
                    )

                if attempt >= int(self._retry_max_attempts or 1):
                    inv.last_response = f"max attempts ({attempt})"
                    await self.state.save_async()
                    pretty.kv_panel(
                        "[yellow]PAY exit[/yellow]",
                        [("inv", inv.invoice_id), ("reason", "max attempts"), ("attempts", attempt)],
                        style="bold yellow",
                    )
                    break

                await asyncio.sleep(max(0.5, float(BLOCKTIME) * float(self._retry_every_blocks)))

        except asyncio.CancelledError:
            inv.last_response = "cancelled"
            await self.state.save_async()
            pretty.kv_panel("[grey]PAY cancelled[/grey]", [("inv", inv.invoice_id)], style="bold magenta")
            raise
        except Exception as e:
            inv.last_response = f"error: {e}"
            await self.state.save_async()
            pretty.kv_panel(
                "[yellow]PAY worker error[/yellow]",
                [("inv", getattr(inv, "invoice_id", "?")), ("err", str(e)[:120])],
                style="bold yellow",
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
        await self.runtime._ensure_async_subtensor()
        self.ensure_payment_config()

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

        treasury_ck = self.state.treasuries.get(caller_hot or "") or self.state.treasuries.get(vkey)
        if not treasury_ck:
            note = f"validator not allowlisted: {caller_hot or vkey or '?'}"
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

        accepted_alpha = _as_float(getattr(synapse, "accepted_alpha", None) or getattr(synapse, "alpha", 0.0))
        requested_alpha = _as_float(getattr(synapse, "requested_alpha", None) or accepted_alpha)
        was_partial = bool(getattr(synapse, "was_partially_accepted", accepted_alpha < requested_alpha - 1e-12))

        amount_rao = int(round(accepted_alpha * PLANCK))
        epoch_now = int(getattr(self.runtime, "epoch_index", 0) or getattr(self, "epoch_index", 0) or 0)

        pay_start = _as_int(getattr(synapse, "pay_window_start_block", 0))
        pay_end = _as_int(getattr(synapse, "pay_window_end_block", 0))
        pay_ep = _as_int(getattr(synapse, "pay_epoch_index", 0))
        clearing_bps = _as_int(getattr(synapse, "clearing_discount_bps", 0))

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
        inv.invoice_id = hashlib.sha1(
            f"{vkey}|{inv.subnet_id}|{inv.alpha:.12f}|{inv.discount_bps}|{inv.amount_rao}|{inv.pay_window_start_block}|{inv.pay_window_end_block}|{inv.pay_epoch_index or 0}".encode("utf-8")
        ).hexdigest()[:12]

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

        await self.state.save_async()

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

        self._schedule_payment(inv)
        self.state.status_tables()

        synapse.ack = True
        synapse.payment_attempted = bool(inv.pay_attempts > 0)
        synapse.payment_ok = bool(inv.paid)
        synapse.attempts = int(inv.pay_attempts)
        synapse.last_response = inv.last_response[:300] if inv.last_response else ""
        return synapse
