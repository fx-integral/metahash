# ======================================================================
#
# metahash/validator/alpha_transfers.py
#
# Scanner for α-stake transfer events with robust RPC hygiene:
#   • _rpc(): pre-awaits any awaitable args/kwargs to prevent coroutine
#     leakage into async_substrate_interface JSON-RPC payloads.
#   • _get_block(): strict normalization of events/extrinsics.
#   • scan(): resilient workers that skip bad blocks instead of aborting.
#
# Security:
#   • Drops cross-subnet credits (src_subnet_id must equal dest subnet_id).
#   • Optional whitelist for Utility.{batch,*} via allow_batch.
#
# ======================================================================

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

import bittensor as bt
import websockets  # WS errors
from substrateinterface.utils.ss58 import (
    ss58_decode as _ss58_decode_generic,
    ss58_encode as _ss58_encode_generic,
)
from async_substrate_interface.errors import SubstrateRequestException

from metahash.config import MAX_CONCURRENCY

# ── extrinsic filter ───────────────────────────────────────────────────
UTILITY_FUNS: set[str] = {"batch", "force_batch", "batch_all"}

T = TypeVar("T")


async def maybe_async(fn: Callable[..., T] | T, *args, **kwargs) -> T:  # noqa: N802
    """
    Await *fn* whether it's:
    • a coroutine object / awaitable,
    • a coroutine function,
    • or a plain blocking function (runs in default thread-pool).
    """
    if inspect.isawaitable(fn):
        return await fn  # type: ignore[return-value]
    if asyncio.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)  # type: ignore[misc]
    # Call synchronous function in thread to avoid blocking the event loop.
    # Critically, if it returns an awaitable (e.g., a cached coroutine), await it here.
    result = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result  # type: ignore[return-value]
    return result  # type: ignore[return-value]


def _name(obj) -> str | None:
    """Return a *string* name for a call-module / call-function regardless of the exact shape that py-substrate-interface gives us."""
    if obj is None:
        return None
    if hasattr(obj, "name"):
        return obj.name
    if isinstance(obj, (bytes, str)):
        return obj.decode() if isinstance(obj, bytes) else obj
    if isinstance(obj, dict):
        return obj.get("name")
    return str(obj)




# ── constants ───────────────────────────────────────────────────────────
LOG_EVERY = 50
DUMP_LAST = 5

# ── dataclasses ─────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class TransferEvent:
    """Container for a single α-stake transfer."""
    block: int
    from_uid: int
    to_uid: int
    subnet_id: int  # destination (kept for API compatibility)
    amount_rao: int
    src_coldkey: Optional[str]
    dest_coldkey: Optional[str]
    src_coldkey_raw: Optional[bytes]
    dest_coldkey_raw: Optional[bytes]
    # NEW – origin netuid (None if companion StakeRemoved is missing)
    src_subnet_id: Optional[int] = None


