# ╭────────────────────────────────────────────────────────────────────────╮
# metahash/validator/rewards.py            Epoch reward‑calculation logic
# ╰────────────────────────────────────────────────────────────────────────╯


from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Protocol,
    Sequence,
    Tuple,
    Optional,
    runtime_checkable,
)

import bittensor as bt
from metahash.config import (
    K_SLIP,
    SLIP_TOLERANCE,
    FORBIDDEN_ALPHA_SUBNETS,  # ← NEW
)

# ───────────────────────────── GLOBAL CONSTANTS ────────────────────────── #

getcontext().prec = 60  # 60‑digit arithmetic precision

PLANCK: int = 10**18
DECIMALS: Decimal = Decimal(10) ** 18

# Keep monetary constants in Decimal space
K_SLIP_D: Decimal = Decimal(str(K_SLIP))
SLIP_TOLERANCE_D: Decimal = Decimal(str(SLIP_TOLERANCE))

# ───────────────────────────── PROTOCOLS (DI) ──────────────────────────── #


@runtime_checkable
class TransferScanner(Protocol):
    async def scan(self, from_block: int, to_block: int) -> List["TransferEvent"]: ...


@runtime_checkable
class BalanceLike(Protocol):
    tao: float  # TAO price of 1 α
    rao: int | None  # current pool depth (planck)


@runtime_checkable
class PricingProvider(Protocol):
    async def __call__(self, subnet_id: int, start: int, end: int) -> BalanceLike: ...


@runtime_checkable
class PoolDepthProvider(Protocol):
    async def __call__(self, subnet_id: int) -> int: ...


@runtime_checkable
class MinerResolver(Protocol):
    async def __call__(self, coldkey: str) -> int | None: ...


# ──────────────────────────────── EVENTS ───────────────────────────────── #


@dataclass(slots=True, frozen=True)
class TransferEvent:
    coldkey: str
    subnet_id: int
    amount_rao: int


# ──────────────────────────────── MODEL ───────────────────────────────── #


@dataclass(slots=True)
class AlphaDeposit:
    coldkey: str
    subnet_id: int
    alpha_raw: int

    # Runtime‑enriched fields
    miner_uid: int | None = None
    avg_price: Decimal | None = None
    tao_value: Decimal | None = None
    tao_value_post_slip: Decimal | None = None
    sn73_awarded: Decimal | None = None

    # Convenience ------------------------------------------------------ #
    def merge_from(self, other: "AlphaDeposit") -> None:
        self.alpha_raw += other.alpha_raw

    def __repr__(self) -> str:
        return (
            f"AlphaDeposit(coldkey={self.coldkey!r}, uid={self.miner_uid}, "
            f"subnet={self.subnet_id}, α_raw={self.alpha_raw}, "
            f"tao_post_slip={self.tao_value_post_slip}, "
            f"sn73={self.sn73_awarded})"
        )


# ────────────────────────────── HELPERS ────────────────────────────────── #


def _apply_slippage(
    alpha_raw: int, price: Decimal, depth_rao: int
) -> Decimal:
    """
    Convert raw α (planck) to **post‑slippage TAO**.
    All arithmetic stays in Decimal space.
    """
    if depth_rao <= 0:
        return Decimal(0)

    ratio = Decimal(alpha_raw) / (Decimal(depth_rao) + Decimal(alpha_raw))
    slip = K_SLIP_D * ratio

    if slip <= SLIP_TOLERANCE_D:
        slip = Decimal(0)
    slip = min(slip, Decimal(1))

    return Decimal(alpha_raw) * price * (Decimal(1) - slip) / DECIMALS


# ╭────────────────────────────── PHASE 0 ─────────────────────────────────╮
# Combine transfers per miner / subnet **before** pricing & slippage.
# ╰────────────────────────────────────────────────────────────────────────╯


def _combine_deposits_by_miner_subnet(
    deposits: List[AlphaDeposit],
) -> List[AlphaDeposit]:
    """
    Aggregate raw α per **(miner_uid | coldkey, subnet)**.
    Unknown miners are keyed by coldkey to avoid coalescing unrelated
    addresses under a single (None, subnet) bucket.
    """
    merged: Dict[Tuple[str | int, int], AlphaDeposit] = {}

    for d in deposits:
        uid_or_ck = d.miner_uid if d.miner_uid is not None else d.coldkey
        key = (uid_or_ck, d.subnet_id)
        if key in merged:
            merged[key].merge_from(d)
        else:
            merged[key] = d

    return list(merged.values())


