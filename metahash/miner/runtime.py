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
from metahash.miner.logging import (
    MinerPhase, LogLevel, miner_logger, 
    log_init, log_auction, log_commitments, log_settlement
)

from metahash.miner.models import BidLine
from metahash.miner.state import StateStore


def clamp_bps(x: int) -> int:
    return 0 if x < 0 else (10_000 if x > 10_000 else x)


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
        return int(float(str(x).strip()))
    except Exception:
        return default


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        return float(str(x).strip())
    except Exception:
        return default


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

        # Discount mode (config-parsed) - now always weight-adjusted
        self._bids_raw_discount: bool = False  # Deprecated, kept for compatibility

        # Block cache
        self._blk_cache_height: int = 0
        self._blk_cache_ts: float = 0.0
        

    # ---------------------- Config → Bid lines ----------------------

    def build_lines_from_config(self) -> List[BidLine]:
        log_init(LogLevel.MEDIUM, "Building bid lines from configuration", "config")
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
            log_init(LogLevel.CRITICAL, "Configuration error: bid parameter lengths don't match", "config", {
                "netuids_count": len(netuids),
                "amounts_count": len(amounts), 
                "discounts_count": len(discounts)
            })
            raise ValueError("miner.bids.* lengths must match (netuids, amounts, discounts)")

        lines: List[BidLine] = []
        skipped_count = 0
        for sid, amt, disc in zip(netuids, amounts, discounts):
            if amt <= 0:
                log_init(LogLevel.HIGH, "Skipping non-positive amount", "config", {
                    "subnet_id": sid,
                    "amount": amt
                })
                skipped_count += 1
                continue
            if disc < 0 or disc > 10_000:
                log_init(LogLevel.HIGH, "Skipping invalid discount", "config", {
                    "subnet_id": sid,
                    "discount_bps": disc
                })
                skipped_count += 1
                continue
            lines.append(BidLine(subnet_id=int(sid), alpha=float(amt), discount_bps=int(disc)))
        
        log_init(LogLevel.MEDIUM, "Bid lines built successfully", "config", {
            "total_lines": len(lines),
            "skipped_lines": skipped_count,
            "subnets": [line.subnet_id for line in lines]
        })
        return lines

    def log_cfg_summary(self, lines: List[BidLine]):
        mode = "EFFECTIVE-DISCOUNT (weight-adjusted)"
        rows = [[i, ln.subnet_id, f"{ln.alpha:.4f} α", f"{ln.discount_bps} bps"] for i, ln in enumerate(lines)]
        if rows:
            miner_logger.phase_table(
                MinerPhase.INITIALIZATION, "Configured Bid Lines", 
                ["#", "Subnet", "Alpha", "Discount (cfg)"], rows
            )

    # ---------------------- Chain (lazy) ----------------------

    async def _ensure_async_subtensor(self):
        if self._async_subtensor is None:
            log_init(LogLevel.MEDIUM, "Initializing async subtensor connection", "chain")
            # Create with just network parameter to avoid config issues that cause slice attachment
            try:
                network = getattr(self.config.subtensor, 'network', 'finney')
                stxn = bt.AsyncSubtensor(network=network)
            except Exception as e:
                log_init(LogLevel.HIGH, "Failed to create async subtensor with network, using fallback", "chain", {"error": str(e)})
                stxn = bt.AsyncSubtensor(self.config)
            init = getattr(stxn, "initialize", None)
            if callable(init):
                try:
                    await init()
                except Exception as e:
                    log_init(LogLevel.HIGH, "Failed to initialize async subtensor", "chain", {"error": str(e)})
                    # Don't fail completely, try to continue with uninitialized subtensor
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
        last_error = None
        
        # Try AsyncSubtensor methods first (serialize over the websocket)
        for name in ("get_current_block", "current_block", "block", "get_block_number"):
            try:
                async with self._rpc_lock:  # type: ignore[arg-type]
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
            except Exception as e:
                last_error = e
                continue

        # Try substrate method if AsyncSubtensor methods failed
        if height <= 0:
            try:
                async with self._rpc_lock:  # type: ignore[arg-type]
                    substrate = getattr(st, "substrate", None)
                    if substrate is not None:
                        meth = getattr(substrate, "get_block_number", None)
                        if callable(meth):
                            b = int(meth(None) or 0)
                            if b > 0:
                                height = b
            except Exception as e:
                last_error = e

        # If all methods failed, try to use metagraph block as fallback
        if height <= 0:
            try:
                metagraph_block = getattr(self.metagraph, "block", None)
                if metagraph_block and int(metagraph_block) > 0:
                    height = int(metagraph_block)
                    log_init(LogLevel.MEDIUM, "Using metagraph block as fallback", "chain", {
                        "block": height
                    })
                else:
                    log_init(LogLevel.HIGH, "Failed to get current block", "chain", {
                        "error": str(last_error) if last_error else "unknown",
                        "async_subtensor": str(type(st)),
                        "has_substrate": hasattr(st, "substrate"),
                        "metagraph_block": metagraph_block
                    })
            except Exception as e:
                log_init(LogLevel.HIGH, "Failed to get current block", "chain", {
                    "error": str(last_error) if last_error else "unknown",
                    "metagraph_fallback_error": str(e),
                    "async_subtensor": str(type(st)),
                    "has_substrate": hasattr(st, "substrate")
                })

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

        # Serialize RPCs to avoid concurrent recv() on the same websocket
        out: Dict[int, float] = {}
        for sid in unique_ids:
            try:
                async with self._rpc_lock:  # type: ignore[arg-type]
                    res = await st.get_stake(
                        coldkey_ss58=cold_ss58,
                        hotkey_ss58=validator_hotkey_ss58,
                        netuid=int(sid),
                        reuse_block=True,
                    )
                out[int(sid)] = self._balance_to_alpha(res)
            except Exception:
                out[int(sid)] = 0.0
        return out

    async def get_multi_validator_stakes_map(self, validator_hotkeys: List[str], subnet_ids: List[int]) -> Dict[int, float]:
        """
        Sum delegated α to our coldkey across multiple validator hotkeys per subnet.
        Used when --miner.bids.validators is provided to pool availability.
        """
        if not validator_hotkeys:
            return await self.get_validator_stakes_map(None, subnet_ids)
        totals: Dict[int, float] = {int(s): 0.0 for s in subnet_ids}
        for hk in validator_hotkeys:
            m = await self.get_validator_stakes_map(hk, subnet_ids)
            for sid, amt in m.items():
                totals[int(sid)] = float(totals.get(int(sid), 0.0)) + float(amt or 0.0)
        return totals

    async def get_alpha_balance(self, subnet_id: int, hotkey_ss58: str) -> Optional[float]:
        await self._ensure_async_subtensor()
        st = self._async_subtensor
        for name in ("get_alpha_balance", "alpha_balance", "get_balance_alpha"):
            try:
                async with self._rpc_lock:  # type: ignore[arg-type]
                    attr = getattr(st, name)
            except AttributeError:
                continue
            try:
                res = attr(hotkey_ss58, netuid=int(subnet_id)) if callable(attr) else attr
                if inspect.isawaitable(res):
                    res = await res
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
        uid_raw = getattr(syn, "validator_uid", None)
        uid: Optional[int]
        try:
            uid = int(uid_raw) if uid_raw is not None else None
        except Exception:
            uid = None

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

    def _sanitize_auction_synapse_inplace(self, syn: AuctionStartSynapse) -> None:
        # ints
        for name in ("validator_uid", "epoch_index", "auction_start_block"):
            if hasattr(syn, name):
                try:
                    setattr(syn, name, _as_int(getattr(syn, name)))
                except Exception:
                    pass
        # floats
        for name in ("auction_budget_alpha", "min_stake_alpha"):
            if hasattr(syn, name):
                try:
                    setattr(syn, name, _as_float(getattr(syn, name)))
                except Exception:
                    pass
        # dict normalization
        wb = getattr(syn, "weights_bps", {}) or {}
        setattr(syn, "weights_bps", {int(_as_int(k)): int(clamp_bps(_as_int(v))) for k, v in (wb.items() if isinstance(wb, dict) else {})})

    async def handle_auction_start(self, synapse: AuctionStartSynapse, lines: List[BidLine]) -> AuctionStartSynapse:
        await self._ensure_async_subtensor()

        # Sanitize inbound first (important for Axon serializer)
        self._sanitize_auction_synapse_inplace(synapse)

        uid, caller_hot = self._resolve_caller(synapse)
        vkey = self._validator_key(uid, caller_hot)

        epoch = _as_int(getattr(synapse, "epoch_index", 0))
        budget_alpha = _as_float(getattr(synapse, "auction_budget_alpha", 0.0))
        min_stake_alpha = _as_float(getattr(synapse, "min_stake_alpha", S_MIN_ALPHA_MINER) or S_MIN_ALPHA_MINER)
        auction_start_block = _as_int(getattr(synapse, "auction_start_block", 0))
        treasury_ck_req = str(getattr(synapse, "treasury_coldkey", "") or "")

        weights_bps_in = getattr(synapse, "weights_bps", {}) or {}
        weights_bps: Dict[int, int] = self._normalize_weights_bps(weights_bps_in)
        
        log_auction(LogLevel.MEDIUM, "Processing auction start request", "auction", {
            "validator": vkey,
            "epoch": epoch,
            "budget_alpha": budget_alpha,
            "min_stake_alpha": min_stake_alpha,
            "weights_count": len(weights_bps)
        })

        # ---------------- Allowlist ----------------
        treasury_ck = self.state.treasuries.get(caller_hot) or self.state.treasuries.get(vkey)
        if not treasury_ck:
            note = f"validator not allowlisted: {caller_hot or vkey or '?'}"
            log_auction(LogLevel.HIGH, "Auction start ignored - validator not allowlisted", "auction", {
                "validator": f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})",
                "reason": "not allowlisted"
            })
            miner_logger.phase_panel(
                MinerPhase.AUCTION, "AuctionStart Ignored",
                [("validator", f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})"),
                 ("reason", "not allowlisted"),
                 ("note", note)],
                LogLevel.HIGH
            )
            # Mutate response in-place with clean types
            synapse.ack = True
            synapse.retries_attempted = 0
            synapse.bids = []
            synapse.bids_sent = 0
            synapse.note = note
            synapse.epoch_index = int(epoch)
            synapse.auction_start_block = int(auction_start_block)
            synapse.min_stake_alpha = float(min_stake_alpha)
            synapse.auction_budget_alpha = float(budget_alpha)
            synapse.weights_bps = dict(weights_bps)
            synapse.treasury_coldkey = str(treasury_ck_req)
            synapse.validator_uid = uid
            synapse.validator_hotkey = caller_hot or None
            return synapse

        # Enhanced auction start logging with structured information
        log_auction(LogLevel.MEDIUM, "Auction start received from allowlisted validator", "auction", {
            "validator": f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})",
            "treasury_coldkey": treasury_ck[:8] + "…"
        })
        
        # Use the new clean auction summary method
        miner_logger.auction_summary(
            validator=f"{caller_hot or vkey} (uid={uid if uid is not None else '?'})",
            epoch=epoch,
            budget_alpha=budget_alpha,
            min_stake_alpha=min_stake_alpha,
            weights_count=len(weights_bps),
            discount_mode="EFFECTIVE-DISCOUNT (weight-adjusted)",
            timeline=f"bid now (e) → pay in (e+1={epoch+1}) → weights from e in (e+2={epoch+2})",
            treasury_src="LOCAL allowlist (pinned)"
        )
        

        candidate_subnets = [
            int(ln.subnet_id)
            for ln in lines
            if isfinite(ln.alpha)
            and ln.alpha > 0
            and 0 <= ln.discount_bps <= 10_000
            and not self.state.is_subnet_disabled(int(ln.subnet_id))
        ]
        stake_by_subnet: Dict[int, float] = {}
        if candidate_subnets and (caller_hot or vkey):
            log_auction(LogLevel.MEDIUM, "Checking validator stakes for candidate subnets", "stake", {
                "candidate_subnets": candidate_subnets,
                "validator": caller_hot or vkey
            })
            try:
                # If user provided --miner.bids.validators, compute availability across them
                cfg_miner = getattr(self.config, "miner", None)
                cfg_bids = getattr(cfg_miner, "bids", cfg_miner)
                try:
                    cfg_validators = list(getattr(cfg_bids, "validators", []) or [])
                except Exception:
                    cfg_validators = []
                if cfg_validators:
                    stake_by_subnet = await self.get_multi_validator_stakes_map(cfg_validators, candidate_subnets)
                else:
                    stake_by_subnet = await self.get_validator_stakes_map(caller_hot or vkey, candidate_subnets)
                # Use the new clean stake summary method
                miner_logger.stake_summary(stake_by_subnet, min_stake_alpha)
            except Exception as _e:
                stake_by_subnet = {int(s): 0.0 for s in candidate_subnets}
                log_auction(LogLevel.HIGH, "Stake check failed, defaulting to 0 availability", "stake", {"error": str(_e)})

        if min_stake_alpha > 0:
            has_any = any(amt >= min_stake_alpha for amt in stake_by_subnet.values())
            if not has_any:
                top3 = sorted(stake_by_subnet.items(), key=lambda kv: kv[1], reverse=True)[:3]
                log_auction(LogLevel.HIGH, "Stake gate triggered - insufficient stake for any subnet", "stake", {
                    "epoch": epoch,
                    "min_stake_alpha": min_stake_alpha,
                    "max_available": max(stake_by_subnet.values()) if stake_by_subnet else 0.0
                })
                miner_logger.phase_panel(
                    MinerPhase.AUCTION, "Stake Gate (per-subnet)",
                    [
                        ("epoch", epoch),
                        ("min_stake_α", f"{min_stake_alpha:.4f}"),
                        ("max_available_α", f"{(max(stake_by_subnet.values()) if stake_by_subnet else 0.0):.4f}"),
                        *[(f"sid{sid}", f"{amt:.4f} α") for sid, amt in top3],
                    ],
                    LogLevel.HIGH
                )
                self.state.status_tables()

                synapse.ack = True
                synapse.retries_attempted = 0
                synapse.bids = []
                synapse.bids_sent = 0
                synapse.note = "stake gate (per-subnet)"
                synapse.epoch_index = int(epoch)
                synapse.auction_start_block = int(auction_start_block)
                synapse.min_stake_alpha = float(min_stake_alpha)
                synapse.auction_budget_alpha = float(budget_alpha)
                synapse.weights_bps = dict(weights_bps)
                synapse.treasury_coldkey = str(treasury_ck_req)
                synapse.validator_uid = uid
                synapse.validator_hotkey = caller_hot or None
                return synapse

        remaining_by_subnet: Dict[int, float] = dict(stake_by_subnet)

        # out_bids are 4-tuples: (int subnet_id, float alpha, int discount_bps, str bid_id)
        out_bids: List[Tuple[int, float, int, str]] = []
        rows_sent = []

        log_auction(LogLevel.MEDIUM, "Processing bid lines", "bidding", {
            "total_lines": len(lines),
            "available_subnets": len(remaining_by_subnet)
        })
        
        for ln in lines:
            # skip disabled subnets proactively
            if self.state.is_subnet_disabled(int(ln.subnet_id)):
                log_auction(LogLevel.MEDIUM, "Bid skipped - subnet disabled", "bidding", {
                    "subnet_id": int(ln.subnet_id)
                })
                continue
            if not isfinite(ln.alpha) or ln.alpha <= 0:
                log_auction(LogLevel.HIGH, "Invalid alpha - skipping line", "bidding", {
                    "subnet_id": ln.subnet_id,
                    "alpha": ln.alpha
                })
                continue
            if not (0 <= ln.discount_bps <= 10_000):
                log_auction(LogLevel.HIGH, "Invalid discount - skipping line", "bidding", {
                    "subnet_id": ln.subnet_id,
                    "discount_bps": ln.discount_bps
                })
                continue

            subnet_id = int(ln.subnet_id)
            weight_bps = weights_bps.get(subnet_id, 10_000)

            available = float(remaining_by_subnet.get(subnet_id, 0.0))
            if available <= 0.0:
                log_auction(LogLevel.HIGH, "Bid skipped - insufficient delegated stake", "bidding", {
                    "subnet_id": subnet_id,
                    "cfg_alpha": ln.alpha,
                    "available_alpha": available
                })
                miner_logger.phase_panel(
                    MinerPhase.AUCTION, "Bid Skipped – Insufficient Delegated Stake",
                    [("subnet", subnet_id), ("cfg α", f"{ln.alpha:.4f}"), ("available α", f"{available:.4f}")],
                    LogLevel.HIGH
                )
                continue

            send_alpha = min(float(ln.alpha), available)
            remaining_by_subnet[subnet_id] = max(0.0, available - send_alpha)


            # ln.discount_bps is the discount the miner wants to pay vs current price
            # subnet_weight < 1.0 means validator already applies a discount (weight discount)
            # We need to calculate the effective additional discount on top of weight discount
            configured_discount_bps = int(ln.discount_bps)
            weight_factor = weight_bps / 10_000.0
            
            # Calculate effective discount: configured_discount - (1 - weight_factor)
            # This gives us the additional discount on top of the weight discount
            effective_discount = (configured_discount_bps / 10_000.0) - (1.0 - weight_factor)
            
            # If effective discount is too small or negative, skip this bid
            # Threshold: minimum 0.5% effective discount to be worth bidding
            min_effective_discount = 0.005  # 0.5%
            if effective_discount < min_effective_discount:
                log_auction(LogLevel.MEDIUM, "Bid skipped - effective discount too small", "bidding", {
                    "subnet_id": subnet_id,
                    "configured_discount_bps": configured_discount_bps,
                    "weight_factor": weight_factor,
                    "effective_discount": effective_discount,
                    "min_required": min_effective_discount
                })
                continue
            
            send_disc_bps = int(round(effective_discount * 10_000))
            
            # Calculate effective factor for logging
            eff_factor_bps = int(round(weight_bps * (1.0 - (send_disc_bps / 10_000.0))))
            mode_note = "effective-adjusted"

            if self.state.has_bid(vkey, epoch, subnet_id, send_alpha, send_disc_bps):
                continue

            bid_id = hashlib.sha256(f"{vkey}|{epoch}|{subnet_id}|{send_alpha:.12f}|{send_disc_bps}".encode("utf-8")).hexdigest()[:10]

            out_bids.append((int(subnet_id), float(send_alpha), int(send_disc_bps), str(bid_id)))

            self.state.remember_bid(vkey, epoch, subnet_id, send_alpha, send_disc_bps)

            rows_sent.append([
                subnet_id,
                f"{send_alpha:.4f} α",
                f"{configured_discount_bps} bps (cfg)",
                f"{send_disc_bps} bps (eff)",
                f"{weight_bps} w_bps",
                f"{eff_factor_bps} eff_bps",
                bid_id,
                epoch,
                mode_note
            ])

        if not out_bids:
            log_auction(LogLevel.HIGH, "No bids were added", "bidding", {
                "reason": "invalid/duplicate/insufficient stake"
            })
        else:
            # Enhanced bids sent table with better formatting
            log_auction(LogLevel.MEDIUM, "Bids sent successfully", "bidding", {
                "bid_count": len(out_bids)
            })
            
            # Use the new clean bid summary method
            miner_logger.bid_summary(rows_sent, epoch)

        await self.state.save_async()
        self.state.status_tables()

        # Mutate the same inbound synapse with clean types
        synapse.ack = True
        synapse.retries_attempted = 0
        # Convert tuples to dictionaries as expected by protocol
        synapse.bids = [{"subnet_id": bid[0], "alpha": bid[1], "discount_bps": bid[2], "bid_id": bid[3]} for bid in out_bids]
        synapse.bids_sent = int(len(out_bids))
        synapse.note = None

        synapse.epoch_index = int(epoch)
        synapse.auction_start_block = int(auction_start_block)
        synapse.min_stake_alpha = float(min_stake_alpha)
        synapse.auction_budget_alpha = float(budget_alpha)
        synapse.weights_bps = dict({int(k): int(v) for k, v in weights_bps.items()})
        synapse.treasury_coldkey = str(treasury_ck_req)
        synapse.validator_uid = uid
        synapse.validator_hotkey = caller_hot or None

        log_auction(LogLevel.MEDIUM, "Auction start processing completed", "auction", {
            "bids_sent": len(out_bids),
            "ack": True
        })
        return synapse
