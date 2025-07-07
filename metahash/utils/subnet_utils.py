# ====================================================================== #
# metahash/utils/subnet_utils.py                            
# ====================================================================== #


from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from statistics import mean
from typing import AsyncGenerator, Optional
from bittensor.core.metagraph import AsyncMetagraph
from bittensor import AsyncSubtensor
from bittensor.utils.balance import Balance
import time
from typing import List
import random
import bittensor as bt
from substrateinterface.exceptions import SubstrateRequestException


# ── project‑specific constants (keep or replace) ────────────────────────────
from metahash.config import (
    DEFAULT_BITTENSOR_NETWORK,   # e.g. "finney"
    DEFAULT_NETUID,       # default netuid for your project
    DECIMALS,          # 10**9 for TAO
    SAMPLE_POINTS)
# ─────────────────────────────────────────────────────────────────────────────


# ╭────────────────────────────── context helpers ───────────────────────────╮


@asynccontextmanager
async def _auto_subtensor(network=DEFAULT_BITTENSOR_NETWORK) -> AsyncGenerator[AsyncSubtensor, None]:
    """Create, initialise, yield, and tear‑down a subtensor client."""
    st = AsyncSubtensor(network=network)   # sync ctor
    await st.initialize()                  # async handshake
    try:
        yield st
    finally:
        await st.close()


@asynccontextmanager
async def _with_subtensor(
    st: Optional[AsyncSubtensor],
) -> AsyncGenerator[AsyncSubtensor, None]:
    """Yield the supplied client (if any) or manage one automatically."""
    if st is None:
        async with _auto_subtensor() as temp:
            yield temp
    else:
        yield st


# ╭───────────────────────────── public helpers ──────────────────────────────╮
async def subnet_info(
    netuid: int = DEFAULT_NETUID,
    *,
    st: AsyncSubtensor | None = None,
    block: int | None = None,
):
    """Return the ``DynamicInfo`` object for *netuid* at *block* (or head)."""
    async with _with_subtensor(st) as sub:
        return await sub.subnet(netuid, block=block)


async def current_epoch(netuid: int = DEFAULT_NETUID, *, st=None):
    """Snapshot of where we are in the current epoch."""
    async with _with_subtensor(st) as sub:
        tempo = await sub.tempo(netuid)
        head = await sub.get_current_block()
        return {
            "epoch_index": head // tempo if tempo else None,
            "blocks_into_epoch": head % tempo if tempo else None,
            "tempo": tempo,
            "chain_block": head,
            "utc_time": datetime.now(tz=timezone.utc),
        }


async def subnet_price(netuid: int = DEFAULT_NETUID, *, st=None) -> Balance:
    """TAO price of 1 α‑share in the subnet’s liquidity pool."""
    async with _with_subtensor(st) as sub:
        info = await sub.subnet(netuid)
        return info.price


async def liquidity_and_slippage(netuid: int = DEFAULT_NETUID, tao_in=1, *, st=None):
    """Return pool sizes and simple slippage estimate for a buy of *tao_in* TAO."""
    async with _with_subtensor(st) as sub:
        info = await sub.subnet(netuid)
        tao_pool = info.tao_in
        alpha_pool = info.alpha_in
        price = info.price

        tao_in_rao = int(tao_in * DECIMALS)
        new_price = Balance.from_rao(
            (tao_pool.rao + tao_in_rao) * DECIMALS // (alpha_pool.rao or 1)
        )
        slip = new_price.tao / price.tao - 1

        return {"tao_pool": tao_pool, "alpha_pool": alpha_pool,
                "price": price, "slippage": slip}


async def average_price(
    netuid: int,
    start_block: int,
    end_block: int,
    *,
    st=None,
    sample: int = SAMPLE_POINTS,
    concurrent: int = 32,
    even: bool = True,
) -> Balance:
    """
    Arithmetic mean TAO price sampled from *sample* blocks in the
    closed interval [start_block, end_block].

    Parameters
    ----------
    netuid        : subnet identifier.
    start_block   : first block (inclusive).
    end_block     : last block  (inclusive).
    st            : existing Subtensor connection (None → open one temporarily).
    sample        : number of blocks to sample (defaults to SAMPLE_POINTS).
    concurrent    : maximum number of simultaneous RPC calls.
    even          : if True sample evenly spaced, else sample uniformly at random.
    """
    if end_block < start_block:
        raise ValueError("end_block must be ≥ start_block")

    total = end_block - start_block + 1
    sample = max(1, min(sample, total))          # clamp

    # ───────────────────────────────────────────────────────── sample block numbers
    if sample == total:                          # small range → just take them all
        block_sample: List[int] = list(range(start_block, end_block + 1))
    elif even:                                   # deterministic, evenly spaced
        step = (total - 1) / (sample - 1)
        block_sample = [round(start_block + i * step) for i in range(sample)]
    else:                                        # random (but reproducible if you seed random)
        block_sample = random.sample(range(start_block, end_block + 1), sample)
        block_sample.sort()

    async def _price_at(sub, blk: int) -> int:
        """Fetch price at a single block, with graceful fallback."""
        try:
            return (await sub.subnet(netuid, block=blk)).price.rao
        except Exception:
            price_rao = await sub.query_runtime_api(
                "StakeInfoRuntimeApi",
                "get_subnet_price_at",
                params=[netuid, blk],
            )
            return int(price_rao)

    prices: List[int] = []
    async with _with_subtensor(st) as sub:
        # ───────────────────────────── batched concurrency ──────────────────────
        for i in range(0, len(block_sample), concurrent):
            batch = block_sample[i : i + concurrent]
            prices.extend(await asyncio.gather(*(_price_at(sub, b) for b in batch)))

    return Balance.from_rao(int(mean(prices))) if prices else Balance.tao(0)


