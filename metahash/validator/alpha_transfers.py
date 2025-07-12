# ====================================================================== #
#  metahash/validator/alpha_transfers.py                                 #
#  Patched 2025‑07‑12                                                    #
#    • works on runtimes  ≤v10  and  ≥v11                                #
#    • amount = first StakeAdded *after* StakeTransferred                #
#    • src_subnet_id = first matching StakeRemoved                       #
#    • cross‑subnet siphon blocked                                       #
#    • Utility.batch/force_batch disabled by default                     #
# ====================================================================== #
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import bittensor as bt
import websockets
from substrateinterface.utils.ss58 import (
    ss58_decode as _ss58_decode_generic,
    ss58_encode as _ss58_encode_generic,
)

from metahash.config import MAX_CONCURRENCY
from metahash.utils.async_substrate import maybe_async

# ───────────────────────── constants ────────────────────────────────── #
MAX_CHUNK_DEFAULT = 1_000
LOG_EVERY = 50
DUMP_LAST = 5

UTILITY_FUNS = {"batch", "batch_all", "force_batch"}

# ──────────────────────── helper dataclass ──────────────────────────── #


@dataclass(slots=True, frozen=True)
class TransferEvent:
    """One α‑stake transfer."""

    block: int
    from_uid: int
    to_uid: int

    subnet_id: int
    amount_rao: int                        # α credited to treasury (rao)

    src_coldkey: Optional[str]
    dest_coldkey: Optional[str]
    src_coldkey_raw: Optional[bytes]
    dest_coldkey_raw: Optional[bytes]

    src_subnet_id: Optional[int] = None    # origin subnet (for filter)


# ──────────────────────── SS58 helpers (unchanged) ──────────────────── #

def _encode_ss58(raw: bytes, fmt: int) -> str:
    try:
        return _ss58_encode_generic(raw, fmt)
    except TypeError:
        return _ss58_encode_generic(raw, address_type=fmt)


def _decode_ss58(addr: str) -> bytes:
    try:
        return _ss58_decode_generic(addr)
    except TypeError:
        return _ss58_decode_generic(addr, valid_ss58_format=True)


def _account_id(obj) -> bytes | None:
    if isinstance(obj, (bytes, bytearray)) and len(obj) == 32:
        return bytes(obj)
    if isinstance(obj, (list, tuple)):
        if len(obj) == 32 and all(isinstance(x, int) for x in obj):
            return bytes(obj)
        if len(obj) == 1:
            return _account_id(obj[0])
    if isinstance(obj, dict):
        inner = (
            obj.get("Id") or obj.get("AccountId") or next(iter(obj.values()), None)
        )
        return _account_id(inner)
    return None


# ───────────────────── generic event accessors ──────────────────────── #

def _event_name(ev) -> str:
    ev = ev.get("event", ev) if isinstance(ev, dict) else getattr(ev, "event", ev)
    if hasattr(ev, "method"):
        return str(ev.method)
    if isinstance(ev, dict):
        return str(ev.get("event_id") or ev.get("name", "<unknown>"))
    return "<unknown>"


def _event_fields(ev) -> Sequence:
    ev = ev.get("event", ev) if isinstance(ev, dict) else getattr(ev, "event", ev)
    if isinstance(ev, dict):
        return ev.get("attributes") or ev.get("params") or ev.get("data") or ()
    return (
        getattr(ev, "attributes", ())
        or getattr(ev, "params", ())
        or getattr(ev, "data", ())
        or ()
    )


def _f(params, idx, default=None):
    try:
        val = params[idx]
        return val["value"] if isinstance(val, dict) and "value" in val else val
    except (IndexError, TypeError):
        return default


def _mask(ck: Optional[str]) -> str:
    return ck if ck is None or len(ck) < 10 else f"{ck[:4]}…{ck[-4:]}"


# ──────────────────────── field parsers ─────────────────────────────── #

def _parse_stake_transferred(params, fmt: int) -> TransferEvent:
    """
    v11 layout (≥ June 2025):
        0  from_coldkey
        1  dest_coldkey
        2  hotkey_staked_to
        3  dest_netuid
        4  amount
        5  from_uid

    v10 and earlier:
        0‑3 identical
        4  to_uid
        5  amount
    """
    from_ck_raw = _account_id(_f(params, 0))
    dest_ck_raw = _account_id(_f(params, 1))
    dest_netuid = int(_f(params, 3, -1))

    slot4 = int(_f(params, 4, 0))
    slot5 = int(_f(params, 5, 0))

    # heuristic: UID is unbounded, amount (rao) is <= 21e14
    if slot4 > 10**12:       # assume slot4 is UID  (runtime ≤v10)
        to_uid = slot4
        from_uid = -1
        amount_dummy = slot5
    else:                    # runtime ≥v11
        amount_dummy = slot4
        from_uid = slot5
        to_uid = -1

    return TransferEvent(
        block=-1,
        from_uid=from_uid,
        to_uid=to_uid,
        subnet_id=dest_netuid,
        amount_rao=amount_dummy,          # replaced later
        src_coldkey=_encode_ss58(from_ck_raw, fmt),
        dest_coldkey=_encode_ss58(dest_ck_raw, fmt),
        src_coldkey_raw=from_ck_raw,
        dest_coldkey_raw=dest_ck_raw,
    )


