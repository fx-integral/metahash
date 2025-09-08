# ╭────────────────────────────────────────────────────────────────────────╮
# metahash/validator/rewards.py — Simple monetary helpers + alloc utilities
#
#  • rao_to_tao_slippage(): convert α (rao) → TAO with a small slippage model
#  • compute_epoch_rewards(): optional helper used elsewhere (unchanged signature)
#  • Allocation helpers (BidInput / WinAllocation / allocate_bids, etc.) kept
#    to avoid breaking AuctionEngine or other callers.
# ╰────────────────────────────────────────────────────────────────────────╯

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    runtime_checkable,
)

import bittensor as bt
from metahash.config import (
    K_SLIP,
    SLIP_TOLERANCE,
    AUCTION_BUDGET_ALPHA,
    PLANCK,
    FORBIDDEN_ALPHA_SUBNETS,
)

# Authoritative on-chain transfer event model (scanner returns these)
from metahash.validator.alpha_transfers import TransferEvent  # noqa: F401

# ───────────────────────────── GLOBAL CONSTANTS ───────────────────────── #

getcontext().prec = 60                          # high-precision arithmetic
_1e9: Decimal = Decimal(10) ** 9                # 1 α = 1e9 planck(rao)
K_SLIP_D: Decimal = Decimal(str(K_SLIP))
SLIP_TOLERANCE_D: Decimal = Decimal(str(SLIP_TOLERANCE))


# ──────────────────────────────── PROTOCOLS ───────────────────────────── #

@runtime_checkable
class TransferScanner(Protocol):
    async def scan(self, from_block: int, to_block: int) -> List["TransferEvent"]: ...


@runtime_checkable
class BalanceLike(Protocol):
    tao: float          # TAO price of 1 α
    rao: int | None     # optional: current pool depth (planck)


@runtime_checkable
class PricingProvider(Protocol):
    async def __call__(self, subnet_id: int, start: int, end: int) -> BalanceLike: ...


@runtime_checkable
class PoolDepthProvider(Protocol):
    async def __call__(self, subnet_id: int) -> int: ...


@runtime_checkable
class MinerResolver(Protocol):
    async def __call__(self, coldkey: str) -> int | None: ...


# ────────────────────────────── MONETARY CORE ─────────────────────────── #

def rao_to_tao_slippage(alpha_rao: int, price_tao_per_alpha: float | Decimal, depth_rao: int) -> float:
    """
    Convert a *raw* α amount (in rao/planck) into TAO after slippage:
        slip = K_SLIP * (α / (depth + α))
        TAO = α * price * (1 - slip)
    A tiny slip under SLIP_TOLERANCE is ignored for stability.
    """
    if alpha_rao <= 0 or depth_rao <= 0:
        return 0.0

    price = Decimal(str(price_tao_per_alpha))
    if price <= 0:
        return 0.0

    a = Decimal(alpha_rao)
    d = Decimal(depth_rao)
    ratio = a / (d + a)
    slip = K_SLIP_D * ratio
    if slip <= SLIP_TOLERANCE_D:
        slip = Decimal(0)
    if slip > 1:
        slip = Decimal(1)

    tao = a * price * (Decimal(1) - slip) / _1e9
    return float(tao)


# ╭────────────────────────────── PIPELINE (optional) ─────────────────────╮
# This preserves the original signature so other code can keep using it.
# It simply aggregates α paid per (uid, subnet), applies price+depth slippage,
# and returns a TAO value list aligned to `miner_uids`.
# ╰────────────────────────────────────────────────────────────────────────╯

