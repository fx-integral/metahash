# ====================================================================== #
# metahash/utils/wallet_utils.py                                         #
# ====================================================================== #


from __future__ import annotations

import sys
from typing import Optional, Sequence, Union

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

    # SR25519 primero, ED25519 como respaldo
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
    Comprueba que cada firma sea válida.

    Parameters
    ----------
    entries
        Secuencia de dicts con ``address`` (cold‑key SS58) y ``signature`` (hex).
    message
        **Nuevo (opcional)**.  Si se pasa, *todas* las firmas se verifican contra
        ese mensaje (p. ej. el hot‑key del miner).  
        Si se deja en ``None`` se usa, como antes, la propia dirección del
        cold‑key.
    """
    verified: list[dict] = []

    # Normalizar el mensaje una sola vez
    if message is not None and isinstance(message, str):
        message_bytes = message.encode()
    else:
        message_bytes = None  # se calculará por entrada

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

    clog.success(f"✓ All {len(verified)} cold‑keys verified", color="green")
    return verified


async def transfer_alpha(
    *,
    subtensor: AsyncSubtensor,
    wallet,                      # bittensor.wallet (already unlocked)
    hotkey_ss58: str,
    origin_netuid: int,
    dest_coldkey_ss58: str,
    amount,
    dest_netuid: Optional[int] = None,
    wait_for_inclusion: bool = True,
    wait_for_finalization: bool = False,
    period: int = 256,           # safer default on slow nodes
) -> bool:
    """
    Convenience wrapper around `AsyncSubtensor.transfer_stake`.

    Only the arguments that differ from the default template are exposed here.
    """
    return await subtensor.transfer_stake(
        wallet=wallet,
        destination_coldkey_ss58=dest_coldkey_ss58,
        hotkey_ss58=hotkey_ss58,
        origin_netuid=origin_netuid,
        destination_netuid=dest_netuid or origin_netuid,
        amount=amount,
        wait_for_inclusion=wait_for_inclusion,
        wait_for_finalization=wait_for_finalization,
        period=period,
    )
