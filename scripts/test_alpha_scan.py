# ============================================================================
# scripts/test_alpha_scanner.py
# ============================================================================
# End‑to‑end integration test for
# `metahash.validator.alpha_transfers.AlphaTransfersScanner`.
#
# The script:
#   1. Builds (or unlocks) a wallet.
#   2. Sends α‑stake (`transfer_alpha`) from that wallet to the treasury
#      cold‑key on a chosen subnet.
#   3. Waits until the inclusion block is finalised.
#   4. Runs AlphaTransfersScanner over the finalised window
#      `[tip‑scan_back, tip]`.
#   5. Verifies that exactly one matching StakeTransferred event
#      (cold‑key + amount) is detected.
#
# Exit status is 0 on success, 1 otherwise.
#
# Example:
#   python3 scripts/test_alpha_scanner.py \
#       --network finney \
#       --wallet $WALLET_NAME \
#       --hotkey $WALLET_HOTKEY \
#       --pwd-env WALLET_PASSWORD \
#       --treasury 5DLULtxCS9vA3pZRgSTd9gkvGrUwgNje2TNQeLibo9wUWjSS \
#       --amount 0.1 \
#       --subnet 348 \
#       --dest-netuid 348
# ============================================================================

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass

from dotenv import load_dotenv
from loguru import logger
from tqdm.auto import tqdm

import bittensor as bt
from substrateinterface.utils.ss58 import ss58_decode as _ss58_decode_generic

# --------------------------------------------------------------------------- #
#  Internal project imports – adjust PYTHONPATH if your layout differs
# --------------------------------------------------------------------------- #
from metahash.validator.alpha_transfers import (
    AlphaTransfersScanner,
    TransferEvent,
)
from metahash.utils.wallet_utils import transfer_alpha  # must exist in your repo

# --------------------------------------------------------------------------- #
#  Environment & logging
# --------------------------------------------------------------------------- #
load_dotenv()
bt.logging.set_debug(True)

# ╭────────────────────────────── CLI ───────────────────────────────────────╮ #
parser = argparse.ArgumentParser(
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    description="Send α then verify the AlphaTransfersScanner detects it.",
)

# — network / timing —
parser.add_argument("--network", default="finney", help="Bittensor network")
parser.add_argument(
    "--scan-back",
    type=int,
    default=2,
    help="Blocks back from tip to start scan window",
)
parser.add_argument(
    "--wait-finalization",
    type=float,
    default=20.0,
    help="Seconds between inclusion check and finality polling",
)
parser.add_argument(
    "--timeout",
    type=float,
    default=300.0,
    help="Hard timeout (s) for the whole test run",
)

# — treasury / transfer —
parser.add_argument(
    "--treasury",
    default="5DLULtxCS9vA3pZRgSTd9gkvGrUwgNje2TNQeLibo9wUWjSS",
    help="Treasury cold‑key to receive α",
)
parser.add_argument(
    "--amount",
    type=float,
    default=0.1,
    help="α (TAO) to transfer for the test",
)
parser.add_argument("--subnet", type=int, default=348, help="Sender subnet id")
parser.add_argument(
    "--dest-netuid",
    type=int,
    default=348,
    help="Destination subnet id on Dynamic‑TAO",
)

# — wallet secrets —
parser.add_argument(
    "--wallet",
    default=os.getenv("WALLET_NAME"),
    help="Cold‑key name",
)
parser.add_argument(
    "--hotkey",
    default=os.getenv("WALLET_HOTKEY"),
    help="Hot‑key name",
)
parser.add_argument(
    "--pwd-env",
    default="WALLET_PASSWORD",
    help="Env‑var holding the wallet password",
)

# — logging —
parser.add_argument(
    "--log-level",
    default="INFO",
    help="Loguru verbosity (TRACE|DEBUG|INFO|...)",
)

args = parser.parse_args()
logger.remove()
logger.add(sys.stderr, level=args.log_level.upper())

# ╭──────────────────── Helper dataclass for expectation ─────────────────────╮ #


@dataclass(slots=True, frozen=True)
class ExpectedTransfer:
    """
    Spec we look for among `TransferEvent` objects.

    Matching is prefix‑agnostic (raw AccountId comparison) and allows
    ±10 % tolerance on the transferred amount to absorb fee dust.
    """

    dest_coldkey: str
    amount_rao: int
    tolerance: float = 0.10  # 10 %

    # -- helpers --------------------------------------------------------- #
    @staticmethod
    def _raw(addr: str) -> bytes:
        """Return 32‑byte AccountId for any valid SS58 string."""
        try:
            return _ss58_decode_generic(addr)
        except TypeError:  # very old substrate‑interface wheels
            return _ss58_decode_generic(addr, valid_ss58_format=True)

    # -- public API ------------------------------------------------------ #
    def matches(self, ev: TransferEvent) -> bool:
        if ev.dest_coldkey_raw is None:  # corrupt / malformed
            return False

        # SS58‑prefix‑agnostic equality
        if self._raw(self.dest_coldkey) != ev.dest_coldkey_raw:
            return False

        # ±10 % on amount
        lower = self.amount_rao * (1 - self.tolerance)
        upper = self.amount_rao * (1 + self.tolerance)
        return lower <= ev.amount_rao <= upper


# ╭───────────────────────── Wallet & chain helpers ──────────────────────────╮ #
def _wallet(name: str, hot: str, env: str) -> bt.wallet:
    w = bt.wallet(name=name, hotkey=hot)
    pwd = os.getenv(env)
    if not pwd:
        logger.error("Password env‑var '{}' is missing", env)
        sys.exit(1)
    w.coldkey_file.save_password_to_env(pwd)
    w.unlock_coldkey()
    return w