# Choose ONE of the two depending on the policy you want:
# 1)  No slippage ever
FALLBACK_DEPTH_RAO: int = 10 ** 30            # ~1e21 TAO
# 2)  Max ~3 % slippage
# FALLBACK_DEPTH_RAO: int = 2 ** 200          # ~1.6e60 planck

_warned = False


async def average_depth(
    netuid: int,
    start_block: int,
    end_block: int,
    *,
    st: Optional[AsyncSubtensor] = None,
    sample: int = SAMPLE_POINTS,
    concurrent: int = 32,
    even: bool = True,
) -> int:
    """
    Arithmetic mean α‑stake depth of *netuid* sampled from *sample* blocks
    in the closed interval ``[start_block, end_block]``.

    Parameters
    ----------
    netuid        : Subnet identifier.
    start_block   : First block (inclusive).
    end_block     : Last block  (inclusive).
    st            : Existing ``AsyncSubtensor`` connection
                    (``None`` → open one temporarily).
    sample        : Number of blocks to sample (default ``SLIP_SAMPLE_POINTS``).
    concurrent    : Maximum simultaneous RPC calls.
    even          : If *True* sample evenly spaced, else uniformly at random.

    Returns
    -------
    int
        Depth in **planck** (raw α).
    """
    if end_block < start_block:
        raise ValueError("end_block must be ≥ start_block")

    total = end_block - start_block + 1
    sample = max(1, min(sample, total))

    # ───────────────────────────── block sampling ─────────────────────────── #
    if sample == total:                             # small range → take all
        block_sample: List[int] = list(range(start_block, end_block + 1))
    elif even:
        step = (total - 1) / (sample - 1)
        block_sample = [round(start_block + i * step) for i in range(sample)]
    else:                                           # reproducible random sample
        block_sample = random.sample(range(start_block, end_block + 1), sample)
        block_sample.sort()

    async def _depth_at(sub: AsyncSubtensor, blk: int) -> int:
        """
        Return α-depth in planck for *netuid* at block *blk*.

        • Normal path: pallet storage         (always exists)
        • On any error: emit one CRITICAL log and return FALLBACK_DEPTH_RAO
        """
        global _warned
        try:
            bh = await sub.substrate.get_block_hash(block_id=blk)
            storage = await sub.substrate.query(
                "SubtensorModule",
                "SubnetAlphaIn",
                [netuid],
                block_hash=bh,
            )
            val = getattr(storage, "value", storage)
            return int(val) if val is not None else 0

        except (SubstrateRequestException, Exception) as err:
            if not _warned:
                bt.logging.critical(
                    f"[depth_at] Failed to read SubnetAlphaIn (netuid={netuid}, "
                    f"block={blk}): {err}\n"
                    f"          Using FALLBACK_DEPTH_RAO = {FALLBACK_DEPTH_RAO:,}. "
                    f"Slippage will be {'disabled' if FALLBACK_DEPTH_RAO==10**30 else 'capped at ≈3%'} "
                    f"until restart."
                )
                _warned = True
            return FALLBACK_DEPTH_RAO

    depths: List[int] = []
    async with _with_subtensor(st) as sub:
        for i in range(0, len(block_sample), concurrent):
            batch = block_sample[i : i + concurrent]
            depths.extend(await asyncio.gather(*(_depth_at(sub, b) for b in batch)))

    return int(mean(depths)) if depths else 0
# ╭────────────────────────────── NEW helper ────────────────────────────────╮


async def get_metagraph(
    netuid: int,
    *,
    st: AsyncSubtensor | None = None,
    lite: bool = False,              # need full fields for emission
    block: int | None = None,
) -> AsyncMetagraph:
    """
    Wrapper around ``AsyncSubtensor.metagraph()`` with auto‑managed client.

    Returns a fully‑synced ``AsyncMetagraph`` for *netuid*.
    """
    async with _with_subtensor(st) as sub:
        return await sub.metagraph(netuid, lite=lite, block=block)


# ╭──────────────────────────────── quick demo ──────────────────────────────╮
if __name__ == "__main__":          # pragma: no cover
    async def _demo():
        print(await current_epoch())
        print(await subnet_price())
        print(await liquidity_and_slippage(tao_in=5))

    asyncio.run(_demo())
