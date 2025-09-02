# ╭────────────────────────────────────────────────────────────────────╮
# neurons/validator.py – SN‑73 v2 (commitments + reputation caps)      #
# Pretty logs for testnet using Rich (with safe fallback).             #
# ╰────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set

import bittensor as bt
from bittensor import BLOCKTIME

from metahash.base.utils.logging import ColoredLogger as clog

# ── config ------------------------------------------------------------- #
import metahash.config as mh_cfg

from metahash.config import (
    VERSION,
    PLANCK,
    AUCTION_BUDGET_ALPHA,
    PAYMENT_WINDOW_BLOCKS,
    MAX_BIDS_PER_MINER,
    S_MIN_ALPHA_MINER,
    S_MIN_MASTER_VALIDATOR,
    STRATEGY_PATH,
    VALIDATOR_TREASURIES,
    START_V2_BLOCK,
    TESTING,
    FORCE_BURN_WEIGHTS,
    PARTIAL_PAYMENT_PENALTY_BPS,
    POST_PAYMENT_CHECK_DELAY_BLOCKS,
    JAIL_EPOCHS_NO_PAY,
    JAIL_EPOCHS_PARTIAL,
    # reputation caps
    REPUTATION_ENABLED,
    REPUTATION_BETA,
    REPUTATION_BASELINE_CAP_FRAC,
    REPUTATION_MAX_CAP_FRAC,
)

from metahash.validator.strategy import load_weights
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.validator.alpha_transfers import AlphaTransfersScanner
from metahash.validator.epoch_validator import EpochValidatorNeuron
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.protocol import (
    BidSynapse, AckSynapse, WinSynapse, AuctionStartSynapse
)
from metahash.utils.commitments import (
    write_plain_commitment_json,
    read_all_plain_commitments,
    read_plain_commitment,
)
from metahash.utils.pretty_logs import pretty  # ← Rich logs (fallback-safe)


# ───────────────────────── data models ──────────────────────────────── #

@dataclass(slots=True)
class _Bid:
    epoch: int
    subnet_id: int
    alpha: float                 # α requested for this bid (payment target)
    miner_uid: int
    coldkey: str
    discount_bps: int            # valuation discount (NOT payment)
    weight_snap: float = 0.0     # snapshot at acceptance (0..1)
    idx: int = 0                 # stable order per miner+subnet


# ──────────────────────────  Validator  ─────────────────────────────── #

