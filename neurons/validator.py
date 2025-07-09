# ╭────────────────────────────────────────────────────────────────────╮
# neurons/validator.py                                                 #
# SN‑73 Batch‑Auction validator – increment 3                          #
# ╰────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import bittensor as bt
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.config import (
    PLANCK,
    AUCTION_BUDGET_ALPHA,
    PAYMENT_WINDOW_BLOCKS,
    JAIL_BLOCKS,
    SOFT_QUOTA_LAMBDA,
    MAX_BIDS_PER_MINER,
    S_MIN_ALPHA,
    COMMISSION_BPS,
    S_VALI_MIN,
    STRATEGY_PATH,
    VALIDATOR_TREASURIES,
    STARTING_AUCTIONS_BLOCK,
)
from metahash.validator.strategy import load_weights
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.validator.alpha_transfers import AlphaTransfersScanner
from metahash.validator.epoch_validator import EpochValidatorNeuron
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.protocol import BidSynapse, AckSynapse, WinSynapse

# ───────────────────────── data models ───────────────────────────────


@dataclass(slots=True)
class _Bid:
    subnet_id: int
    alpha: float
    miner_uid: int
    discount_bps: int
    score: float = 0.0


@dataclass(slots=True)
class _Pending:
    alpha: float                  # α expected (float)
    deadline: int                 # block height
    sink: str                     # sink cold‑key
    epoch: int                    # epoch the bid belongs to


# ──────────────────────────  Validator  ──────────────────────────────

