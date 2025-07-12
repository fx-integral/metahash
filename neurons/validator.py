# ╭────────────────────────────────────────────────────────────────────╮
# neurons/validator.py – SN‑73 treasury‑only (July 2025, patched)      #
# ╰────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from math import isfinite
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
    STRATEGY_PATH,
    VALIDATOR_TREASURIES,
    STARTING_AUCTIONS_BLOCK,
)
from metahash.validator.strategy import load_weights
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.validator.alpha_transfers import AlphaTransfersScanner
from metahash.validator.epoch_validator import EpochValidatorNeuron
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.protocol import (
    BidSynapse, AckSynapse, WinSynapse, AuctionStartSynapse
)

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
    alpha: float
    discount_bps: int          # bidder‑specific discount
    deadline: int
    src_coldkey: str           # payer
    epoch: int                 # auction epoch index


# ──────────────────────────  Validator  ──────────────────────────────

class Validator(EpochValidatorNeuron):
    """
    SN‑73 batch‑auction:
      • miners pay α directly to the validator’s treasury cold‑key
      • auction cleared at epoch (e‑1); payments occur during epoch e;
        rewards accounted for at epoch (e+1)
    """

    # ─── init ─────────────────────────────────────────────────────────
    def __init__(self, config=None):
        super().__init__(config=config)

        # — treasury address —
        self.treasury_coldkey: str = VALIDATOR_TREASURIES.get(
            self.wallet.hotkey.ss58_address,
            self.wallet.coldkey.ss58_address,   # fallback = own cold‑key
        )

        # — strategy (weights per subnet) —
        self.weights_bps = load_weights(Path(STRATEGY_PATH))

        # — auction / epoch state —
        self._bid_book: Dict[Tuple[int, int], _Bid] = {}     # (uid, subnet) → bid
        self._bid_counter: Dict[int, int] = defaultdict(int)
        self._pending: Dict[int, _Pending] = {}              # uid → expected payment
        self._paid_events: Dict[int, List[TransferEvent]] = defaultdict(list)
        self._cleared_epoch: int | None = None
        self._jailed_until: Dict[int, int] = {}

        # — blockchain interface —
        self._async_subtensor: Optional[bt.AsyncSubtensor] = None
        self._scanner: Optional[AlphaTransfersScanner] = None
        self._rpc_lock: asyncio.Lock = asyncio.Lock()
        self._last_scan_block: int = self.block

        # — persistence of validated epochs —
        self._validated_epochs: set[int] = self._load_set("validated_epochs.json")

    # ─── persistence helpers ──────────────────────────────────────────
    # NOTE: these now save in the current working directory so that
    #       they no longer depend on self.wallet.hotkey_file.
    def _load_set(self, filename: str) -> set[int]:
        """
        Load a JSON array of integers from *filename* in the CWD.
        Returns an empty set on first run or if the file is unreadable.
        """
        path = Path.cwd() / filename
        try:
            return set(json.loads(path.read_text()))
        except Exception:
            return set()

    def _save_set(self, s: set[int], filename: str):
        """
        Atomically write *s* (sorted list) to *filename* in the CWD.
        """
        path = Path.cwd() / filename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(s)))
        tmp.replace(path)

    # ─── async‑Subtensor getter ───────────────────────────────────────
    async def _stxn(self):
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
                              error=f"jailed until {self._jailed_until[uid]}")

        # stake gate
        stake_alpha = self.metagraph.stake[uid]
        if stake_alpha < S_MIN_ALPHA:
            return AckSynapse(**syn.dict(), accepted=False,
                              error=f"stake {stake_alpha:.3f} α < S_MIN_ALPHA")

        # rate limit
        if self._bid_counter[uid] >= MAX_BIDS_PER_MINER:
            return AckSynapse(**syn.dict(), accepted=False, error="rate‑limit exceeded")

        # sanity‑check inputs (issue #9)
        if not isfinite(syn.alpha) or syn.alpha <= 0:
            return AckSynapse(**syn.dict(), accepted=False, error="invalid α")
        if syn.alpha > AUCTION_BUDGET_ALPHA:
            return AckSynapse(**syn.dict(), accepted=False, error="α exceeds budget")
        if not (0 <= syn.discount_bps <= 10_000):
            return AckSynapse(**syn.dict(), accepted=False, error="discount out of range")

        self._bid_counter[uid] += 1

        # deduplicate by (uid, subnet)
        self._bid_book[(uid, syn.subnet_id)] = _Bid(
            subnet_id=syn.subnet_id,
            alpha=syn.alpha,
            miner_uid=uid,
            discount_bps=syn.discount_bps,
        )
        return AckSynapse(**syn.dict(), accepted=True)

