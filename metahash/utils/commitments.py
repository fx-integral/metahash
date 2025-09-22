# ====================================================================== #
# metahash/utils/commitments.py
# Compact helpers to store & fetch JSON commitments on-chain
# using an initialized AsyncSubtensor and the caller's Wallet.
# Plain commitments only (no timelock reveal) to keep things simple.
# ====================================================================== #

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from bittensor import AsyncSubtensor
from bittensor_wallet import Wallet

# Keep on-chain payloads small (a few KB). Raise if exceeded.
MAX_COMMIT_BYTES = 16 * 1024  # 16 KiB


# ───────────────────────────── serialization ───────────────────────────── #

def _json_dump_compact(data: Any) -> str:
    """Serialize to compact JSON and enforce byte-size guard."""
    s = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    b = s.encode("utf-8")
    if len(b) > MAX_COMMIT_BYTES:
        raise ValueError(
            f"JSON payload too large: {len(b)} bytes (limit {MAX_COMMIT_BYTES}). "
            "Store less history or compress further."
        )
    return s


def _maybe_json_load(s: Optional[str]) -> Any:
    """Parse JSON if possible, else return raw string/None."""
    if s is None:
        return None
    try:
        return json.loads(s)
    except Exception:
        return s


# ─────────────────────── uid / hotkey resolution helpers ────────────────── #

async def uid_for_hotkey(
    st: AsyncSubtensor,
    *,
    hotkey_ss58: str,
    netuid: int,
) -> Optional[int]:
    """Resolve UID for a hotkey on a subnet, or None if not registered."""
    return await st.get_uid_for_hotkey_on_subnet(hotkey_ss58, netuid)


async def hotkey_for_uid(
    st: AsyncSubtensor,
    *,
    uid: int,
    netuid: int,
    block: Optional[int] = None,
) -> Optional[str]:
    """
    Resolve hotkey SS58 for a UID using metagraph info (fast).
    Returns None if out-of-range or subnet missing.
    """
    meta = await st.get_metagraph_info(netuid, block=block)
    if meta and 0 <= uid < len(meta.hotkeys):
        return meta.hotkeys[uid]
    return None


# ─────────────────────────── plain commitments ─────────────────────────── #

async def write_plain_commitment_json(
    st: AsyncSubtensor,
    *,
    wallet: Wallet,
    data: Any,
    netuid: int,
    period: Optional[int] = None,
) -> bool:
    """
    Write a small JSON blob as your hotkey's current metadata on the subnet.
    Overwrites the previous plain commitment for (hotkey, netuid).
    `period` is the mortal era (in blocks) for inclusion, not a retention TTL.
    """
    payload = _json_dump_compact(data)
    return await st.commit(wallet=wallet, netuid=netuid, data=payload, period=period)


async def read_plain_commitment(
    st: AsyncSubtensor,
    *,
    netuid: int,
    uid: Optional[int] = None,
    hotkey_ss58: Optional[str] = None,
    block: Optional[int] = None,
) -> Any:
    """
    Read the latest plain commitment for (uid OR hotkey) on a subnet.
    Returns parsed JSON when possible, else the raw string/None.
    """
    if uid is None and not hotkey_ss58:
        raise ValueError("Provide either `uid` or `hotkey_ss58`")

    if uid is None:
        uid = await st.get_uid_for_hotkey_on_subnet(hotkey_ss58, netuid)  # type: ignore[arg-type]
        if uid is None:
            return None

    raw: str = await st.get_commitment(netuid=netuid, uid=uid, block=block)
    return _maybe_json_load(raw)


async def read_all_plain_commitments(
    st: AsyncSubtensor,
    *,
    netuid: int,
    block: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Read *all* hotkeys' plain commitments on a subnet at (optional) block.
    Returns { hotkey_ss58: parsed_json_or_raw_str }.
    """
    commits = await st.get_all_commitments(
        netuid=netuid, block=block, reuse_block=False
    )
    return {hk: _maybe_json_load(v) for hk, v in commits.items()}


# ───────────────────────── convenience wrappers ────────────────────────── #

async def upsert_my_plain_json(
    st: AsyncSubtensor,
    *,
    wallet: Wallet,
    netuid: int,
    payload: Any,
    period: Optional[int] = None,
) -> bool:
    """
    Upsert your hotkey's current plain commitment with the given JSON payload.
    """
    return await write_plain_commitment_json(
        st, wallet=wallet, data=payload, netuid=netuid, period=period
    )


async def read_my_plain_json(
    st: AsyncSubtensor,
    *,
    wallet: Wallet,
    netuid: int,
    block: Optional[int] = None,
) -> Any:
    """Read your hotkey’s current plain commitment as parsed JSON."""
    uid = await st.get_uid_for_hotkey_on_subnet(wallet.hotkey.ss58_address, netuid)
    if uid is None:
        return None
    return await read_plain_commitment(st, netuid=netuid, uid=uid, block=block)
