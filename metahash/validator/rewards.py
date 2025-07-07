# ╭────────────────────────────────────────────────────────────────────────╮
# metahash/validator/rewards.py        Epoch reward‑calculation logic      #
#                                                                           #
# Patched 2025‑07‑07                                                        #
#   • Imports the enhanced `TransferEvent` that now carries `src_subnet_id` #
#     (defined in metahash.validator.alpha_transfers).                      #
#   • All reward‑calculation semantics are unchanged; cross‑net events      #
#     have already been stripped out by the scanner.                        #
# ╰────────────────────────────────────────────────────────────────────────╯
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
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

# ↓ NEW – use the single authoritative definition
from metahash.validator.alpha_transfers import TransferEvent

# ───────────────────────────── GLOBAL CONSTANTS ───────────────────────── #

getcontext().prec = 60                          # 60‑digit arithmetic precision

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