# ─────────────────────────  broadcast helper  ──────────────────────── #
    async def _broadcast_auction_start(self):
        """
        Sends exactly **one** AuctionStartSynapse to every miner at the
        beginning of the epoch in which bidding will occur.

        Executed from forward() after we have reset the bid book.
        """
        if getattr(self, "_auction_start_sent_for", None) == self.epoch_index:
            return  # already sent for this epoch

        axons = self.metagraph.axons
        if not axons:
            return

        syn = AuctionStartSynapse(
            epoch_index=self.epoch_index,
            auction_start_block=self.block,
            min_stake_alpha=S_MIN_ALPHA,
            auction_budget_alpha=AUCTION_BUDGET_ALPHA,
            weights_bps=dict(self.weights_bps),
            treasury_coldkey=self.treasury_coldkey,
        )

        # Fire‑and‑forget – we do NOT need responses here.
        await self.dendrite(
            axons=axons,
            synapse=syn,
            deserialize=False,
            timeout=8,
        )

        self._auction_start_sent_for = self.epoch_index
        clog.info(f"AuctionStart broadcasted for epoch {self.epoch_index}", color="cyan")

# ─────────────────────────  forward() tail  ────────────────────────── #
    async def forward(self):
        await self._stxn()
        await self._watch_payments()
        await self._reward_accounting_prev_prev()
        await self._clear_auction()

        # ↓↓↓ trigger miners for the *current* epoch
        await self._broadcast_auction_start()

        # reset bid book when auction of (e‑1) is done
        if self._cleared_epoch == self.epoch_index - 1:
            self._bid_book.clear()
            self._bid_counter.clear()

    # ─── Auction clearing (epoch e‑1) ─────────────────────────────────
    async def _clear_auction(self):
        epoch_to_clear = self.epoch_index - 1
        if epoch_to_clear < 0 or self._cleared_epoch == epoch_to_clear:
            return
        self._cleared_epoch = epoch_to_clear

        if not self._bid_book:
            clog.info(f"epoch {epoch_to_clear}: no bids to clear", color="gray")
            return

        bids = list(self._bid_book.values())

        # quota & λ‑penalty
        total_stake = sum(self.metagraph.stake[b.miner_uid] for b in bids) or 1.0
        quota = {
            uid: (self.metagraph.stake[uid] / total_stake) * AUCTION_BUDGET_ALPHA
            for uid in {b.miner_uid for b in bids}
        }
        for b in bids:
            stretch = max(0.0, b.alpha - quota[b.miner_uid])
            weight = self.weights_bps[b.subnet_id]
            b.score = weight - SOFT_QUOTA_LAMBDA * stretch

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
            clog.warning(f"epoch {epoch_to_clear}: nobody filled budget", color="red")
            return

        deadline = self.block + PAYMENT_WINDOW_BLOCKS
        for w in winners:
            src_ck = self.metagraph.coldkeys[w.miner_uid]
            self._pending[w.miner_uid] = _Pending(
                alpha=w.alpha,
                discount_bps=w.discount_bps,
                deadline=deadline,
                src_coldkey=src_ck,
                epoch=epoch_to_clear,
            )
            await self._send_win(w, deadline)

        clog.success(
            f"auction cleared (epoch {epoch_to_clear})  "
            f"winners={len(winners)}  budget_left={budget:.2f} α",
            color="green",
        )

    async def _send_win(self, bid: _Bid, deadline: int):
        try:
            await self.dendrite(
                axons=[self.metagraph.axons[bid.miner_uid]],
                synapse=WinSynapse(
                    subnet_id=bid.subnet_id,
                    alpha=bid.alpha,
                    clearing_discount_bps=bid.discount_bps,
                    pay_deadline_block=deadline,
                ),
                deserialize=False,
            )
        except Exception as e:
            clog.warning(f"WinSynapse to uid={bid.miner_uid} failed: {e}", color="red")

    # ─── Payment watcher ──────────────────────────────────────────────
    async def _watch_payments(self):
        if not self._pending:
            return

        if self._scanner is None:
            self._scanner = AlphaTransfersScanner(
                await self._stxn(),
                dest_coldkey=self.treasury_coldkey,
                rpc_lock=self._rpc_lock,
            )

        frm, to = self._last_scan_block, self.block - 1
        if frm > to:
            return
        events = await self._scanner.scan(frm, to)
        self._last_scan_block = to + 1

        # group by (src_coldkey)
        by_src: Dict[str, List[TransferEvent]] = defaultdict(list)
        for ev in events:
            if ev.dest_coldkey == self.treasury_coldkey:
                by_src[ev.src_coldkey].append(ev)

        paid, jailed = [], []
        for uid, pend in list(self._pending.items()):
            evs = [
                ev for ev in by_src.get(pend.src_coldkey, [])
                if ev.block <= pend.deadline
            ]
            received_rao = sum(ev.amount_rao for ev in evs)

            # apply individual discount
            pay_alpha = pend.alpha * (1 - pend.discount_bps / 10_000)
            required_rao = int(pay_alpha * PLANCK)

            if received_rao >= required_rao:
                paid.append(uid)
                self._paid_events[pend.epoch].extend(evs)
                del self._pending[uid]
            elif self.block > pend.deadline:
                self._jailed_until[uid] = self.block + JAIL_BLOCKS
                jailed.append(uid)
                del self._pending[uid]

        if paid:
            clog.success(f"payments received: {paid}", color="green")
        if jailed:
            clog.warning(f"jailed for non‑payment: {jailed}", color="red")

    # ─── Reward accounting for epoch (e‑2) ────────────────────────────
    async def _reward_accounting_prev_prev(self):
        target_epoch = self.epoch_index - 2
        if target_epoch < 0 or target_epoch in self._validated_epochs:
            return
        if any(p.epoch == target_epoch for p in self._pending.values()):
            return  # still awaiting payments

        events = self._paid_events.pop(target_epoch, [])

        miner_uids = list(self.get_miner_uids())
        start_block = self.epoch_start_block - 2 * self.epoch_tempo
        end_block = self.epoch_start_block - self.epoch_tempo - 1

        rewards = await compute_epoch_rewards(
            miner_uids=miner_uids,
            events=events,               # already filtered to treasury
            scanner=None,
            pricing=self._make_price(start_block, end_block),
            pool_depth_of=self._make_depth(start_block, end_block),
            uid_of_coldkey=self._uid_resolver(),
            start_block=start_block,
            end_block=end_block,
        )

        # burn if auction not started yet or no one paid
        burn = self.block < STARTING_AUCTIONS_BLOCK or not any(rewards)
        if burn:
            rewards = [1.0 if uid == 0 else 0.0 for uid in miner_uids]

        self.update_scores(rewards, miner_uids)
        if not self.config.no_epoch:
            self.set_weights()

        # mark validated *after* weights are set
        self._validated_epochs.add(target_epoch)
        self._save_set(self._validated_epochs, "validated_epochs.json")
        clog.success(f"weights set for epoch {target_epoch}", color="cyan")

    # ─── oracle helpers ───────────────────────────────────────────────
    def _make_price(self, start: int, end: int):
        async def _price(subnet_id: int, *_):
            return await average_price(subnet_id, start_block=start, end_block=end, st=await self._stxn())
        return _price

    def _make_depth(self, start: int, end: int):
        async def _depth(subnet_id: int):
            return await average_depth(subnet_id, start_block=start, end_block=end, st=await self._stxn())
        return _depth

    def _uid_resolver(self):
        async def _res(ck: str) -> Optional[int]:
            if len(self.metagraph.coldkeys) != getattr(self, "_ck_sz", 0):
                self._ck_map = {ck: i for i, ck in enumerate(self.metagraph.coldkeys)}
                self._ck_sz = len(self.metagraph.coldkeys)
            return self._ck_map.get(ck)
        self._ck_map = {ck: i for i, ck in enumerate(self.metagraph.coldkeys)}
        self._ck_sz = len(self.metagraph.coldkeys)
        return _res


# ╭────────────────── keep‑alive (optional) ───────────────────────────╮
if __name__ == "__main__":
    from metahash.bittensor_config import config
    with Validator(config=config()) as v:
        while True:
            clog.info("Validator running…", color="gray")
            time.sleep(120)
