# ====================================================================== #
#  metahash/validator/alpha_transfers.py                                 #
#  Patched 2025‑07‑07 – close cross‑subnet α‑swap loophole               #
#                           • track   src_netuid  via companion events   #
#                           • discard src_netuid ≠ dest_netuid transfers #
#                           • keep API 100 % backward‑compatible         #
# ====================================================================== #
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import bittensor as bt
import websockets                                           # WS errors
from substrateinterface.utils.ss58 import (
    ss58_decode as _ss58_decode_generic,
    ss58_encode as _ss58_encode_generic,
)

from metahash.config import MAX_CONCURRENCY
from metahash.utils.async_substrate import maybe_async

# ── constants ─────────────────────────────────────────────────────────── #
MAX_CHUNK_DEFAULT = 1_000
LOG_EVERY = 50
DUMP_LAST = 5

# ── dataclasses ───────────────────────────────────────────────────────── #


@dataclass(slots=True, frozen=True)
class TransferEvent:
    """
    Container for a single α‑stake transfer.

    • **subnet_id**      – destination netuid (unchanged public API)
    • **src_subnet_id**  – origin netuid            (new, may be None)
    • **from_uid / to_uid** – UIDs in the *destination* subnet
    • **amount_rao**     – α amount in planck
    • **src_coldkey / dest_coldkey** – 32 byte SS58
    """
    block: int
    from_uid: int
    to_uid: int

    subnet_id: int             # ← destination (kept for API compatibility)
    amount_rao: int

    src_coldkey: Optional[str]
    dest_coldkey: Optional[str]
    src_coldkey_raw: Optional[bytes]
    dest_coldkey_raw: Optional[bytes]

    # NEW – origin netuid (None if companion StakeRemoved is missing)
    src_subnet_id: Optional[int] = None


# ── SS58 helpers (unchanged) ──────────────────────────────────────────── #

def _encode_ss58(raw: bytes, fmt: int) -> str:                # noqa: D401
    try:
        return _ss58_encode_generic(raw, fmt)
    except TypeError:
        return _ss58_encode_generic(raw, address_type=fmt)


def _decode_ss58(addr: str) -> bytes:                         # noqa: D401
    try:
        return _ss58_decode_generic(addr)
    except TypeError:
        return _ss58_decode_generic(addr, valid_ss58_format=True)


def _account_id(obj) -> bytes | None:                         # noqa: ANN001,D401
    if isinstance(obj, (bytes, bytearray)) and len(obj) == 32:
        return bytes(obj)
    if isinstance(obj, (list, tuple)):
        if len(obj) == 32 and all(isinstance(x, int) for x in obj):
            return bytes(obj)
        if len(obj) == 1:
            return _account_id(obj[0])
    if isinstance(obj, dict):
        inner = (
            obj.get("Id")
            or obj.get("AccountId")
            or next(iter(obj.values()), None)
        )
        return _account_id(inner)
    return None


# ── generic event accessors (unchanged) ───────────────────────────────── #

def _event_name(ev) -> str:                                   # noqa: ANN001
    ev = ev.get("event", ev) if isinstance(ev, dict) else getattr(ev, "event", ev)
    if hasattr(ev, "method"):
        return str(ev.method)
    if isinstance(ev, dict):
        return str(ev.get("event_id") or ev.get("name", "<unknown>"))
    return "<unknown>"


def _event_fields(ev) -> Sequence:                            # noqa: ANN001
    ev = ev.get("event", ev) if isinstance(ev, dict) else getattr(ev, "event", ev)
    if isinstance(ev, dict):
        return ev.get("attributes") or ev.get("params") or ev.get("data") or ()
    return (
        getattr(ev, "attributes", ())
        or getattr(ev, "params", ())
        or getattr(ev, "data", ())
        or ()
    )


def _f(params, idx, default=None):                            # noqa: ANN001
    try:
        val = params[idx]
        return val["value"] if isinstance(val, dict) and "value" in val else val
    except (IndexError, TypeError):
        return default


def _mask(ck: Optional[str]) -> str:
    return ck if ck is None or len(ck) < 10 else f"{ck[:4]}…{ck[-4:]}"


# ── parser helpers ────────────────────────────────────────────────────── #