async def compute_epoch_rewards(
    *,
    miner_uids: Sequence[int],
    scanner: Optional[TransferScanner] = None,
    events: Optional[Sequence[TransferEvent]] = None,
    pricing: PricingProvider,
    uid_of_coldkey: MinerResolver,
    start_block: int,
    end_block: int,
    pool_depth_of: PoolDepthProvider,
    log: Callable[[str], None] | None = None,
) -> List[float]:
    # 1) Get events
    if events is None:
        if scanner is None:
            raise ValueError("compute_epoch_rewards: need either events or scanner")
        raw = await scanner.scan(from_block=start_block, to_block=end_block)
    else:
        raw = list(events)

    # 2) Filter forbidden subnets
    kept: List[TransferEvent] = [ev for ev in raw if ev.subnet_id not in FORBIDDEN_ALPHA_SUBNETS]

    # 3) Resolve coldkeys → UIDs (accept sync or async resolvers)
    coldkeys = {ev.src_coldkey for ev in kept}

    async def _resolve(ck: str) -> Tuple[str, Optional[int]]:
        res = uid_of_coldkey(ck)
        if inspect.isawaitable(res):
            return ck, await res  # type: ignore[arg-type]
        return ck, res  # type: ignore[return-value]

    pairs = await asyncio.gather(*(_resolve(ck) for ck in coldkeys))
    uid_by_ck: Dict[str, Optional[int]] = dict(pairs)

    # 4) Aggregate α by (uid, subnet)
    rao_by_uid_sid: Dict[Tuple[int, int], int] = defaultdict(int)
    for ev in kept:
        uid = uid_by_ck.get(ev.src_coldkey)
        if uid is None:
            continue
        rao_by_uid_sid[(uid, ev.subnet_id)] += int(ev.amount_rao)

    if not rao_by_uid_sid:
        return [0.0 for _ in miner_uids]

    # 5) Price & depth caches
    subnets: Set[int] = {sid for (_, sid) in rao_by_uid_sid.keys()}

    async def _fetch_price(sid: int) -> Tuple[int, float]:
        p = await pricing(sid, start_block, end_block)
        val = float(getattr(p, "tao", 0.0) or 0.0)
        return sid, val

    async def _fetch_depth(sid: int) -> Tuple[int, int]:
        try:
            return sid, int(await pool_depth_of(sid))
        except Exception:
            return sid, 0

    price_pairs, depth_pairs = await asyncio.gather(
        asyncio.gather(*(_fetch_price(s) for s in subnets)),
        asyncio.gather(*(_fetch_depth(s) for s in subnets)),
    )
    price_cache = dict(price_pairs)   # sid -> float
    depth_cache = dict(depth_pairs)   # sid -> int

    # 6) Convert to TAO with slippage and sum per uid
    tao_by_uid: Dict[int, float] = defaultdict(float)
    for (uid, sid), rao in rao_by_uid_sid.items():
        price = price_cache.get(sid, 0.0)
        depth = depth_cache.get(sid, 0)
        tao_by_uid[uid] += rao_to_tao_slippage(rao, price, depth)

    # 7) List aligned to miner_uids
    return [float(tao_by_uid.get(uid, 0.0)) for uid in miner_uids]


# ╭──────────────────────── Allocation / Budget Helpers ───────────────────╮
# Kept intact (or near-intact) to avoid breaking other engines.
# ╰────────────────────────────────────────────────────────────────────────╯

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
    if share <= 0:
        return 0.0
    return auction_budget_alpha * float(share)


def compute_budget_share(
    *,
    my_stake: float,
    total_master_stake: float,
    auction_budget_alpha: float = AUCTION_BUDGET_ALPHA,
) -> tuple[float, float]:
    if my_stake <= 0 or total_master_stake <= 0:
        return 0.0, 0.0
    share = float(my_stake) / float(total_master_stake)
    return share, budget_from_share(share=share, auction_budget_alpha=auction_budget_alpha)


def calc_required_rao(alpha: float, discount_bps: int, *, planck: int = PLANCK) -> int:
    _ = discount_bps  # discount does not reduce payment owed
    return int(round(float(alpha) * float(planck)))


def _bid_order_key(b: BidInput) -> tuple:
    per_unit_value = (b.weight_bps / 10_000.0) * (1.0 - b.discount_bps / 10_000.0)
    return (-per_unit_value, -float(b.alpha), int(b.idx))


