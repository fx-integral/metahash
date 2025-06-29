# =====================
# scripts/test_alpha_scanner.py
# ============================================================================
"""
Integration test for metahash.validator.alpha_transfers.AlphaTransfersScanner.

Usage example
-------------
$ python3 scripts/test_alpha_scanner.py \
      --network finney \
      --wallet $WALLET_NAME \
      --hotkey $WALLET_HOTKEY \
      --pwd-env WALLET_PASSWORD \
      --treasury 5DLULtxCS9vA3pZRgSTd9gkvGrUwgNje2TNQeLibo9wUWjSS \
      --amount 0.1 \
      --subnet 348 \
      --dest-netuid 348

Exit status is 0 on success (transfer detected) and 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from typing import Sequence

from dotenv import load_dotenv
from loguru import logger
from tqdm.auto import tqdm

import bittensor as bt
from substrateinterface.utils.ss58 import ss58_decode as _ss58_decode_generic

# --------------------------------------------------------------------------- #
#  Project imports – adjust paths if you store code differently
# --------------------------------------------------------------------------- #
from metahash.validator.alpha_transfers import AlphaTransfersScanner, TransferEvent
from metahash.utils.wallet_utils import transfer_alpha   # ← must exist in your repo

load_dotenv()
bt.logging.set_debug(True)
# ╭───────────────────── Helper dataclass for expectations ───────────────────╮ #


@dataclass(slots=True, frozen=True)
class ExpectedTransfer:
    """
    A minimal spec we try to find in the list of TransferEvent objects.
    We match on *raw* AccountId bytes (format-agnostic) and on the amount
    in rao (±10 % tolerance to absorb fee-dust or emissions).
    """
    dest_coldkey: str
    amount_rao: int
    tolerance: float = 0.10          # 10 %

    # -- helpers --------------------------------------------------------- #
    @staticmethod
    def _raw(addr: str) -> bytes:
        """Return the 32-byte AccountId for any valid SS58 string."""
        try:                                  # substrate-interface ≥ 2024
            return _ss58_decode_generic(addr)
        except TypeError:                     # very old wheels
            return _ss58_decode_generic(addr, valid_ss58_format=True)

    # -- public API ------------------------------------------------------ #
    def matches(self, ev: TransferEvent) -> bool:
        if ev.dest_coldkey_raw is None:       # corrupt / malformed event
            return False

        # prefix-agnostic comparison
        if self._raw(self.dest_coldkey) != ev.dest_coldkey_raw:
            return False

        # allow ±10 % slippage on the amount
        lower = self.amount_rao * (1 - self.tolerance)
        upper = self.amount_rao * (1 + self.tolerance)
        return lower <= ev.amount_rao <= upper


# ╭────────────────────────────── CLI parsing ───────────────────────────────╮ #
parser = argparse.ArgumentParser(
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    description="End‑to‑end test: send alpha then verify scanner sees it",
)

# network / windows
parser.add_argument("--network", default="finney")

parser.add_argument("--scan-back", type=int, default=2,
                    help="How many blocks back from current tip to start scan")
parser.add_argument("--wait-finalization", type=float, default=20.0,
                    help="Seconds to wait between inclusion and finalization check")
parser.add_argument("--timeout", type=float, default=120.0,
                    help="Total seconds to wait for transferred event to appear")

# treasury / amount
parser.add_argument("--treasury",
                    default="5DLULtxCS9vA3pZRgSTd9gkvGrUwgNje2TNQeLibo9wUWjSS")
parser.add_argument("--amount", type=float, default=0.1,
                    help="α (in TAO) to send in the test transfer")
parser.add_argument("--subnet", type=int, default=348)
parser.add_argument("--dest-netuid", type=int, default=348)

# wallet secrets
parser.add_argument("--wallet", default=os.getenv("WALLET_NAME"))
parser.add_argument("--hotkey", default=os.getenv("WALLET_HOTKEY"))
parser.add_argument("--pwd-env", default="WALLET_PASSWORD")

# log / verbosity
parser.add_argument("--log-level", default="INFO")

args = parser.parse_args()
logger.remove()
logger.add(sys.stderr, level=args.log_level.upper())

# ╭────────────────────────────── Utilities ─────────────────────────────────╮ #


def _wallet(name: str, hot: str, env: str) -> bt.wallet:
    w = bt.wallet(name=name, hotkey=hot)
    pwd = os.getenv(env)
    if not pwd:
        logger.error("password env‑var {} missing", env)
        sys.exit(1)
    w.coldkey_file.save_password_to_env(pwd)
    w.unlock_coldkey()
    return w


def _api_tip(st: bt.Subtensor) -> int:
    for fn in ("get_current_block", "get_current_block_number", "block"):
        if hasattr(st, fn):
            h = getattr(st, fn)
            h = h() if callable(h) else h
            return int(asyncio.run(h) if asyncio.iscoroutine(h) else h)
    raise RuntimeError("No usable tip method on subtensor object")


async def _wait_for_finalization(
    st: bt.Subtensor | bt.AsyncSubtensor,
    start_height: int,
    *, window: int = 1,
) -> int:
    """
    Poll tip until at least window blocks have been built on top of start_height.
    Returns the final tip height.
    """
    while True:
        tip = _api_tip(st) if isinstance(st, bt.Subtensor) else await st.block()
        if tip - start_height >= window:
            return tip
        logger.info("Waiting for finality… tip {} (need ≥ {})", tip, start_height + window)
        await asyncio.sleep(args.wait_finalization)


# ╭────────────────────────────── Core logic ────────────────────────────────╮ #
async def _main() -> None:
    st = bt.Subtensor(network=args.network)
    tip_before = _api_tip(st)
    logger.info("Chain tip before transfer: {}", tip_before)

    # 1.  Build wallet & broadcast test transfer --------------------------------
    transfer_amount_tao = args.amount
    transfer_amount_rao = int(transfer_amount_tao * 1e9)           # 1 TAO = 1 000 000 000 rao
    expected = ExpectedTransfer(args.treasury, transfer_amount_rao)

    async_sub = await bt.AsyncSubtensor(network=args.network).initialize()
    wlt = _wallet(args.wallet, args.hotkey, args.pwd_env)

    logger.info("Broadcasting α transfer: {} TAO to {}", transfer_amount_tao, args.treasury)
    await transfer_alpha(
        subtensor=async_sub,
        wallet=wlt,
        hotkey_ss58=wlt.hotkey.ss58_address,
        origin_netuid=args.subnet,
        dest_coldkey_ss58=args.treasury,
        dest_netuid=args.dest_netuid,
        amount=transfer_amount_tao,
        wait_for_inclusion=True,
        wait_for_finalization=False,  # we'll confirm manually
    )
    inclusion_height = _api_tip(st)
    logger.success("Transfer included in block {}", inclusion_height)

    # 2.  Wait for finality window (default 12 blk) -----------------------------
    final_tip = await _wait_for_finalization(st, inclusion_height)
    logger.success("Block {} is now finalized (tip {})", inclusion_height, 12)

    # 3.  Scan window [tip‑scan_back, tip] using AlphaTransfersScanner ----------
    frm = max(1, final_tip - args.scan_back)
    to = final_tip
    logger.info("Scanning blocks {}→{} for StakeTransferred events", frm, to)

    bar = tqdm(total=to - frm + 1, unit="blk", leave=False)

    def _progress(blks_scanned: int, ev_seen: int, kept: int):
        bar.n = blks_scanned
        bar.set_postfix(ev=ev_seen, kept=kept)
        bar.refresh()

    scanner = AlphaTransfersScanner(
        st,
        dest_coldkey=args.treasury,
        on_progress=_progress,
        dump_events=True,
        dump_last=10,
    )

    start = time.perf_counter()
    transfers: Sequence[TransferEvent] = await scanner.scan(frm, to)

    elapsed = time.perf_counter() - start
    bar.close()
    logger.info("Scan completed in {:.2f}s (≈ {:.4f}s/blk)", elapsed, elapsed / (to - frm + 1))

    # 4.  Verify we detect exactly *one* matching transfer ----------------------
    hits = [t for t in transfers if expected.matches(t)]
    if len(hits) == 1:
        t = hits[0]
        logger.success(
            "✅ Transfer detected! blk={}  from_uid={}  to_uid={}  "
            "dest={}  amount_rao={:,}",
            t.block, t.from_uid, t.to_uid, t.dest_coldkey, t.amount_rao,
        )
        sys.exit(0)
    elif len(hits) == 0:
        logger.error("❌ No matching transfer found in scanned window.")
    else:  # > 1
        logger.error("❌ Expected exactly one match, found {} duplicates:", len(hits))
        for t in hits:
            logger.error("    blk={} uid_from={} uid_to={} amt={:,}",
                         t.block, t.from_uid, t.to_uid, t.amount_rao)

    # optional dump of all kept events for debugging
    if transfers:
        logger.info("Kept events:")
        for t in transfers:
            logger.info("  blk={} uid_from={}→{} amt={:,} dest={}",
                        t.block, t.from_uid, t.to_uid, t.amount_rao, t.dest_coldkey)

    sys.exit(1)   # non‑zero means test failed


# ╭─────────────────────────────── Entrypoint ───────────────────────────────╮ #
if __name__ == "__main__":
    try:
        asyncio.run(_main())
    finally:
        # Avoid spurious SSL tracebacks on interpreter shutdown
        pass