# ─────────────── parsers for StakeAdded / StakeRemoved ─────────────── #

def _amount_from_stake_added(params) -> int:
    # v9+: 0 dest_ck, 1 hotkey, 2 to_uid, 3 amount, 4 dest_netuid
    return int(_f(params, 3, _f(params, 2, 0)))


def _subnet_from_stake_added(params) -> int:
    return int(_f(params, 4, -1))


def _amount_from_stake_removed(params) -> int:
    return int(_f(params, 3, _f(params, 2, 0)))


def _subnet_from_stake_removed(params) -> int:
    return int(_f(params, 4, _f(params, 1, -1)))


# ─────────────────────── main scanner class ─────────────────────────── #

class AlphaTransfersScanner:
    """Scan a block‑range for α transfers to a treasury cold‑key."""

    def __init__(
        self,
        subtensor: bt.Subtensor | bt.AsyncSubtensor,
        *,
        dest_coldkey: Optional[str] = None,
        allow_utility_batch: bool = False,
        dump_events: bool = False,
        dump_last: int = DUMP_LAST,
        on_progress: Optional[Callable[[int, int, int], None]] = None,
        max_concurrency: int = MAX_CONCURRENCY,
        rpc_lock: Optional[asyncio.Lock] = None,
    ):
        self.st = subtensor
        self.dest_ck = dest_coldkey
        self.dest_ck_raw = _decode_ss58(dest_coldkey) if dest_coldkey else None
        self.allow_batch = allow_utility_batch
        self.dump_events = dump_events
        self.dump_last = dump_last
        self.on_progress = on_progress
        self.max_conc = max_concurrency
        self.ss58_format = subtensor.substrate.ss58_format
        self._rpc_lock: asyncio.Lock = rpc_lock or asyncio.Lock()

    # ─────────────────── substrate helpers ─────────────────────────── #

    async def _rpc(self, fn, *a, **kw):
        async with self._rpc_lock:
            return await maybe_async(fn, *a, **kw)

    async def _get_block(self, bn: int):
        bh = await self._rpc(self.st.substrate.get_block_hash, block_id=bn)
        events = await self._rpc(self.st.substrate.get_events, block_hash=bh)
        blk = await self._rpc(self.st.substrate.get_block, block_hash=bh)

        # -------- extract extrinsics no matter how substrate‑interface formats it
        if isinstance(blk, dict):
            extrinsics = (
                blk.get("block", {}).get("extrinsics")      # v1.7+
                or blk.get("extrinsics")                    # some older forks
                or []
            )
        else:
            # older substrate‑interface returns an object with .extrinsics attr
            extrinsics = getattr(blk, "extrinsics", [])

        return events, extrinsics

    # ───────────────────────── scan loop ───────────────────────────── #

    async def scan(self, frm: int, to: int) -> List[TransferEvent]:
        if frm > to:
            return []

        total = to - frm + 1
        bt.logging.info(f"Scanner: frm={frm}  to={to}  ({total} blocks)")

        q: asyncio.Queue[int | None] = asyncio.Queue()
        events_by_block: Dict[int, list[TransferEvent]] = {}
        blk_cnt = ev_cnt = keep_cnt = 0

        async def _flush():
            if self.on_progress:
                self.on_progress(blk_cnt, ev_cnt, keep_cnt)

        async def producer():
            for bn in range(frm, to + 1):
                await q.put(bn)
            for _ in range(self.max_conc):
                await q.put(None)

        async def worker():
            nonlocal blk_cnt, ev_cnt, keep_cnt
            while True:
                bn = await q.get()
                if bn is None:
                    break

                try:
                    raw_events, extrinsics = await self._get_block(bn)
                except websockets.exceptions.WebSocketException as err:
                    bt.logging.warning(f"RPC error at block {bn}: {err}; reconnecting…")
                    async with self._rpc_lock:
                        await self.st.__aexit__(None, None, None)
                        await self.st.initialize()
                    raw_events, extrinsics = await self._get_block(bn)

                allowed_idx = self._allowed_extrinsic_indices(extrinsics)
                bucket = events_by_block.setdefault(bn, [])

                seen, kept = self._accumulate(
                    raw_events,
                    bucket,
                    allowed_idx=allowed_idx,
                    block_hint_single=bn,
                    dump=self.dump_events and bn >= to - self.dump_last + 1,
                )
                ev_cnt += seen
                keep_cnt += kept
                blk_cnt += 1
                if blk_cnt % LOG_EVERY == 0:
                    bt.logging.info(f"… scanned {blk_cnt} / {total} blocks")
                await _flush()

        await asyncio.gather(
            producer(),
            *[asyncio.create_task(worker()) for _ in range(self.max_conc)],
        )

        bt.logging.info(
            f"✓ scan finished: {blk_cnt} blk, {ev_cnt} ev, {keep_cnt} kept"
        )
        await _flush()

        ordered = []
        for bn in sorted(events_by_block.keys()):
            ordered.extend(events_by_block[bn])
        return ordered

    # ──────────────────── extrinsic filtering ─────────────────────── #

    def _allowed_extrinsic_indices(self, extrinsics) -> set[int]:
        """
        • Always allow SubtensorModule.transfer_stake
        • Allow Utility.{batch,force_batch,batch_all} only if
          allow_utility_batch=True
        """
        allowed: set[int] = set()
        for idx, ex in enumerate(extrinsics):
            try:
                call = ex["call"]
                pallet = call["call_module"]
                func = call["call_function"]
            except Exception:
                pallet = getattr(ex, "call_module", "")
                func = getattr(ex, "call_function", "")

            pallet = str(pallet)
            func = str(func)

            if (pallet, func) == ("SubtensorModule", "transfer_stake"):
                allowed.add(idx)
            elif (
                pallet == "Utility"
                and func in UTILITY_FUNS
                and self.allow_batch
            ):
                allowed.add(idx)
        return allowed

    # ─────────────── event processor (per single block) ────────────── #

    def _accumulate(
        self,
        raw_events,
        out: List[TransferEvent],
        *,
        allowed_idx: set[int],
        block_hint_single: int,
        dump: bool,
    ) -> Tuple[int, int]:
        """
        Pair events **in the order they are emitted**.

        • amount = first StakeAdded *after* StakeTransferred
        • src_subnet = first matching StakeRemoved
        • if either is missing  → unsafe → drop
        """
        seen = kept = 0
        fmt = self.ss58_format

        # per‑extrinsic queues of "open" transfers waiting for StakeAdded / Removed
        waiting: Dict[int, List[dict]] = {idx: [] for idx in allowed_idx}

        for ev in raw_events:
            idx = ev.get("extrinsic_idx")
            if idx is None or idx not in allowed_idx:
                continue

            name = _event_name(ev)
            fields = _event_fields(ev)

            if name == "StakeTransferred":
                seen += 1
                try:
                    te = _parse_stake_transferred(fields, fmt)
                except Exception as exc:
                    bt.logging.debug(f"Malformed event skipped: {exc}")
                    continue

                hk = _account_id(_f(fields, 2)) or b""
                from_ck = te.src_coldkey_raw or b""

                waiting[idx].append(
                    {
                        "hk": hk,
                        "from_ck": from_ck,
                        "te": te,
                        "have_add": False,
                        "have_rem": False,
                        "amount": 0,
                        "src_netuid": None,
                    }
                )

            elif name == "StakeAdded":
                info = waiting[idx]
                if not info:
                    continue
                top = info[0]          # first unmatched transfer
                dest_ck = _account_id(_f(fields, 0)) or b""
                if dest_ck != top["te"].dest_coldkey_raw:
                    continue   # belongs to another call later in the batch
                if top["have_add"]:
                    continue   # we already paired first add
                top["amount"] = _amount_from_stake_added(fields)
                top["have_add"] = True

            elif name == "StakeRemoved":
                info = waiting[idx]
                if not info:
                    continue
                top = info[0]
                hk = _account_id(_f(fields, 1)) or b""
                from_ck = _account_id(_f(fields, 0)) or b""
                if hk != top["hk"] or from_ck != top["from_ck"]:
                    continue
                if top["have_rem"]:
                    continue
                top["src_netuid"] = _subnet_from_stake_removed(fields)
                top["have_rem"] = True

            # whenever the first queued transfer has both pieces, emit it
            while waiting[idx] and waiting[idx][0]["have_add"] and waiting[idx][0]["have_rem"]:
                entry = waiting[idx].pop(0)
                te: TransferEvent = entry["te"]
                te = replace(
                    te,
                    amount_rao=entry["amount"],
                    src_subnet_id=entry["src_netuid"],
                    block=block_hint_single,
                )

                # cross‑subnet gate
                if te.src_subnet_id != te.subnet_id:
                    if dump:
                        bt.logging.info(
                            f"[blk {block_hint_single}] cross‑net α‑transfer "
                            f"{te.src_subnet_id} ➞ {te.subnet_id}  "
                            f"(α={te.amount_rao})  **IGNORED**"
                        )
                    continue

                if te.amount_rao > 0 and (
                    self.dest_ck is None or te.dest_coldkey == self.dest_ck
                ):
                    kept += 1
                    out.append(te)
                    if dump:
                        bt.logging.info(
                            f"[blk {block_hint_single}] StakeTransferred "
                            f"net={te.subnet_id}  α={te.amount_rao}  "
                            f"src={_mask(te.src_coldkey)}  dest={_mask(te.dest_coldkey)}"
                        )

        return seen, kept
