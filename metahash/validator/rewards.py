# ╭────────────────────────────────────────────────────────────────────────╮
# metahash/validator/rewards.py
# ╰────────────────────────────────────────────────────────────────────────╯

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import (
    Any,
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
    FORBIDDEN_ALPHA_SUBNETS,
    AUCTION_BUDGET_ALPHA,
    PLANCK,
)

# ↓ single authoritative event model
from metahash.validator.alpha_transfers import TransferEvent

# ───────────────────────────── GLOBAL CONSTANTS ───────────────────────── #

getcontext().prec = 60                          # 60‑digit arithmetic precision
DECIMALS: Decimal = Decimal(10) ** 9            # planck scaling (1 α = 10^9 planck)

# Keep monetary constants in Decimal space
K_SLIP_D: Decimal = Decimal(str(K_SLIP))
SLIP_TOLERANCE_D: Decimal = Decimal(str(SLIP_TOLERANCE))

# ──────────────────────────────── PROTOCOLS ───────────────────────────── #


@runtime_checkable
class TransferScanner(Protocol):
    async def scan(self, from_block: int, to_block: int) -> List["TransferEvent"]: ...


@runtime_checkable
class BalanceLike(Protocol):
    tao: float          # TAO price of 1 α
    rao: int | None     # current pool depth (planck)


@runtime_checkable
class PricingProvider(Protocol):
    async def __call__(self, subnet_id: int, start: int, end: int) -> BalanceLike: ...


@runtime_checkable
class PoolDepthProvider(Protocol):
    async def __call__(self, subnet_id: int) -> int: ...


@runtime_checkable
class MinerResolver(Protocol):
    async def __call__(self, coldkey: str) -> int | None: ...


# ──────────────────────────────── MODEL ───────────────────────────────── #

@dataclass(slots=True)
class AlphaDeposit:
    """
    Intermediate representation: aggregated α coming **into the treasury**
    from a single cold‑key on a single subnet during an epoch.
    """
    coldkey: str          # origin miner cold‑key
    subnet_id: int
    alpha_raw: int

    # Runtime‑enriched fields
    miner_uid: int | None = None
    avg_price: Decimal | None = None
    tao_value: Decimal | None = None
    tao_value_post_slip: Decimal | None = None

    # Convenience ------------------------------------------------------ #
    def merge_from(self, other: "AlphaDeposit") -> None:
        self.alpha_raw += other.alpha_raw

    def __repr__(self) -> str:
        return (
            f"AlphaDeposit(coldkey={self.coldkey!r}, uid={self.miner_uid}, "
            f"subnet={self.subnet_id}, α_raw={self.alpha_raw}, "
            f"tao_post_slip={self.tao_value_post_slip})"
        )

# ────────────────────────────── HELPERS ───────────────────────────────── #


def _apply_slippage(alpha_raw: int, price: Decimal, depth_rao: int) -> Decimal:
    """
    Convert raw α (planck) to **post‑slippage TAO**.
    All arithmetic is done in Decimal space.
    """
    if depth_rao <= 0:
        return Decimal(0)

    ratio = Decimal(alpha_raw) / (Decimal(depth_rao) + Decimal(alpha_raw))
    slip = K_SLIP_D * ratio

    if slip <= SLIP_TOLERANCE_D:
        slip = Decimal(0)
    slip = min(slip, Decimal(1))

    return Decimal(alpha_raw) * price * (Decimal(1) - slip) / DECIMALS

# ╭──────────────────────── PHASE 0 – COMBINE ─────────────────────────────╯


def _combine_deposits_by_miner_subnet(
    deposits: List[AlphaDeposit],
) -> List[AlphaDeposit]:
    """
    Aggregate raw α per **(miner_uid | coldkey, subnet)**.

    Unknown miners get bucketed by cold‑key so we don’t accidentally merge
    unrelated addresses under a single `(None, subnet)` key.
    """
    merged: Dict[Tuple[str | int, int], AlphaDeposit] = {}
    for d in deposits:
        uid_or_ck: str | int = d.miner_uid if d.miner_uid is not None else d.coldkey
        k = (uid_or_ck, d.subnet_id)
        if k in merged:
            merged[k].merge_from(d)
        else:
            merged[k] = d
    return list(merged.values())