class Validator(EpochValidatorNeuron):
    """
    SN‑73 auction + real reward accounting.
    """

    # ─── init ─────────────────────────────────────────────────────────
    def __init__(self, config=None):
        super().__init__(config=config)

        # —— per‑validator treasury  ——————————————— #
        self.treasury_coldkey: str = VALIDATOR_TREASURIES.get(
            self.wallet.hotkey.ss58_address,
            self.wallet.coldkey.ss58_address,      # fallback: own cold‑key
        )

        # —— strategy file (weights per subnet) —— #
        self.weights_bps = load_weights(Path(STRATEGY_PATH))

        # —— auction state ———————————————————— #
        self._bid_book: Dict[Tuple[int, int], _Bid] = {}      # (uid, subnet) → bid
        self._bid_counter: Dict[int, int] = defaultdict(int)  # uid → bids this epoch
        self._pending: Dict[int, _Pending] = {}               # uid → payment‑awaited
        self._cleared_epoch: int | None = None

        # —— payment / reward bookkeeping ————————— #
        self._paid_events: Dict[int, List[TransferEvent]] = defaultdict(list)
        self._jailed_until: Dict[int, int] = {}

        # —— RPC plumbing ———————————————————— #
        self._rpc_lock: asyncio.Lock = asyncio.Lock()
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None
        self._scanner: Optional[AlphaTransfersScanner] = None
        self._last_scan_block: int = self.block

        # —— “already validated” guard (persisted) — #
        self._validated_epochs: set[int] = self._load_validated_epochs()

    # ─────────────—— persistent epoch‑set —————————————— #
    _STATE_FILE = "validated_epochs.json"
    _STATE_KEY = "validated_epochs"

    def _state_path(self) -> Path:
        root = Path(self.wallet.hotkey_file).expanduser().parent
        root.mkdir(parents=True, exist_ok=True)
        return root / self._STATE_FILE

    def _load_validated_epochs(self) -> set[int]:
        try:
            data = json.loads(self._state_path().read_text())
            return set(int(x) for x in data.get(self._STATE_KEY, []))
        except Exception:
            return set()

    def _save_validated_epochs(self):
        tmp = self._state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps({self._STATE_KEY: sorted(self._validated_epochs)}))
        tmp.replace(self._state_path())

    # ─── helpers ──────────────────────────────────────────────────────
    async def _get_async_stxn(self):
        if self._async_subtensor is None:
            stxn = bt.AsyncSubtensor(network=self.config.subtensor.network)
            await stxn.initialize()
            self._async_subtensor = stxn
        return self._async_subtensor

    # ─── dendrite RPC entry (BidSynapse) ──────────────────────────────
    async def server_step(self, syn: BidSynapse) -> AckSynapse:  # type: ignore[override]
        uid = syn.dendrite.origin
        blk = self.block

        # jail gate
        if uid in self._jailed_until and blk < self._jailed_until[uid]:
            return AckSynapse(**syn.dict(), accepted=False,
                              error=f"jailed until block {self._jailed_until[uid]}")

        # stake gate
        stake_alpha = self.metagraph.stake[uid]
        if stake_alpha < S_MIN_ALPHA:
            return AckSynapse(**syn.dict(), accepted=False,
                              error=f"stake {stake_alpha:.3f} α < S_MIN_ALPHA")

        # rate‑limit
        if self._bid_counter[uid] >= MAX_BIDS_PER_MINER:
            return AckSynapse(**syn.dict(), accepted=False,
                              error="max bids per epoch exceeded")
        self._bid_counter[uid] += 1

        # store / deduplicate (uid, subnet)
        self._bid_book[(uid, syn.subnet_id)] = _Bid(
            subnet_id=syn.subnet_id,
            alpha=syn.alpha,
            miner_uid=uid,
            discount_bps=syn.discount_bps,
        )
        return AckSynapse(**syn.dict(), accepted=True)

    # ─── main loop override ───────────────────────────────────────────
    async def forward(self):
        await self._ensure_async_subtensor()

        # 0. handle payments & jailing continuously
        await self._watch_payments()

        # 1. clear the auction for epoch (n‑1) only once
        await self._clear_auction()

        # 2. run reward accounting for epoch (n‑1)
        await self._reward_accounting_prev_epoch()

        # 3. reset bid book for the upcoming epoch
        if self._cleared_epoch == self.epoch_index - 1:
            self._bid_book.clear()
            self._bid_counter.clear()

    # ─── Auction clearing ─────────────────────────────────────────────
    async def _clear_auction(self):
        epoch_to_clear = self.epoch_index - 1
        if self._cleared_epoch == epoch_to_clear or epoch_to_clear < 0:
            return
        self._cleared_epoch = epoch_to_clear

        if not self._bid_book:
            clog.info(f"epoch {epoch_to_clear}: no bids to clear", color="gray")
            return

        bids = list(self._bid_book.values())

        # —— quota & λ‑penalty score ——————————————— #
        total_stake = sum(self.metagraph.stake[b.miner_uid] for b in bids) or 1.0
        quota_of = {
            uid: (self.metagraph.stake[uid] / total_stake) * AUCTION_BUDGET_ALPHA
            for uid in {b.miner_uid for b in bids}
        }
        for b in bids:
            stretch = max(0.0, b.alpha - quota_of[b.miner_uid])
            w = self.weights_bps[b.subnet_id]
            b.score = w - SOFT_QUOTA_LAMBDA * stretch

        bids.sort(key=lambda b: b.score, reverse=True)

        budget = AUCTION_BUDGET_ALPHA
        winners: List[_Bid] = []
        for b in bids:
            if budget <= 0:
                break
            take = min(b.alpha, budget)
            winners.append(replace(b, alpha=take))
            budget -= take

        if not winners:
            clog.warning(f"epoch {epoch_to_clear}: nobody filled budget", color="red")
            return

        clearing_discount = winners[-1].discount_bps
        deadline = self.block + PAYMENT_WINDOW_BLOCKS
        for w in winners:
            sink = self._derive_sink(w)
            self._pending[w.miner_uid] = _Pending(
                alpha=w.alpha, deadline=deadline, sink=sink, epoch=epoch_to_clear
            )
            await self._send_win(w, clearing_discount, sink, deadline)

        clog.success(
            f"auction (epoch {epoch_to_clear}) cleared – "
            f"winners={len(winners)}  budget_left={budget:.2f} α",
            color="green",
        )

    async def _send_win(self, bid: _Bid, disc: int, sink: str, deadline: int):
        try:
            await self.dendrite(
                axons=[self.metagraph.axons[bid.miner_uid]],
                synapse=WinSynapse(
                    subnet_id=bid.subnet_id,
                    alpha=bid.alpha,
                    clearing_discount_bps=disc,
                    alpha_sink=sink,
                    pay_deadline_block=deadline,
                ),
                deserialize=False,
            )
        except Exception as e:
            clog.warning(f"WinSynapse to uid={bid.miner_uid} failed: {e}", color="red")

    def _derive_sink(self, bid: _Bid) -> str:
        """
        Temporary sink derivation (until `set_alpha_sink` is on‑chain):
            <vali_cold[:6]>‑<uid>‑<epoch>
        """
        return f"{self.wallet.coldkey.ss58_address[:6]}-{bid.miner_uid}-{self._cleared_epoch}"

    # ─── Payment watcher & jailing ────────────────────────────────────
    async def _watch_payments(self):
        if not self._pending:
            return

        # scanner initialises lazily (scans *all* dest addresses)
        if self._scanner is None:
            self._scanner = AlphaTransfersScanner(
                await self._get_async_stxn(), dest_coldkey=None, rpc_lock=self._rpc_lock
            )

        frm, to = self._last_scan_block, self.block - 1
        if frm > to:
            return
        events = await self._scanner.scan(frm, to)
        self._last_scan_block = to + 1

        # index by sink
        by_dest: Dict[str, List[TransferEvent]] = defaultdict(list)
        for ev in events:
            by_dest[ev.dest_coldkey].append(ev)

        paid_uids, jailed_uids = [], []
        for uid, pend in list(self._pending.items()):
            evs = by_dest.get(pend.sink, [])
            received_rao = sum(ev.amount_rao for ev in evs)
            required_rao = int(pend.alpha * PLANCK)

            if received_rao >= required_rao:
                paid_uids.append(uid)
                self._paid_events[pend.epoch].extend(evs)
                del self._pending[uid]
            elif self.block > pend.deadline:
                # jail offender
                self._jailed_until[uid] = self.block + JAIL_BLOCKS
                jailed_uids.append(uid)
                del self._pending[uid]

        if paid_uids:
            clog.success(f"payments received from {paid_uids}", color="green")
        if jailed_uids:
            clog.warning(f"jailed {jailed_uids} for non‑payment", color="red")

    # ─── Reward accounting (epoch n‑1) ────────────────────────────────
    async def _reward_accounting_prev_epoch(self):
        prev_epoch = self.epoch_index - 1
        if prev_epoch < 0 or prev_epoch in self._validated_epochs:
            return

        # pre‑check: auction must have been cleared
        if self._cleared_epoch != prev_epoch:
            return  # auction not cleared yet

        # collect events for that epoch (may be empty)
        events = self._paid_events.pop(prev_epoch, [])

        miner_uids = list(self.get_miner_uids())
        start_block = self.epoch_start_block - self.epoch_tempo
        end_block = self.epoch_start_block - 1

        rewards = await compute_epoch_rewards(
            miner_uids=miner_uids,
            events=events,
            scanner=None,                       # we supply events directly
            pricing=self._make_pricing(start_block, end_block),
            pool_depth_of=self._make_depth(start_block, end_block),
            uid_of_coldkey=self._make_uid_resolver(),
            start_block=start_block,
            end_block=end_block,
        )

        total_tao = sum(rewards)
        total_alpha_paid = sum(ev.amount_rao for ev in events) / PLANCK
        surplus_alpha = max(0.0, AUCTION_BUDGET_ALPHA - total_alpha_paid)

        # convert surplus α → TAO using a coarse volume‑weighted price
        surplus_tao = await self._convert_surplus_to_tao(
            surplus_alpha, start_block, end_block, events
        )

        commission_tao = surplus_tao * COMMISSION_BPS / 10_000
        delegator_tao = surplus_tao - commission_tao

        bt.logging.info(
            f"[epoch {prev_epoch}] total_tao={total_tao:.6f}  "
            f"surplus_α={surplus_alpha:.2f}  "
            f"surplus_tao={surplus_tao:.6f} "
            f"(vali {commission_tao:.6f} | deleg {delegator_tao:.6f})"
        )

        # burn weights if nothing paid or auction hasn’t started yet
        burn = self.block < STARTING_AUCTIONS_BLOCK or not any(rewards)
        if burn:
            rewards = [1.0 if uid == 0 else 0.0 for uid in miner_uids]

        self.update_scores(rewards, miner_uids)
        if not self.config.no_epoch:
            self.set_weights()

        self._validated_epochs.add(prev_epoch)
        self._save_validated_epochs()

    # ─── surplus α → TAO conversion (simple VWAP) ─────────────────────
    async def _convert_surplus_to_tao(
        self,
        surplus_alpha: float,
        start_block: int,
        end_block: int,
        events: List[TransferEvent],
    ) -> float:
        if surplus_alpha <= 0 or not events:
            return 0.0

        # volume‑weighted price over subnets that actually had payments
        vols: Dict[int, float] = defaultdict(float)
        for ev in events:
            vols[ev.subnet_id] += ev.amount_rao / PLANCK

        stxn = await self._get_async_stxn()

        vwap_num = vwap_den = 0.0
        for sid, vol in vols.items():
            p = await average_price(
                sid, start_block=start_block, end_block=end_block, st=stxn
            )
            if not p or p.tao is None:
                continue
            vwap_num += p.tao * vol
            vwap_den += vol

        if vwap_den == 0:
            return 0.0
        avg_price = vwap_num / vwap_den
        return avg_price * surplus_alpha

    # ─── oracle helpers (reuse old code) ───────────────────────────────
    def _make_pricing(self, start: int, end: int):
        async def _pricing(subnet_id: int, *_):
            return await average_price(
                subnet_id,
                start_block=start,
                end_block=end,
                st=await self._get_async_stxn(),
            )
        return _pricing

    def _make_depth(self, start: int, end: int):
        async def _depth(subnet_id: int):
            return await average_depth(
                subnet_id,
                start_block=start,
                end_block=end,
                st=await self._get_async_stxn(),
            )
        return _depth

    def _make_uid_resolver(self):
        async def _resolver(coldkey: str) -> Optional[int]:
            if len(self.metagraph.coldkeys) != getattr(self, "_ck_sz", 0):
                self._ck_map = {ck: uid for uid, ck in enumerate(self.metagraph.coldkeys)}
                self._ck_sz = len(self.metagraph.coldkeys)
            return self._ck_map.get(coldkey)
        self._ck_map = {ck: uid for uid, ck in enumerate(self.metagraph.coldkeys)}
        self._ck_sz = len(self.metagraph.coldkeys)
        return _resolver


# ╭────────────────── keep‑alive (optional) ───────────────────────────╮
if __name__ == "__main__":
    from metahash.bittensor_config import config
    with Validator(config=config()) as v:
        while True:
            clog.info("Validator running…", color="gray")
            time.sleep(120)