def allocate_bids(
    bids: Iterable[BidInput],
    *,
    my_budget_alpha: float,
    cap_alpha_by_ck: Mapping[str, float] | None = None,
) -> tuple[List[WinAllocation], float, dict]:
    """
    Deterministic greedy allocation with optional per‑CK caps and a
    second pass to utilize leftover budget.
    """
    ordered = sorted(bids, key=_bid_order_key)

    def _pua(b: BidInput) -> float:
        return (b.weight_bps / 10_000.0) * (1.0 - b.discount_bps / 10_000.0)

    debug: dict = {
        "ordered": [
            {
                "miner_uid": b.miner_uid, "coldkey": b.coldkey, "subnet_id": b.subnet_id,
                "alpha": float(b.alpha), "discount_bps": int(b.discount_bps),
                "weight_bps": int(b.weight_bps), "idx": int(b.idx),
                "per_unit_value": _pua(b),
            } for b in ordered
        ],
        "caps": dict(cap_alpha_by_ck or {}),
        "budget_initial": float(my_budget_alpha),
        "pass1_takes": [],
        "pass2_takes": [],
    }

    from collections import defaultdict as _dd
    allocated_by_ck: dict[str, float] = _dd(float)
    taken_by_bid: dict[tuple, float] = _dd(float)
    alloc_by_key: dict[tuple, WinAllocation] = {}

    def _key(b: BidInput) -> tuple:
        return (b.miner_uid, b.coldkey, b.subnet_id, b.discount_bps, b.weight_bps, b.idx)

    def _take(pass_caps: Mapping[str, float] | None, budget: float, pass_name: str) -> float:
        for b in ordered:
            if budget <= 0:
                break
            key = _key(b)
            remaining = float(b.alpha) - taken_by_bid.get(key, 0.0)
            if remaining <= 0:
                continue

            cap_rem = float("inf")
            cap_before = None
            cap_after = None
            if pass_caps is not None:
                cap = float(pass_caps.get(b.coldkey, my_budget_alpha))
                cap_before = max(0.0, cap - allocated_by_ck[b.coldkey])
                cap_rem = max(0.0, cap_before)

            take = min(remaining, budget, cap_rem)
            if take <= 0:
                continue

            if key in alloc_by_key:
                wa = alloc_by_key[key]
                wa.alpha_accepted += take
            else:
                alloc_by_key[key] = WinAllocation(
                    miner_uid=b.miner_uid,
                    coldkey=b.coldkey,
                    subnet_id=b.subnet_id,
                    alpha_requested=b.alpha,
                    alpha_accepted=take,
                    discount_bps=b.discount_bps,
                    weight_bps=b.weight_bps,
                )

            budget -= take
            taken_by_bid[key] = taken_by_bid.get(key, 0.0) + take
            allocated_by_ck[b.coldkey] += take
            if pass_caps is not None:
                cap_after = max(0.0, float(pass_caps.get(b.coldkey, my_budget_alpha)) - allocated_by_ck[b.coldkey])

            debug[f"{pass_name}_takes"].append({
                "miner_uid": b.miner_uid, "coldkey": b.coldkey, "subnet_id": b.subnet_id,
                "alpha_req": float(b.alpha), "take": float(take),
                "discount_bps": int(b.discount_bps), "weight_bps": int(b.weight_bps), "idx": int(b.idx),
                "cap_before": cap_before, "cap_after": cap_after,
            })
        return budget

    budget = float(my_budget_alpha)
    budget = _take(cap_alpha_by_ck or {}, budget, "pass1")
    debug["budget_after_pass1"] = float(budget)

    if budget > 0:
        budget = _take(None, budget, "pass2")
    debug["budget_after_pass2"] = float(budget)

    wins: List[WinAllocation] = list(alloc_by_key.values())
    return wins, budget, debug


__all__ = [
    "TransferEvent",
    "PricingProvider",
    "PoolDepthProvider",
    "MinerResolver",
    "rao_to_tao_slippage",
    "compute_epoch_rewards",
    "BidInput",
    "WinAllocation",
    "budget_from_share",
    "compute_budget_share",
    "calc_required_rao",
    "allocate_bids",
]
