#!/usr/bin/env python3
# metahash/miner/mixins.py — state I/O, helpers, chain info helpers (no write to self.block)

import os
import json
import asyncio
import hashlib
import tempfile
from dataclasses import asdict
from math import isfinite
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
import time

import bittensor as bt
from bittensor import Synapse
from metahash.miner.models import BidLine, WinInvoice
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.utils.pretty_logs import pretty
from metahash.config import PLANCK, LOG_TOP_N


class MinerMixins:
    """
    Utility mixins: state I/O, helpers, and chain-info helpers.
    Assumes the host class defines:
      - self.config, self.wallet, self.uid, self.metagraph
      - self._state_file: Path
      - self._treasuries: Dict[str,str]
      - self._already_bid: Dict[str, List[Tuple[int,int,float,int]]]
      - self._wins: List[WinInvoice]
      - self._state_lock: asyncio.Lock
      - self._async_subtensor: Optional[bt.AsyncSubtensor]
      - self._loop: Optional[asyncio.AbstractEventLoop]
      - self.lines: List[BidLine]
    """

    # ---------------------- state I/O ----------------------

    def _wipe_state(self):
        self._treasuries.clear()
        self._already_bid.clear()
        self._wins.clear()
        try:
            if self._state_file.exists():
                self._state_file.unlink()
        except Exception as e:
            clog.warning(f"[state] could not remove state file: {e}", color="yellow")

    def _make_invoice_id(
        self,
        validator_key: str,
        subnet_id: int,
        alpha: float,
        discount_bps: int,
        amount_rao: int,
        pay_window_start_block: int,
        pay_window_end_block: int,
        pay_epoch_index: int | None = None,
    ) -> str:
        """
        Window-stable identity for an accepted allocation (no epoch_seen).
        """
        payload = (
            f"{validator_key}|{int(subnet_id)}|{alpha:.12f}|{int(discount_bps)}|"
            f"{int(amount_rao)}|{int(pay_window_start_block)}|{int(pay_window_end_block)}|{int(pay_epoch_index or 0)}"
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def _make_bid_id(self, validator_key: str, epoch: int, subnet_id: int, alpha: float, discount_bps: int) -> str:
        payload = f"{validator_key}|{epoch}|{subnet_id}|{alpha:.12f}|{discount_bps}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]

    def _load_state_file(self):
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text())
        except Exception as e:
            clog.warning(f"[state] could not load state (json): {e}", color="yellow")
            return

        self._treasuries = dict(data.get("treasuries", {}))
        self._already_bid = {k: [tuple(x) for x in v] for k, v in data.get("already_bid", {}).items()}

        self._wins = []
        for w in data.get("wins", []):
            try:
                allowed = set(WinInvoice.__dataclass_fields__.keys())
                clean = {k: v for k, v in w.items() if k in allowed}

                if not clean.get("invoice_id"):
                    inv_id = self._make_invoice_id(
                        clean.get("validator_key", ""),
                        int(clean.get("subnet_id", 0) or 0),
                        float(clean.get("alpha", 0.0) or 0.0),
                        int(clean.get("discount_bps", 0) or 0),
                        int(clean.get("amount_rao", 0) or 0),
                        int(clean.get("pay_window_start_block", 0) or 0),
                        int(clean.get("pay_window_end_block", 0) or 0),
                        int(clean.get("pay_epoch_index", 0) or 0),
                    )
                    clean["invoice_id"] = inv_id

                clean.setdefault("alpha_requested", float(clean.get("alpha", 0.0) or 0.0))
                clean.setdefault("was_partial", False)

                self._wins.append(WinInvoice(**clean))
            except Exception as e:
                clog.warning(f"[state] skipping malformed win entry: {e}", color="yellow")

    def _save_state_file(self):
        """
        Atomic write with tempfile + os.replace().
        This function itself is sync; guard calls with _state_lock where possible using _async_save_state().
        """
        payload = {
            "treasuries": self._treasuries,
            "already_bid": self._already_bid,
            "wins": [asdict(w) for w in self._wins],
        }
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=self._state_file.name + ".",
            suffix=".tmp",
            dir=str(self._state_file.parent) if self._state_file.parent else None,
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp_path, self._state_file)  # atomic on same fs
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass

    async def _async_save_state(self):
        async with self._state_lock:
            self._save_state_file()

    def save_state(self):
        self._save_state_file()

    # ---------------------- helpers ----------------------

    def _parse_discount_token(self, tok: str) -> int:
        t = str(tok).strip().lower().replace("%", "")
        if t.endswith("bps"):
            try:
                bps = int(float(t[:-3]))
                return max(0, min(10_000, bps))
            except Exception:
                pass
        val = float(t)
        if val > 100:
            return max(0, min(10_000, int(round(val))))
        return max(0, min(10_000, int(round(val * 100))))

    def _build_lines_from_config(self) -> List[BidLine]:
        bids = getattr(self.config, "miner", None)
        nets = getattr(getattr(bids, "bids", bids), "netuids", []) if bids else []
        amts = getattr(getattr(bids, "bids", bids), "amounts", []) if bids else []
        discs = getattr(getattr(bids, "bids", bids), "discounts", []) if bids else []

        netuids = [int(x) for x in list(nets or [])]
        amounts = [float(x) for x in list(amts or [])]
        discounts = [self._parse_discount_token(str(x)) for x in list(discs or [])]

        if not (len(netuids) == len(amounts) == len(discounts)):
            raise ValueError("miner.bids.* lengths must match (netuids, amounts, discounts)")

        lines: List[BidLine] = []
        for sid, amt, disc in zip(netuids, amounts, discounts):
            if amt <= 0:
                pretty.log(f"[yellow]Skipping non-positive amount: {amt}[/yellow]")
                continue
            if disc < 0 or disc > 10_000:
                pretty.log(f"[yellow]Skipping invalid discount: {disc} bps[/yellow]")
                continue
            lines.append(BidLine(subnet_id=int(sid), alpha=float(amt), discount_bps=int(disc)))
        return lines

    def _log_cfg_summary(self):
        rows = [[i, ln.subnet_id, f"{ln.alpha:.4f} α", f"{ln.discount_bps} bps"] for i, ln in enumerate(self.lines)]
        if rows:
            pretty.table("Configured Bid Lines", ["#", "Subnet", "Alpha", "Discount"], rows)

    def _status_tables(self):
        pend = [w for w in self._wins if not w.paid]
        done = [w for w in self._wins if w.paid]

        rows_pend = [[
            (w.validator_key[:8] + "…"),
            w.epoch_seen,
            w.subnet_id,
            f"{w.alpha:.4f} α",
            f"{w.alpha_requested:.4f} α",
            "P" if w.was_partial else "F",
            f"{w.discount_bps} bps",
            w.pay_epoch_index,
            w.pay_window_start_block,
            (w.pay_window_end_block if w.pay_window_end_block else 0),
            f"{w.amount_rao/PLANCK:.4f} α",
            w.pay_attempts,
            (w.invoice_id[:8] + "…"),
            (w.last_response or "")[:24] + ("…" if len(w.last_response or "") > 24 else "")
        ] for w in sorted(pend, key=lambda x: (x.pay_epoch_index, x.pay_window_start_block, x.validator_key))[:LOG_TOP_N]]

        rows_done = [[
            (w.validator_key[:8] + "…"),
            w.epoch_seen,
            w.subnet_id,
            f"{w.alpha:.4f} α",
            f"{w.alpha_requested:.4f} α",
            "P" if w.was_partial else "F",
            f"{w.discount_bps} bps",
            w.pay_epoch_index,
            w.pay_window_start_block,
            (w.pay_window_end_block if w.pay_window_end_block else 0),
            f"{w.amount_rao/PLANCK:.4f} α",
            w.pay_attempts,
            (w.invoice_id[:8] + "…"),
            (w.last_response or "")[:24] + ("…" if len(w.last_response or "") > 24 else "")
        ] for w in sorted(done, key=lambda x: (-w.last_attempt_ts))[:LOG_TOP_N]]

        if rows_pend:
            pretty.table(
                "Pending Wins (to pay)",
                ["Validator", "E_seen", "Subnet", "Accepted", "Requested", "Fill", "Disc", "PayE", "WinStart", "WinEnd", "Amount", "Attempts", "InvID", "LastResp"],
                rows_pend,
            )
        if rows_done:
            pretty.table(
                "Paid Wins (recent)",
                ["Validator", "E_seen", "Subnet", "Accepted", "Requested", "Fill", "Disc", "PayE", "WinStart", "WinEnd", "Amount", "Attempts", "InvID", "LastResp"],
                rows_done,
            )

    def _resolve_caller(self, synapse: Synapse) -> Tuple[Optional[int], str]:
        """Return (uid, hotkey) for caller, best-effort. Uses synapse identity if present."""
        uid = getattr(synapse, "validator_uid", None)
        hk = getattr(synapse, "validator_hotkey", None)

        if uid is None:
            try:
                uid = int(getattr(getattr(synapse, "dendrite", None), "origin", None))
            except Exception:
                uid = None
        if not hk and uid is not None and 0 <= uid < len(self.metagraph.axons):
            hk = getattr(self.metagraph.axons[uid], "hotkey", None)
        if not hk and hasattr(self.metagraph, "hotkeys") and uid is not None and uid < len(self.metagraph.hotkeys):
            hk = self.metagraph.hotkeys[uid]
        if not hk:
            hk = getattr(synapse, "caller_hotkey", None)
        return uid, hk or ""

    def _validator_key(self, uid: Optional[int], hotkey: str) -> str:
        return hotkey if hotkey else (f"uid:{uid}" if uid is not None else "<unknown>")

    def _remember_bid(self, validator_key: str, epoch: int, subnet_id: int, alpha: float, discount_bps: int):
        rec = (int(epoch), int(subnet_id), float(alpha), int(discount_bps))
        self._already_bid.setdefault(validator_key, [])
        if rec not in self._already_bid[validator_key]:
            self._already_bid[validator_key].append(rec)

    def _has_bid(self, validator_key: str, epoch: int, subnet_id: int, alpha: float, discount_bps: int) -> bool:
        rec = (int(epoch), int(subnet_id), float(alpha), int(discount_bps))
        return rec in self._already_bid.get(validator_key, [])

    def _aggregate_by_subnet(self) -> Dict[int, Tuple[float, float, float]]:
        """
        Returns mapping: subnet_id -> (alpha_bidded_total, alpha_won_total, alpha_paid_total)
        All totals are lifetime (current process state file).
        """
        bidded = defaultdict(float)
        for _vkey, recs in self._already_bid.items():
            for epoch, subnet, alpha, _disc in recs:
                if isfinite(alpha) and alpha > 0:
                    bidded[int(subnet)] += float(alpha)

        won = defaultdict(float)
        paid = defaultdict(float)
        for w in self._wins:
            sid = int(w.subnet_id)
            won[sid] += float(w.alpha or 0.0)
            if w.paid:
                paid[sid] += (float(w.amount_rao) / PLANCK) if w.amount_rao else float(w.alpha or 0.0)

        all_sids = set(bidded) | set(won) | set(paid)
        return {sid: (float(bidded.get(sid, 0.0)), float(won.get(sid, 0.0)), float(paid.get(sid, 0.0)))
                for sid in sorted(all_sids)}

    def _log_aggregate_summary(self):
        stats = self._aggregate_by_subnet()
        if not stats:
            pretty.log("[grey]No aggregate data yet.[/grey]")
            return
        rows = [[sid, f"{b:.4f} α", f"{w:.4f} α", f"{p:.4f} α"] for sid, (b, w, p) in stats.items()]
        pretty.table("Aggregate per Subnet (α bidded / won / paid)", ["Subnet", "Bidded", "Won", "Paid"], rows)

    # ---------------------- chain info helpers ----------------------

    async def _ensure_async_subtensor(self):
        """
        Creates and initializes AsyncSubtensor bound to the *current* running loop (axon loop).
        Also captures the loop for later scheduling.
        """
        if getattr(self, "_async_subtensor", None) is None:
            stxn = bt.AsyncSubtensor(network=self.config.subtensor.network)
            await stxn.initialize()
            self._async_subtensor = stxn
        try:
            if getattr(self, "_loop", None) is None:
                self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    async def _get_current_block(self) -> int:
        """
        Return a fresh(ish) block height.
        - Never assigns to self.block (read-only in BaseNeuron).
        - Uses a ~1s TTL cache to avoid RPC hammering.
        """
        await self._ensure_async_subtensor()
        stxn = self._async_subtensor
        now = time.time()

        cached_h = int(getattr(self, "_blk_cache_height", 0) or 0)
        cached_ts = float(getattr(self, "_blk_cache_ts", 0.0) or 0.0)

        if cached_h > 0 and (now - cached_ts) < 1.0:
            return cached_h

        height = 0

        # Try common async methods/properties
        for name in ("get_current_block", "current_block", "block"):
            try:
                attr = getattr(stxn, name, None)
                if attr is None:
                    continue
                res = attr() if callable(attr) else attr
                if asyncio.iscoroutine(res):
                    res = await res
                b = int(res or 0)
                if b > 0:
                    height = b
                    break
            except Exception:
                continue

        # Fallback via substrate (sync)
        if height == 0:
            try:
                substrate = getattr(stxn, "substrate", None)
                if substrate is not None:
                    meth = getattr(substrate, "get_block_number", None)
                    if callable(meth):
                        b = int(meth(None) or 0)
                        if b > 0:
                            height = b
            except Exception:
                height = 0

        if height > 0:
            self._blk_cache_height = height
            self._blk_cache_ts = now
            return height

        # Last resort: stale cache or 0
        return cached_h
