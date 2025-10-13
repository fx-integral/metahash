# metahash/finance/treasury_chain.py
# --------------------------------------------------------------------------- #
# Asynchronous helpers for interacting with the Bittensor archive node.
# This module keeps all chain I/O in one place so higher level code can focus
# on pure calculations that are easy to unit test.
# --------------------------------------------------------------------------- #

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Iterable, List, Optional, Sequence, Set

import bittensor as bt
from bittensor.core.chain_data.delegate_info import DelegatedInfo
from bittensor.utils.balance import Balance

from metahash.utils.commitments import read_plain_commitment, uid_for_hotkey

from .constants import UNITS
from .models import CommitmentSnapshot, HoldingsSnapshot


# --------------------------------------------------------------------------- #
# Subtensor lifecycle helpers
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def open_subtensor(network: str) -> bt.AsyncSubtensor:
    """Async context manager that yields an initialised AsyncSubtensor."""
    subtensor = bt.AsyncSubtensor(network=network)
    await subtensor.initialize()
    try:
        yield subtensor
    finally:
        await subtensor.close()


async def get_current_block(subtensor: bt.AsyncSubtensor) -> int:
    return int(await subtensor.get_current_block())


async def get_tempo(subtensor: bt.AsyncSubtensor, netuid: int) -> int:
    tempo = await subtensor.tempo(netuid)
    return int(tempo or 1)


# --------------------------------------------------------------------------- #
# Delegated stake snapshots
# --------------------------------------------------------------------------- #

def _delegated_to_snapshot(
    delegated: Sequence[DelegatedInfo],
    *,
    block: int,
) -> HoldingsSnapshot:
    per_subnet: Dict[int, float] = {}
    for info in delegated:
        if info is None:
            continue
        amount_alpha = float(info.stake.rao) * UNITS.alpha_from_rao
        per_subnet[int(info.netuid)] = per_subnet.get(int(info.netuid), 0.0) + amount_alpha
    return HoldingsSnapshot(block=block, per_subnet_alpha=per_subnet)


async def fetch_holdings_snapshot(
    subtensor: bt.AsyncSubtensor,
    *,
    coldkeys: Iterable[str],
    block: Optional[int] = None,
    concurrent: int = 4,
) -> HoldingsSnapshot:
    """
    Aggregate delegated stake for the treasury coldkeys at a given block.

    Returns:
        HoldingsSnapshot with alpha per subnet summed across all coldkeys.
    """
    coldkey_list = list(dict.fromkeys(addr for addr in coldkeys if addr))
    block_query = block if block is not None else await get_current_block(subtensor)

    if not coldkey_list:
        return HoldingsSnapshot(block=block_query)

    sem = asyncio.Semaphore(max(1, concurrent))

    async def _fetch_for_coldkey(addr: str) -> Sequence[DelegatedInfo]:
        async with sem:
            try:
                return await subtensor.get_delegated(addr, block=block_query)
            except Exception as exc:  # pragma: no cover - network errors logged upstream
                bt.logging.warning(f"[treasury-chain] Failed to fetch delegated stake for {addr}: {exc}")
                return []

    delegated_lists = await asyncio.gather(*(_fetch_for_coldkey(addr) for addr in coldkey_list))

    snapshot = HoldingsSnapshot(block=block_query)
    for delegated in delegated_lists:
        for info in delegated:
            if info is None:
                continue
            net = int(info.netuid)
            amount_alpha = float(info.stake.rao) * UNITS.alpha_from_rao
            snapshot.add_alpha(net, amount_alpha)

    return snapshot


# --------------------------------------------------------------------------- #
# Subnet pricing
# --------------------------------------------------------------------------- #

async def fetch_prices(
    subtensor: bt.AsyncSubtensor,
    subnet_ids: Iterable[int],
    *,
    block: Optional[int] = None,
) -> Dict[int, float]:
    """
    Retrieve TAO/α prices for the requested subnets.

    Returns:
        Mapping netuid -> price (float, TAO per alpha unit).
    """
    prices: Dict[int, float] = {}
    seen: Set[int] = set()
    for net in subnet_ids:
        sid = int(net)
        if sid in seen:
            continue
        seen.add(sid)
        try:
            info = await subtensor.subnet(sid, block=block)
            if info and isinstance(info.price, Balance):
                prices[sid] = float(info.price.tao)
            else:
                prices[sid] = 0.0
        except Exception as exc:  # pragma: no cover
            bt.logging.warning(f"[treasury-chain] Failed to fetch price for subnet {sid}: {exc}")
            prices[sid] = 0.0
    return prices


# --------------------------------------------------------------------------- #
# Commitment history
# --------------------------------------------------------------------------- #

async def fetch_commitment_history(
    subtensor: bt.AsyncSubtensor,
    *,
    netuid: int,
    hotkeys: Iterable[str],
    start_block: int,
    end_block: int,
    tempo: int,
    max_epochs: Optional[int] = None,
) -> List[CommitmentSnapshot]:
    """
    Extract commitment snapshots for the validator hotkeys across a block range.

    The function walks the chain in tempo-sized strides and records the first
    commitment encountered for each epoch inside the window.
    """
    tempo = max(1, int(tempo))
    start_block = max(0, int(start_block))
    end_block = max(start_block, int(end_block))

    snapshots: List[CommitmentSnapshot] = []
    seen_epochs: Set[int] = set()

    hotkey_list = [hk for hk in dict.fromkeys(hotkeys) if hk]
    if not hotkey_list:
        return []

    # Resolve UIDs once to avoid repeated RPCs.
    uid_map: Dict[str, Optional[int]] = {}
    for hk in hotkey_list:
        try:
            uid_map[hk] = await uid_for_hotkey(subtensor, hotkey_ss58=hk, netuid=netuid)
        except Exception as exc:  # pragma: no cover
            bt.logging.warning(f"[treasury-chain] Failed to resolve UID for {hk}: {exc}")
            uid_map[hk] = None

    for hk in hotkey_list:
        uid = uid_map.get(hk)
        if uid is None:
            continue

        block = start_block
        while block <= end_block:
            if max_epochs is not None and len(seen_epochs) >= max_epochs:
                break

            try:
                payload = await read_plain_commitment(
                    subtensor,
                    netuid=netuid,
                    uid=uid,
                    block=block,
                )
            except Exception as exc:  # pragma: no cover
                bt.logging.debug(f"[treasury-chain] Commitment read failed at block {block}: {exc}")
                block += tempo
                continue

            if isinstance(payload, dict):
                epoch = int(payload.get("e", -1))
                if epoch >= 0 and epoch not in seen_epochs:
                    snapshot = CommitmentSnapshot.from_payload(payload, block_sampled=block)
                    if snapshot:
                        snapshots.append(snapshot)
                        seen_epochs.add(epoch)

            block += tempo

    snapshots.sort(key=lambda snap: snap.epoch)
    return snapshots

