#!/usr/bin/env python3
# neurons/miner.py

import asyncio
import time
import threading
import inspect
from math import isfinite
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import bittensor as bt
from bittensor import Synapse, BLOCKTIME

from metahash.miner.models import BidLine, WinInvoice
from metahash.base.miner import BaseMinerNeuron
from metahash.miner.mixins import MinerMixins

from metahash.base.utils.logging import ColoredLogger as clog
from metahash.protocol import AuctionStartSynapse, WinSynapse
from metahash.config import PLANCK, S_MIN_ALPHA_MINER
from metahash.utils.pretty_logs import pretty
from metahash.utils.wallet_utils import unlock_wallet, transfer_alpha
from metahash.treasuries import VALIDATOR_TREASURIES


class Miner(BaseMinerNeuron, MinerMixins):
    def __init__(self, config=None):
        super().__init__(config=config)

        # Background asyncio loop (daemon thread)
        self._bg_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._bg_thread = threading.Thread(
            target=self._run_bg_loop, name="miner-bg-loop", daemon=True
        )
        self._bg_thread.start()

        # ---------------------- PER-COLDKEY STATE SCOPING ----------------------
        # Persist all miner state in a coldkey-specific folder so multiple coldkeys
        # can run safely on the same server/workdir.
        self._coldkey_ss58: str = getattr(getattr(self.wallet, "coldkey", None), "ss58_address", "") or "unknown_coldkey"
        self._state_dir: Path = Path("miner_state") / self._coldkey_ss58
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Fall back to current directory on failure (still per-coldkey filename).
            self._state_dir = Path(".")
        self._state_file = self._state_dir / "miner_state.json"

        # Persistent state (in-memory; will be loaded/saved via _state_file)
        self._treasuries: Dict[str, str] = {}  # pinned allowlist
        self._already_bid: Dict[str, List[Tuple[int, int, float, int]]] = {}
        self._wins: List[WinInvoice] = []

        # Guards
        self._pay_lock: asyncio.Lock = asyncio.Lock()
        self._state_lock: asyncio.Lock = asyncio.Lock()
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None
        self._subtensor_lock: asyncio.Lock = asyncio.Lock()
        self._rpc_lock: asyncio.Lock = asyncio.Lock()
        self._payment_tasks: Dict[str, "concurrent.futures.Future"] = {}

        # Discounts
        self._bids_raw_discount: bool = False

        # Optional fresh start
        if getattr(self.config, "fresh", False):
            self._wipe_state()
            pretty.log(
                f"[magenta]Fresh start requested: cleared local miner state for coldkey {self._coldkey_ss58}.[/magenta]"
            )

        # Load persisted state and re-pin treasuries
        self._load_state_file()
        self._treasuries = dict(VALIDATOR_TREASURIES)

        # Build bid lines
        self.lines: List[BidLine] = self._build_lines_from_config()

        pretty.banner(
            "Miner started",
            (
                f"uid={self.uid} | coldkey={self._coldkey_ss58} | "
                f"hotkey={self.wallet.hotkey.ss58_address} | lines={len(self.lines)} | "
                f"epoch(e)={getattr(self, 'epoch_index', 0)}"
            ),
            style="bold magenta",
        )
        pretty.kv_panel(
            "[magenta]State scope[/magenta]",
            [("state_dir", str(self._state_dir)), ("state_file", str(self._state_file.name))],
            style="bold magenta",
        )
        self._log_cfg_summary()
        pretty.kv_panel(
            "[magenta]Local treasuries loaded[/magenta]",
            [("allowlisted_validators", len(self._treasuries))],
            style="bold magenta",
        )

        unlock_wallet(wallet=self.wallet)

        # Payment config
        self._pay_cfg_initialized: bool = False
        self._pay_rr_index: int = 0
        self._pay_pool: List[str] = []
        self._pay_map: Dict[int, str] = {}
        self._pay_start_safety_blocks: int = 0
        self._retry_every_blocks: int = 2
        self._retry_max_attempts: int = 12

        # Eager scheduling on startup
        try:
            self._ensure_payment_config()
            self._schedule_unpaid_pending()
            pretty.log("[green]Startup: eagerly scheduled unpaid invoices (no waiting).[/green]")
        except Exception as _e:
            pretty.log(f"[yellow]Eager scheduling skipped:[/yellow] {_e}")

        # Resume + watchdog
        self._safe_create_task(self._resume_pending_payments(), name="resume_pending_payments")
        self._safe_create_task(self._pending_payments_watchdog(), name="payments_watchdog")

    # ---------------------- background loop helpers ----------------------

    def _run_bg_loop(self):
        asyncio.set_event_loop(self._bg_loop)
        self._bg_loop.run_forever()

    def _submit(self, coro) -> "concurrent.futures.Future":
        import concurrent.futures
        if not asyncio.iscoroutine(coro):
            raise TypeError("Expected coroutine in _submit()")
        return asyncio.run_coroutine_threadsafe(coro, self._bg_loop)

    def _safe_create_task(self, coro, *, name: Optional[str] = None):
        self._submit(coro)

    # ---------------------- AsyncSubtensor singleton + block helpers ----------------------

    async def _ensure_async_subtensor_singleton(self):
        if self._async_subtensor is not None:
            return
        async with self._subtensor_lock:
            if self._async_subtensor is not None:
                return
            try:
                await super()._ensure_async_subtensor()  # type: ignore[attr-defined]
                self._async_subtensor = getattr(self, "_async_subtensor", None)
            except AttributeError:
                self._async_subtensor = bt.AsyncSubtensor(self.config)

    async def _await_maybe(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _read_block_generic(self) -> Optional[int]:
        """
        Robust across Bittensor variants:
        - 'block' may be an async method, a callable returning a coroutine, or an awaitable attribute.
        - Use inspect.getattr_static to avoid instantiating coroutine properties while probing.
        - Try multiple names; fallback to metagraph.block if needed.
        """
        await self._ensure_async_subtensor_singleton()
        st = self._async_subtensor
        if st is None:
            return None

        for name in ("block", "block_number", "get_current_block", "get_block_number"):
            try:
                # Probe without invoking descriptors/coroutines
                inspect.getattr_static(st, name)
            except AttributeError:
                continue

            try:
                attr = getattr(st, name)  # may be callable, awaitable, or concrete
                res = attr() if callable(attr) else attr
                res = await self._await_maybe(res)
                val = int(res)
                if val > 0:
                    return val
            except Exception:
                continue

        # Fallback (approximate)
        try:
            mg_block = int(getattr(self.metagraph, "block", 0) or 0)
            if mg_block > 0:
                return mg_block
        except Exception:
            pass
        return None

    async def _get_current_block_locked(self) -> int:
        """Fetch block number under _rpc_lock (no re-entrancy)."""
        async with self._rpc_lock:
            val = await self._read_block_generic()
            return int(val or 0)

    async def _get_current_block_nolock(self) -> int:
        """Fetch block number WITHOUT taking _rpc_lock (use only when already holding it)."""
        val = await self._read_block_generic()
        return int(val or 0)

    # ---------------------- startup tasks ----------------------

    async def _resume_pending_payments(self):
        try:
            self._ensure_payment_config()
            self._schedule_unpaid_pending()
            pretty.log("[green]Startup: resumed scheduling of unpaid invoices.[/green]")
        except Exception as e:
            pretty.log(f"[yellow]Startup resume failed:[/yellow] {e}")

    async def _pending_payments_watchdog(self):
        try:
            while True:
                self._schedule_unpaid_pending()  # idempotent
                await asyncio.sleep(max(0.5, float(BLOCKTIME)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            pretty.log(f"[yellow]Payments watchdog error:[/yellow] {e}")

    # ---------------------- configuration ----------------------

    def _ensure_payment_config(self):
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

    # ---------------------- allowlist helpers ----------------------

    def _lookup_local_treasury(self, caller_hot: Optional[str], vkey: str) -> Tuple[Optional[str], Optional[str]]:
        if isinstance(caller_hot, str) and caller_hot:
            ck = self._treasuries.get(caller_hot)
            if ck:
                return ck, caller_hot
        if isinstance(vkey, str) and vkey:
            ck = self._treasuries.get(vkey)
            if ck:
                return ck, vkey
        return None, None

    def _allowed_validator_note(self, caller_hot: Optional[str], vkey: str) -> str:
        key_preview = (caller_hot or vkey or "?")
        return f"validator not allowlisted: {key_preview}"

    # ---------------------- invoice helpers ----------------------

    @staticmethod
    def _invoice_is_final(inv: WinInvoice) -> bool:
        return bool(getattr(inv, "paid", False) or getattr(inv, "expired", False))

    # ---------------------- per-invoice scheduler ----------------------

    def _schedule_payment(self, inv: WinInvoice):
        if self._invoice_is_final(inv):
            return
        tid = inv.invoice_id
        t = self._payment_tasks.get(tid)
        if t and not t.done():
            return  # already scheduled

        inv.last_response = f"scheduled (wait ≥{self._pay_start_safety_blocks} blk)"
        self._submit(self._async_save_state())

        fut = self._submit(self._payment_worker(inv))
        self._payment_tasks[tid] = fut

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

    async def _maybe_transfer_alpha(self, **kwargs) -> Any:
        """Call transfer_alpha whether it's sync or async depending on environment."""
        try:
            result = transfer_alpha(**kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except TypeError:
            return await transfer_alpha(**kwargs)  # type: ignore[misc]

    async def _payment_worker(self, inv: WinInvoice):
        try:
            await self._ensure_async_subtensor_singleton()

            start = int(inv.pay_window_start_block or 0)
            end = int(inv.pay_window_end_block or 0)
            allowed_start = start + int(self._pay_start_safety_blocks or 0)

            attempt = 0
            while True:
                blk = await self._get_current_block_locked()

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
                    inv.expired = True  # <-- mark final; don't reschedule again
                    await self._async_save_state()
                    pretty.kv_panel(
                        "[yellow]PAY exit[/yellow]",
                        [("inv", inv.invoice_id), ("reason", "window over"), ("blk", blk), ("end", end)],
                        style="bold yellow",
                    )
                    self._status_tables()
                    break

                # Attempt payment
                attempt += 1
                inv.last_attempt_ts = time.time()
                await self._async_save_state()
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

                async with self._pay_lock:
                    async with self._rpc_lock:
                        blk2 = await self._get_current_block_nolock()
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
                                    subtensor=self._async_subtensor,
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
                            *([("tx", inv.tx_hash[:10] + "…")] if getattr(inv, "tx_hash", None) else []),
                            *([("in_block", inv.paid_at_block)] if getattr(inv, "paid_at_block", None) else []),
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
                        [("inv", inv.invoice_id), ("resp", inv.last_response[:80]), ("sid", inv.subnet_id), ("attempts", inv.pay_attempts)],
                        style="bold red",
                    )

                if attempt >= int(self._retry_max_attempts or 1):
                    inv.last_response = f"max attempts ({attempt})"
                    await self._async_save_state()
                    pretty.kv_panel(
                        "[yellow]PAY exit[/yellow]",
                        [("inv", inv.invoice_id), ("reason", "max attempts"), ("attempts", attempt)],
                        style="bold yellow",
                    )
                    break

                await asyncio.sleep(max(0.5, float(BLOCKTIME) * float(self._retry_every_blocks)))

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
            # cleanup finished/cancelled future
            t = self._payment_tasks.get(inv.invoice_id)
            try:
                if t and t.done():
                    self._payment_tasks.pop(inv.invoice_id, None)
            except Exception:
                self._payment_tasks.pop(inv.invoice_id, None)

    def _schedule_unpaid_pending(self):
        """Idempotent: schedules only invoices that are not paid and not expired."""
        for w in self._wins:
            if self._invoice_is_final(w):
                continue
            treasury_ck, _matched = self._lookup_local_treasury(None, w.validator_key)
            if treasury_ck:
                w.treasury_coldkey = treasury_ck
                self._schedule_payment(w)
            else:
                pretty.kv_panel(
                    "[yellow]Skipping pending invoice[/yellow]",
                    [("inv", getattr(w, "invoice_id", "?")), ("reason", "validator not allowlisted")],
                    style="bold yellow",
                )

    # ---------------------- discount math ----------------------

    @staticmethod
    def _clamp_bps(x: int) -> int:
        return 0 if x < 0 else (10_000 if x > 10_000 else x)

    @staticmethod
    def _compute_raw_discount_bps(effective_factor_bps: int, weight_bps: int) -> int:
        w_bps = Miner._clamp_bps(int(weight_bps or 0))
        ef_bps = Miner._clamp_bps(int(effective_factor_bps or 0))
        if w_bps <= 0:
            return 0
        w = w_bps / 10_000.0
        ef = ef_bps / 10_000.0
        raw = 1.0 - (ef / w)
        if raw < 0.0:
            raw = 0.0
        elif raw > 1.0:
            raw = 1.0
        return int(round(raw * 10_000))

    # ---------------------- stake helpers (validator pair) ----------------------

    @staticmethod
    def _balance_to_alpha(bal: Optional[bt.Balance]) -> float:
        """Convert a bt.Balance to α (float). Robust to differing Balance APIs."""
        try:
            if bal is None:
                return 0.0
            rao = getattr(bal, "rao", None)
            if isinstance(rao, int):
                return float(rao) / float(PLANCK)
            val = getattr(bal, "value", None)
            if isinstance(val, int):
                return float(val) / float(PLANCK)
            v = float(bal)
            return v if isfinite(v) else 0.0
        except Exception:
            return 0.0

    async def _get_validator_stakes_map(
        self,
        validator_hotkey_ss58: Optional[str],
        subnet_ids: List[int],
    ) -> Dict[int, float]:
        """
        Returns per-subnet α that THIS miner's coldkey has delegated to the given validator hotkey.
        Missing/failed lookups resolve to 0.0 for safety.
        """
        await self._ensure_async_subtensor_singleton()
        st = self._async_subtensor
        if st is None or not validator_hotkey_ss58:
            return {int(s): 0.0 for s in subnet_ids}

        try:
            cold_ss58 = self.wallet.coldkey.ss58_address
        except Exception:
            cold_ss58 = None
        if not cold_ss58:
            return {int(s): 0.0 for s in subnet_ids}

        unique_ids = list(dict.fromkeys(int(s) for s in subnet_ids))
        coros = [
            st.get_stake(
                coldkey_ss58=cold_ss58,
                hotkey_ss58=validator_hotkey_ss58,
                netuid=int(sid),
                reuse_block=True,
            )
            for sid in unique_ids
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        out: Dict[int, float] = {}
        for sid, res in zip(unique_ids, results):
            if isinstance(res, Exception):
                out[int(sid)] = 0.0
            else:
                out[int(sid)] = self._balance_to_alpha(res)
        return out

    # ---------------------- auction handlers ----------------------

    async def auctionstart_forward(self, synapse: AuctionStartSynapse) -> AuctionStartSynapse:
        await self._ensure_async_subtensor_singleton()
        self._ensure_payment_config()

        uid, caller_hot = self._resolve_caller(synapse)
        vkey = self._validator_key(uid, caller_hot)
        epoch = int(synapse.epoch_index)

        treasury_ck, _matched_key = self._lookup_local_treasury(caller_hot, vkey)
        if not treasury_ck:
            note = self._allowed_validator_note(caller_hot, vkey)
            pretty.kv_panel(
                "[red]AuctionStart ignored[/red]",
                [("validator", f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})"),
                 ("reason", "not allowlisted"),
                 ("note", note)],
                style="bold red",
            )
            synapse.ack = True
            synapse.bids = []
            synapse.bids_sent = 0
            synapse.note = note
            return synapse

        budget_alpha = float(getattr(synapse, "auction_budget_alpha", 0.0) or 0.0)
        min_stake_alpha = float(getattr(synapse, "min_stake_alpha", S_MIN_ALPHA_MINER) or S_MIN_ALPHA_MINER)

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

        # Re-kick any pending invoices (idempotent)
        self._schedule_unpaid_pending()

        my_stake = float(self.metagraph.stake[self.uid])
        if my_stake < min_stake_alpha:
            pretty.log(f"[yellow]Stake below S_MIN_ALPHA_MINER – not bidding to this validator (epoch {epoch}).[/yellow]")
            self._status_tables()
            synapse.ack = True
            synapse.bids = []
            synapse.bids_sent = 0
            synapse.note = "stake gate"
            return synapse

        # ---------- Fetch available stake with this validator per subnet ----------
        validator_hot_ss58 = caller_hot or vkey
        candidate_subnets: List[int] = []
        for ln in self.lines:
            try:
                if isfinite(ln.alpha) and ln.alpha > 0 and 0 <= int(ln.discount_bps) <= 10_000:
                    candidate_subnets.append(int(ln.subnet_id))
            except Exception:
                continue

        stake_by_subnet: Dict[int, float] = {}
        if candidate_subnets and validator_hot_ss58:
            try:
                stake_by_subnet = await self._get_validator_stakes_map(validator_hot_ss58, candidate_subnets)
                rows = [[sid, f"{amt:.4f} α"] for sid, amt in sorted(stake_by_subnet.items())]
                if rows:
                    pretty.table("[blue]Available stake with validator (per subnet)[/blue]", ["Subnet", "α available"], rows)
            except Exception as _e:
                stake_by_subnet = {int(s): 0.0 for s in candidate_subnets}
                pretty.log(f"[yellow]Stake check failed; defaulting to 0 availability.[/yellow] {_e}")

        # Track remaining stake per subnet across multiple lines to prevent oversubscription
        remaining_by_subnet: Dict[int, float] = dict(stake_by_subnet)

        out_bids = []
        sent = 0
        rows_sent = []
        for ln in self.lines:
            if not isfinite(ln.alpha) or ln.alpha <= 0:
                pretty.log(f"[yellow]Invalid alpha {ln.alpha} for subnet {ln.subnet_id} – skipping line.[/yellow]")
                continue
            if not (0 <= ln.discount_bps <= 10_000):
                pretty.log(f"[yellow]Invalid discount {ln.discount_bps} bps – skipping line.[/yellow]")
                continue

            subnet_id = int(ln.subnet_id)
            weight_bps = weights_bps.get(subnet_id, 10_000)

            available = float(remaining_by_subnet.get(subnet_id, 0.0))
            if available <= 0.0:
                pretty.kv_panel(
                    "[red]Bid skipped – insufficient delegated stake with validator[/red]",
                    [("subnet", subnet_id), ("cfg α", f"{ln.alpha:.4f}"), ("available α", f"{available:.4f}")],
                    style="bold red",
                )
                continue

            send_alpha = float(ln.alpha)
            if send_alpha > available:
                send_alpha = available
                pretty.kv_panel(
                    "[yellow]Partial bid (limited by available stake)[/yellow]",
                    [("subnet", subnet_id), ("cfg α", f"{ln.alpha:.4f}"), ("send α", f"{send_alpha:.4f}"), ("remaining before", f"{available:.4f}")],
                    style="bold yellow",
                )

            # Decrement remaining to avoid double-allocating across lines for the same subnet
            remaining_by_subnet[subnet_id] = max(0.0, available - send_alpha)

            # Budget notice (uses the amount to be sent)
            if budget_alpha > 0 and send_alpha > budget_alpha:
                pretty.log(f"[grey58]Note:[/grey58] line α {send_alpha:.4f} exceeds validator budget α {budget_alpha:.4f}; may be partially filled.")

            # Compute discount to send
            if self._bids_raw_discount:
                send_disc_bps = int(ln.discount_bps)
                eff_factor_bps = int(round(weight_bps * (1.0 - (send_disc_bps / 10_000.0))))
                mode_note = "raw"
            else:
                send_disc_bps = self._compute_raw_discount_bps(ln.discount_bps, weight_bps)
                eff_factor_bps = int(round(weight_bps * (1.0 - (send_disc_bps / 10_000.0))))
                mode_note = "effective→raw"

            # Skip if identical bid already recorded
            if self._has_bid(vkey, epoch, subnet_id, send_alpha, send_disc_bps):
                continue

            bid_id = self._make_bid_id(vkey, epoch, subnet_id, send_alpha, send_disc_bps)
            out_bids.append({"subnet_id": subnet_id, "alpha": float(send_alpha), "discount_bps": int(send_disc_bps), "bid_id": bid_id})
            self._remember_bid(vkey, epoch, subnet_id, send_alpha, send_disc_bps)
            sent += 1

            rows_sent.append([
                subnet_id,
                f"{send_alpha:.4f} α",
                f"{ln.discount_bps} bps" + (" (cfg)" if not self._bids_raw_discount else ""),
                f"{send_disc_bps} bps",
                f"{weight_bps} w_bps",
                f"{eff_factor_bps} eff_bps",
                bid_id,
                epoch,
                mode_note
            ])

        if sent == 0:
            pretty.log("[grey]No bids were added (invalid/duplicate/insufficient stake).[/grey]")
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
        await self._ensure_async_subtensor_singleton()
        self._ensure_payment_config()

        uid, caller_hot = self._resolve_caller(synapse)
        vkey = self._validator_key(uid, caller_hot)

        treasury_ck, _matched_key = self._lookup_local_treasury(caller_hot, vkey)
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

        accepted_alpha = float(getattr(synapse, "accepted_alpha", None) or synapse.alpha)
        requested_alpha = float(getattr(synapse, "requested_alpha", None) or accepted_alpha)
        was_partial = bool(getattr(synapse, "was_partially_accepted", accepted_alpha < requested_alpha - 1e-12))

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
            vkey, inv.subnet_id, inv.alpha, inv.discount_bps, inv.amount_rao,
            inv.pay_window_start_block, inv.pay_window_end_block, inv.pay_epoch_index
        )

        # Merge duplicate if present
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

        self._schedule_payment(inv)
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
            _t.sleep(12)
