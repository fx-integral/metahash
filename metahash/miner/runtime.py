# metahash/miner/runtime.py
from __future__ import annotations

import asyncio
import inspect
import hashlib
from math import isfinite
from typing import Dict, List, Optional, Iterable, Tuple, Any

import bittensor as bt
from metahash.config import PLANCK, S_MIN_ALPHA_MINER
from metahash.protocol import AuctionStartSynapse
from metahash.utils.pretty_logs import pretty

from metahash.miner.models import BidLine
from metahash.miner.state import StateStore


def clamp_bps(x: int) -> int:
    return 0 if x < 0 else (10_000 if x > 10_000 else x)


def compute_raw_discount_bps(effective_factor_bps: int, weight_bps: int) -> int:
    w_bps = clamp_bps(int(weight_bps or 0))
    ef_bps = clamp_bps(int(effective_factor_bps or 0))
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


def parse_discount_token(tok: str) -> int:
    t = str(tok).strip().lower().replace("%", "")
    if t.endswith("bps"):
        try:
            bps = int(float(t[:-3]))
            return max(0, min(10_000, bps))
        except Exception:
            pass
    try:
        val = float(t)
    except Exception:
        return 0
    if val > 100:
        return max(0, min(10_000, int(round(val))))
    return max(0, min(10_000, int(round(val * 100))))


