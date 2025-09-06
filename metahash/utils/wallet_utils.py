# ====================================================================== #
# metahash/utils/wallet_utils.py                                         #
# ====================================================================== #

from __future__ import annotations

import asyncio
import os
import sys
from typing import Sequence, Union

import bittensor as bt
from bittensor import AsyncSubtensor
from substrateinterface import Keypair, KeypairType

from metahash.base.utils.logging import ColoredLogger as clog


def verify_coldkey(
    cold_ss58: str,
    message: Union[str, bytes],
    signature_hex: str,
) -> bool:
    if isinstance(message, str):
        message = message.encode()

    sig = bytes.fromhex(signature_hex)

    # Try SR25519 first, ED25519 as fallback
    for crypto in (KeypairType.SR25519, KeypairType.ED25519):
        try:
            kp = Keypair(ss58_address=cold_ss58, crypto_type=crypto)
            if kp.verify(message, sig):
                return True
        except Exception:
            pass
    return False


def check_coldkeys_and_signatures(
    entries: Sequence[dict],
    *,
    message: Union[str, bytes] | None = None,
) -> list[dict]:
    """
    Verifies each signature.

    Parameters
    ----------
    entries
        Sequence of dicts with ``address`` (cold-key SS58) and ``signature`` (hex).
    message
        If provided, **all** signatures are verified against this message (e.g. miner hotkey).
        If ``None``, the cold-key address itself is used as the signed message.
    """
    verified: list[dict] = []

    # Normalize message (if any) once
    if message is not None and isinstance(message, str):
        message_bytes = message.encode()
    else:
        message_bytes = None  # computed per entry

    for idx, item in enumerate(entries, 1):
        addr = item.get("address") or item.get("coldkey")
        sig_hex = item.get("signature")

        if not addr or not sig_hex:
            missing = "address" if not addr else "signature"
            clog.error(f"Entry #{idx}: missing {missing}", color="red")
            sys.exit(1)

        mbytes = message_bytes or addr.encode()
        if not verify_coldkey(addr, mbytes, sig_hex):
            clog.error(
                f"Entry #{idx}: INVALID signature for cold-key {addr}", color="red"
            )
            sys.exit(1)

        verified.append({"address": addr, "signature": sig_hex})

    clog.success(f"✓ All {len(verified)} cold-keys verified", color="green")
    return verified


async def transfer_alpha(
    *,
    subtensor: AsyncSubtensor,
    wallet,                      # bittensor.wallet (already unlocked)
    hotkey_ss58: str,
    origin_and_dest_netuid: int,
    dest_coldkey_ss58: str,
    amount,
    wait_for_inclusion: bool = True,
    wait_for_finalization: bool = False,
    period: int = 512,           # safer default on slow nodes
    max_retries: int = 3,        # retry on subscription loss
) -> bool:
    """
    Submit a SubtensorModule.transfer_stake extrinsic and (optionally) wait.

    We work around async-substrate subscription flakiness by catching the
    KeyError raised when a subscription id vanishes (WS hiccup), reconnecting,
    and retrying the submission up to `max_retries` times.
    """
    bt.logging.info(
        f"Transferring stake from coldkey [blue]{wallet.coldkeypub.ss58_address}[/blue] to coldkey "
        f"[blue]{dest_coldkey_ss58}[/blue]\n"
        f"Amount: [green]{amount}[/green] from netuid [yellow]{origin_and_dest_netuid}[/yellow] to netuid "
        f"[yellow]{origin_and_dest_netuid}[/yellow]"
    )

    # Compose call once; it’s fine to reuse across retries
    call = await subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="transfer_stake",
        call_params={
            "destination_coldkey": dest_coldkey_ss58,  # AccountId
            "hotkey": hotkey_ss58,                     # AccountId
            "origin_netuid": origin_and_dest_netuid,   # u16
            "destination_netuid": origin_and_dest_netuid,
            "alpha_amount": amount.rao,                # u64
        },
    )

    for attempt in range(1, max_retries + 1):
        try:
            success, err_msg = await subtensor.sign_and_send_extrinsic(
                call=call,
                wallet=wallet,
                wait_for_inclusion=wait_for_inclusion,
                wait_for_finalization=wait_for_finalization,
                period=period,
            )
            if success:
                if wait_for_inclusion or wait_for_finalization:
                    bt.logging.success(":white_heavy_check_mark: [green]Included[/green]")
                return True
            else:
                bt.logging.error(f":cross_mark: [red]Failed[/red]: {err_msg}")
                return False

        except KeyError as e:
            # Lost subscription id (WS hiccup). Reconnect + retry.
            bt.logging.warning(
                f"Subscription lost (attempt {attempt}/{max_retries}): {e}. Reconnecting…"
            )
            try:
                await subtensor.substrate.close()
            except Exception:
                pass
            await asyncio.sleep(0.8)
            await subtensor.initialize()
            continue

        except Exception as e:
            bt.logging.error(f":cross_mark: [red]Failed[/red]: {str(e)}")
            raise

    bt.logging.error(":cross_mark: [red]Failed[/red]: subscription retries exhausted")
    return False


def load_wallet(coldkey_name: str, hotkey_name: str, unlock: bool = True):
    """
    Convenience loader used by other scripts.
    """
    bt.logging.debug(f"Loading wallet coldkey: {coldkey_name} hotkey: {hotkey_name}")
    w = bt.wallet(name=coldkey_name, hotkey=hotkey_name)

    if unlock:
        pwd = os.getenv("WALLET_PASSWORD")
        if not pwd:
            bt.logging.error("WALLET_PASSWORD not set in .env or env")
            return None
        try:
            w.coldkey_file.save_password_to_env(pwd)
            w.unlock_coldkey()
        except Exception as e:  # noqa: BLE001
            bt.logging.error(f"cannot unlock cold-key: {e}")
            raise Exception(
                "Unable to unlock wallet with: coldkey name: {cold}, hotkey name: {hot}"
            )

        bt.logging.debug(f"Wallet unlocked {w.coldkey.ss58_address} {w.hotkey.ss58_address}")

    return w