# ╭────────────────────────────◀ CAST EVENTS ▶─────────────────────────────╮


def cast_events(events: Sequence[TransferEvent]) -> List[AlphaDeposit]:
    """
    Convert raw TransferEvents → AlphaDeposits, *dropping* any event whose
    `subnet_id` is listed in FORBIDDEN_ALPHA_SUBNETS.
    """
    kept: List[AlphaDeposit] = []
    dropped = 0
    for ev in events:
        if ev.subnet_id in FORBIDDEN_ALPHA_SUBNETS:
            dropped += 1
            continue
        kept.append(
            AlphaDeposit(
                coldkey=ev.coldkey,
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


# ╭────────────────────────────── PHASE 1 ─────────────────────────────────╮
async def scan_transfers(
    *, scanner: TransferScanner, from_block: int, to_block: int
) -> List[TransferEvent]:
    return await scanner.scan(from_block, to_block)


# ╭────────────────────────◀ CAST → AlphaDeposit ▶─────────────────────────╮
# (already implemented above)


# ╭────────────────────────────── PHASE 3 ─────────────────────────────────╮
async def resolve_miners(
    deposits: List[AlphaDeposit], *, uid_of_coldkey: MinerResolver
) -> None:
    coldkeys = {d.coldkey for d in deposits}
    cache = {ck: await uid_of_coldkey(ck) for ck in coldkeys}

    for d in deposits:
        d.miner_uid = cache[d.coldkey]


# ╭────────────────────────────── PHASE 4 ─────────────────────────────────╮
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

    bt.logging.info(f"Price Cache Dict: {price_cache}")

    for d in deposits:
        price = price_cache[d.subnet_id]
        d.avg_price = price
        d.tao_value = Decimal(d.alpha_raw) * price / DECIMALS


# ╭────────────────────────────── PHASE 5 ─────────────────────────────────╮
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

    bt.logging.info(f"Depth Cache Dict: {depth_cache}")

    for d in deposits:
        depth = depth_cache[d.subnet_id]
        d.tao_value_post_slip = _apply_slippage(
            d.alpha_raw, d.avg_price, depth
        )


# ╭────────────────────────────── PHASE 6 ─────────────────────────────────╮
def allocate_bond_curve(
    *,
    deposits: List[AlphaDeposit],
    bag_sn73: int,
    c0: float,
    beta: float,
    r_min: float,
) -> None:
    if not deposits:
        return

    bag = Decimal(str(bag_sn73))
    beta_d = Decimal(str(beta))
    c0_d = Decimal(str(c0))
    r_min_d = Decimal(str(r_min))

    m_used = Decimal(0)

    for d in deposits:
        if d.tao_value_post_slip is None:
            continue

        rate = max(r_min_d, c0_d / (Decimal(1) + beta_d * m_used))
        wish = d.tao_value_post_slip * rate
        room = bag - m_used
        awarded = max(Decimal(0), min(wish, room))

        d.sn73_awarded = awarded
        m_used += awarded

        if m_used >= bag:
            break


# ╭────────────────────────────── PHASE 7 ─────────────────────────────────╮
def aggregate_miner_rewards(
    deposits: Iterable[AlphaDeposit],
) -> Dict[int, Decimal]:
    """
    Map of {uid → SN‑73}.  
    Unknown miners’ awards are burned (UID 0).
    """
    by_uid: Dict[int, Decimal] = {}
    for d in deposits:
        if not d.sn73_awarded:
            continue
        uid = d.miner_uid if d.miner_uid is not None else 0
        by_uid[uid] = by_uid.get(uid, Decimal(0)) + d.sn73_awarded
    return by_uid


# ╭────────────────────────────── PHASE 8 ─────────────────────────────────╮
def build_weights(
    *,
    rewards: Mapping[int, Decimal | float],
    miner_uids: Sequence[int],
    burn_uid: int | None = 0,
    eps: Decimal = Decimal("1e-24"),
) -> List[float]:
    total = Decimal(0)
    for uid in miner_uids:
        if uid == burn_uid:
            continue
        total += Decimal(str(rewards.get(uid, 0)))

    if total < eps:
        return [0.0] * len(miner_uids)

    inv_total = Decimal(1) / total
    weights: List[float] = []
    for uid in miner_uids:
        if uid == burn_uid:
            weights.append(0.0)
        else:
            amt = Decimal(str(rewards.get(uid, 0)))
            weights.append(float(amt * inv_total))
    return weights


# ╭────────────────────────────── PIPELINE ────────────────────────────────╮
@dataclass(slots=True, frozen=True)
class EpochRewards:
    deposits: List[AlphaDeposit]
    rewards_per_miner: Dict[int, Decimal]
    miner_uids: List[int]
    rewards: List[Decimal]
    weights: List[float]


async def compute_epoch_rewards(
    *,
    validator: Any,
    scanner: Optional[TransferScanner] = None,
    events: Optional[Sequence[TransferEvent]] = None,
    pricing: PricingProvider,
    uid_of_coldkey: MinerResolver,
    start_block: int,
    end_block: int,
    bag_sn73: int,
    c0: float,
    beta: float,
    r_min: float,
    pool_depth_of: PoolDepthProvider,
    log: Callable[[str], None] | None = None,
) -> EpochRewards:
    """
    Calculate SN‑73 rewards for a given (partial) auction window.

    Either *events* **or** *scanner* must be provided.

    • If *events* is given, it is taken as the authoritative list of
      α‑transfers between *start_block*..*end_block* **inclusive**.

    • Otherwise, *scanner* is used to fetch transfers on‑chain.
    """
    # ------------------------------------------------------------------ #
    miner_uids = list(validator.get_miner_uids())
    metagraph_size = len(miner_uids)

    # 1. TRANSFER COLLECTION ------------------------------------------------ #
    if events is not None:
        raw = list(events)
        bt.logging.info(
            f"[rewards] Using {len(raw)} injected transfer event(s) "
            f"for blocks {start_block}-{end_block}"
        )
    else:
        if scanner is None:
            raise ValueError("compute_epoch_rewards: need either events or scanner")
        bt.logging.info(
            f"[rewards] Scanning transfers on‑chain "
            f"({start_block}-{end_block})…"
        )
        raw = await scan_transfers(scanner=scanner,
                                   from_block=start_block,
                                   to_block=end_block)

    # 2. CAST (forbidden subnet filter) ------------------------------------ #
    bt.logging.info("[rewards] Casting events…")
    deposits = cast_events(raw)

    # 3. RESOLVE MINERS ----------------------------------------------------- #
    bt.logging.info("[rewards] Resolving miner UIDs…")
    await resolve_miners(deposits, uid_of_coldkey=uid_of_coldkey)

    # 4. COMBINE ------------------------------------------------------------ #
    bt.logging.info("[rewards] Combining deposits per miner/subnet…")
    deposits = _combine_deposits_by_miner_subnet(deposits)

    # 5. PRICE -------------------------------------------------------------- #
    bt.logging.info("[rewards] Attaching prices…")
    await attach_prices(deposits,
                        pricing=pricing,
                        epoch_start=start_block,
                        epoch_end=end_block)

    # 6. SLIPPAGE ----------------------------------------------------------- #
    bt.logging.info("[rewards] Applying slippage with averaged depths…")
    await apply_slippage(deposits, pool_depth_of=pool_depth_of)

    # 7. BOND CURVE --------------------------------------------------------- #
    bt.logging.info("[rewards] Applying bond curve…")
    allocate_bond_curve(deposits=deposits,
                        bag_sn73=bag_sn73,
                        c0=c0,
                        beta=beta,
                        r_min=r_min)

    # 8. AGGREGATION -------------------------------------------------------- #
    rewards_per_miner = aggregate_miner_rewards(deposits)
    bt.logging.info(f"[rewards] Rewards per miner: {rewards_per_miner}")
    rewards_list = [rewards_per_miner.get(uid, Decimal(0)) for uid in miner_uids]

    # Burn any surplus
    bag = Decimal(str(bag_sn73))
    surplus = bag - sum(rewards_per_miner.values())
    if surplus > 0:
        rewards_per_miner[0] = rewards_per_miner.get(0, Decimal(0)) + surplus
    bt.logging.info(
        f"[rewards] Surplus to burn: {surplus} (bag {bag})"
    )

    # 9. NORMALISATION ------------------------------------------------------ #
    weights = build_weights(rewards=rewards_per_miner,
                            miner_uids=miner_uids,
                            burn_uid=0)

    issued = float(sum(rewards_per_miner.values()))
    (log or bt.logging.info)(
        f"✓ Window {start_block}-{end_block}: "
        f"{metagraph_size} miners, "
        f"{issued:.2f}/{bag_sn73} SN‑73 issued, "
        f"last deposit: {deposits[-1] if deposits else 'n/a'}"
    )

    return EpochRewards(
        deposits=deposits,
        rewards_per_miner=rewards_per_miner,
        miner_uids=miner_uids,
        rewards=rewards_list,
        weights=weights,
    )
