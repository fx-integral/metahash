# metahash/validator/engines/auction.py
from __future__ import annotations

import inspect
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from metahash.utils.pretty_logs import pretty
from metahash.treasuries import VALIDATOR_TREASURIES

from metahash.validator.state import StateStore

# Config constants
from metahash.config import (AUCTION_BUDGET_ALPHA, AUCTION_START_TIMEOUT, LOG_TOP_N,
    MAX_BIDS_PER_MINER, S_MIN_ALPHA_MINER, S_MIN_MASTER_VALIDATOR, START_V3_BLOCK)
from metahash.protocol import AuctionStartSynapse

EPS_ALPHA = 1e-12  # keep small epsilon for partial fill checks


@dataclass(slots=True)
class _Bid:
    epoch: int
    subnet_id: int
    alpha: float              # α requested for this bid (payment target)
    miner_uid: int
    coldkey: str
    discount_bps: int         # for ordering (not payment)
    weight_snap: float = 0.0  # snapshot at acceptance (0..1)
    idx: int = 0              # stable order per miner+subnet


@dataclass(slots=True, frozen=True)
class BidInput:
    miner_uid: int
    coldkey: str
    subnet_id: int
    alpha: float                # requested α (in α units, not rao)
    discount_bps: int           # 0..10_000
    weight_bps: int             # 0..10_000
    idx: int = 0                # stable per-miner ordering


@dataclass(slots=True)
class WinAllocation:
    miner_uid: int
    coldkey: str
    subnet_id: int
    alpha_requested: float
    alpha_accepted: float
    discount_bps: int
    weight_bps: int


def budget_from_share(*, share: float, auction_budget_alpha: float = AUCTION_BUDGET_ALPHA) -> float:
    """(Legacy) Returns an α-denominated budget = share * AUCTION_BUDGET_ALPHA."""
    if share <= 0:
        return 0.0
    return auction_budget_alpha * float(share)


def budget_tao_from_share(
    *,
    share: float,
    auction_budget_alpha: float = AUCTION_BUDGET_ALPHA,
    price_tao_per_alpha_base: float,
) -> float:
    """
    Convert the base α budget (for validator's netuid) to a TAO budget for allocation:
        budget_tao = share * auction_budget_alpha (α) * price_tao_per_alpha_base
    """
    if share <= 0 or auction_budget_alpha <= 0 or price_tao_per_alpha_base <= 0:
        return 0.0
    return float(share) * float(auction_budget_alpha) * float(price_tao_per_alpha_base)


