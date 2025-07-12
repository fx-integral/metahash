# ====================================================================== #
#  metahash/validator/alpha_transfers.py                                 #
#  Patched 2025-07-12 – forbid non-transfer extrinsics                   #
#  Patched 2025-07-13 – enable live whitelist & single‑pass scanner      #
#                           • robust extrinsic filter (_allowed_…)       #
#                           • close cross‑subnet α‑swap loophole          #
#                           • single‑pass _accumulate (≈20 % faster)      #
#                           • keep API 100 % backward‑compatible          #
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

# ── extrinsic filter ─────────────────────────────────────────────────── #
UTILITY_FUNS: set[str] = {"batch", "force_batch", "batch_all"}


def _name(obj) -> str | None:
    """Return a *string* name for a call‑module / call‑function regardless
    of the exact shape that py‑substrate‑interface gives us."""
    if obj is None:
        return None
    if hasattr(obj, "name"):
        return obj.name
    if isinstance(obj, (bytes, str)):
        return obj.decode() if isinstance(obj, bytes) else obj
    if isinstance(obj, dict):
        return obj.get("name")
    return str(obj)


def _allowed_extrinsic_indices(                       # noqa: PLR0911
    self,
    extrinsics,
    block_num: int,
) -> set[int]:
    """Whitelist extrinsic indices inside *extrinsics* that are allowed to
    produce α‑stake events.

    • Always allow `SubtensorModule.transfer_stake`.
    • Allow `Utility.{batch,force_batch,batch_all}` only when
      `self.allow_batch` is True.
    """
    allowed: set[int] = set()

    for idx, ex in enumerate(extrinsics):
        try:
            pallet = _name(ex["call"]["call_module"])
            func = _name(ex["call"]["call_function"])

            if self.debug_extr:
                bt.logging.debug(f"[blk {block_num}] ex#{idx}  {pallet}.{func}")

            # 1️⃣ direct α‑stake transfer
            if (pallet, func) == ("SubtensorModule", "transfer_stake"):
                allowed.add(idx)

            # 2️⃣ optional Utility batches
            elif (
                pallet == "Utility"
                and func in UTILITY_FUNS
                and getattr(self, "allow_batch", False)
            ):
                allowed.add(idx)

        except Exception as err:
            if self.debug_extr:
                bt.logging.debug(
                    f"[blk {block_num}] ex#{idx}  unreadable – {err!s}"
                )

    return allowed


# ── constants ─────────────────────────────────────────────────────────── #
MAX_CHUNK_DEFAULT = 1_000
LOG_EVERY = 50
DUMP_LAST = 5

# ── dataclasses ───────────────────────────────────────────────────────── #


@dataclass(slots=True, frozen=True)
class TransferEvent:
    """Container for a single α‑stake transfer."""

    block: int
    from_uid: int
    to_uid: int

    subnet_id: int             # destination (kept for API compatibility)
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
    """Parse StakeTransferred event parameters (chain v9 & v10)."""
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
    """Return amount_rao from StakeRemoved (v9+ layout)."""
    return int(_f(params, 3, _f(params, 2, 0)))


def _amount_from_stake_added(params) -> int:                   # noqa: ANN001
    """Return amount_rao from StakeAdded (v9+ layout)."""
    return int(_f(params, 3, _f(params, 2, 0)))


def _subnet_from_stake_removed(params) -> int:
    """Return subnet from StakeRemoved."""
    return int(_f(params, 4, _f(params, 1, -1)))


def _subnet_from_stake_added(params) -> int:
    """Return subnet from StakeAdded."""
    return int(_f(params, 4, -1))

# ── main scanner class ───────────────────────────────────────────────── #