def _parse_stake_transferred(
    params, fmt: int
) -> TransferEvent:                                           # noqa: ANN001
    """
    Parse StakeTransferred event parameters (chain v9 & v10).

    Expected layout (v9+):
        0  from_coldkey
        1  dest_coldkey           (treasury)
        2  hotkey_staked_to
        3  dest_netuid            ←── our subnet_id
        4  to_uid
        5  amount (TAO)           ←── patched later with α

    **origin netuid is *not* included** in this event and is picked
    up from the companion StakeRemoved emitted in the same extrinsic.
    """
    from_coldkey_raw = _account_id(_f(params, 0))
    dest_coldkey_raw = _account_id(_f(params, 1))

    subnet_id = int(_f(params, 3, -1))            # destination netuid
    to_uid = int(_f(params, 4, -1))

    return TransferEvent(
        block=-1,
        from_uid=-1,                               # origin UID not provided
        to_uid=to_uid,
        subnet_id=subnet_id,                       # = dest netuid
        amount_rao=int(_f(params, 5, 0)),          # placeholder, fixed later
        src_coldkey=_encode_ss58(from_coldkey_raw, fmt),
        dest_coldkey=_encode_ss58(dest_coldkey_raw, fmt),
        src_coldkey_raw=from_coldkey_raw,
        dest_coldkey_raw=dest_coldkey_raw,
    )


# ── NEW: robust helpers for Add / Remove events (v9+) ─────────────────── #

def _amount_from_stake_removed(params) -> int:
    """
    v9+ StakeRemoved layout (6 fields):
        0  src_coldkey
        1  hotkey
        2  from_uid
        3  amount_rao          ← **this**
        4  src_netuid          ← paired with TransferEvent
        5  ...
    Older networks (≤ v8) used index 2 for the amount.  We keep the fallback.
    """
    return int(_f(params, 3, _f(params, 2, 0)))


def _amount_from_stake_added(params) -> int:                   # noqa: ANN001
    """
    v9+ StakeAdded layout (6 fields):
        0  dest_coldkey        (treasury)
        1  hotkey
        2  to_uid
        3  amount_rao          ← **this**
        4  dest_netuid
        5  ...
    ≤ v8 fallback keeps index 2.
    """
    return int(_f(params, 3, _f(params, 2, 0)))


def _subnet_from_stake_removed(params) -> int:
    """
    v9+ StakeRemoved: subnet is field 4.
    ≤ v8 (legacy) keeps field 1.
    """
    return int(_f(params, 4, _f(params, 1, -1)))


def _subnet_from_stake_added(params) -> int:
    """
    v9+ StakeAdded: subnet is field 4.
    """
    return int(_f(params, 4, -1))

# ── main scanner class ───────────────────────────────────────────────── #