def _chain_tip(st: bt.Subtensor) -> int:
    """
    Cross‑version helper: returns current chain height from any Subtensor.
    """
    for fn in ("get_current_block", "get_current_block_number", "block"):
        if hasattr(st, fn):
            h = getattr(st, fn)
            h = h() if callable(h) else h
            return int(asyncio.run(h) if asyncio.iscoroutine(h) else h)
    raise RuntimeError("No usable tip method found on Subtensor")


async def _wait_for_finality(
    st: bt.Subtensor | bt.AsyncSubtensor,
    target_height: int,
    window: int = 1,
) -> int:
    """
    Poll the chain until `window` blocks have built on top of `target_height`.
    Returns the final tip height.
    """
    while True:
        tip = (
            _chain_tip(st)
            if isinstance(st, bt.Subtensor)
            else await st.block()
        )
        if tip - target_height >= window:
            return tip
        logger.info(
            "Waiting for finality… tip={} (need ≥ {})",
            tip,
            target_height + window,
        )
        await asyncio.sleep(args.wait_finalization)


# ╭────────────────────────────── Core routine ───────────────────────────────╮ #
async def _run_test() -> None:
    # ---------- 1.  Init chain connection & wallet ----------------------------
    st = bt.Subtensor(network=args.network)
    tip_before = _chain_tip(st)
    logger.info("Chain tip (pre‑transfer): {}", tip_before)

    wlt = _wallet(args.wallet, args.hotkey, args.pwd_env)
    async_st = await bt.AsyncSubtensor(network=args.network).initialize()

    # ---------- 2.  Broadcast α transfer --------------------------------------
    amt_tao = args.amount
    amt_rao = int(amt_tao * 1e9)  # 1 TAO = 1 000 000 000 rao
    expected = ExpectedTransfer(args.treasury, amt_rao)

    logger.info("Broadcasting α transfer: {} TAO → {}", amt_tao, args.treasury)
    await transfer_alpha(
        subtensor=async_st,
        wallet=wlt,
        hotkey_ss58=wlt.hotkey.ss58_address,
        origin_netuid=args.subnet,
        dest_coldkey_ss58=args.treasury,
        dest_netuid=args.dest_netuid,
        amount=amt_tao,
        wait_for_inclusion=True,
        wait_for_finalization=False,  # we’ll handle finality manually
    )
    inclusion_height = _chain_tip(st)
    logger.success("Transfer included at block {}", inclusion_height)

    # ---------- 3.  Wait for finality window ----------------------------------
    final_tip = await _wait_for_finality(st, inclusion_height)
    logger.success("Block {} is finalized (tip={})", inclusion_height, final_tip)

    # ---------- 4.  Scan window [tip‑scan_back, tip] ---------------------------
    frm = max(1, final_tip - args.scan_back)
    to = final_tip
    logger.info("Scanning blocks {} → {} for StakeTransferred events", frm, to)

    bar = tqdm(total=to - frm + 1, unit="blk", leave=False)

    def _progress(blk_cnt: int, ev_seen: int, kept: int):
        bar.n = blk_cnt
        bar.set_postfix(ev=ev_seen, kept=kept)
        bar.refresh()

    scanner = AlphaTransfersScanner(
        st,
        dest_coldkey=args.treasury,
        on_progress=_progress,
        dump_events=True,
        dump_last=10,
    )

    start_scan = time.perf_counter()
    transfers = await scanner.scan(frm, to)
    elapsed = time.perf_counter() - start_scan
    bar.close()
    logger.info(
        "Scan completed in {:.2f}s (≈{:.4f}s/blk)",
        elapsed,
        elapsed / (to - frm + 1),
    )

    # ---------- 5.  Verify results --------------------------------------------
    hits = [t for t in transfers if expected.matches(t)]

    if len(hits) == 1:
        t = hits[0]
        logger.success(
            "✅ Transfer detected! blk={}  uid_from={}  uid_to={}  "
            "dest={}  amount_rao={:,}",
            t.block,
            t.from_uid,
            t.to_uid,
            t.dest_coldkey,
            t.amount_rao,
        )
        sys.exit(0)

    if len(hits) == 0:
        logger.error("❌ No matching transfer found in scanned window.")
    else:
        logger.error(
            "❌ Expected exactly one match, found {} duplicates:", len(hits)
        )
        for t in hits:
            logger.error(
                "    blk={} uid_from={}→{} amt={:,}",
                t.block,
                t.from_uid,
                t.to_uid,
                t.amount_rao,
            )

    # Optional dump of *all* kept events for debugging
    if transfers:
        logger.info("Kept events (dest cold‑key matches):")
        for t in transfers:
            logger.info(
                "  blk={} uid_from={}→{} amt={:,} dest={}",
                t.block,
                t.from_uid,
                t.to_uid,
                t.amount_rao,
                t.dest_coldkey,
            )

    sys.exit(1)  # non‑zero => test failure


# ╭────────────────────────────── Entrypoint ───────────────────────────────╮ #
if __name__ == "__main__":
    try:
        if args.timeout:
            asyncio.run(
                asyncio.wait_for(_run_test(), timeout=args.timeout)
            )
        else:
            asyncio.run(_run_test())
    except asyncio.TimeoutError:
        logger.error("❌ Timed out after {} s", args.timeout)
        sys.exit(1)
    finally:
        # Avoid spurious SSL tracebacks on interpreter shutdown
        pass
