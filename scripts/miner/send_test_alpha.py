#!/usr/bin/env python3
"""
send_test_alpha.py – One-shot Alpha transfer helper
Now with secure password management!
"""
import argparse
import asyncio
import sys

import bittensor as bt
from metahash.utils.wallet_utils import transfer_alpha
from metahash.utils.secure_wallet import load_wallet_secure
from dotenv import load_dotenv
from loguru import logger
from metahash.config import TREASURY_COLDKEY
from metahash.config import (
    DEFAULT_BITTENSOR_NETWORK,
)


load_dotenv()


# ───────────────────────── logging ──────────────────────────


def setup_logging(level: str = "INFO") -> None:
    """
    Configure Loguru **once** and then point Bittensor's helper
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


async def run(args: argparse.Namespace) -> None:
    # 1. Connect
    subtensor = bt.AsyncSubtensor(network=args.network)
    await subtensor.initialize()

    # 2. Wallet - using secure manager
    try:
        wallet = load_wallet_secure(args.coldkey, args.hotkey)
    except Exception as e:
        die(f"Failed to load wallet: {e}")

    dest = args.dest or TREASURY_COLDKEY
    logger.info(  # ← now logger, not bt.logging
        "Sending {amt} α from {src} to {dst} (origin-netuid={nid})…",
        amt=args.amount,
        src=wallet.coldkey.ss58_address,
        dst=dest,
        nid=args.netuid,
    )

    # 3. Transfer
    ok = await transfer_alpha(
        subtensor=subtensor,
        wallet=wallet,
        hotkey_ss58=args.validator_hotkey,
        origin_and_dest_netuid=args.netuid,
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
    p = argparse.ArgumentParser(
        description="Send Alpha from any wallet to a coldkey (secure version)"
    )
    p.add_argument("--coldkey", required=True, help="Coldkey name")
    p.add_argument("--hotkey", required=True, help="Hotkey name")
    p.add_argument("--validator_hotkey", required=True, help="Validator hotkey address")
    p.add_argument("--dest", default=TREASURY_COLDKEY, help="Destination coldkey")
    p.add_argument("--amount", type=float, default=10, help="Amount of ALPHA to send")
    p.add_argument("--netuid", type=int, help="Subnet ID")
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK, help="Network to use")
    p.add_argument("--wait-final", action="store_true", help="Wait for finalization")
    p.add_argument("--log-level", default="DEBUG", help="DEBUG, INFO, WARNING …")
    
    args = p.parse_args()

    # configure sinks *before* anything logs
    setup_logging(args.log_level)
    
    print("\n🔒 Secure Wallet Manager Active")
    print("📝 You will be prompted for password if not stored in keyring\n")
    
    asyncio.run(run(args))