class AlphaTransfersScanner:
    """Scans a block‑range for α‑stake transfers to one treasury cold‑key."""

    def __init__(
        self,
        subtensor: bt.Subtensor | bt.AsyncSubtensor,
        *,
        dest_coldkey: Optional[str] = None,
        allow_batch: bool = False,          # enable Utility batch unwrap
        debug_extr: bool = False,           # log every extrinsic inspected
        dump_events: bool = False,
        dump_last: int = DUMP_LAST,
        on_progress: Optional[Callable[[int, int, int], None]] = None,
        max_concurrency: int = MAX_CONCURRENCY,
        rpc_lock: Optional[asyncio.Lock] = None,
    ) -> None:
        self.st = subtensor
        self.dest_ck = dest_coldkey
        self.dest_ck_raw = _decode_ss58(dest_coldkey) if dest_coldkey else None
        self.allow_batch = allow_batch
        self.debug_extr = debug_extr
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

    async def _get_block(self, bn: int):
        """Return *(events, extrinsics_list)* for block *bn* while tolerating
        several return formats of substrate‑interface."""
        bh = await self._rpc(self.st.substrate.get_block_hash, block_id=bn)
        events = await self._rpc(self.st.substrate.get_events, block_hash=bh)
        blk = await self._rpc(self.st.substrate.get_block, block_hash=bh)

        # extract extrinsics from every known shape
        if isinstance(blk, dict):
            extrinsics = (
                blk.get("block", {}).get("extrinsics")      # v1.7+ (deep)
                or blk.get("extrinsics")                      # shallow
                or []
            )
        else:
            extrinsics = getattr(blk, "extrinsics", [])

        return events, extrinsics

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
                    raw_events, extrinsics = await self._get_block(bn)

                    # ── security: drop events belonging to disallowed extrinsics ── #
                    allowed_idx = _allowed_extrinsic_indices(self, extrinsics, bn)

                    raw_events = [
                        ev
                        for ev in raw_events
                        if ev.get("extrinsic_idx") in allowed_idx      # allowed transfer
                        or ev.get("extrinsic_idx") is None             # system event (no idx)
                    ]

                except websockets.exceptions.WebSocketException as err:
                    bt.logging.error(f"RPC error at block {bn}: {err}; reconnecting…")
                    raise err

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
    def _accumulate(  # single‑pass version
        self,
        raw_events,
        out: List[TransferEvent],
        *,
        block_hint_single: int,
        dump: bool,
    ) -> Tuple[int, int]:
        """Filters one block’s events; mutates *out*; returns (seen, kept)."""

        seen = kept = 0
        scratch: Dict[int, Dict] = {}           # per‑extrinsic bucket

        for ev in raw_events:
            idx = ev.get("extrinsic_idx")
            if idx is None:
                continue                        # ignore system events

            bucket = scratch.setdefault(idx, {})
            name = _event_name(ev)
            fields = _event_fields(ev)

            # companion facts -------------------------------------------------- #
            if name == "StakeRemoved":
                bucket["src_amt"] = _amount_from_stake_removed(fields)
                bucket["src_net"] = _subnet_from_stake_removed(fields)

            elif name == "StakeAdded":
                bucket["dst_amt"] = _amount_from_stake_added(fields)
                bucket["dst_net"] = _subnet_from_stake_added(fields)

            elif name == "StakeTransferred":
                seen += 1
                bucket["te"] = _parse_stake_transferred(fields, self.ss58_format)

            # can we finalise this transfer now? ------------------------------ #
            te: TransferEvent | None = bucket.get("te")
            if te is None:
                continue

            if "dst_amt" in bucket:
                te = replace(te, amount_rao=bucket["dst_amt"])
            if "src_net" in bucket:
                te = replace(te, src_subnet_id=bucket["src_net"])

            # SECURITY: drop cross‑subnet
            if te.src_subnet_id is not None and te.src_subnet_id != te.subnet_id:
                scratch.pop(idx, None)
                continue

            te = replace(te, block=block_hint_single)

            if te.amount_rao > 0 and (
                self.dest_ck is None or te.dest_coldkey == self.dest_ck
            ):
                kept += 1
                out.append(te)
                if dump:
                    bt.logging.info(
                        f"[blk {block_hint_single}] StakeTransferred "
                        f"net={te.subnet_id} uid={te.from_uid}->{te.to_uid} "
                        f"α={te.amount_rao}  **KEPT**"
                    )

            # done with this extrinsic – free memory
            scratch.pop(idx, None)

        return seen, kept