# ╭──────────────────────────── CAST EVENTS ───────────────────────────────╯


def cast_events(events: Sequence[TransferEvent]) -> List[AlphaDeposit]:
    """
    Convert TransferEvents → AlphaDeposits, *dropping* any event whose
    destination subnet is listed in `FORBIDDEN_ALPHA_SUBNETS`.
    """
    kept: List[AlphaDeposit] = []
    dropped = 0
    for ev in events:
        if ev.subnet_id in FORBIDDEN_ALPHA_SUBNETS:
            dropped += 1
            continue
        kept.append(
            AlphaDeposit(
                coldkey=ev.src_coldkey,
                subnet_id=ev.subnet_id,
                alpha_raw=ev.amount_rao,
            )
        )
    if dropped:
        bt.logging.debug(
            f"[rewards] {dropped} α‑transfers ignored "
            f"(forbidden subnets: {FORBIDDEN_ALPHA_SUBNETS})"
        )
    return kept

# ╭────────────────────────────── PHASE 1 ─────────────────────────────────╯


async def scan_transfers(
    *, scanner: TransferScanner, from_block: int, to_block: int
) -> List[TransferEvent]:
    return await scanner.scan(from_block, to_block)

# ╭────────────────────────────── PHASE 2 ─────────────────────────────────╯
# (no dedicated phase 2 – handled in cast/resolve)

# ╭────────────────────────────── PHASE 3 ─────────────────────────────────╯


async def resolve_miners(
    deposits: List[AlphaDeposit], *, uid_of_coldkey: MinerResolver
) -> None:
    """
    Fill `miner_uid` field by mapping cold‑keys → UIDs.
    Unknown miners stay as `None` and will have their α burned silently.
    """
    coldkeys = {d.coldkey for d in deposits}
    cache = {ck: await uid_of_coldkey(ck) for ck in coldkeys}
    for d in deposits:
        d.miner_uid = cache[d.coldkey]

# ╭────────────────────────────── PHASE 4 ─────────────────────────────────╯


async def attach_prices(
    deposits: List[AlphaDeposit],
    *,
    pricing: PricingProvider,
    epoch_start: int,
    epoch_end: int,
) -> None:
    if not deposits:
        return

    subnets = {d.subnet_id for d in deposits}
    price_cache: Dict[int, Decimal] = {}
    for sid in subnets:
        p = await pricing(sid, epoch_start, epoch_end)
        if p is None or p.tao is None:
            raise RuntimeError(f"Price oracle returned None for subnet {sid}")
        price_cache[sid] = Decimal(str(p.tao))

    bt.logging.debug(f"[rewards] price_cache: {price_cache}")

    for d in deposits:
        price = price_cache[d.subnet_id]
        d.avg_price = price
        d.tao_value = Decimal(d.alpha_raw) * price / DECIMALS

# ╭────────────────────────────── PHASE 5 ─────────────────────────────────╯


async def apply_slippage(
    deposits: List[AlphaDeposit], *, pool_depth_of: PoolDepthProvider
) -> None:
    if not deposits:
        return

    subnets = {d.subnet_id for d in deposits}

    async def _gather(sid: int) -> Tuple[int, int]:
        return sid, await pool_depth_of(sid)

    depth_pairs = await asyncio.gather(*(_gather(s) for s in subnets))
    depth_cache = dict(depth_pairs)
    bt.logging.debug(f"[rewards] depth_cache: {depth_cache}")

    for d in deposits:
        depth = depth_cache[d.subnet_id]
        d.tao_value_post_slip = _apply_slippage(
            d.alpha_raw, d.avg_price, depth
        )

# ╭────────────────────────────── PHASE 6 ─────────────────────────────────╯


def _aggregate_post_slip_tao(
    deposits: Iterable[AlphaDeposit],
) -> Dict[int, Decimal]:
    """
    Map of {uid → post‑slippage TAO}.  
    Deposits whose miner UID could not be resolved are **ignored**.
    """
    by_uid: Dict[int, Decimal] = {}
    for d in deposits:
        if d.miner_uid is None:
            continue
        if d.tao_value_post_slip is None:
            continue
        by_uid[d.miner_uid] = (
            by_uid.get(d.miner_uid, Decimal(0)) + d.tao_value_post_slip
        )
    return by_uid