class Runtime:
    """
    Combines:
      - AsyncSubtensor client + RPC helpers (block/balance/stake)
      - Config → bid lines + summary
      - AuctionStart handling (including per-target-subnet stake gate)
    """

    def __init__(self, config, wallet, metagraph, state: StateStore):
        self.config = config
        self.wallet = wallet
        self.metagraph = metagraph
        self.state = state

        # Async subtensor + locks (created lazily)
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None
        self._rpc_lock: Optional[asyncio.Lock] = None

        # Discount mode (config-parsed)
        self._bids_raw_discount: bool = False

        # Block cache
        self._blk_cache_height: int = 0
        self._blk_cache_ts: float = 0.0

    # ---------------------- Config → Bid lines ----------------------

    def build_lines_from_config(self) -> List[BidLine]:
        cfg_miner = getattr(self.config, "miner", None)
        cfg_bids = getattr(cfg_miner, "bids", cfg_miner)

        nets = getattr(cfg_bids, "netuids", []) if cfg_bids else []
        amts = getattr(cfg_bids, "amounts", []) if cfg_bids else []
        discs = getattr(cfg_bids, "discounts", []) if cfg_bids else []

        raw_flag = False
        try:
            raw_flag = bool(getattr(cfg_bids, "raw_discount", False))
        except Exception:
            raw_flag = False
        self._bids_raw_discount = bool(raw_flag)

        netuids = [int(x) for x in list(nets or [])]
        amounts = [float(x) for x in list(amts or [])]
        discounts = [parse_discount_token(str(x)) for x in list(discs or [])]

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

    def log_cfg_summary(self, lines: List[BidLine]):
        mode = "RAW (pass-through)" if self._bids_raw_discount else "EFFECTIVE (weight-adjusted)"
        rows = [[i, ln.subnet_id, f"{ln.alpha:.4f} α", f"{ln.discount_bps} bps"] for i, ln in enumerate(lines)]
        if rows:
            pretty.table("Configured Bid Lines", ["#", "Subnet", "Alpha", "Discount (cfg)"], rows)
            pretty.log(f"[cyan]Discount interpretation[/cyan]: {mode}")

    # ---------------------- Chain (lazy) ----------------------

    async def _ensure_async_subtensor(self):
        if self._async_subtensor is None:
            stxn = bt.AsyncSubtensor(self.config)
            # Some versions require explicit initialize()
            init = getattr(stxn, "initialize", None)
            if callable(init):
                await init()
            self._async_subtensor = stxn
        if self._rpc_lock is None:
            self._rpc_lock = asyncio.Lock()

    async def get_current_block(self) -> int:
        await self._ensure_async_subtensor()
        st = self._async_subtensor
        now = __import__("time").time()

        if self._blk_cache_height > 0 and (now - self._blk_cache_ts) < 1.0:
            return self._blk_cache_height

        height = 0
        for name in ("get_current_block", "current_block", "block", "get_block_number"):
            try:
                attr = getattr(st, name, None)
                if attr is None:
                    continue
                res = attr() if callable(attr) else attr
                if inspect.isawaitable(res):
                    res = await res
                b = int(res or 0)
                if b > 0:
                    height = b
                    break
            except Exception:
                continue

        if height > 0:
            self._blk_cache_height = height
            self._blk_cache_ts = now
            return height

        # Last resort: substrate helper
        try:
            substrate = getattr(st, "substrate", None)
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

    @staticmethod
    def _balance_to_alpha(bal: Optional[bt.Balance]) -> float:
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

    async def get_validator_stakes_map(self, validator_hotkey_ss58: Optional[str], subnet_ids: List[int]) -> Dict[int, float]:
        """
        Per-subnet α that THIS miner's coldkey has delegated to the given validator hotkey.
        Missing/failed lookups resolve to 0.0.
        """
        await self._ensure_async_subtensor()
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

    async def get_alpha_balance(self, subnet_id: int, hotkey_ss58: str) -> Optional[float]:
        """
        Try to read α balance for a hotkey on a given subnet. Returns α (float) or None if unknown.
        NOTE: Only call α-specific methods to avoid confusing TAO balance with α.
        """
        await self._ensure_async_subtensor()
        st = self._async_subtensor
        for name in ("get_alpha_balance", "alpha_balance", "get_balance_alpha"):
            try:
                attr = getattr(st, name)
            except AttributeError:
                continue
            try:
                res = attr(hotkey_ss58, netuid=int(subnet_id)) if callable(attr) else attr
                if inspect.isawaitable(res):
                    res = await res
                # Normalize to float α
                if hasattr(res, "rao"):
                    return float(getattr(res, "rao")) / float(PLANCK)
                if hasattr(res, "value"):
                    return float(getattr(res, "value")) / float(PLANCK)
                try:
                    return float(res)
                except Exception:
                    continue
            except Exception:
                continue
        return None

    # ---------------------- AuctionStart handler ----------------------

    def _resolve_caller(self, syn: AuctionStartSynapse) -> tuple[Optional[int], str]:
        uid = getattr(syn, "validator_uid", None)
        hk = getattr(syn, "validator_hotkey", None)
        if uid is None:
            try:
                uid = int(getattr(getattr(syn, "dendrite", None), "origin", None))
            except Exception:
                uid = None
        if not hk and uid is not None and 0 <= uid < len(self.metagraph.axons):
            hk = getattr(self.metagraph.axons[uid], "hotkey", None)
        if not hk and hasattr(self.metagraph, "hotkeys") and uid is not None and uid < len(self.metagraph.hotkeys):
            hk = self.metagraph.hotkeys[uid]
        if not hk:
            hk = getattr(syn, "caller_hotkey", None)
        return uid, hk or ""

    def _validator_key(self, uid: Optional[int], hotkey: str) -> str:
        return hotkey if hotkey else (f"uid:{uid}" if uid is not None else "<unknown>")

    @staticmethod
    def _normalize_weights_bps(raw: Any) -> Dict[int, int]:
        """
        Accept dict-like {sid: bps} or list-like [(sid, bps), ...],
        coerce to {int(sid): clamp_bps(int(bps))}.
        """
        out: Dict[int, int] = {}
        if isinstance(raw, dict):
            items: Iterable[Tuple[Any, Any]] = raw.items()
        elif isinstance(raw, (list, tuple)):
            items = (tuple(x) if isinstance(x, (list, tuple)) else (None, None) for x in raw)
        else:
            return out

        for k, v in items:
            try:
                sid = int(k)
            except Exception:
                continue
            try:
                bps = clamp_bps(int(float(v)))
            except Exception:
                bps = 0
            out[int(sid)] = int(bps)
        return out

    async def handle_auction_start(self, synapse: AuctionStartSynapse, lines: List[BidLine]) -> AuctionStartSynapse:
        await self._ensure_async_subtensor()

        uid, caller_hot = self._resolve_caller(synapse)
        vkey = self._validator_key(uid, caller_hot)
        epoch = int(getattr(synapse, "epoch_index", 0) or 0)

        # Allowlist
        treasury_ck = self.state.treasuries.get(caller_hot) or self.state.treasuries.get(vkey)
        if not treasury_ck:
            note = f"validator not allowlisted: {caller_hot or vkey or '?'}"
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
            # best-effort type hygiene for anything we echo back
            return synapse

        budget_alpha = float(getattr(synapse, "auction_budget_alpha", 0.0) or 0.0)
        min_stake_alpha = float(getattr(synapse, "min_stake_alpha", S_MIN_ALPHA_MINER) or S_MIN_ALPHA_MINER)

        # Normalize weights map (handles dict or list-of-pairs, str->int coercion)
        weights_bps_in = getattr(synapse, "weights_bps", {}) or {}
        weights_bps: Dict[int, int] = self._normalize_weights_bps(weights_bps_in)
        try:
            # write normalized copy back so pydantic doesn't warn on outbound serialize
            synapse.weights_bps = dict(weights_bps)
        except Exception:
            pass

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

        # Fetch per-target-subnet delegated α with this validator
        candidate_subnets = [int(ln.subnet_id) for ln in lines if isfinite(ln.alpha) and ln.alpha > 0 and 0 <= ln.discount_bps <= 10_000]
        stake_by_subnet: Dict[int, float] = {}
        if candidate_subnets and (caller_hot or vkey):
            try:
                stake_by_subnet = await self.get_validator_stakes_map(caller_hot or vkey, candidate_subnets)
                rows = [[sid, f"{amt:.4f} α"] for sid, amt in sorted(stake_by_subnet.items())]
                if rows:
                    pretty.table("[blue]Available stake with validator (per subnet)[/blue]", ["Subnet", "α available"], rows)
            except Exception as _e:
                stake_by_subnet = {int(s): 0.0 for s in candidate_subnets}
                pretty.log(f"[yellow]Stake check failed; defaulting to 0 availability.[/yellow] {_e}")

        # Stake gate by *target-subnet* availability
        if min_stake_alpha > 0:
            has_any = any(amt >= min_stake_alpha for amt in stake_by_subnet.values())
            if not has_any:
                # Show top 3 availability for clarity
                top3 = sorted(stake_by_subnet.items(), key=lambda kv: kv[1], reverse=True)[:3]
                pretty.kv_panel(
                    "[yellow]Stake gate (per-subnet)[/yellow]",
                    [
                        ("epoch", epoch),
                        ("min_stake_α", f"{min_stake_alpha:.4f}"),
                        ("max_available_α", f"{(max(stake_by_subnet.values()) if stake_by_subnet else 0.0):.4f}"),
                        *[(f"sid{sid}", f"{amt:.4f} α") for sid, amt in top3],
                    ],
                    style="bold yellow",
                )
                self.state.status_tables()
                synapse.ack = True
                synapse.bids = []
                synapse.bids_sent = 0
                synapse.note = "stake gate (per-subnet)"
                return synapse

        # Track remaining stake per subnet to avoid oversubscription across lines
        remaining_by_subnet: Dict[int, float] = dict(stake_by_subnet)

        out_bids: List[List[Any]] = []
        rows_sent = []

        for ln in lines:
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

            send_alpha = min(float(ln.alpha), available)
            remaining_by_subnet[subnet_id] = max(0.0, available - send_alpha)

            if budget_alpha > 0 and send_alpha > budget_alpha:
                pretty.log(f"[grey58]Note:[/grey58] line α {send_alpha:.4f} exceeds validator budget α {budget_alpha:.4f}; may be partially filled.")

            # Compute discount to send
            if self._bids_raw_discount:
                send_disc_bps = int(ln.discount_bps)
                eff_factor_bps = int(round(weight_bps * (1.0 - (send_disc_bps / 10_000.0))))
                mode_note = "raw"
            else:
                send_disc_bps = compute_raw_discount_bps(ln.discount_bps, weight_bps)
                eff_factor_bps = int(round(weight_bps * (1.0 - (send_disc_bps / 10_000.0))))
                mode_note = "effective→raw"

            # Skip duplicates
            if self.state.has_bid(vkey, epoch, subnet_id, send_alpha, send_disc_bps):
                continue

            bid_id = hashlib.sha1(f"{vkey}|{epoch}|{subnet_id}|{send_alpha:.12f}|{send_disc_bps}".encode("utf-8")).hexdigest()[:10]

            # >>> IMPORTANT: send bid as 4-positional list (sid, alpha, disc_bps, bid_id)
            out_bids.append([int(subnet_id), float(send_alpha), int(send_disc_bps), str(bid_id)])

            self.state.remember_bid(vkey, epoch, subnet_id, send_alpha, send_disc_bps)

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

        if not out_bids:
            pretty.log("[grey]No bids were added (invalid/duplicate/insufficient stake).[/grey]")
        else:
            pretty.table(
                "[yellow]Bids Sent[/yellow]",
                ["Subnet", "Alpha", "Disc(cfg)", "Disc(send)", "Weight", "Eff", "BidID", "Epoch", "Mode"],
                rows_sent,
            )

        await self.state.save_async()
        self.state.status_tables()

        synapse.ack = True
        synapse.bids = out_bids
        synapse.bids_sent = int(len(out_bids))
        # leave note unset/empty for success to avoid type noise
        try:
            if getattr(synapse, "note", None) is not None and not synapse.bids:
                synapse.note = str(getattr(synapse, "note"))
            elif getattr(synapse, "note", None) is not None:
                # clear any stale incoming 'note' that might be non-str
                synapse.note = None
        except Exception:
            pass

        return synapse