class AuctionEngine:
    """AuctionStart broadcast & bid management (masters only)."""

    def __init__(self, parent, state: StateStore, weights_bps: Dict[int, float], clearer=None):
        self.parent = parent
        self.state = state
        self.weights_bps = weights_bps
        self.clearer = clearer  # object with async clear_now_and_notify(epoch: int)

        # epoch-local bid state (masters only)
        self._bid_book: Dict[int, Dict[Tuple[int, int], _Bid]] = defaultdict(dict)
        self._ck_uid_epoch: Dict[int, Dict[str, int]] = defaultdict(dict)
        self._ck_subnets_bid: Dict[int, Dict[str, int]] = defaultdict(dict)  # distinct subnet count per coldkey

        # snapshot of master stakes & our share per epoch e
        self._master_stakes_by_epoch: Dict[int, Dict[str, float]] = {}
        self._my_share_by_epoch: Dict[int, float] = {}

        # remember keyed-by-epoch control flags
        self._auction_start_sent_for: Optional[int] = None
        self._wins_notified_for: Optional[int] = None
        self._not_master_log_epoch: Optional[int] = None  # avoid spam

        # Detect WinAllocation signature once (compat across versions)
        try:
            self._winalloc_params = set(inspect.signature(WinAllocation).parameters.keys())
        except Exception:
            self._winalloc_params = set()

    # ---------- utilities ----------
    def _hotkey_to_uid(self) -> Dict[str, int]:
        mapping: Dict[str, int] = {}
        for i, ax in enumerate(self.parent.metagraph.axons):
            hk = getattr(ax, "hotkey", None)
            if hk:
                mapping[hk] = i
        if not mapping and hasattr(self.parent.metagraph, "hotkeys"):
            for i, hk in enumerate(getattr(self.parent.metagraph, "hotkeys")):
                mapping[hk] = i
        return mapping

    def _is_master_now(self) -> bool:
        tre = VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58)
        if not tre:
            return False
        uid = self._hotkey_to_uid().get(self.parent.hotkey_ss58)
        if uid is None:
            return False
        try:
            return float(self.parent.metagraph.stake[uid]) >= S_MIN_MASTER_VALIDATOR
        except Exception:
            return False

    def _norm_weight(self, subnet_id: int) -> float:
        try:
            raw = float(self.weights_bps[subnet_id])
        except Exception:
            raw = float(self.weights_bps.get(subnet_id, 0.0))
        if raw > 1.0:
            raw = raw / 10_000.0
        return max(0.0, min(1.0, raw))

    def _snapshot_master_stakes_for_epoch(self, epoch: int) -> float:
        """Snapshots current masters' stakes and returns our share in [0,1]."""
        pretty.kv_panel(
            "Master Stakes => Budget",
            [("epoch (e)", epoch), ("action", "Calculating master validators’ stakes to compute personal budget share…")],
            style="bold cyan",
        )
        hk2uid = self._hotkey_to_uid()
        stakes: Dict[str, float] = {}
        total = 0.0
        shares_table: List[Tuple[str, int, float, float]] = []
        for hk in VALIDATOR_TREASURIES.keys():
            uid = hk2uid.get(hk)
            if uid is None or uid >= len(self.parent.metagraph.stake):
                continue
            st = float(self.parent.metagraph.stake[uid])
            if st >= S_MIN_MASTER_VALIDATOR:
                stakes[hk] = st
                total += st
        self._master_stakes_by_epoch[epoch] = stakes
        my_stake = stakes.get(self.parent.hotkey_ss58, 0.0)
        share = (my_stake / total) if (total > 0 and my_stake > 0) else 0.0
        self._my_share_by_epoch[epoch] = share
        if total > 0:
            for hk, st in stakes.items():
                uid = hk2uid.get(hk, -1)
                shares_table.append((hk, uid, st, AUCTION_BUDGET_ALPHA * (st / total)))
            pretty.show_master_shares(shares_table)
        else:
            pretty.log("[yellow]No active masters meet the threshold this epoch.[/yellow]")
        return share

    def _accept_bid_from(self, *, uid: int, subnet_id: int, alpha: float, discount_bps: int) -> Tuple[bool, Optional[str]]:
        epoch = self.parent.epoch_index
        if self.parent.block < START_V3_BLOCK:
            return False, "auction not started (v3 gating)"
        if not self._is_master_now():
            return False, "bids disabled on non-master validator"
        if uid == 0:
            return False, "uid 0 not allowed"

        # epoch‑jail (by coldkey)
        ck = self.parent.metagraph.coldkeys[uid]
        jail_upto = self.state.ck_jail_until_epoch.get(ck, -1)
        if jail_upto is not None and epoch < jail_upto:
            return False, f"jailed until epoch {jail_upto}"

        # stake gate (miner’s UID)
        stake_alpha = self.parent.metagraph.stake[uid]
        if stake_alpha < S_MIN_ALPHA_MINER:
            return False, f"stake {stake_alpha:.3f} α < S_MIN_ALPHA_MINER"

        # sanity‑checks
        if not (alpha > 0):
            return False, "invalid α"
        if alpha > AUCTION_BUDGET_ALPHA:
            return False, "α exceeds max per bid"
        if not (0 <= discount_bps <= 10_000):
            return False, "discount out of range"

        # weight gate: reject zero‑weight subnets outright
        w = self._norm_weight(subnet_id)
        if w <= 0.0:
            return False, f"subnet weight {w:.4f} ≤ 0 (sid={subnet_id})"

        # enforce one uid per coldkey (per epoch per master)
        ck_map = self._ck_uid_epoch[epoch]
        existing_uid = ck_map.get(ck)
        if existing_uid is not None and existing_uid != uid:
            return False, f"coldkey already bidding as uid={existing_uid}"

        # per‑coldkey bid limit on distinct subnets
        count = self._ck_subnets_bid[epoch].get(ck, 0)
        first_on_subnet = (uid, subnet_id) not in self._bid_book[epoch]
        if first_on_subnet and count >= MAX_BIDS_PER_MINER:
            return False, "per-coldkey bid limit reached"

        # accept / upsert
        ck_map.setdefault(ck, uid)
        if first_on_subnet:
            self._ck_subnets_bid[epoch][ck] = count + 1

        idx = len([b for (u, s), b in self._bid_book[epoch].items() if u == uid and s == subnet_id])
        self._bid_book[epoch][(uid, subnet_id)] = _Bid(
            epoch=epoch,
            subnet_id=subnet_id,
            alpha=alpha,
            miner_uid=uid,
            coldkey=ck,
            discount_bps=discount_bps,
            weight_snap=w,
            idx=idx,
        )
        return True, None

    async def broadcast_auction_start(self):
        """Broadcast AuctionStart and accept bids; then clear and notify winners immediately."""
        if not self._is_master_now():
            return
        if getattr(self, "_auction_start_sent_for", None) == self.parent.epoch_index:
            return
        if self.parent.block < START_V3_BLOCK:
            return

        pretty.kv_panel(
            "2. AuctionStart",
            [("epoch (e)", self.parent.epoch_index), ("action", "Starting auction…")],
            style="bold cyan",
        )

        share = self._snapshot_master_stakes_for_epoch(self.parent.epoch_index)
        my_budget = budget_from_share(share=share, auction_budget_alpha=AUCTION_BUDGET_ALPHA)
        if my_budget <= 0:
            pretty.log("[yellow]Budget share is zero – not broadcasting AuctionStart (no stake share this epoch).[/yellow]")
            self._auction_start_sent_for = self.parent.epoch_index
            return

        axons = self.parent.metagraph.axons
        if not axons:
            pretty.log("[yellow]Metagraph has no reachable axons; skipping AuctionStart broadcast.[/yellow]")
            self._auction_start_sent_for = self.parent.epoch_index
            return

        e = self.parent.epoch_index
        v_uid = self._hotkey_to_uid().get(self.parent.hotkey_ss58, None)
        syn = AuctionStartSynapse(
            epoch_index=e,
            auction_start_block=self.parent.block,
            min_stake_alpha=S_MIN_ALPHA_MINER,
            auction_budget_alpha=my_budget,
            weights_bps=dict(self.weights_bps),
            treasury_coldkey=VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, ""),
            validator_uid=v_uid,
            validator_hotkey=self.parent.hotkey_ss58,
        )

        try:
            pretty.log("[cyan]Broadcasting AuctionStart to miners…[/cyan]")
            resps = await self.parent.dendrite(axons=axons, synapse=syn, deserialize=True, timeout=AUCTION_START_TIMEOUT)
        except Exception as e_exc:
            resps = []
            pretty.log(f"[yellow]AuctionStart broadcast exceptions: {e_exc}[/yellow]")

        hk2uid = self._hotkey_to_uid()
        ack_count = 0
        total = len(axons) if axons else 0
        bids_accepted = 0
        bids_rejected = 0
        reject_rows: List[List[object]] = []
        reasons_counter: Dict[str, int] = defaultdict(int)

        for idx, ax in enumerate(axons or []):
            resp = resps[idx] if isinstance(resps, list) and idx < len(resps) else None
            if not isinstance(resp, AuctionStartSynapse):
                continue
            if bool(getattr(resp, "ack", False)):
                ack_count += 1
            bids = getattr(resp, "bids", None) or []
            uid = hk2uid.get(ax.hotkey)
            if uid is None:
                continue
            for b in bids:
                try:
                    subnet_id = int(b.get("subnet_id"))
                    alpha = float(b.get("alpha"))
                    discount_bps = int(b.get("discount_bps"))
                except Exception:
                    bids_rejected += 1
                    reason = "malformed bid"
                    reasons_counter[reason] += 1
                    if len(reject_rows) < max(10, LOG_TOP_N):
                        reject_rows.append([uid, b.get("subnet_id", "?"), f"{b.get('alpha', '?')}", f"{b.get('discount_bps', '?')}", reason])
                    continue
                ok, reason = self._accept_bid_from(uid=uid, subnet_id=subnet_id, alpha=alpha, discount_bps=discount_bps)
                if ok:
                    bids_accepted += 1
                else:
                    bids_rejected += 1
                    r = reason or "rejected"
                    reasons_counter[r] += 1
                    if len(reject_rows) < max(10, LOG_TOP_N):
                        reject_rows.append([uid, subnet_id, f"{alpha:.4f} α", f"{discount_bps} bps", r])

        self._auction_start_sent_for = self.parent.epoch_index

        pretty.kv_panel(
            "AuctionStart Broadcast (epoch e)",
            [
                ("e (now)", e),
                ("block", self.parent.block),
                ("budget α", f"{my_budget:.3f}"),
                ("treasury", VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, "")),
                ("acks_received", f"{ack_count}/{total}"),
                ("bids_accepted", bids_accepted),
                ("bids_rejected", bids_rejected),
                ("note", "miners pay for e in e+1; weights from e in e+2"),
            ],
            style="bold cyan",
        )

        if bids_rejected > 0:
            pretty.log("[red]Some bids were rejected. See reasons below.[/red]")
            if reject_rows:
                pretty.table("Rejected bids (why)", ["UID", "Subnet", "Alpha", "Discount", "Reason"], reject_rows)
            reason_rows = [[k, v] for k, v in sorted(reasons_counter.items(), key=lambda x: (-x[1], x[0]))[:max(6, LOG_TOP_N // 2)]]
            if reason_rows:
                pretty.table("Rejection counts (top)", ["Reason", "Count"], reason_rows)

        # Clear & notify (fills pending_commits for epoch e with winners window).
        if self._wins_notified_for != self.parent.epoch_index:
            if self.clearer is not None:
                await self.clearer.clear_now_and_notify(epoch_to_clear=self.parent.epoch_index)
            else:
                pretty.log("[yellow]Clearing engine not configured; skipping clear_now_and_notify.[/yellow]")
            self._wins_notified_for = self.parent.epoch_index

    def cleanup_old_epoch_books(self, before_epoch: int):
        """Cleanup old bid state older than (e−2)."""
        self._bid_book.pop(before_epoch, None)
        self._ck_uid_epoch.pop(before_epoch, None)
        self._ck_subnets_bid.pop(before_epoch, None)
