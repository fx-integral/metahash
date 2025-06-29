#!/usr/bin/env python3
"""
add_coldkey.py
==============

Add **one** cold‑key entry to *miner.yml*, *under the signer’s hot‑key*.

New YAML layout
---------------
miner.yml now keeps a **mapping** – *hot‑key SS58 → list[ {address, signature} ]*:

    coldkeys:
      <HOTKEY_SS58_A>:
        - address: <COLD_SS58_X>
          signature: <hex‑sig>
        - address: <COLD_SS58_Y>
          signature: <hex‑sig>
      <HOTKEY_SS58_B>:
        - …

Only ``coldkeys:`` is touched; comments and other top‑level keys (``profiles:``, …)
are left exactly as they were.

Example
-------
    python add_coldkey.py --coldkey owner --hotkey miner1 --log-level DEBUG
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import bittensor as bt
import yaml
from dotenv import load_dotenv
from loguru import logger
from metahash.utils.wallet_utils import verify_coldkey

load_dotenv()

# ───────────────────── loguru setup ──────────────────────


def setup_logging(level: str = "INFO") -> None:
    """Configure Loguru once, honouring --log-level."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level.upper(),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
               "| <level>{level:<8}</level> | <level>{message}</level>",
        enqueue=True,
    )

# ──────────────────────── helpers ────────────────────────


def die(msg: str) -> None:
    """Red‑line message and exit."""
    logger.error(f"✗ {msg}")
    sys.exit(1)


def load_wallet(cold: str, hot: str, pw_env: str) -> bt.wallet:
    logger.debug("Loading wallet cold=%s hot=%s", cold, hot)
    pwd = os.getenv(pw_env)
    if not pwd:
        die(f"{pw_env} not set")

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


def read_yaml(path: Path) -> dict:
    """Load the entire miner.yml as a mapping (may be empty)."""
    logger.debug("Reading YAML file %s", path)
    if not path.exists():
        logger.debug("YAML file absent – starting empty")
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as e:  # noqa: BLE001
        die(f"cannot parse {path}: {e}")

    if not isinstance(data, dict):
        die(f"{path} must contain a YAML mapping (use keys like 'coldkeys', 'profiles')")
    return data


def write_yaml(path: Path, doc: dict) -> None:
    """Dump the **entire** YAML document back, keeping original key order."""
    logger.debug("Writing YAML file %s", path)
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    )

# ─────────────────────────── main ────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coldkey", required=True, help="Name of the cold‑key wallet")
    ap.add_argument("--hotkey", required=True, help="Name of the *miner* hot‑key wallet")
    ap.add_argument("--yaml", default="miner.yml", help="Path to miner.yml")
    ap.add_argument("--password", default="WALLET_PASSWORD",
                    help="Environment variable holding the cold‑key password")
    ap.add_argument("--log-level", default="DEBUG",
                    help="Loguru level (DEBUG, INFO, WARNING …)")
    args = ap.parse_args()

    setup_logging(args.log_level)
    logger.info("add_coldkey start (cold=%s hot=%s)", args.coldkey, args.hotkey)

    wallet = load_wallet(args.coldkey, args.hotkey, args.password)
    cold_ss58 = wallet.coldkey.ss58_address
    hot_ss58 = wallet.hotkey.ss58_address     # ⬅ the *miner* hot‑key address

    # ── sign the miner *hot‑key* address with the cold‑key ────────────────
    message = hot_ss58.encode()
    sig_hex = wallet.coldkey.sign(message).hex()
    if not verify_coldkey(cold_ss58, hot_ss58, sig_hex):
        die("signature did NOT verify")
    logger.debug("Signature verified")

    # ── merge / update miner.yml ──────────────────────────────────────────
    yaml_path = Path(args.yaml).expanduser()
    doc = read_yaml(yaml_path)

    # coldkeys: { <hot‑key>: [ {address, signature}, … ] }
    ck_map = doc.setdefault("coldkeys", {})
    if not isinstance(ck_map, dict):
        die(f"'coldkeys' in {yaml_path} must be a YAML mapping keyed by hot‑key")

    entries = ck_map.setdefault(hot_ss58, [])
    if not isinstance(entries, list):
        die(f"'coldkeys.{hot_ss58}' in {yaml_path} must be a list")

    # update or append
    for item in entries:
        if item.get("address") == cold_ss58:
            logger.info("Updating existing entry for cold‑key %s", cold_ss58)
            item["signature"] = sig_hex
            write_yaml(yaml_path, doc)
            logger.success("✓ Updated entry for cold‑key %s under %s",
                           cold_ss58, hot_ss58)
            return

    logger.info("Adding new entry for cold‑key %s under %s", cold_ss58, hot_ss58)
    entries.append({"address": cold_ss58, "signature": sig_hex})
    write_yaml(yaml_path, doc)
    logger.success("✓ Added entry for cold‑key %s under %s", cold_ss58, hot_ss58)


if __name__ == "__main__":
    main()
