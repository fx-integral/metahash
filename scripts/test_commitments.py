#!/usr/bin/env python3
"""
Utility + smoke tests for on‑chain plain commitments.

Covers:
 1) Compact JSON size calculation (same serializer as commitments.py).
 2) Safe write with **two** guards:
    • 16 KiB JSON cap (our own), and
    • a conservative **RawN variant** ceiling (defaults to 176 B) to avoid
      "Value 'RawNNN' not present in type_mapping of this enum" on some chains.
 3) Read‑back of your current plain commitment.
 4) Fuzzer to find an upper bound for ring‑buffer length.
 5) **wrv** mode: Write → Read → Verify and print **✅** on success.

Why you saw the error
---------------------
Even tiny payloads (e.g., 199 bytes) can fail if the runtime only supports
`Raw0..Raw176`. That’s separate from our 16 KiB JSON limit. This tool lets you
use a **--profile tiny** payload that always fits, and blocks writes above the
conservative Raw ceiling to fail fast with a clear message.

Examples
--------
# 1) See size of a tiny (always‑safe) payload
python test_commitments.py size-check --profile tiny

# 2) Write tiny payload to your subnet
python test_commitments.py write \
  --network ws://128.140.68.225:11144 --netuid 348 \
  --wallet.name owner --wallet.hotkey validator \
  --profile tiny

# 3) One‑shot Write→Read→Verify with ✅
python test_commitments.py wrv \
  --network ws://128.140.68.225:11144 --netuid 348 \
  --wallet.name owner --wallet.hotkey validator \
  --profile tiny --with-nonce

# 4) Try a small normal payload (check size first!)
python test_commitments.py size-check --snaps 1 --winners 1 --lines 1 --profile normal
python test_commitments.py write \
  --network ws://128.140.68.225:11144 --netuid 348 \
  --wallet.name owner --wallet.hotkey validator \
  --snaps 1 --winners 1 --lines 1 --profile normal

# 5) Read your current plain commitment
python test_commitments.py read --network ws://128.140.68.225:11144 --netuid 348 \
  --wallet.name owner --wallet.hotkey validator

# 6) Fuzz ring‑buffer length (reports 16 KiB and Raw caps)
python test_commitments.py fuzz --network ws://128.140.68.225:11144 --netuid 348 \
  --wallet.name owner --wallet.hotkey validator --winners 2 --lines 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import string
from typing import Any, Dict, List

# Import the real helpers so behavior matches production
from metahash.utils.commitments import (
    MAX_COMMIT_BYTES,
    _json_dump_compact,   # type: ignore  # private but we want parity
    read_plain_commitment,
    read_all_plain_commitments,
    write_plain_commitment_json,
)
from bittensor import AsyncSubtensor
from bittensor_wallet import Wallet

# Conservative RawN ceiling used by some chains (avoid RawNNN errors)
RAW_ENUM_SAFE_MAX = 176  # bytes

# --------------------------- payload builders --------------------------- #

def _mk_invoice(uid: int, ck: str, n_lines: int = 1) -> Dict[str, Any]:
    """Make a compact invoice entry like your pending snapshot would store.
    Shape: { "ck": <coldkey>, "ln": [[subnet_id, acc_alpha, disc_bps, rao], ...] }
    """
    lines = []
    for i in range(n_lines):
        subnet_id = 100 + (uid % 20)  # deterministic but varied
        acc_alpha = round(1.0 + i * 0.001, 6)
        disc_bps = 100 + (i % 10) * 5
        rao = int(1e9 * acc_alpha)  # arbitrary
        lines.append([subnet_id, acc_alpha, disc_bps, rao])
    return {"ck": ck, "ln": lines}


def build_snapshot(epoch_cleared: int, pe: int, t: str, as_blk: int, de_blk: int,
                   winners: List[int], lines_per_win: int = 1) -> Dict[str, Any]:
    """Build a synthetic v2 snapshot similar to `build_pending_commitment` output.
    Keys kept tiny to minimize overhead, matching your code expectations:
      e: epoch_cleared, pe: pay_epoch, t: treasury, as: start, de: end
      inv: { uid_str: { ck: str, ln: [[sid, acc, disc, rao], ...] }, ... }
    """
    inv: Dict[str, Any] = {}
    for uid in winners:
        ck = f"ck{uid:04d}{random.choice(string.ascii_lowercase)}"
        inv[str(uid)] = _mk_invoice(uid, ck, n_lines=lines_per_win)
    return {"e": epoch_cleared, "pe": pe, "t": t, "as": as_blk, "de": de_blk, "inv": inv}


def build_payload(snaps: int = 1, winners_per_snap: int = 2, lines_per_win: int = 1) -> Dict[str, Any]:
    # Keep previous one snapshot if snaps>1 (ring buffer style)
    base = build_snapshot(12345, 12346, "TREASURY_SS58", 5_300_000, 5_300_360,
                          winners=list(range(10, 10 + winners_per_snap)),
                          lines_per_win=lines_per_win)
    arr: List[Dict[str, Any]] = [base]
    for i in range(1, snaps):
        prev = build_snapshot(12345 - i, 12346 - i, "TREASURY_SS58", 5_299_000 - i * 400,
                              5_299_360 - i * 400,
                              winners=list(range(10 + i, 10 + i + winners_per_snap)),
                              lines_per_win=lines_per_win)
        arr.append(prev)
    return {"v": 2, "sn": arr}


def tiny_payload() -> Dict[str, Any]:
    """An ultra‑compact payload guaranteed to fit Raw caps on strict runtimes."""
    return {"v": 2, "sn": [{"e": 1, "pe": 2, "as": 1, "de": 2, "inv": {}}]}


def payload_size_bytes(data: Any) -> int:
    return len(_json_dump_compact(data).encode("utf-8"))

# ----------------------------- live helpers ----------------------------- #

async def _init_st(network: str) -> AsyncSubtensor:
    st = AsyncSubtensor(network=network)
    await st.initialize()
    return st

async def _init_wallet(name: str, hotkey: str) -> Wallet:
    w = Wallet(name=name, hotkey=hotkey)
    return w

# -------------------------------- modes -------------------------------- #

def _make_payload_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    base = tiny_payload() if args.profile == "tiny" else build_payload(
        snaps=args.snaps, winners_per_snap=args.winners, lines_per_win=args.lines
    )
    if getattr(args, "with_nonce", False):
        try:
            n = int(args.with_nonce)
        except Exception:
            import time as _t
            n = int(_t.time())
        if isinstance(base.get("sn"), list) and base["sn"]:
            base["sn"][0]["n"] = n  # tiny nonce
    return base

async def _write_payload(st: AsyncSubtensor, w: Wallet, netuid: int, payload: Dict[str, Any]) -> bool:
    size = payload_size_bytes(payload)
    print(
        f"Attempting write… payload_bytes={size} (json_cap={MAX_COMMIT_BYTES})"
    )
    if size > RAW_ENUM_SAFE_MAX:
        print(
            f"Refusing to write: payload {size}B exceeds RawN ceiling {RAW_ENUM_SAFE_MAX}B. "
            f"Use --profile tiny or reduce snaps/winners/lines."
        )
        return False
    if size > MAX_COMMIT_BYTES:
        print(
            f"Refusing to write: payload {size}B exceeds JSON cap {MAX_COMMIT_BYTES}B. "
            f"Trim ring buffer or compact fields."
        )
        return False
    ok = await write_plain_commitment_json(st, wallet=w, data=payload, netuid=netuid)
    print(f"write_commitment -> {ok}")
    return bool(ok)

async def cmd_size_check(args: argparse.Namespace) -> None:
    payload = tiny_payload() if args.profile == "tiny" else build_payload(
        snaps=args.snaps, winners_per_snap=args.winners, lines_per_win=args.lines
    )
    size = payload_size_bytes(payload)
    print(
        f"payload_bytes={size} (limit={MAX_COMMIT_BYTES}) | "
        f"snaps={args.snaps} winners/snap={args.winners} lines/win={args.lines} profile={args.profile}"
    )
    raw_ok = size <= RAW_ENUM_SAFE_MAX
    print(f"raw_enum_ok={raw_ok} (ceiling={RAW_ENUM_SAFE_MAX}B)")
    print(json.dumps(payload, ensure_ascii=False))

async def cmd_write(args: argparse.Namespace) -> None:
    st = await _init_st(args.network)
    w = await _init_wallet(args.wallet_name, args.wallet_hotkey)
    payload = _make_payload_from_args(args)
    try:
        await _write_payload(st, w, args.netuid, payload)
    except ValueError as ve:
        print(f"ERROR(ValueError): {ve}")
    except Exception as e:
        print(f"ERROR(Exception): {type(e).__name__}: {e}")

async def cmd_read(args: argparse.Namespace) -> None:
    st = await _init_st(args.network)
    w = await _init_wallet(args.wallet_name, args.wallet_hotkey)
    data = await read_plain_commitment(st, netuid=args.netuid, hotkey_ss58=w.hotkey.ss58_address)
    size = len(json.dumps(data).encode("utf-8")) if data is not None else 0
    print(f"read_plain_commitment: bytes={size}")
    print(json.dumps(data, ensure_ascii=False, indent=2))

async def cmd_fuzz(args: argparse.Namespace) -> None:
    """Binary‑search the max `sn` length that fits JSON cap; also show Raw fit for each step."""
    low, high = 1, 64
    best_json = 0
    best_raw = 0
    while low <= high:
        mid = (low + high) // 2
        size_mid = payload_size_bytes(build_payload(snaps=mid, winners_per_snap=args.winners, lines_per_win=args.lines))
        if size_mid <= MAX_COMMIT_BYTES:
            best_json = mid
            low = mid + 1
        else:
            high = mid - 1
    snaps = 1
    while True:
        size_mid = payload_size_bytes(build_payload(snaps=snaps, winners_per_snap=args.winners, lines_per_win=args.lines))
        if size_mid <= RAW_ENUM_SAFE_MAX:
            best_raw = snaps
            snaps += 1
            if snaps > 64:
                break
        else:
            break
    print(
        f"Max snaps (JSON cap): {best_json} | Max snaps (Raw cap {RAW_ENUM_SAFE_MAX}B): {best_raw} "
        f"(winners/snap={args.winners}, lines/win={args.lines})"
    )

async def cmd_write_read_verify(args: argparse.Namespace) -> None:
    """Write a payload, read it back, and print a ✅ if it matches byte‑for‑byte JSON."""
    st = await _init_st(args.network)
    w = await _init_wallet(args.wallet_name, args.wallet_hotkey)
    payload = _make_payload_from_args(args)

    ok = await _write_payload(st, w, args.netuid, payload)
    if not ok:
        print("❌ write failed or refused by guards")
        return

    # Small pause to allow inclusion if needed
    await asyncio.sleep(getattr(args, "sleep_after_write", 0.5))

    fetched = await read_plain_commitment(st, netuid=args.netuid, hotkey_ss58=w.hotkey.ss58_address)
    wrote = _json_dump_compact(payload)
    got = _json_dump_compact(fetched) if fetched is not None else ""

    if wrote == got:
        print("✅ write/read verified: payload matches exactly")
    else:
        print("❌ mismatch between written and fetched payload")
        print("--- wrote ---")
        print(wrote)
        print("--- got ---")
        print(got)

# --------------------------------- cli --------------------------------- #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Plain commitments test tool")
    sub = p.add_subparsers(dest="mode", required=True)

    # shared live args
    def add_live(sp):
        sp.add_argument("--network", required=False, default="ws://127.0.0.1:9944",
                        help="Subtensor endpoint (ws://…)")
        sp.add_argument("--netuid", type=int, required=False, default=0)
        sp.add_argument("--wallet.name", dest="wallet_name", required=False, default="owner")
        sp.add_argument("--wallet.hotkey", dest="wallet_hotkey", required=False, default="validator")
        sp.add_argument("--sleep_after_write", type=float, default=0.5, help="Seconds to wait before read-back in wrv mode")

    # knobs impacting payload size
    def add_size(sp):
        sp.add_argument("--snaps", type=int, default=1, help="Snapshots in ring buffer")
        sp.add_argument("--winners", type=int, default=2, help="Winners per snapshot")
        sp.add_argument("--lines", type=int, default=1, help="Lines per winner (ln entries)")
        sp.add_argument("--profile", choices=["tiny", "normal"], default="normal",
                        help="Use ultra-compact payload to stay under RawN caps")
        sp.add_argument("--with-nonce", dest="with_nonce", nargs="?", const="1", help="Add a tiny nonce for verification")

    s1 = sub.add_parser("size-check", help="Print payload + exact byte size")
    add_size(s1)

    s2 = sub.add_parser("write", help="Write payload to chain (be careful on testnet)")
    add_live(s2)
    add_size(s2)

    s3 = sub.add_parser("read", help="Read your current plain commitment")
    add_live(s3)

    s4 = sub.add_parser("fuzz", help="Find max ring‑buffer snapshots that fit size limit")
    add_live(s4)
    add_size(s4)

    s5 = sub.add_parser("wrv", help="Write → Read → Verify (prints ✅ on success)")
    add_live(s5)
    add_size(s5)

    return p.parse_args()

async def amain():
    args = parse_args()
    if args.mode == "size-check":
        await cmd_size_check(args)
    elif args.mode == "write":
        await cmd_write(args)
    elif args.mode == "read":
        await cmd_read(args)
    elif args.mode == "fuzz":
        await cmd_fuzz(args)
    elif args.mode == "wrv":
        await cmd_write_read_verify(args)
    else:
        raise SystemExit(2)

if __name__ == "__main__":
    asyncio.run(amain())
