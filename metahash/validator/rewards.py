# ╭────────────────────────────────────────────────────────────────────────╮
# metahash/validator/rewards.py            Epoch reward‑calculation logic
# Patched 2025‑06‑29 – cumulative fixes:
#   • Decimal/float type clash in _apply_slippage
#   • Safe UID handling for unknown miners
#   • Flexible SubtensorAvgPoolDepth constructor
#   • Surplus always burned (UID 0)
#   • NEW (2025‑06‑29): filter‑out α deposits originating from forbidden
#     subnets (e.g. the SN‑73 issuance subnet) via FORBIDDEN_ALPHA_SUBNETS
# ╰────────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import logging
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
    runtime_checkable,
)

import bittensor as bt
from metahash.config import (
    K_SLIP,
    SLIP_TOLERANCE,
    SLIP_SAMPLE_POINTS,
    FORBIDDEN_ALPHA_SUBNETS,  # ← NEW
)
from metahash.utils.async_substrate import maybe_async

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


# ──────────────────────────── DEPTH PROVIDER  ──────────────────────────── #


class SubtensorAvgPoolDepth:
    """
    Lazily computes – and caches – the average α‑stake depth of a subnet
    across a configurable set of probe blocks.
    """

    def __init__(
        self,
        subtensor: bt.Subtensor | bt.AsyncSubtensor,
        probe_blocks: Sequence[int] | None = None,
        *,
        start_block: int | None = None,
        end_block: int | None = None,
        samples: int = 32,
    ) -> None:
        """
        One of:
        • provide **probe_blocks** explicitly, or
        • provide **start_block, end_block, samples** and the constructor
          will sample `samples` blocks uniformly across the range
          [start_block, end_block] (inclusive).
        """
        # Argument validation & automatic probe list
        if probe_blocks is None:
            if start_block is None or end_block is None:
                raise ValueError(
                    "Either ‘probe_blocks’ or the trio "
                    "(start_block, end_block, samples) must be provided"
                )
            if end_block < start_block:
                raise ValueError("end_block must be ≥ start_block")

            span = end_block - start_block
            step = max(1, span // max(1, samples - 1))
            probe_blocks = range(end_block, start_block - 1, -step)[:samples]

        self._probe_blocks: List[int] = sorted(set(probe_blocks))
        self._subtensor = subtensor
        self._cache: Dict[int, int] = {}

    # ------------------------------------------------------------------ #
    async def __call__(self, subnet_id: int) -> int:
        """
        Average pool depth for *subnet_id* at the probe blocks.
        Result is memoised per‑epoch / per‑subnet.
        """
        if subnet_id in self._cache:
            return self._cache[subnet_id]

        depths: List[int] = []

        async def _probe(bn: int) -> None:
            try:
                block_hash = await maybe_async(
                    self._subtensor.substrate.get_block_hash, block_id=bn
                )
                storage = await maybe_async(
                    self._subtensor.substrate.query,
                    "SubtensorModule",
                    "SubnetAlphaIn",
                    [subnet_id],
                    block_hash=block_hash,
                )
                value = getattr(storage, "value", storage)
                if value is not None:
                    depths.append(int(value))
            except Exception as err:
                logging.debug(
                    f"[depth] subnet={subnet_id} block={bn} failed: {err!r}"
                )

        await asyncio.gather(*(_probe(b) for b in self._probe_blocks))

        avg = int(sum(depths) / len(depths)) if depths else 0
        self._cache[subnet_id] = avg
        return avg


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
    scanner: TransferScanner,
    pricing: PricingProvider,
    uid_of_coldkey: MinerResolver,
    start_block: int,
    end_block: int,
    bag_sn73: int,
    c0: float,
    beta: float,
    r_min: float,
    pool_depth_of: PoolDepthProvider | None = None,
    depth_samples: int = SLIP_SAMPLE_POINTS,
    log: Callable[[str], None] | None = None,
) -> EpochRewards:
    # ------------------------------------------------------------------ #
    miner_uids = list(validator.get_miner_uids())
    metagraph_size = len(miner_uids)

    # 1. SCAN
    bt.logging.info(
        f"Starting Transfers Scan from block {start_block} to {end_block}"
    )
    raw = await scan_transfers(
        scanner=scanner,
        from_block=start_block,
        to_block=end_block,
    )

    # 2. CAST (now with forbidden subnet filter)
    bt.logging.info("Casting events…")
    deposits = cast_events(raw)

    # 3. RESOLVE MINERS
    bt.logging.info("Resolving miner UIDs…")
    await resolve_miners(deposits, uid_of_coldkey=uid_of_coldkey)

    # 4. COMBINE
    bt.logging.info("Combining deposits per miner/subnet…")
    deposits = _combine_deposits_by_miner_subnet(deposits)

    # 5. PRICE
    bt.logging.info("Attaching prices…")
    await attach_prices(
        deposits,
        pricing=pricing,
        epoch_start=start_block,
        epoch_end=end_block,
    )

    # 6. SLIPPAGE
    bt.logging.info("Building pool‑depth provider (average of previous epoch)…")
    if pool_depth_of is None:
        epoch_len = end_block - start_block + 1
        prev_start = max(0, start_block - epoch_len)
        prev_end = start_block - 1

        pool_depth_of = SubtensorAvgPoolDepth(
            validator._async_subtensor,
            start_block=prev_start,
            end_block=prev_end,
            samples=depth_samples,
        )

    bt.logging.info("Applying slippage with averaged depths…")
    await apply_slippage(deposits, pool_depth_of=pool_depth_of)

    # 7. BOND CURVE
    bt.logging.info("Applying bond curve…")
    allocate_bond_curve(
        deposits=deposits,
        bag_sn73=bag_sn73,
        c0=c0,
        beta=beta,
        r_min=r_min,
    )

    # 8. AGGREGATION
    rewards_per_miner = aggregate_miner_rewards(deposits)

    # burn any surplus
    bag = Decimal(str(bag_sn73))
    surplus = bag - sum(rewards_per_miner.values())
    if surplus > Decimal(0):
        rewards_per_miner[0] = rewards_per_miner.get(0, Decimal(0)) + surplus

    # 9. NORMALISATION
    weights = build_weights(
        rewards=rewards_per_miner,
        miner_uids=miner_uids,
        burn_uid=0,
    )

    rewards_list = [
        rewards_per_miner.get(uid, Decimal(0)) for uid in miner_uids
    ]

    issued = float(sum(rewards_per_miner.values()))
    msg = (
        f"✓ Epoch @{start_block}-{end_block}: "
        f"{metagraph_size} miners accounted, "
        f"{issued:.2f}/{bag_sn73} SN‑73 issued, "
        f"last deposit: {deposits[-1] if deposits else 'n/a'}"
    )
    (log or logging.info)(msg)

    return EpochRewards(
        deposits=deposits,
        rewards_per_miner=rewards_per_miner,
        miner_uids=miner_uids,
        rewards=rewards_list,
        weights=weights,
    )