# ╭────────────────────────────── PIPELINE ────────────────────────────────╯


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
    """
    Calculate **post‑slippage TAO rewards** for each `miner_uid`.

    Parameters
    ----------
    miner_uids
        Order of miners that must appear in the output list.
    start_block / end_block
        Inclusive block range to inspect.

    Returns
    -------
    List[float]
        Post‑slippage TAO amounts per miner **as native floats**
        (aligned with `miner_uids`).
    """

    # 1. TRANSFER COLLECTION ------------------------------------------- #
    if events is not None:
        raw = list(events)
        bt.logging.debug(
            f"[rewards] Using {len(raw)} injected transfer event(s) "
            f"for blocks {start_block}-{end_block}"
        )
    else:
        if scanner is None:
            raise ValueError("compute_epoch_rewards: need either events or scanner")
        bt.logging.debug(
            f"[rewards] Scanning transfers on‑chain "
            f"({start_block}-{end_block})…"
        )
        t0 = time.time()
        raw = await scan_transfers(
            scanner=scanner,
            from_block=start_block,
            to_block=end_block,
        )
        bt.logging.debug(f"[rewards] scan finished in {time.time() - t0:.2f}s")

    # 2. CAST ----------------------------------------------------------- #
    deposits = cast_events(raw)
    bt.logging.debug(f"[rewards] deposits(after cast): {deposits}")

    # 3. RESOLVE MINERS ------------------------------------------------- #
    await resolve_miners(deposits, uid_of_coldkey=uid_of_coldkey)

    # 4. COMBINE -------------------------------------------------------- #
    deposits = _combine_deposits_by_miner_subnet(deposits)

    # 5. PRICE ---------------------------------------------------------- #
    await attach_prices(
        deposits,
        pricing=pricing,
        epoch_start=start_block,
        epoch_end=end_block,
    )

    # 6. SLIPPAGE ------------------------------------------------------- #
    await apply_slippage(deposits, pool_depth_of=pool_depth_of)

    # 7. AGGREGATE ------------------------------------------------------ #
    rewards_dec = _aggregate_post_slip_tao(deposits)
    bt.logging.debug(f"[rewards] value_per_miner_dict: {rewards_dec}")

    # 8. BUILD FLOAT LIST ---------------------------------------------- #
    rewards_list_float: List[float] = [
        float(rewards_dec.get(uid, Decimal(0))) for uid in miner_uids
    ]
    total_value = sum(rewards_list_float)
    bt.logging.debug(f"[rewards] total_value: {total_value}")
    bt.logging.debug(f"[rewards] rewards_list_float: {rewards_list_float}")

    return rewards_list_float


# ╭──────────────────────────── Allocation Utils ──────────────────────────╯

@dataclass(slots=True, frozen=True)
class BidInput:
    """
    Minimal, serializable bid shape used by allocation helpers.
    """
    miner_uid: int
    coldkey: str
    subnet_id: int
    alpha: float                # α requested for this line
    discount_bps: int           # valuation discount in bps
    weight_bps: int             # subnet weight in bps (0..10_000)
    idx: int = 0                # stable, per-miner ordering


@dataclass(slots=True)
class WinAllocation:
    """
    A winning allocation after capping and budget constraints are applied.
    """
    miner_uid: int
    coldkey: str
    subnet_id: int
    alpha_accepted: float       # α actually accepted (may be truncated)
    discount_bps: int
    weight_bps: int


def budget_from_share(*, share: float, auction_budget_alpha: float = AUCTION_BUDGET_ALPHA) -> float:
    """
    Compute this validator's α budget from its share among master validators.
    """
    if share <= 0:
        return 0.0
    return auction_budget_alpha * float(share)


def compute_budget_share(
    *,
    my_stake: float,
    total_master_stake: float,
    auction_budget_alpha: float = AUCTION_BUDGET_ALPHA,
) -> tuple[float, float]:
    """
    Return (share, budget_α) given my stake and the total active masters' stake.
    """
    if my_stake <= 0 or total_master_stake <= 0:
        return 0.0, 0.0
    share = float(my_stake) / float(total_master_stake)
    return share, budget_from_share(share=share, auction_budget_alpha=auction_budget_alpha)


