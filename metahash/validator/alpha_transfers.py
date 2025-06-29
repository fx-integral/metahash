# ====================================================================== #
#  metahash/validator/alpha_transfers.py                                 #
# ====================================================================== #

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Sequence, Tuple, Dict

import bittensor as bt
from substrateinterface.utils.ss58 import (
    ss58_encode as _ss58_encode_generic,
    ss58_decode as _ss58_decode_generic,
)

from metahash.utils.async_substrate import maybe_async
from metahash.config import MAX_CONCURRENCY

# ── constants ─────────────────────────────────────────────────────────── #
MAX_CHUNK_DEFAULT = 1_000
LOG_EVERY = 50
DUMP_LAST = 5

# ── dataclasses ───────────────────────────────────────────────────────── #


@dataclass(slots=True, frozen=True)
class TransferEvent:
    block: int
    from_uid: int
    to_uid: int
    subnet_id: int
    amount_rao: int
    dest_coldkey: Optional[str]
    dest_coldkey_raw: Optional[bytes]

# ── SS58 helpers (unchanged) ──────────────────────────────────────────── #


def _encode_ss58(raw: bytes, fmt: int) -> str:
    try:
        return _ss58_encode_generic(raw, fmt)
    except TypeError:  # very old substrate-interface
        return _ss58_encode_generic(raw, address_type=fmt)


def _decode_ss58(addr: str) -> bytes:
    try:
        return _ss58_decode_generic(addr)
    except TypeError:
        return _ss58_decode_generic(addr, valid_ss58_format=True)


def _account_id(obj) -> bytes | None:  # noqa: ANN001
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

# ── generic event accessors (unchanged) ──────────────────────────────── #


def _event_name(ev) -> str:  # noqa: ANN001
    ev = ev.get("event", ev) if isinstance(ev, dict) else getattr(ev, "event", ev)
    if hasattr(ev, "method"):  # object style
        return str(ev.method)
    if isinstance(ev, dict):
        return str(ev.get("event_id") or ev.get("name", "<unknown>"))
    return "<unknown>"


def _event_fields(ev) -> Sequence:  # noqa: ANN001
    ev = ev.get("event", ev) if isinstance(ev, dict) else getattr(ev, "event", ev)
    if isinstance(ev, dict):
        return ev.get("attributes") or ev.get("params") or ev.get("data") or ()
    return getattr(ev, "attributes", ()) or getattr(ev, "params", ()) or getattr(
        ev, "data", ()) or ()


def _f(params, idx, default=None):  # noqa: ANN001
    try:
        val = params[idx]
        return val["value"] if isinstance(val, dict) and "value" in val else val
    except (IndexError, TypeError):
        return default


def _mask(ck: Optional[str]) -> str:
    return ck if ck is None or len(ck) < 10 else f"{ck[:4]}…{ck[-4:]}"


# ── parser (unchanged) ───────────────────────────────────────────────── #


def _parse_stake_transferred(
        params, fmt: int, treasury_raw: Optional[bytes]
) -> TransferEvent:  # noqa: ANN001
    """Return a TransferEvent for either legacy (≤ v8) *or* Dynamic-TAO (v9 +) layout."""
    from_hot = _account_id(_f(params, 0))
    a_raw = _account_id(_f(params, 1))
    b_raw = _account_id(_f(params, 2))

    if None in (from_hot, a_raw, b_raw):
        raise ValueError("Malformed StakeTransferred parameters")

    # Decide which of a_raw / b_raw is the cold-key
    if treasury_raw:
        if a_raw == treasury_raw:
            dest_raw, to_hot = a_raw, b_raw
        elif b_raw == treasury_raw:
            dest_raw, to_hot = b_raw, a_raw
        else:  # fall back
            dest_raw, to_hot = b_raw, a_raw
    else:  # heuristic: cold-key ≠ any hot-key
        dest_raw, to_hot = (a_raw, b_raw) if a_raw != from_hot else (b_raw, a_raw)

    subnet_id = int(_f(params, 3, -1))
    from_uid = subnet_id  # kept for backward compat
    to_uid = int(_f(params, 4, -1))
    amount = int(_f(params, 5, 0))

    return TransferEvent(
        block=-1,
        from_uid=from_uid,
        to_uid=to_uid,
        subnet_id=subnet_id,
        amount_rao=amount,
        dest_coldkey=_encode_ss58(dest_raw, fmt),
        dest_coldkey_raw=dest_raw,
    )


def _amount_from_stake_removed(params) -> int:  # noqa: ANN001
    return int(_f(params, 3, 0))

# ── main scanner class (async) ────────────────────────────────────────── #