class AlphaTransfersScanner:
    """Scans a block‑range for α‑stake transfers to one treasury cold‑key."""

    def __init__(
        self,
        subtensor: bt.Subtensor | bt.AsyncSubtensor,
        *,
        dest_coldkey: Optional[str] = None,
        dump_events: bool = False,
        dump_last: int = DUMP_LAST,
        on_progress: Optional[Callable[[int, int, int], None]] = None,
        max_concurrency: int = MAX_CONCURRENCY,
        rpc_lock: Optional[asyncio.Lock] = None,
    ) -> None:
        self.st = subtensor
        self.dest_ck = dest_coldkey
        self.dest_ck_raw = _decode_ss58(dest_coldkey) if dest_coldkey else None
        self.dump_events = dump_events
        self.dump_last = dump_last
        self.on_progress = on_progress
        self.max_conc = max_concurrency
        self.ss58_format = subtensor.substrate.ss58_format
        self._rpc_lock: asyncio.Lock = rpc_lock or asyncio.Lock()

    # ------------------------------------------------------------------ #
    async def _rpc(self, fn, *a, **kw):
        async with self._rpc_lock:
            return await maybe_async(fn, *a, **kw)

    async def _get_events_at(self, bn: int):
        bh = await self._rpc(self.st.substrate.get_block_hash, block_id=bn)
        return await self._rpc(self.st.substrate.get_events, block_hash=bh)

    # ------------------------------------------------------------------ #
    async def scan(self, frm: int, to: int) -> List[TransferEvent]:
        if frm > to:
            return []

        total = to - frm + 1
        bt.logging.info(f"Scanner: frm={frm}  to={to}  ({total} blocks)")

        q: asyncio.Queue[int | None] = asyncio.Queue()
        events_by_block: Dict[int, list[TransferEvent]] = {}
        blk_cnt = ev_cnt = keep_cnt = 0

        async def _flush_progress():
            if self.on_progress:
                self.on_progress(blk_cnt, ev_cnt, keep_cnt)

        # producer ----------------------------------------------------- #
        async def producer():
            for bn in range(frm, to + 1):
                await q.put(bn)
            for _ in range(self.max_conc):
                await q.put(None)

        # worker ------------------------------------------------------- #
        async def worker():
            nonlocal blk_cnt, ev_cnt, keep_cnt
            while True:
                bn = await q.get()
                if bn is None:
                    break
                try:
                    raw_events = await self._get_events_at(bn)
                except websockets.exceptions.WebSocketException as err:
                    bt.logging.warning(f"RPC error at block {bn}: {err}; reconnecting…")
                    async with self._rpc_lock:
                        await self.st.__aexit__(None, None, None)
                        await self.st.initialize()
                    raw_events = await self._get_events_at(bn)

                bucket = events_by_block.setdefault(bn, [])
                seen, kept = self._accumulate(
                    raw_events,
                    bucket,
                    block_hint_single=bn,
                    dump=self.dump_events and bn >= to - self.dump_last + 1,
                )
                ev_cnt += seen
                keep_cnt += kept
                blk_cnt += 1
                if blk_cnt % LOG_EVERY == 0:
                    bt.logging.info(f"… scanned {blk_cnt} / {total} blocks")
                await _flush_progress()

        await asyncio.gather(
            producer(),
            *[asyncio.create_task(worker()) for _ in range(self.max_conc)],
        )

        bt.logging.info(
            f"✓ scan finished: {blk_cnt} blk, {ev_cnt} ev, {keep_cnt} kept"
        )
        await _flush_progress()

        # deterministic order ----------------------------------------- #
        ordered_events: List[TransferEvent] = []
        for bn in sorted(events_by_block.keys()):
            ordered_events.extend(events_by_block[bn])
        return ordered_events

    # ------------------------------------------------------------------ #
    def _accumulate(  # noqa: PLR0915
        self,
        raw_events,
        out: List[TransferEvent],
        *,
        block_hint_single: int,
        dump: bool,
    ) -> Tuple[int, int]:
        """
        Filters one block’s events; mutates *out*; returns (seen, kept).

        • src_netuid is picked from companion StakeRemoved (if present)
        • amount_rao is replaced by α from companion StakeAdded (preferred)
        • Cross‑net transfers (src_netuid ≠ dest_netuid) are **dropped**
        """

        seen = kept = 0
        rem_amount: Dict[int, int] = {}
        add_amount: Dict[int, int] = {}
        rem_netuid: Dict[int, int] = {}
        add_netuid: Dict[int, int] = {}

        # pass‑1: collect companion data ------------------------------ #
        for ev in raw_events:
            x = ev.get("extrinsic_idx")
            if x is None:
                continue
            name = _event_name(ev)
            fields = _event_fields(ev)

            if name == "StakeRemoved":
                rem_amount[x] = _amount_from_stake_removed(fields)
                rem_netuid[x] = _subnet_from_stake_removed(fields)

            elif name == "StakeAdded":
                add_amount[x] = _amount_from_stake_added(fields)
                add_netuid[x] = _subnet_from_stake_added(fields)

        # pass‑2: process StakeTransferred ---------------------------- #
        for ev in raw_events:
            if _event_name(ev) != "StakeTransferred":
                continue
            seen += 1
            fields = _event_fields(ev)

            try:
                te = _parse_stake_transferred(fields, self.ss58_format)
            except Exception as exc:  # pragma: no cover
                bt.logging.debug(f"Malformed event skipped: {exc}")
                continue

            x = ev.get("extrinsic_idx")

            # enrich amount + netuid data from pass‑1
            if x is not None:
                if x in add_amount:
                    te = replace(te, amount_rao=add_amount[x])
                if x in rem_netuid:
                    te = replace(te, src_subnet_id=rem_netuid[x])
                if x in add_netuid and add_netuid[x] != te.subnet_id:
                    # metadata mismatch (should **never** happen) – log & continue
                    bt.logging.warning(
                        f"Inconsistent dest netuid in x={x}: "
                        f"event={te.subnet_id}  add_netuid={add_netuid[x]}"
                    )

            # ── SECURITY GATE – drop cross‑subnet transfers ───────── #
            if te.src_subnet_id is not None and te.src_subnet_id != te.subnet_id:
                if dump:
                    bt.logging.info(
                        f"[blk {block_hint_single}] cross‑net α‑transfer "
                        f"{te.src_subnet_id} ➞ {te.subnet_id}  "
                        f"(amount={te.amount_rao})  **IGNORED**"
                    )
                continue

            te = replace(te, block=block_hint_single)

            matches = (
                te.amount_rao > 0
                and (self.dest_ck is None or te.dest_coldkey == self.dest_ck)
            )
            if matches:
                kept += 1
                out.append(te)

            if dump and matches:
                bt.logging.info(
                    f"[blk {block_hint_single}] StakeTransferred "
                    f"net={te.subnet_id} uid={te.from_uid}->{te.to_uid}  "
                    f"src={_mask(te.src_coldkey)} dest={_mask(te.dest_coldkey)} "
                    f"α={te.amount_rao}  **KEPT**"
                )

        return seen, kept