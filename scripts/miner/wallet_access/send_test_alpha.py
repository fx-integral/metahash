#!/usr/bin/env python3
"""
send_test_alpha.py – One-shot Alpha transfer helper

Usage example:
  export WALLET_PASSWORD='<your-coldkey-password>'
  python send_test_alpha.py \
    --coldkey owner \
    --hotkey miner2 \
    --validator_hotkey 1eqXoBtUBZ4mcKTSaM7Scgv6At96GEqfmPnHSSxh \
    --amount 10 \
    --netuid 348 \
    --network ws://128.140.68.225:11144 \
    --wait-final \
    --log-level DEBUG
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import bittensor as bt
from dotenv import load_dotenv
from loguru import logger

from metahash.config import DEFAULT_BITTENSOR_NETWORK
from metahash.utils.wallet_utils import transfer_alpha

TREASURY_COLDKEY = "5DLULtxCS9vA3pZRgSTd9gkvGrUwgNje2TNQeLibo9wUWjSS"


load_dotenv()


# ───────────────────────── logging ──────────────────────────
def setup_logging(level: str = "INFO") -> None:
    """
    Configure Loguru **once** and then point Bittensor’s helper
    (bt.logging) at the very same Loguru logger.
    """
    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
        "| <level>{level:<8}</level> | <level>{message}</level>"
    )
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=fmt, enqueue=True)
    # Bridge bt.logging to loguru for consistent output
    bt.logging = logger


def die(msg: str) -> None:
    logger.error(f"✗ {msg}")
    sys.exit(1)


def load_wallet(cold: str, hot: str) -> bt.wallet:
    logger.debug("Loading wallet cold=%s hot=%s", cold, hot)
    pwd = os.getenv("WALLET_PASSWORD")
    if not pwd:
        die("WALLET_PASSWORD not set (export it or put it in .env)")

    w = bt.wallet(name=cold, hotkey=hot)
    w.coldkey_file.save_password_to_env(pwd)
    try:
        w.unlock_coldkey()
    except Exception as e:  # noqa: BLE001
        die(f"cannot unlock cold-key: {e}")

    logger.debug(
        "Wallet unlocked (cold=%s hot=%s)",
        w.coldkey.ss58_address,
        w.hotkey.ss58_address,
    )
    return w


async def run(args: argparse.Namespace) -> None:
    # 1) Connect
    subtensor = bt.AsyncSubtensor(network=args.network)
    await subtensor.initialize()

    # 2) Wallet
    wallet = load_wallet(args.coldkey, args.hotkey)

    # 3) Resolve dest
    dest = args.dest or TREASURY_COLDKEY
    logger.info(
        "Sending {amt} α from {src} to {dst} (origin-netuid={nid})…",
        amt=args.amount,
        src=wallet.coldkey.ss58_address,
        dst=dest,
        nid=args.netuid,
    )

    # 4) Transfer
    ok = await transfer_alpha(
        subtensor=subtensor,
        wallet=wallet,
        hotkey_ss58=args.validator_hotkey,
        origin_and_dest_netuid=args.netuid,
        dest_coldkey_ss58=dest,
        amount=bt.Balance.from_tao(args.amount),
        # Only wait (subscribe) when user asked for --wait-final
        wait_for_inclusion=args.wait_final,
        wait_for_finalization=args.wait_final,
        period=512,
        max_retries=3,
    )

    if not ok:
        logger.error("Alpha transfer failed")
        sys.exit(1)

    current_block = await subtensor.get_current_block()
    logger.success(f"✓ Alpha transfer submitted (current block {current_block}) 🎉")


# ────────────────────────── CLI ────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Send Alpha from any wallet to a coldkey")
    p.add_argument("--coldkey", required=True, help="Wallet name (cold)")
    p.add_argument("--hotkey", required=True, help="Wallet hotkey name")
    p.add_argument("--validator_hotkey", required=True, help="Validator hotkey SS58")
    p.add_argument("--dest", default=TREASURY_COLDKEY, help="Destination coldkey SS58")
    p.add_argument("--amount", type=float, default=10, help="Alpha amount (in τ units)")
    p.add_argument("--netuid", type=int, required=True, help="Origin/Destination netuid")
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK, help="Subtensor WS url or known network")
    p.add_argument("--wait-final", action="store_true", help="Wait for inclusion/finalization")
    p.add_argument("--log-level", default="DEBUG", help="DEBUG, INFO, WARNING …")
    args = p.parse_args()

    setup_logging(args.log_level)
    asyncio.run(run(args))