class AlphaTransfersScanner:
    """Scans a block-range for α-stake transfers to *one* treasury cold-key."""

    def __init__(
        self,
        subtensor: bt.Subtensor | bt.AsyncSubtensor,
        *,
        dest_coldkey: Optional[str] = None,
        dump_events: bool = False,
        dump_last: int = DUMP_LAST,
        on_progress: Optional[Callable[[int, int, int], None]] = None,
        max_concurrency: int = MAX_CONCURRENCY,
    ) -> None:
        self.st = subtensor
        self.dest_ck = dest_coldkey
        self.dest_ck_raw = _decode_ss58(dest_coldkey) if dest_coldkey else None
        self.dump_events = dump_events
        self.dump_last = dump_last
        self.on_progress = on_progress
        self.max_conc = max_concurrency
        self.ss58_format = subtensor.substrate.ss58_format

    # ------------------------------------------------------------------ #
    async def _get_events_at(self, bn: int):
        bh = await maybe_async(self.st.substrate.get_block_hash, block_id=bn)
        return await maybe_async(self.st.substrate.get_events, block_hash=bh)

    # ------------------------------------------------------------------ #
    async def scan(self, frm: int, to: int) -> List[TransferEvent]:
        """
        Return all α-stake transfers in the **safe** range
        `[frm, to]` (inclusive).

        Uses a bounded-concurrency producer/consumer pattern so that no
        more than `max_conc` worker tasks exist at any time.  Events are
        returned **deterministically sorted by block height**.
        """
        if frm > to:
            return []

        safe_to = max(frm - 1, to)
        if safe_to < frm:
            return []

        total = safe_to - frm + 1
        bt.logging.info(f"Scanner: frm={frm}  to={to}  ({total} blocks)")

        q: asyncio.Queue[int | None] = asyncio.Queue()
        events_by_block: Dict[int, list[TransferEvent]] = {}
        blk_cnt = ev_cnt = keep_cnt = 0

        async def _flush_progress():
            if self.on_progress:
                self.on_progress(blk_cnt, ev_cnt, keep_cnt)

        # ── producer ───────────────────────────────────────────────────
        async def producer():
            for bn in range(frm, safe_to + 1):
                await q.put(bn)
            for _ in range(self.max_conc):
                await q.put(None)  # sentinel

        # ── worker ─────────────────────────────────────────────────────
        async def worker():
            nonlocal blk_cnt, ev_cnt, keep_cnt
            while True:
                bn = await q.get()
                if bn is None:
                    break
                raw_events = await self._get_events_at(bn)
                bucket = events_by_block.setdefault(bn, [])
                seen, kept = self._accumulate(
                    raw_events,
                    bucket,
                    block_hint_single=bn,
                    dump=self.dump_events and bn >= safe_to - self.dump_last + 1,
                )
                ev_cnt += seen
                keep_cnt += kept
                blk_cnt += 1
                if blk_cnt % LOG_EVERY == 0:
                    bt.logging.info(f"… scanned {blk_cnt} / {total} blocks")
                await _flush_progress()

        # ── run producer & N workers ───────────────────────────────────
        await asyncio.gather(
            producer(),
            *[asyncio.create_task(worker()) for _ in range(self.max_conc)],
        )

        bt.logging.info(
            f"✓ scan finished: {blk_cnt} blk, {ev_cnt} ev, {keep_cnt} kept"
        )
        await _flush_progress()

        # ── flatten deterministically ──────────────────────────────────
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
        """Filters one block’s events; mutates *out*; returns (seen, kept)."""
        seen = kept = 0
        removed_by_x: dict[int, int] = {}

        for ev in raw_events:
            if _event_name(ev) == "StakeRemoved":
                x = ev.get("extrinsic_idx")
                if x is not None:
                    removed_by_x[x] = _amount_from_stake_removed(_event_fields(ev))

        for ev in raw_events:
            if _event_name(ev) != "StakeTransferred":
                continue
            seen += 1

            try:
                te = _parse_stake_transferred(
                    _event_fields(ev), self.ss58_format, self.dest_ck_raw
                )
            except Exception as exc:  # pragma: no cover
                bt.logging.debug(f"Malformed event skipped: {exc}")
                continue

            # Patch amount (token burn emits 0 here)
            x = ev.get("extrinsic_idx")
            if x is not None and x in removed_by_x:
                te = replace(te, amount_rao=removed_by_x[x])

            te = replace(te, block=block_hint_single)

            matches = (
                te.amount_rao > 0
                and (self.dest_ck is None or te.dest_coldkey == self.dest_ck)
            )
            if matches:
                kept += 1
                out.append(te)

            if dump:
                bt.logging.info(
                    f"[blk {block_hint_single}] StakeTransferred uid={te.from_uid}→{te.to_uid} "
                    f"dest={_mask(te.dest_coldkey)} amt={te.amount_rao} "
                    f"{'KEPT' if matches else 'SKIP'}"
                )

        return seen, kept