# ── SS58 helpers ────────────────────────────────────────────────────────
def _encode_ss58(raw: Optional[bytes], fmt: int) -> Optional[str]:  # noqa: D401
    """
    Safely encode a 32-byte AccountId to SS58; returns None if input missing or if library signatures mismatch.

    Handles both:
      • ss58_encode(bytes_or_hex, address_type)
      • ss58_encode(pubkey=<bytes>, address_type=<int>)
      • ss58_encode(address=<hexstr>, address_type=<int>)
    """
    if raw is None:
        bt.logging.debug("[SS58] encode skipped: raw AccountId is None")
        return None

    # 1) Try the common positional (pubkey, fmt)
    try:
        out = _ss58_encode_generic(raw, fmt)  # some versions accept pubkey bytes here
        bt.logging.debug("[SS58] encode path=positional(pubkey,fmt) ok")
        return out
    except Exception as e1:
        bt.logging.debug(f"[SS58] encode path=positional failed: {type(e1).__name__}: {str(e1)[:120]}")

    # 2) Try explicit keywords (pubkey=..., address_type=...)
    try:
        out = _ss58_encode_generic(pubkey=raw, address_type=fmt)  # substrate-interface style
        bt.logging.debug("[SS58] encode path=keywords(pubkey=,address_type=) ok")
        return out
    except Exception as e2:
        bt.logging.debug(f"[SS58] encode path=keywords(pubkey=,address_type=) failed: {type(e2).__name__}: {str(e2)[:120]}")

    # 3) Try hex "address" path that some scalecodec versions expect
    try:
        out = _ss58_encode_generic(address="0x" + raw.hex(), address_type=fmt)
        bt.logging.debug("[SS58] encode path=keywords(address=hex,address_type=) ok")
        return out
    except Exception as e3:
        bt.logging.debug(f"[SS58] encode path=keywords(address=,address_type=) failed: {type(e3).__name__}: {str(e3)[:120]}")

    # 4) Last resort: give up quietly; caller will drop the event if None
    bt.logging.warning("[SS58] encode failed: all strategies exhausted; dropping event")
    return None


def _decode_ss58(addr: str) -> bytes:  # noqa: D401
    """
    Decode SS58 to raw 32-byte account id; tolerate signature differences across libraries.
    """
    try:
        return _ss58_decode_generic(addr)
    except TypeError:
        # older/newer variants
        return _ss58_decode_generic(addr, valid_ss58_format=True)


def _account_id(obj) -> bytes | None:  # noqa: ANN001,D401
    # Direct bytes-like 32 length
    if isinstance(obj, (bytes, bytearray)):
        try:
            return bytes(obj) if len(obj) == 32 else None
        except Exception:
            return None
    # Common scalecodec container shapes
    if hasattr(obj, "value"):
        try:
            return _account_id(getattr(obj, "value"))
        except Exception:
            pass
    if isinstance(obj, dict):
        # Some versions wrap under 'value' first
        if "value" in obj:
            v = obj.get("value")
            got = _account_id(v)
            if got is not None:
                return got
        # Named variants e.g. {'Id': '0x..'}, {'AccountId': bytes}, {'AccountId32': {...}}
        inner = (
            obj.get("Id")
            or obj.get("AccountId")
            or obj.get("AccountId32")
            or next(iter(obj.values()), None)
        )
        return _account_id(inner)
    # List of ints or nested singleton
    if isinstance(obj, (list, tuple)):
        if len(obj) == 32 and all(isinstance(x, int) for x in obj):
            try:
                return bytes(obj)  # list[int] -> bytes
            except Exception:
                return None
        if len(obj) == 1:
            return _account_id(obj[0])
    # Hex string e.g. '0x..'
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("0x") and len(s) >= 66:  # 32 bytes -> 64 hex chars + 0x
            try:
                b = bytes.fromhex(s[2:])
                return b if len(b) == 32 else None
            except Exception:
                return None
        # Try SS58 decode
        try:
            b = _decode_ss58(s)
            if isinstance(b, (bytes, bytearray)) and len(b) == 32:
                bt.logging.debug(f"[SCANNER] SS58 decode succeeded with default format for '{s}'")
                return b
            else:
                bt.logging.debug(f"[SCANNER] SS58 decode returned invalid result for '{s}': {type(b)} len={len(b) if hasattr(b, '__len__') else 'N/A'}")
        except Exception as e:
            bt.logging.debug(f"[SCANNER] SS58 decode failed for '{s}': {e}")
        
        # Try with different SS58 formats as fallback
        for fmt in [42, 0, 2]:  # Common SS58 formats
            try:
                b = _ss58_decode_generic(s, valid_ss58_format=fmt)
                if isinstance(b, (bytes, bytearray)) and len(b) == 32:
                    bt.logging.debug(f"[SCANNER] SS58 decode succeeded with format {fmt} for '{s}'")
                    return b
                else:
                    bt.logging.debug(f"[SCANNER] SS58 decode with format {fmt} returned invalid result for '{s}': {type(b)} len={len(b) if hasattr(b, '__len__') else 'N/A'}")
            except Exception as fmt_e:
                bt.logging.debug(f"[SCANNER] SS58 decode with format {fmt} failed for '{s}': {fmt_e}")
                continue
        return None
    return None