def calc_required_rao(alpha: float, discount_bps: int, *, planck: int = PLANCK) -> int:
    """
    Given α and discount, compute the RAO required to cover the line.
    """
    disc = max(0, min(10_000, int(discount_bps)))
    eff_alpha = float(alpha) * (1.0 - disc / 10_000.0)
    return int(round(eff_alpha * planck))


def caps_by_reputation_for_bids(
    bids: Iterable[BidInput],
    *,
    my_budget_alpha: float,
    reputation: Mapping[str, float],
    baseline_cap_frac: float,
    max_cap_frac: float,
) -> dict[str, float]:
    """
    Per-coldkey α caps derived from reputation for exactly the coldkeys present in `bids`.
    """
    caps: dict[str, float] = {}
    bcf = float(baseline_cap_frac)
    mcf = float(max_cap_frac)
    for b in bids:
        R = float(reputation.get(b.coldkey, 0.0))
        R = max(0.0, min(1.0, R))
        cap_frac = bcf + R * (mcf - bcf)
        cap_frac = max(bcf, min(mcf, cap_frac))
        caps[b.coldkey] = my_budget_alpha * cap_frac
    return caps


def _bid_order_key(b: BidInput) -> tuple:
    """
    Deterministic order:
      1) higher subnet weight,
      2) higher discount (cheaper),
      3) larger α,
      4) earlier index.
    """
    return (-b.weight_bps, -b.discount_bps, -float(b.alpha), int(b.idx))


def allocate_bids(
    bids: Iterable[BidInput],
    *,
    my_budget_alpha: float,
    cap_alpha_by_ck: Mapping[str, float] | None = None,
) -> tuple[List[WinAllocation], float]:
    """
    Greedy, deterministic allocation under (global budget + per‑CK caps).
    """
    ordered = sorted(bids, key=_bid_order_key)
    caps = dict(cap_alpha_by_ck or {})
    allocated: dict[str, float] = defaultdict(float)
    budget = float(my_budget_alpha)

    wins: List[WinAllocation] = []
    for b in ordered:
        if budget <= 0:
            break
        cap_rem = float(caps.get(b.coldkey, my_budget_alpha)) - allocated[b.coldkey]
        take = min(float(b.alpha), budget, max(0.0, cap_rem))
        if take <= 0:
            continue
        wins.append(
            WinAllocation(
                miner_uid=b.miner_uid,
                coldkey=b.coldkey,
                subnet_id=b.subnet_id,
                alpha_accepted=take,
                discount_bps=b.discount_bps,
                weight_bps=b.weight_bps,
            )
        )
        budget -= take
        allocated[b.coldkey] += take

    return wins, budget


def build_pending_commitment(
    wins: Iterable[WinAllocation],
    *,
    epoch_cleared: int,
    treasury_coldkey: str,
    stake_snapshot_block: int,
    planck: int = PLANCK,
) -> dict[str, Any]:
    """
    Produce the exact pending commitment payload used later when publishing.
    """
    inv: dict[str, dict[str, Any]] = {}
    for w in wins:
        uid_key = str(int(w.miner_uid))
        if uid_key not in inv:
            inv[uid_key] = {"ck": w.coldkey, "ln": []}
        req_rao = calc_required_rao(w.alpha_accepted, w.discount_bps, planck=planck)
        inv[uid_key]["ln"].append(
            [
                int(w.subnet_id),
                int(w.discount_bps),
                int(w.weight_bps),
                int(req_rao),
            ]
        )
    return {
        "e": int(epoch_cleared),
        "t": str(treasury_coldkey),
        "inv": inv,
        "st_blk": int(stake_snapshot_block),
        # ("pe", "as", "de") are attached at publish time by validator.
    }


# ───────── settlement: commitments + payments → per‑UID value/burn/offenders ────── #

def _line_fill_order_key(ln_tuple: tuple[int, int, int, int, int]) -> tuple[float, int, int]:
    """
    Sorting key for commitment invoice lines within a subnet:
      - higher weight first,
      - higher discount next,
      - larger required RAO next,
      - earlier index last.
    """
    sid, disc_bps, w_bps, req_rao_paid, idx = ln_tuple
    score = (w_bps / 10_000.0) * (1.0 + disc_bps / 10_000.0)
    return (score, req_rao_paid, -idx)


