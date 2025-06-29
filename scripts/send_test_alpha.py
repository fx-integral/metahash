#!/usr/bin/env python3
"""
send_test_alpha.py – One-shot Alpha transfer helper
"""
import argparse
import asyncio
import os
import sys

import bittensor as bt
from metahash.utils.wallet_utils import transfer_alpha
from dotenv import load_dotenv
from loguru import logger
from metahash.config import TREASURY_COLDKEY

load_dotenv()


# ───────────────────────── logging ──────────────────────────


def setup_logging(level: str = "INFO") -> None:
    """
    Configure Loguru **once** and then point Bittensor’s helper
    (`bt.logging`) at the very same Loguru logger.
    """
    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
        "| <level>{level:<8}</level> | <level>{message}</level>"
    )
    # Wipe any default sinks and add our own
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), format=fmt, enqueue=True)

    # Monkey-patch: make every bt.logging.* call hit Loguru too
    bt.logging = logger


def die(msg: str) -> None:
    logger.error(f"✗ {msg}")
    sys.exit(1)


def load_wallet(cold: str, hot: str) -> bt.wallet:
    logger.debug("Loading wallet cold=%s hot=%s", cold, hot)
    pwd = os.getenv("WALLET_PASSWORD")
    if not pwd:
        die("WALLET_PASSWORD not set")

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
    # 1. Connect
    subtensor = bt.AsyncSubtensor(network=args.network)
    await subtensor.initialize()

    # 2. Wallet
    wallet = load_wallet(args.wallet_name, args.wallet_hotkey)

    dest = args.dest_coldkey or TREASURY_COLDKEY
    logger.info(  # ← now logger, not bt.logging
        "Sending {amt} α from {src} to {dst} (origin-netuid={nid})…",
        amt=args.amount,
        src=wallet.coldkey.ss58_address,
        dst=dest,
        nid=args.origin_netuid,
    )

    # 3. Transfer
    ok = await transfer_alpha(
        subtensor=subtensor,
        wallet=wallet,
        hotkey_ss58=wallet.hotkey.ss58_address,
        origin_netuid=args.origin_netuid,
        dest_coldkey_ss58=dest,
        amount=bt.Balance.from_tao(args.amount),
        wait_for_inclusion=True,
        wait_for_finalization=args.wait_final,
    )

    if not ok:
        logger.error("Alpha transfer failed")  # ← logger
        sys.exit(1)
    current_block = await subtensor.get_current_block()
    logger.success(f"✓ Alpha transfer included in block {current_block} 🎉")  # ← logger


# ────────────────────────── CLI ────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Send Alpha from any wallet to a coldkey")
    p.add_argument("--wallet-name", required=True)
    p.add_argument("--wallet-hotkey", required=True)
    p.add_argument("--dest-coldkey")
    p.add_argument("--amount", type=float, default=10)
    p.add_argument(
        "--origin-netuid",
        type=int,
        default=int(os.getenv("ORIGIN_NETUID", "348")),
    )
    p.add_argument("--network", default="test")
    p.add_argument("--wait-final", action="store_true")
    p.add_argument("--log-level", default="DEBUG", help="DEBUG, INFO, WARNING …")
    args = p.parse_args()

    # configure sinks *before* anything logs
    setup_logging(args.log_level)
    asyncio.run(run(args))