# ── generic event accessors ─────────────────────────────────────────────
def _event_name(ev) -> str:  # noqa: ANN001
    ev = ev.get("event", ev) if isinstance(ev, dict) else getattr(ev, "event", ev)
    if hasattr(ev, "method"):
        return str(ev.method)
    if isinstance(ev, dict):
        return str(ev.get("event_id") or ev.get("name", "<unknown>"))
    return "<unknown>"


def _event_fields(ev) -> Sequence:  # noqa: ANN001
    ev = ev.get("event", ev) if isinstance(ev, dict) else getattr(ev, "event", ev)
    if isinstance(ev, dict):
        return ev.get("attributes") or ev.get("params") or ev.get("data") or ()
    return (
        getattr(ev, "attributes", ())
        or getattr(ev, "params", ())
        or getattr(ev, "data", ())
        or ()
    )


def _f(params, idx, default=None):  # noqa: ANN001
    try:
        # Support both list/tuple and dict-like event params
        if isinstance(params, dict):
            # Preserve insertion order from dict.values() (Python 3.7+)
            vals = list(params.values())
            val = vals[idx]
        else:
            val = params[idx]
        return val["value"] if isinstance(val, dict) and "value" in val else val
    except (IndexError, TypeError):
        return default


def _fk(params, key_names: Sequence[str], default=None):  # noqa: ANN001
    """Fetch parameter by any of the candidate names from dict-shaped params or list of (name,value) dicts."""
    try:
        # Direct dict with named attributes
        if isinstance(params, dict):
            for k in key_names:
                if k in params:
                    v = params.get(k)
                    return v["value"] if isinstance(v, dict) and "value" in v else v
        # List/tuple of dicts that look like {name:..., value:...}
        if isinstance(params, (list, tuple)):
            for item in params:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("id") or item.get("key")
                    if isinstance(name, str) and name in key_names:
                        v = item.get("value") if "value" in item else item.get(name)
                        return v
        return default
    except Exception:
        return default


def _mask(ck: Optional[str]) -> str:
    return ck if ck is None or len(ck) < 10 else f"{ck[:4]}…{ck[-4:]}"


# ── parser helpers ──────────────────────────────────────────────────────
def _parse_stake_transferred(params, fmt: int) -> TransferEvent:  # noqa: ANN001
    """Parse StakeTransferred event parameters (chain v9 & v10)."""
    # Prefer named keys when available
    from_val = _fk(params, ["from_coldkey", "from", "src", "source", "who"]) or _f(params, 0)
    to_val = _fk(params, ["to_coldkey", "to", "dest", "destination"]) or _f(params, 1)
    net_val = _fk(params, ["dest_netuid", "netuid", "subnet", "to_netuid"]) or _f(params, 3, -1)
    uid_val = _fk(params, ["to_uid", "uid", "dest_uid"]) or _f(params, 4, -1)
    amt_val = _fk(params, ["amount", "value", "stake", "amt", "rao"]) or _f(params, 5, 0)

    from_coldkey_raw = _account_id(from_val)
    dest_coldkey_raw = _account_id(to_val)
    subnet_id = int(net_val if net_val is not None else -1)
    to_uid = int(uid_val if uid_val is not None else -1)
    return TransferEvent(
        block=-1,
        from_uid=-1,  # origin UID not provided
        to_uid=to_uid,
        subnet_id=subnet_id,  # = dest netuid
        amount_rao=int(amt_val or 0),  # placeholder, fixed later
        src_coldkey=_encode_ss58(from_coldkey_raw, fmt),
        dest_coldkey=_encode_ss58(dest_coldkey_raw, fmt),
        src_coldkey_raw=from_coldkey_raw,
        dest_coldkey_raw=dest_coldkey_raw,
    )