def evaluate_commitment_snapshots(
    *,
    snapshots: Sequence[dict],
    rao_by_uid_dest_subnet: Mapping[tuple[int, str, int], int],
    tao_by_uid_dest_subnet: Mapping[tuple[int, str, int], float],
    price_cache: Mapping[int, float],
    planck: int = PLANCK,
) -> tuple[
    dict[int, float],       # scores_by_uid
    float,                  # burn_total_value
    Set[str],               # offenders_nopay
    Set[str],               # offenders_partial
    dict[str, float],       # paid_effective_by_ck
]:
    """
    Core **reward calculation** used at settlement:
    takes the published commitment snapshots and the actually‑paid transfers
    → returns per‑UID TAO value, burn amount, and offender sets.
    """
    scores_by_uid: Dict[int, float] = defaultdict(float)
    burn_total_value: float = 0.0
    offenders_nopay: Set[str] = set()
    offenders_partial: Set[str] = set()
    paid_effective_by_ck: Dict[str, float] = defaultdict(float)

    for s in snapshots:
        tre = s.get("t", "")
        inv = s.get("inv", {})
        for uid_s, inv_d in inv.items():
            try:
                uid = int(uid_s)
            except Exception:
                continue

            ck = inv_d.get("ck")
            by_subnet: Dict[int, List[tuple[int, int, int, int, int]]] = defaultdict(list)
            for idx_line, ln in enumerate(inv_d.get("ln", [])):
                if not (isinstance(ln, list) and len(ln) >= 4):
                    continue
                sid, disc_bps, w_bps, req_rao_paid = int(ln[0]), int(ln[1]), int(ln[2]), int(ln[3])
                by_subnet[sid].append((sid, disc_bps, w_bps, req_rao_paid, idx_line))

            coldkey_any_paid = False
            coldkey_uncovered_exists = False

            for sid, lines in by_subnet.items():
                paid_rao_total = int(rao_by_uid_dest_subnet.get((uid, tre, sid), 0))
                tao_total = float(tao_by_uid_dest_subnet.get((uid, tre, sid), 0.0))

                lines.sort(key=_line_fill_order_key, reverse=True)
                remain = paid_rao_total
                covered: List[tuple[int, int, int, int, int]] = []
                uncovered: List[tuple[int, int, int, int, int]] = []

                for ln_t in lines:
                    _, _, _, req_rao_paid, _ = ln_t
                    if remain >= req_rao_paid:
                        covered.append(ln_t)
                        remain -= req_rao_paid
                        coldkey_any_paid = True
                    else:
                        uncovered.append(ln_t)
                        if req_rao_paid > 0:
                            coldkey_uncovered_exists = True

                if paid_rao_total > 0 and tao_total > 0 and covered:
                    tao_per_rao = tao_total / float(paid_rao_total)
                else:
                    tao_per_rao = 0.0

                # Covered lines → value credited = TAO_paid(line) × subnet_weight
                for sid2, disc_bps, w_bps, req_rao_paid, _ in covered:
                    line_tao = tao_per_rao * float(req_rao_paid) if tao_per_rao > 0 else 0.0
                    eff = line_tao * (w_bps / 10_000.0)
                    scores_by_uid[uid] += eff
                    if ck:
                        paid_effective_by_ck[ck] += eff

                # Uncovered lines → full line value is burned (fallback to price cache if needed)
                for sid2, disc_bps, w_bps, req_rao_paid, _ in uncovered:
                    if tao_per_rao > 0:
                        line_tao = tao_per_rao * float(req_rao_paid)
                    else:
                        price = float(price_cache.get(sid2, 0.0))
                        line_tao = price * (float(req_rao_paid) / float(planck))
                    eff_burn = line_tao * (w_bps / 10_000.0)
                    burn_total_value += eff_burn

            if ck:
                if not coldkey_any_paid:
                    offenders_nopay.add(ck)
                elif coldkey_uncovered_exists:
                    offenders_partial.add(ck)

    return dict(scores_by_uid), float(burn_total_value), offenders_nopay, offenders_partial, dict(paid_effective_by_ck)