class Validator(EpochValidatorNeuron):
    """
    v2 with auto master detection, commitments-based sharing, and
    reputation-capped per-coldkey allocations at clearing time.

    Head E order:
      1) settle E−2 and set weights,
      2) masters broadcast AuctionStart for E,
      3) masters clear & publish invoices (commitment) for E−1.

    Bids placed during epoch e → published at head e+1 → payments in e+1 → settled at head e+2.
    """

    # ─── init ─────────────────────────────────────────────────────────
    def __init__(self, config=None):
        super().__init__(config=config)

        self.hotkey_ss58: str = self.wallet.hotkey.ss58_address

        # strategy (weights per subnet), accepts [0,1] or bps
        self.weights_bps = load_weights(Path(STRATEGY_PATH))

        # epoch-local bid state (masters only)
        self._bid_book: Dict[int, Dict[Tuple[int, int], _Bid]] = defaultdict(dict)
        self._ck_uid_epoch: Dict[int, Dict[str, int]] = defaultdict(dict)
        self._ck_subnets_bid: Dict[int, Dict[str, int]] = defaultdict(dict)  # distinct subnet count per coldkey

        # persistence
        self._validated_epochs: set[int] = self._load_set("validated_epochs.json")
        self._ck_jail_until_epoch: Dict[str, int] = self._load_dict_int("jailed_coldkeys.json")
        self._reputation: Dict[str, float] = self._load_dict_float("reputation.json")  # R in [0,1]

        # chain I/O
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None
        self._scanner: Optional[AlphaTransfersScanner] = None
        self._rpc_lock: asyncio.Lock = asyncio.Lock()

        # remember when we broadcast per-epoch
        self._auction_start_sent_for: Optional[int] = None

        pretty.banner(
            f"Validator v{VERSION} initialized",
            f"hotkey={self.hotkey_ss58} | netuid={self.config.netuid}",
            style="bold magenta",
        )

    # ─── persistence helpers ──────────────────────────────────────────
    def _load_set(self, filename: str) -> set[int]:
        path = Path.cwd() / filename
        try:
            return set(json.loads(path.read_text()))
        except Exception:
            return set()

    def _save_set(self, s: set[int], filename: str):
        path = Path.cwd() / filename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(s)))
        tmp.replace(path)

    def _load_dict_int(self, filename: str) -> Dict[str, int]:
        path = Path.cwd() / filename
        try:
            return {str(k): int(v) for k, v in json.loads(path.read_text()).items()}
        except Exception:
            return {}

    def _save_dict_int(self, d: Dict[str, int], filename: str):
        path = Path.cwd() / filename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, sort_keys=True))
        tmp.replace(path)

    def _load_dict_float(self, filename: str) -> Dict[str, float]:
        path = Path.cwd() / filename
        try:
            return {str(k): float(v) for k, v in json.loads(path.read_text()).items()}
        except Exception:
            return {}

    def _save_dict_float(self, d: Dict[str, float], filename: str):
        path = Path.cwd() / filename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, sort_keys=True))
        tmp.replace(path)

    # ─── async‑Subtensor getter ───────────────────────────────────────
    async def _stxn(self):
        if self._async_subtensor is None:
            stxn = bt.AsyncSubtensor(network=self.config.subtensor.network)
            await stxn.initialize()
            self._async_subtensor = stxn
        return self._async_subtensor

    # ─── utilities ────────────────────────────────────────────────────
    def _norm_weight(self, subnet_id: int) -> float:
        raw = float(self.weights_bps[subnet_id])
        if raw > 1.0:
            raw = raw / 10_000.0
        return max(0.0, min(1.0, raw))

    def _hotkey_to_uid(self) -> Dict[str, int]:
        mapping: Dict[str, int] = {}
        for i, ax in enumerate(self.metagraph.axons):
            hk = getattr(ax, "hotkey", None)
            if hk:
                mapping[hk] = i
        if not mapping and hasattr(self.metagraph, "hotkeys"):
            for i, hk in enumerate(getattr(self.metagraph, "hotkeys")):
                mapping[hk] = i
        return mapping

    def _is_master_now(self) -> bool:
        tre = VALIDATOR_TREASURIES.get(self.hotkey_ss58)
        if not tre:
            return False
        uid = self._hotkey_to_uid().get(self.hotkey_ss58)
        if uid is None:
            return False
        try:
            return float(self.metagraph.stake[uid]) >= S_MIN_MASTER_VALIDATOR
        except Exception:
            return False

    def _master_budget_share(self) -> float:
        """Return this validator's budget multiplier in [0,1] among active masters."""
        hk2uid = self._hotkey_to_uid()
        stakes: Dict[str, float] = {}
        total = 0.0
        shares: List[Tuple[str, int, float, float]] = []  # for pretty log
        for hk in VALIDATOR_TREASURIES.keys():           # ← only listed hotkeys count
            uid = hk2uid.get(hk)
            if uid is None or uid >= len(self.metagraph.stake):
                continue
            st = float(self.metagraph.stake[uid])
            if st >= S_MIN_MASTER_VALIDATOR:             # ← only above threshold
                stakes[hk] = st
                total += st
        my_stake = stakes.get(self.hotkey_ss58, 0.0)
        share = (my_stake / total) if (total > 0 and my_stake > 0) else 0.0

        # pretty table for active masters
        if total > 0:
            for hk, st in stakes.items():
                uid = hk2uid.get(hk, -1)
                shares.append((hk, uid, st, AUCTION_BUDGET_ALPHA * (st / total)))
            pretty.show_master_shares(shares)
        else:
            pretty.log("[yellow]No active masters meet the threshold this epoch.[/yellow]")

        return share

    # ─── RPC entry (BidSynapse) – masters only ────────────────────────
    async def server_step(self, syn: BidSynapse) -> AckSynapse:  # type: ignore[override]
        uid = syn.dendrite.origin
        epoch = self.epoch_index

        if self.block < START_V2_BLOCK:
            return AckSynapse(**syn.dict(), accepted=False, error="auction not started (v2 gating)")

        if not self._is_master_now():
            return AckSynapse(**syn.dict(), accepted=False, error="bids disabled on non-master validator")

        # epoch‑jail (by coldkey)
        ck = self.metagraph.coldkeys[uid]
        jail_upto = self._ck_jail_until_epoch.get(ck, -1)
        if jail_upto is not None and epoch < jail_upto:
            return AckSynapse(**syn.dict(), accepted=False, error=f"jailed until epoch {jail_upto}")

        # stake gate (miner’s UID)
        stake_alpha = self.metagraph.stake[uid]
        if stake_alpha < S_MIN_ALPHA_MINER:
            return AckSynapse(**syn.dict(), accepted=False,
                              error=f"stake {stake_alpha:.3f} α < S_MIN_ALPHA_MINER")

        # sanity‑checks
        if not isfinite(syn.alpha) or syn.alpha <= 0:
            return AckSynapse(**syn.dict(), accepted=False, error="invalid α")
        if syn.alpha > AUCTION_BUDGET_ALPHA:
            return AckSynapse(**syn.dict(), accepted=False, error="α exceeds max per bid")
        if not (0 <= syn.discount_bps <= 10_000):
            return AckSynapse(**syn.dict(), accepted=False, error="discount out of range")

        # enforce one uid per coldkey (per epoch per master)
        ck_map = self._ck_uid_epoch[epoch]
        existing_uid = ck_map.get(ck)
        if existing_uid is not None and existing_uid != uid:
            return AckSynapse(**syn.dict(), accepted=False,
                              error=f"coldkey already bidding as uid={existing_uid}")

        # per‑coldkey bid limit on distinct subnets
        count = self._ck_subnets_bid[epoch].get(ck, 0)
        first_on_subnet = True
        key = (uid, syn.subnet_id)
        if key in self._bid_book[epoch]:
            first_on_subnet = False
        if first_on_subnet and count >= MAX_BIDS_PER_MINER:
            return AckSynapse(**syn.dict(), accepted=False, error="per-coldkey bid limit reached")

        # accept / upsert
        ck_map.setdefault(ck, uid)
        if first_on_subnet:
            self._ck_subnets_bid[epoch][ck] = count + 1

        idx = len([b for (u, s), b in self._bid_book[epoch].items() if u == uid and s == syn.subnet_id])

        self._bid_book[epoch][key] = _Bid(
            epoch=epoch,
            subnet_id=syn.subnet_id,
            alpha=syn.alpha,
            miner_uid=uid,
            coldkey=ck,
            discount_bps=syn.discount_bps,
            weight_snap=self._norm_weight(syn.subnet_id),
            idx=idx,
        )
        return AckSynapse(**syn.dict(), accepted=True)

    # ───────────────────────── broadcast helper (masters) ───────────── #
    async def _broadcast_auction_start(self):
        if not self._is_master_now():
            return
        if getattr(self, "_auction_start_sent_for", None) == self.epoch_index:
            return
        if self.block < START_V2_BLOCK:
            return

        share = self._master_budget_share()
        my_budget = AUCTION_BUDGET_ALPHA * share

        if my_budget <= 0:
            pretty.log("[yellow]Budget share is zero – not broadcasting AuctionStart.[/yellow]")
            self._auction_start_sent_for = self.epoch_index
            return

        axons = self.metagraph.axons
        if not axons:
            return

        syn = AuctionStartSynapse(
            epoch_index=self.epoch_index,
            auction_start_block=self.block,
            min_stake_alpha=S_MIN_ALPHA_MINER,
            auction_budget_alpha=my_budget,
            weights_bps=dict(self.weights_bps),
            treasury_coldkey=VALIDATOR_TREASURIES.get(self.hotkey_ss58, ""),
        )
        await self.dendrite(axons=axons, synapse=syn, deserialize=False, timeout=8)

        self._auction_start_sent_for = self.epoch_index
        pretty.kv_panel(
            "AuctionStart Broadcast",
            [
                ("epoch", self.epoch_index),
                ("block", self.block),
                ("budget α", f"{my_budget:.3f}"),
                ("treasury", VALIDATOR_TREASURIES.get(self.hotkey_ss58, "")),
            ],
            style="bold cyan",
        )

    # ─────────────────────────── forward() ──────────────────────────── #
    async def forward(self):
        """
        Per-epoch driver (weights-first):
          1) Settle epoch (e−2) and set weights (from commitments).
          2) Masters: broadcast auction start for current epoch e.
          3) Masters: clear and publish invoices for epoch (e−1) to commitments.
        """
        await self._stxn()

        if self.block < START_V2_BLOCK:
            pretty.banner(
                "Waiting for START_V2_BLOCK",
                f"current_block={self.block} < start_v2={START_V2_BLOCK}",
                style="bold yellow",
            )
            return

        # Epoch banner
        pretty.banner(
            f"Epoch {self.epoch_index}",
            f"head_block={self.block} | start={self.epoch_start_block} | end={self.epoch_end_block}",
            style="bold white",
        )

        # 1) WEIGHTS FIRST: settle older epoch (e−2)
        await self._settle_and_set_weights_all_masters(epoch_to_settle=self.epoch_index - 2)

        # 2) Masters broadcast auction start for current epoch (e)
        await self._broadcast_auction_start()

        # 3) Masters clear previous epoch’s auction (e−1) and publish to commitments
        await self._clear_publish_commitment(epoch_to_clear=self.epoch_index - 1)

        # cleanup bid books older than (e−1)
        self._bid_book.pop(self.epoch_index - 2, None)
        self._ck_uid_epoch.pop(self.epoch_index - 2, None)
        self._ck_subnets_bid.pop(self.epoch_index - 2, None)

    # ─── (masters) Auction clearing (epoch e-1) → publish commitment ──
    async def _clear_publish_commitment(self, epoch_to_clear: int):
        if epoch_to_clear < 0:
            return
        if not self._is_master_now():
            return

        bid_map = self._bid_book.get(epoch_to_clear, {})
        if not bid_map:
            pretty.log("[grey]No bids to clear this epoch (master).[/grey]")
            return

        share = self._master_budget_share()
        my_budget = AUCTION_BUDGET_ALPHA * share
        if my_budget <= 0:
            pretty.log("[yellow]Budget share is zero – skipping clear.[/yellow]")
            return

        # ---------- reputation-based per-coldkey caps ----------
        if REPUTATION_ENABLED:
            cap_alpha_by_ck: Dict[str, float] = {}
            for b in bid_map.values():
                ck = b.coldkey
                R = max(0.0, min(1.0, float(self._reputation.get(ck, 0.0))))
                cap_frac = REPUTATION_BASELINE_CAP_FRAC + R * (REPUTATION_MAX_CAP_FRAC - REPUTATION_BASELINE_CAP_FRAC)
                cap_frac = max(REPUTATION_BASELINE_CAP_FRAC, min(REPUTATION_MAX_CAP_FRAC, cap_frac))
                cap_alpha_by_ck[ck] = my_budget * cap_frac
            pretty.show_caps(cap_alpha_by_ck)
        else:
            cap_alpha_by_ck = defaultdict(lambda: my_budget)

        # ---------- choose winners with caps, weight-first ----------
        bids = list(bid_map.values())
        bids.sort(key=lambda b: (b.weight_snap, -(10_000 - b.discount_bps), b.alpha, -b.idx), reverse=True)

        budget = my_budget
        winners: List[_Bid] = []
        allocated_by_ck: Dict[str, float] = defaultdict(float)

        for b in bids:
            if budget <= 0:
                break
            cap_rem = cap_alpha_by_ck.get(b.coldkey, my_budget) - allocated_by_ck[b.coldkey]
            if cap_rem <= 0:
                continue
            take = min(b.alpha, budget, cap_rem)
            if take <= 0:
                continue
            winners.append(replace(b, alpha=take))
            budget -= take
            allocated_by_ck[b.coldkey] += take

        if not winners:
            pretty.log("[red]No winners after applying caps & budget.[/red]")
            return

        # winners summary by coldkey
        winners_aggr: Dict[str, float] = defaultdict(float)
        for w in winners:
            winners_aggr[w.coldkey] += w.alpha
        pretty.show_winners(list(winners_aggr.items()))

        deadline = self.block + PAYMENT_WINDOW_BLOCKS
        start_blk = self.epoch_start_block  # include pre-bids within the epoch
        treasury = VALIDATOR_TREASURIES.get(self.hotkey_ss58, "")

        # Notify winners (off-chain RPC)
        for w in winners:
            try:
                await self.dendrite(
                    axons=[self.metagraph.axons[w.miner_uid]],
                    synapse=WinSynapse(
                        subnet_id=w.subnet_id,
                        alpha=w.alpha,
                        clearing_discount_bps=w.discount_bps,
                        pay_deadline_block=deadline,
                    ),
                    deserialize=False,
                )
            except Exception as e:
                clog.warning(f"WinSynapse to uid={w.miner_uid} failed: {e}", color="red")

        # Build compact snapshot (ring buffer: keep last 2)
        inv: Dict[str, Dict] = {}
        for w in winners:
            key = str(w.miner_uid)
            if key not in inv:
                inv[key] = {"ck": w.coldkey, "ln": []}
            inv[key]["ln"].append([
                int(w.subnet_id),
                int(w.discount_bps),
                int(round(w.weight_snap * 10_000)),
                int(w.alpha * PLANCK),  # required RAO for this line (full α)
            ])

        new_snap = {"e": int(epoch_to_clear), "t": treasury, "as": int(start_blk), "de": int(deadline), "inv": inv}
        st = await self._stxn()

        # Try to include the previous snapshot (keep at most 1 older)
        try:
            prev = await read_plain_commitment(st, netuid=self.config.netuid, hotkey_ss58=self.hotkey_ss58)
        except Exception:
            prev = None

        snaps: List[Dict] = [new_snap]
        if isinstance(prev, dict):
            old_list = []
            if "sn" in prev and isinstance(prev["sn"], list):
                old_list = [s for s in prev["sn"] if isinstance(s, dict)]
            elif "e" in prev:
                old_list = [prev]
            for s in old_list:
                if isinstance(s, dict) and s.get("e") != epoch_to_clear:
                    snaps.append(s)
                    break

        payload = {"v": 2, "sn": snaps}

        # Publish commitment
        try:
            ok = await write_plain_commitment_json(st, wallet=self.wallet, data=payload, netuid=self.config.netuid)
            if ok:
                pretty.kv_panel(
                    "Commitment Published",
                    [
                        ("epoch_cleared", epoch_to_clear),
                        ("deadline_block", deadline),
                        ("budget_left α", f"{budget:.3f}"),
                        ("treasury", treasury),
                    ],
                    style="bold green",
                )
            else:
                pretty.log("[yellow]Commitment publish returned False.[/yellow]")
        except ValueError as ve:
            pretty.log(f"[red]Commitment payload too large: {ve}[/red]")
        except Exception as e:
            pretty.log(f"[red]Commitment publish failed: {e}[/red]")

    # ─── (all) Settlement for epoch e-2 across ALL masters ────────────
    async def _settle_and_set_weights_all_masters(self, epoch_to_settle: int):
        if epoch_to_settle < 0 or epoch_to_settle in self._validated_epochs:
            return

        st = await self._stxn()
        try:
            commits = await read_all_plain_commitments(st, netuid=self.config.netuid, block=None)
        except Exception as e:
            pretty.log(f"[red]Commitment read_all failed: {e}[/red]")
            return

        masters_hotkeys: Set[str] = set(VALIDATOR_TREASURIES.keys())
        snapshots: List[Dict] = []

        for hk in masters_hotkeys:
            data = commits.get(hk)
            if not isinstance(data, dict):
                continue
            cand_list: List[Dict] = []
            if "sn" in data and isinstance(data["sn"], list):
                cand_list = [s for s in data["sn"] if isinstance(s, dict)]
            elif "e" in data:
                cand_list = [data]
            chosen = None
            for s in cand_list:
                if s.get("e") == epoch_to_settle:
                    chosen = s
                    break
            if chosen:
                tre = chosen.get("t", "")
                if tre and tre != VALIDATOR_TREASURIES.get(hk, tre):
                    pretty.log(f"[yellow]Treasury mismatch in commitment for {hk}[/yellow]")
                snapshots.append({"hk": hk, **chosen})

        if not snapshots:
            pretty.log("[grey]No master commitments found for this settlement epoch.[/grey]")
            return

        # time window: min(start) .. max(deadline) + delay
        start_block = min(int(s["as"]) for s in snapshots)
        end_block = max(int(s["de"]) for s in snapshots)
        pretty.show_settlement_window(epoch_to_settle, start_block, end_block, POST_PAYMENT_CHECK_DELAY_BLOCKS, len(snapshots))
        end_block += POST_PAYMENT_CHECK_DELAY_BLOCKS

        if self.block < end_block:
            # small ETA banner
            remain = end_block - self.block
            eta_s = remain * BLOCKTIME
            pretty.kv_panel("Waiting for window to close", [("blocks_left", remain), ("~eta", f"{int(eta_s//60)}m{int(eta_s%60):02d}s}")], style="bold yellow")
            return

        # Prepare scanner; accept all, filter by treasuries after
        if self._scanner is None:
            self._scanner = AlphaTransfersScanner(await self._stxn(), dest_coldkey=None, rpc_lock=self._rpc_lock)

        events: List[TransferEvent] = await self._scanner.scan(start_block, end_block)

        master_treasuries: Set[str] = {VALIDATOR_TREASURIES.get(s["hk"], s.get("t", "")) for s in snapshots}
        subnets_needed: Set[int] = set()
        ck_uid_map: Dict[str, int] = {}

        for s in snapshots:
            inv = s.get("inv", {})
            for uid_s, inv_d in inv.items():
                uid = int(uid_s)
                ck = inv_d.get("ck")
                if ck and ck not in ck_uid_map:
                    ck_uid_map[ck] = uid
                for i, ln in enumerate(inv_d.get("ln", [])):
                    if isinstance(ln, list) and len(ln) >= 4:
                        subnets_needed.add(int(ln[0]))

        # Pre-fetch price cache for fallback burn valuation (no slippage)
        price_cache: Dict[int, float] = {}
        price_fn = self._make_price(start_block, end_block)
        for sid in sorted(subnets_needed):
            p = await price_fn(sid, start_block, end_block)
            price_cache[sid] = float(getattr(p, "tao", 0.0) or 0.0)

        # Filter only events to master treasuries and from known coldkeys
        filtered_events = [ev for ev in events if ev.dest_coldkey in master_treasuries and ev.src_coldkey in ck_uid_map]

        # RAO per (uid, dest_treasury, subnet)
        rao_by_uid_dest_subnet: Dict[Tuple[int, str, int], int] = defaultdict(int)
        for ev in filtered_events:
            uid = ck_uid_map.get(ev.src_coldkey)
            if uid is None:
                continue
            rao_by_uid_dest_subnet[(uid, ev.dest_coldkey, ev.subnet_id)] += int(ev.amount_rao)

        # TAO per (uid, dest_treasury, subnet) via canonical pipeline
        miner_uids_all: List[int] = list(self.get_miner_uids())
        tao_by_uid_dest_subnet: Dict[Tuple[int, str, int], float] = {}

        for sid in sorted(subnets_needed):
            sub_events = [ev for ev in filtered_events if ev.subnet_id == sid]
            if not sub_events:
                continue
            for tre in master_treasuries:
                evs = [ev for ev in sub_events if ev.dest_coldkey == tre]
                if not evs:
                    continue
                rewards_list_sub = await compute_epoch_rewards(
                    miner_uids=miner_uids_all,
                    events=evs,
                    scanner=None,
                    pricing=self._make_price(start_block, end_block),
                    pool_depth_of=self._make_depth(start_block, end_block),
                    uid_of_coldkey=self._uid_resolver_from_map(ck_uid_map),
                    start_block=start_block,
                    end_block=end_block,
                )
                for i, uid in enumerate(miner_uids_all):
                    val = float(rewards_list_sub[i])
                    if val > 0:
                        tao_by_uid_dest_subnet[(uid, tre, sid)] = val

        # Build scores + penalties; discrete line allocation; compute burn and rep inputs
        scores_by_uid: Dict[int, float] = {uid: 0.0 for uid in miner_uids_all}
        offenders_nopay: Set[str] = set()
        offenders_partial: Set[str] = set()
        paid_effective_by_ck: Dict[str, float] = defaultdict(float)
        burn_total_value: float = 0.0

        def line_key(ln_tuple: Tuple[int, int, int, int, int]) -> Tuple[float, int, int]:
            sid, disc_bps, w_bps, req_rao, idx = ln_tuple
            score = (w_bps / 10_000.0) * (1.0 - (disc_bps / 10_000.0))
            return (score, req_rao, -idx)

        for s in snapshots:
            tre = VALIDATOR_TREASURIES.get(s["hk"], s.get("t", ""))
            inv = s.get("inv", {})

            for uid_s, inv_d in inv.items():
                uid = int(uid_s)
                ck = inv_d.get("ck")

                by_subnet: Dict[int, List[Tuple[int, int, int, int, int]]] = defaultdict(list)
                for idx_line, ln in enumerate(inv_d.get("ln", [])):
                    if not (isinstance(ln, list) and len(ln) >= 4):
                        continue
                    sid, disc_bps, w_bps, req_rao = int(ln[0]), int(ln[1]), int(ln[2]), int(ln[3])
                    by_subnet[sid].append((sid, disc_bps, w_bps, req_rao, idx_line))

                coldkey_any_paid = False
                coldkey_uncovered_exists = False

                for sid, lines in by_subnet.items():
                    paid_rao_total = rao_by_uid_dest_subnet.get((uid, tre, sid), 0)
                    tao_total = tao_by_uid_dest_subnet.get((uid, tre, sid), 0.0)

                    lines.sort(key=line_key, reverse=True)

                    remain = paid_rao_total
                    covered: List[Tuple[int, int, int, int, int]] = []
                    uncovered: List[Tuple[int, int, int, int, int]] = []
                    for ln_t in lines:
                        _, _, _, req_rao, _ = ln_t
                        if remain >= req_rao:
                            covered.append(ln_t)
                            remain -= req_rao
                            coldkey_any_paid = True
                        else:
                            uncovered.append(ln_t)
                            if req_rao > 0:
                                coldkey_uncovered_exists = True

                    if paid_rao_total > 0 and tao_total > 0 and covered:
                        tao_per_rao = tao_total / float(paid_rao_total)
                    else:
                        tao_per_rao = 0.0

                    for sid2, disc_bps, w_bps, req_rao, _ in covered:
                        line_tao = tao_per_rao * float(req_rao)
                        eff = line_tao * (1.0 - disc_bps / 10_000.0) * (w_bps / 10_000.0)
                        scores_by_uid[uid] += eff
                        if ck:
                            paid_effective_by_ck[ck] += eff

                    for sid2, disc_bps, w_bps, req_rao, _ in uncovered:
                        if tao_per_rao > 0:
                            line_tao = tao_per_rao * float(req_rao)
                        else:
                            price = price_cache.get(sid2, 0.0)
                            line_tao = price * (float(req_rao) / PLANCK)
                        eff_burn = line_tao * (1.0 - disc_bps / 10_000.0) * (w_bps / 10_000.0)
                        burn_total_value += eff_burn

                if ck:
                    if not coldkey_any_paid:
                        offenders_nopay.add(ck)
                        self._reputation[ck] = 0.0
                    elif coldkey_uncovered_exists:
                        offenders_partial.add(ck)
                        self._reputation[ck] = 0.0

        # Jail by coldkey
        if offenders_nopay or offenders_partial:
            now_ep = self.epoch_index
            for ck in offenders_nopay:
                self._ck_jail_until_epoch[ck] = max(self._ck_jail_until_epoch.get(ck, 0), now_ep + JAIL_EPOCHS_NO_PAY)
            for ck in offenders_partial:
                next_ep = now_ep + JAIL_EPOCHS_PARTIAL
                if self._ck_jail_until_epoch.get(ck, 0) < next_ep:
                    self._ck_jail_until_epoch[ck] = next_ep
            self._save_dict_int(self._ck_jail_until_epoch, "jailed_coldkeys.json")
            pretty.show_offenders(sorted(list(offenders_partial)), sorted(list(offenders_nopay)))

        # Reputation update (EMA on paid *effective* share)
        if REPUTATION_ENABLED:
            total_eff = sum(paid_effective_by_ck.values())
            if total_eff > 0:
                beta = float(REPUTATION_BETA)
                for ck, eff in paid_effective_by_ck.items():
                    share = eff / total_eff
                    prev = max(0.0, min(1.0, float(self._reputation.get(ck, 0.0))))
                    new_r = (1.0 - beta) * prev + beta * share
                    self._reputation[ck] = max(0.0, min(1.0, new_r))
                self._save_dict_float(self._reputation, "reputation.json")
                pretty.show_reputation(self._reputation)

        # finalize weights; add burn to UID 0
        miner_uids_all: List[int] = list(self.get_miner_uids())
        if 0 in miner_uids_all and burn_total_value > 0:
            scores_by_uid[0] = scores_by_uid.get(0, 0.0) + burn_total_value
            pretty.show_burn(burn_total_value)

        final_scores: List[float] = [scores_by_uid.get(uid, 0.0) for uid in miner_uids_all]

        burn_all = FORCE_BURN_WEIGHTS or (not any(final_scores))
        if not TESTING:
            if burn_all:
                pretty.log("[red]Burn-all triggered – redirecting emission to UID 0.[/red]")
                final_scores = [1.0 if uid == 0 else 0.0 for uid in miner_uids_all]

            self.update_scores(final_scores, miner_uids_all)
            if not self.config.no_epoch:
                self.set_weights()

        self._validated_epochs.add(epoch_to_settle)
        self._save_set(self._validated_epochs, "validated_epochs.json")

        pretty.kv_panel(
            "Settlement Complete",
            [
                ("epoch_settled", epoch_to_settle),
                ("miners_scored", sum(1 for x in final_scores if x > 0)),
                ("snapshots_used", len(snapshots)),
            ],
            style="bold green",
        )

    # ─── oracle helpers ───────────────────────────────────────────────
    def _make_price(self, start: int, end: int):
        async def _price(subnet_id: int, *_):
            return await average_price(subnet_id, start_block=start, end_block=end, st=await self._stxn())
        return _price

    def _make_depth(self, start: int, end: int):
        async def _depth(subnet_id: int):
            return await average_depth(subnet_id, start_block=start, end_block=end, st=await self._stxn())
        return _depth

    def _uid_resolver_from_map(self, mapping: Dict[str, int]):
        async def _res(ck: str) -> Optional[int]:
            return mapping.get(ck)
        return _res


# ╭────────────────── keep‑alive (optional) ───────────────────────────╮
if __name__ == "__main__":
    from metahash.bittensor_config import config
    with Validator(config=config()) as v:
        while True:
            clog.info("Validator running…", color="gray")
            time.sleep(120)
