# ╭────────────────────────────────────────────────────────────────────────╮
# metahash/validator/rewards.py        Epoch reward‑calculation logic
# Patched 2025‑07‑05 – explicit miner_uids, no validator dependency
# ╰────────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import time
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
    Tuple,
    runtime_checkable,
)
import bittensor as bt
from metahash.config import (
    K_SLIP,
    SLIP_TOLERANCE,
    FORBIDDEN_ALPHA_SUBNETS,
)

# ───────────────────────────── GLOBAL CONSTANTS ────────────────────────── #

getcontext().prec = 60                         # 60‑digit arithmetic precision

PLANCK: int = 10**9
DECIMALS: Decimal = Decimal(10) ** 9

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

# ──────────────────────────────── EVENTS ──────────────────────────────── #


@dataclass(slots=True, frozen=True)
class TransferEvent:
    """
    A single α‑stake transfer credited to the treasury.

    • **src_coldkey**  – cold‑key that *paid* the α (miner’s key)
    • **dest_coldkey** – cold‑key that *received* the α (treasury)
    • **subnet_id**    – originating subnet
    • **amount_rao**   – α amount in planck (RAO)
    """
    src_coldkey: str
    dest_coldkey: str
    subnet_id: int
    amount_rao: int

# ──────────────────────────────── MODEL ───────────────────────────────── #


@dataclass(slots=True)
class AlphaDeposit:
    coldkey: str          # *origin* cold‑key (credited miner)
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
# Combine transfers per miner / subnet **before** pricing & slippage.      #
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
                coldkey=ev.src_coldkey,     # reward goes to *origin* miner
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
            continue  # skip unknown miners instead of burning
        if d.tao_value_post_slip is None:
            continue
        by_uid[d.miner_uid] = (
            by_uid.get(d.miner_uid, Decimal(0)) + d.tao_value_post_slip
        )
    return by_uid

# ╭────────────────────────────── PIPELINE ────────────────────────────────╮


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
) -> List[Decimal]:
    """
    Calculate **raw reward amounts** (post‑slippage TAO) for a given
    (partial) auction window.  

    `miner_uids` defines the order of the resulting list; every position `i`
    in the returned array corresponds to `miner_uids[i]`.
    """

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
        start_time = time.time()
        raw = await scan_transfers(
            scanner=scanner,
            from_block=start_block,
            to_block=end_block,
        )
        bt.logging.info(f"Time took to scan transfers: {time.time() - start_time}s")

    # 2. CAST (forbidden subnet filter) ------------------------------------ #
    bt.logging.info("[rewards] Casting events…")
    deposits = cast_events(raw)
    bt.logging.info(f"[rewards] Deposits: {deposits}")

    # 3. RESOLVE MINERS ----------------------------------------------------- #
    bt.logging.info("[rewards] Resolving miner UIDs…")
    await resolve_miners(deposits, uid_of_coldkey=uid_of_coldkey)
    bt.logging.info(f"[rewards] Deposits with miners: {deposits}")

    # 4. COMBINE ------------------------------------------------------------ #
    bt.logging.info("[rewards] Combining deposits per miner/subnet…")
    deposits = _combine_deposits_by_miner_subnet(deposits)
    bt.logging.info(f"[rewards] Combined deposits: {deposits}")

    # 5. PRICE -------------------------------------------------------------- #
    bt.logging.info("[rewards] Attaching prices…")
    await attach_prices(
        deposits,
        pricing=pricing,
        epoch_start=start_block,
        epoch_end=end_block,
    )
    bt.logging.info(f"[rewards] Deposits with prices attached: {deposits}")

    # 6. SLIPPAGE ----------------------------------------------------------- #
    bt.logging.info("[rewards] Applying slippage with averaged depths…")
    await apply_slippage(deposits, pool_depth_of=pool_depth_of)
    bt.logging.info(f"[rewards] Deposits with slippages attached: {deposits}")

    # 7. AGGREGATION -------------------------------------------------------- #
    rewards_per_miner = _aggregate_post_slip_tao(deposits)
    bt.logging.info(f"[rewards] Rewards: {rewards_per_miner}")

    # 8. BUILD REWARD ARRAY ------------------------------------------------- #
    rewards_list: List[Decimal] = [
        rewards_per_miner.get(uid, Decimal(0)) for uid in miner_uids
    ]
    issued = float(sum(rewards_list))

    bt.logging.info(f"[rewards] Total Tao Received: {issued}")

    return rewards_list