# ── helpers for Add / Remove events (v9+) ───────────────────────────────
def _amount_from_stake_removed(params) -> int:
    """Return amount_rao from StakeRemoved (v9+ layout)."""
    return int(_f(params, 3, _f(params, 2, 0)))


def _amount_from_stake_added(params) -> int:  # noqa: ANN001
    """Return amount_rao from StakeAdded (v9+ layout)."""
    return int(_f(params, 3, _f(params, 2, 0)))


def _subnet_from_stake_removed(params) -> int:
    """Return subnet from StakeRemoved."""
    return int(_f(params, 4, _f(params, 1, -1)))


def _subnet_from_stake_added(params) -> int:
    """Return subnet from StakeAdded."""
    return int(_f(params, 4, -1))


# ── main scanner class ─────────────────────────────────────────────────
class AlphaTransfersScanner:
    """Scans a block-range for α-stake transfers to one treasury cold-key."""

    def __init__(
        self,
        subtensor: bt.Subtensor | bt.AsyncSubtensor,
        *,
        dest_coldkey: Optional[str] = None,
        allow_batch: bool = False,        # enable Utility batch unwrap
        debug_extr: bool = False,         # log every extrinsic inspected
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

    def _allowed_extrinsic_indices(  # noqa: PLR0911
        self,
        extrinsics,
        block_num: int,
    ) -> set[int]:
        """Whitelist extrinsic indices inside *extrinsics* that are allowed to produce α-stake events.

        • Always allow SubtensorModule.transfer_stake.
        • Allow Utility.{batch,force_batch,batch_all} only when self.allow_batch is True.
        """
        allowed: set[int] = set()
        for idx, ex in enumerate(extrinsics):
            try:
                pallet = _name(ex["call"]["call_module"])
                func = _name(ex["call"]["call_function"])
                if self.debug_extr:
                    bt.logging.debug(f"[blk {block_num}] ex#{idx} {pallet}.{func}")
                # 1️⃣ direct α-stake transfer
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
                    bt.logging.debug(f"[blk {block_num}] ex#{idx} unreadable – {err!s}")
        return allowed

    async def _rpc(self, fn, *a, **kw):
        """
        Call substrate functions safely by:
          • pre-awaiting any awaitable args / kwargs (prevents coroutine leakage),
          • serializing the call under a lock to avoid interleaved ws payloads.
        """
        async with self._rpc_lock:
            # Pre-await any awaitable positional args
            a2 = []
            for x in a:
                a2.append((await x) if inspect.isawaitable(x) else x)
            # Pre-await any awaitable keyword args
            kw2 = {}
            for k, v in kw.items():
                kw2[k] = (await v) if inspect.isawaitable(v) else v
            return await maybe_async(fn, *a2, **kw2)

    async def _get_block(self, bn: int):
        """Return *(events, extrinsics_list)* for block *bn* with strict normalization."""
        # Get block hash
        bh = await self._rpc(self.st.substrate.get_block_hash, block_id=int(bn))

        # Guard block hash
        if not bh:
            raise ValueError(f"Got empty block hash for block {bn}")

        # Normalize block hash to a hex string for downstream RPCs.
        # Some substrate clients expect a string and will call .replace on it.
        if isinstance(bh, (bytes, bytearray)):
            bh_str = "0x" + bytes(bh).hex()
        else:
            bh_str = str(bh)

        # Fetch events / block using normalized hash
        events = await self._rpc(self.st.substrate.get_events, block_hash=bh_str)
        blk = await self._rpc(self.st.substrate.get_block, block_hash=bh_str)
        
        bt.logging.debug(
            f"[SCANNER] _get_block types: events={type(events).__name__} blk={type(blk).__name__}"
        )

        # Log detailed structure of events
        if events:
            bt.logging.debug(f"[SCANNER] events count: {len(events)}")
            if len(events) > 0:
                sample_event = events[0]
                bt.logging.debug(f"[SCANNER] sample event type: {type(sample_event).__name__}")
                if isinstance(sample_event, dict):
                    bt.logging.debug(f"[SCANNER] sample event keys: {list(sample_event.keys())}")
                    if "event" in sample_event:
                        evt = sample_event["event"]
                        bt.logging.debug(f"[SCANNER] event.event type: {type(evt).__name__}")
                        if isinstance(evt, dict):
                            bt.logging.debug(f"[SCANNER] event.event keys: {list(evt.keys())}")
                        elif hasattr(evt, '__dict__'):
                            bt.logging.debug(f"[SCANNER] event.event attrs: {list(evt.__dict__.keys())}")

        # Normalize events to a list of dict-ish items
        if not isinstance(events, list):
            bt.logging.error(
                f"[SCANNER] get_events returned {type(events).__name__}, expected list"
            )
            raise TypeError(f"Expected list from get_events, got {type(events).__name__}")

        # Normalize extrinsics shape
        if isinstance(blk, dict):
            extrinsics = (
                blk.get("block", {}).get("extrinsics")  # v1.7+ deep
                or blk.get("extrinsics")                 # shallow
                or []
            )
        else:
            extrinsics = getattr(blk, "extrinsics", None) or []

        # Ensure list
        if not isinstance(extrinsics, list):
            bt.logging.error(
                f"[SCANNER] extrinsics is {type(extrinsics).__name__}, expected list"
            )
            raise TypeError(f"Expected list for extrinsics, got {type(extrinsics).__name__}")

        return events, extrinsics

    async def scan(self, frm: int, to: int) -> List[TransferEvent]:
        if frm > to:
            return []
        total = to - frm + 1
        bt.logging.info(f"[SCANNER] Starting scan: blocks {frm} to {to} ({total} blocks)")
        bt.logging.debug(f"[SCANNER] Block range: frm={frm}, to={to}, total={total}")

        q: asyncio.Queue[int | None] = asyncio.Queue()
        events_by_block: Dict[int, list[TransferEvent]] = {}
        blk_cnt = ev_cnt = keep_cnt = 0

        async def _flush_progress():
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
                raw_events, extrinsics = await self._get_block(bn)
                allowed_idx = self._allowed_extrinsic_indices(extrinsics, bn)
                # Normalize event rows to dicts; drop weird shapes/strings
                cleaned = []
                for ev in raw_events or []:
                    if not isinstance(ev, dict):
                        bt.logging.error(
                            f"[SCANNER] block {bn}: skip non-dict event of type {type(ev).__name__}"
                        )
                        continue
                    idx = ev.get("extrinsic_idx")
                    if idx is None:
                        # Newer interfaces sometimes use 'extrinsic_index'
                        idx = ev.get("extrinsic_index")
                    if idx is None and self.dump_events:
                        bt.logging.debug(
                            f"[SCANNER] block {bn}: event missing extrinsic_idx; keys={list(ev.keys())[:8]}"
                        )
                    if idx in allowed_idx or idx is None:
                        cleaned.append(ev)
                raw_events = cleaned

                bucket = events_by_block.setdefault(bn, [])
                seen, kept = self._accumulate(
                    raw_events,
                    bucket,
                    block_hint_single=bn,
                    dump=self.dump_events and bn >= to - self.dump_last + 1,
                    extrinsics=extrinsics,
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

        bt.logging.info(f"[SCANNER] ✓ Scan finished: {blk_cnt}/{total} blocks processed, {ev_cnt} events found, {keep_cnt} kept")
        if blk_cnt < total:
            bt.logging.warning(f"[SCANNER] ⚠️  Only processed {blk_cnt}/{total} blocks - some blocks were skipped due to errors")
        await _flush_progress()

        ordered_events: List[TransferEvent] = []
        for bn in sorted(events_by_block.keys()):
            ordered_events.extend(events_by_block[bn])
        return ordered_events

    def _accumulate(  # single-pass version
        self,
        raw_events,
        out: List[TransferEvent],
        *,
        block_hint_single: int,
        dump: bool,
        extrinsics: Optional[Sequence] = None,
    ) -> Tuple[int, int]:
        """Filters one block’s events; mutates *out*; returns (seen, kept)."""
        seen = kept = 0
        scratch: Dict[int, Dict] = {}  # per-extrinsic bucket

        def _accounts_from_extrinsic(ex) -> Tuple[bytes | None, bytes | None]:
            """Try to extract up to two distinct 32-byte AccountIds from an extrinsic's call args."""
            try:
                args = None
                if isinstance(ex, dict):
                    args = ex.get("call", {}).get("args") or ex.get("args")
                else:
                    args = getattr(getattr(ex, "call", None), "args", None) or getattr(ex, "args", None)
                if args is None:
                    return None, None
                # Flatten nested values
                vals: list = []
                if isinstance(args, dict):
                    vals.extend(args.values())
                elif isinstance(args, (list, tuple)):
                    vals.extend(args)
                # Dive one level for dict/list containers
                flat: list = []
                for v in vals:
                    if isinstance(v, dict):
                        flat.extend(v.values())
                    elif isinstance(v, (list, tuple)):
                        flat.extend(v)
                    else:
                        flat.append(v)
                found: list[bytes] = []
                for v in flat:
                    raw = _account_id(v)
                    if raw is not None and raw not in found:
                        found.append(raw)
                    if len(found) >= 2:
                        break
                a = found[0] if len(found) >= 1 else None
                b = found[1] if len(found) >= 2 else None
                return a, b
            except Exception:
                return None, None

        for ev in raw_events:
            idx = ev.get("extrinsic_idx")
            name = _event_name(ev)
            if idx is None:
                if name in {"StakeRemoved", "StakeAdded", "StakeTransferred"}:
                    bt.logging.debug(f"[SCANNER] Skipping {name} event with no extrinsic_idx (system event)")
                continue  # ignore system events
            bucket = scratch.setdefault(idx, {})
            # Log all stake-related events we're processing
            if name in {"StakeRemoved", "StakeAdded", "StakeTransferred"}:
                bt.logging.debug(f"[SCANNER] Processing {name} event at extrinsic_idx {idx}")
            fields = _event_fields(ev)
            if not isinstance(fields, (list, tuple)):
                if name in {"StakeRemoved", "StakeAdded", "StakeTransferred"}:
                    bt.logging.debug(
                        f"[SCANNER] block {block_hint_single}: unexpected fields type for {name}: {type(fields).__name__}"
                    )
                    if isinstance(fields, dict):
                        bt.logging.debug(
                            f"[SCANNER] dict-field keys for {name}: {list(fields.keys())}"
                        )
                        # Log sample values for debugging
                        for k, v in list(fields.items())[:3]:
                            bt.logging.debug(f"[SCANNER] {name}.{k}: {type(v).__name__} = {v}")
                # Normalize dict-like fields to a list of values for downstream helpers
                if isinstance(fields, dict):
                    fields = list(fields.values())
                else:
                    fields = ()

            if name == "StakeRemoved":
                bucket["src_amt"] = _amount_from_stake_removed(fields)
                bucket["src_net"] = _subnet_from_stake_removed(fields)
                # Try to capture source coldkey raw from any AccountId-like field
                if "src_raw" not in bucket:
                    # scan both normalized list and original mapping
                    candidates = []
                    if isinstance(fields, (list, tuple)):
                        candidates.extend(fields)
                    elif isinstance(fields, dict):
                        candidates.extend(fields.values())
                    for v in candidates:
                        raw = _account_id(v)
                        if raw is not None:
                            bucket["src_raw"] = raw
                            bt.logging.debug(f"[SCANNER] Found src_raw from StakeRemoved: {raw.hex()[:16]}...")
                            break
            elif name == "StakeAdded":
                bucket["dst_amt"] = _amount_from_stake_added(fields)
                bucket["dst_net"] = _subnet_from_stake_added(fields)
                # Try to capture destination coldkey raw from any AccountId-like field
                if "dst_raw" not in bucket:
                    candidates = []
                    if isinstance(fields, (list, tuple)):
                        candidates.extend(fields)
                    elif isinstance(fields, dict):
                        candidates.extend(fields.values())
                    for v in candidates:
                        raw = _account_id(v)
                        if raw is not None:
                            bucket["dst_raw"] = raw
                            bt.logging.debug(f"[SCANNER] Found dst_raw from StakeAdded: {raw.hex()[:16]}...")
                            break
            elif name == "StakeTransferred":
                seen += 1
                # Log the exact structure of StakeTransferred events for debugging
                bt.logging.debug(f"[SCANNER] StakeTransferred fields type: {type(fields).__name__}")
                if isinstance(fields, dict):
                    bt.logging.debug(f"[SCANNER] StakeTransferred dict keys: {list(fields.keys())}")
                    for k, v in fields.items():
                        bt.logging.debug(f"[SCANNER] StakeTransferred.{k}: {type(v).__name__} = {v}")
                elif isinstance(fields, (list, tuple)):
                    bt.logging.debug(f"[SCANNER] StakeTransferred list length: {len(fields)}")
                    for i, field in enumerate(fields):
                        bt.logging.debug(f"[SCANNER] StakeTransferred[{i}]: {type(field).__name__} = {field}")
                
                bucket["te"] = _parse_stake_transferred(fields, self.ss58_format)
                # Also try to record raw accounts from this event directly if present
                if "src_raw" not in bucket:
                    candidates = []
                    if isinstance(fields, (list, tuple)):
                        candidates.extend(fields)
                    elif isinstance(fields, dict):
                        candidates.extend(fields.values())
                    bt.logging.debug(f"[SCANNER] Checking {len(candidates)} candidates for src_raw")
                    for i, v in enumerate(candidates):
                        raw = _account_id(v)
                        bt.logging.debug(f"[SCANNER] Candidate[{i}]: {type(v).__name__} = {v} -> raw: {raw.hex()[:16] if raw else None}")
                        if raw is not None:
                            bucket["src_raw"] = raw
                            bt.logging.debug(f"[SCANNER] Found src_raw from StakeTransferred: {raw.hex()[:16]}...")
                            break
                if "dst_raw" not in bucket:
                    candidates = []
                    if isinstance(fields, (list, tuple)):
                        candidates.extend(fields)
                    elif isinstance(fields, dict):
                        candidates.extend(fields.values())
                    bt.logging.debug(f"[SCANNER] Checking {len(candidates)} candidates for dst_raw")
                    for i, v in enumerate(candidates):
                        raw = _account_id(v)
                        bt.logging.debug(f"[SCANNER] Candidate[{i}]: {type(v).__name__} = {v} -> raw: {raw.hex()[:16] if raw else None}")
                        if raw is not None:
                            # do not overwrite src_raw if only one account found
                            if "src_raw" in bucket and bucket["src_raw"] != raw:
                                bucket["dst_raw"] = raw
                                bt.logging.debug(f"[SCANNER] Found dst_raw from StakeTransferred: {raw.hex()[:16]}...")
                                break

            te: TransferEvent | None = bucket.get("te")
            if te is None:
                continue

            if "dst_amt" in bucket:
                te = replace(te, amount_rao=bucket["dst_amt"])
            if "src_net" in bucket:
                te = replace(te, src_subnet_id=bucket["src_net"])
            # Enrich with captured raw account ids if missing from parsed TE
            if getattr(te, "src_coldkey_raw", None) is None and "src_raw" in bucket:
                raw = bucket.get("src_raw")
                te = replace(te, src_coldkey_raw=raw, src_coldkey=_encode_ss58(raw, self.ss58_format))
            if getattr(te, "dest_coldkey_raw", None) is None and "dst_raw" in bucket:
                raw = bucket.get("dst_raw")
                te = replace(te, dest_coldkey_raw=raw, dest_coldkey=_encode_ss58(raw, self.ss58_format))
            # Final fallback: try reading accounts from extrinsic args when both missing
            if (te.src_coldkey_raw is None or te.dest_coldkey_raw is None) and extrinsics is not None:
                ex = extrinsics[idx] if isinstance(extrinsics, list) and 0 <= idx < len(extrinsics) else None
                a, b = _accounts_from_extrinsic(ex)
                # Only apply if we don't already have values
                if te.src_coldkey_raw is None and a is not None:
                    te = replace(te, src_coldkey_raw=a, src_coldkey=_encode_ss58(a, self.ss58_format))
                    bt.logging.debug(f"[SCANNER] Found src_raw from extrinsic: {a.hex()[:16]}...")
                if te.dest_coldkey_raw is None and b is not None and b != (te.src_coldkey_raw or a):
                    te = replace(te, dest_coldkey_raw=b, dest_coldkey=_encode_ss58(b, self.ss58_format))
                    bt.logging.debug(f"[SCANNER] Found dst_raw from extrinsic: {b.hex()[:16]}...")

            # SECURITY: drop cross-subnet
            if te.src_subnet_id is not None and te.src_subnet_id != te.subnet_id:
                scratch.pop(idx, None)
                continue

            te = replace(te, block=block_hint_single)
            # Require usable addresses; drop if encoding failed or missing
            if te.amount_rao > 0 \
               and te.src_coldkey is not None \
               and te.dest_coldkey is not None \
               and (self.dest_ck is None or te.dest_coldkey == self.dest_ck):
                kept += 1
                out.append(te)
                if dump:
                    bt.logging.info(
                        f"[blk {block_hint_single}] StakeTransferred "
                        f"net={te.subnet_id} uid={te.from_uid}->{te.to_uid} "
                        f"α={te.amount_rao} **KEPT** (to {_mask(te.dest_coldkey)})"
                    )
            else:
                # Verbose reason for drop
                reason_parts = []
                if te.amount_rao <= 0:
                    reason_parts.append("nonpositive_amount")
                if te.src_coldkey is None:
                    reason_parts.append("src_ck_none")
                if te.dest_coldkey is None:
                    reason_parts.append("dest_ck_none")
                if (self.dest_ck is not None) and (te.dest_coldkey != self.dest_ck):
                    reason_parts.append("treasury_mismatch")
                bt.logging.debug(
                    "[SCANNER] drop StakeTransferred: "
                    f"blk={block_hint_single} sid={te.subnet_id} amt={te.amount_rao} "
                    f"src_raw={'yes' if te.src_coldkey_raw else 'no'} dest_raw={'yes' if te.dest_coldkey_raw else 'no'} "
                    f"src_ck={_mask(te.src_coldkey)} dest_ck={_mask(te.dest_coldkey)} "
                    f"reason={','.join(reason_parts) if reason_parts else 'unknown'}"
                )
                if dump:
                    bt.logging.info(
                        f"[blk {block_hint_single}] DROP details: src_raw_len={(len(te.src_coldkey_raw) if te.src_coldkey_raw else None)} "
                        f"dest_raw_len={(len(te.dest_coldkey_raw) if te.dest_coldkey_raw else None)} src_sid={te.src_subnet_id}"
                    )
            scratch.pop(idx, None)

        return seen, kept
