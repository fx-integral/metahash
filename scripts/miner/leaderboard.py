#!/usr/bin/env python3
# --------------------------------------------------------------------------- #
#  auction_view.py – minimal subnet‑auction dashboard (Rich‑based)            #
# --------------------------------------------------------------------------- #
"""
Show:

• Current block, epoch range, and how many blocks are left in the auction.
• A Rich table:  UID · Coldkey · Hotkey · Reward (TAO).
  – Your own miner row is highlighted so you can quickly see your position.
• Totals (TAO) across all miners.

Run continuously until Ctrl‑C so you can decide in real time whether to
send α‑bids.

Typical usage:
    python3 auction_view.py --netuid 11 --my-coldkey YOUR_COLDKEY_SS58
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from collections import defaultdict
from decimal import Decimal, getcontext
from typing import Dict, List

import bittensor as bt
from rich.console import Console
from rich.table import Table

# ────────────────────────────── config / deps ───────────────────────────── #
getcontext().prec = 60
RAO_PER_TAO = Decimal(10) ** 9

# metahash helpers (used only for reward calc – keep the rest of the code clean)
from metahash.config import (
    AUCTION_DELAY_BLOCKS,
    TREASURY_COLDKEY,
    DEFAULT_BITTENSOR_NETWORK)
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.validator.alpha_transfers import AlphaTransfersScanner
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent

D_START = Decimal(str(_D_START_FLOAT))
bt.logging.set_debug()
# ───────────────────────────────── CLI ──────────────────────────────────── #


def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Very small live auction monitor with Rich leaderboard.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK)
    p.add_argument("--netuid", type=int, required=True, help="Auction subnet uid")
    p.add_argument("--meta-netuid", type=int, default=73,
                   help="Subnet used for UID/coldkey look‑ups (default SN‑73)")
    p.add_argument("--delay", type=int, default=AUCTION_DELAY_BLOCKS,
                   help="Blocks from epoch start until auction opens")
    p.add_argument("--interval", type=float, default=12.0,
                   help="Seconds between refreshes")
    p.add_argument("--treasury", default=TREASURY_COLDKEY,
                   help="Destination coldkey that receives α")
    # highlight
    p.add_argument("--my-coldkey", help="Coldkey SS58 – highlight this row")
    # (optional convenience if you have a local wallet; overrides --my-coldkey)
    p.add_argument("--wallet.name", dest="wallet_name")
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey")
    return p


# ───────────────────────────── helper factories ─────────────────────────── #


def _make_pricing_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _pricing(subnet_id: int, *_):
        return await average_price(subnet_id, start_block=start, end_block=end, st=st)
    return _pricing


def _make_depth_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _depth(subnet_id: int):
        d = await average_depth(subnet_id, start_block=start, end_block=end, st=st)
        return int(getattr(d, "rao", d or 0))
    return _depth


# ──────────────────────────────── monitor ───────────────────────────────── #


async def _monitor(args: argparse.Namespace):
    console = Console()
    st = bt.AsyncSubtensor(network=args.network)
    await st.initialize()

    # Determine whose row to highlight
    highlight_ck = args.my_coldkey
    if not highlight_ck and (args.wallet_name or args.wallet_hotkey):
        wallet = bt.wallet(
            name=args.wallet_name or os.getenv("BT_WALLET_NAME"),
            hotkey=args.wallet_hotkey or os.getenv("BT_HOTKEY"),
        )
        highlight_ck = wallet.coldkey.ss58_address

    # Scanner that picks up α transfers to the treasury coldkey
    scanner = AlphaTransfersScanner(st, dest_coldkey=args.treasury)

    # State that changes once per epoch
    epoch_start: int | None = None
    auction_open: int | None = None
    next_block = 0
    events: List[TransferEvent] = []
    deposits: Dict[int, Decimal] = defaultdict(Decimal)

    # Providers are recomputed each epoch (based on previous epoch averages)
    pricing_provider = depth_provider = None

    while True:
        head = await st.get_current_block()

        # ─── epoch boundaries & auction window ───
        tempo = await st.tempo(args.meta_netuid)         # same as epoch length – 1
        epoch_len = tempo + 1
        curr_epoch_start = head - (head % epoch_len)
        if epoch_start != curr_epoch_start:
            # New epoch – reset state
            epoch_start = curr_epoch_start
            auction_open = epoch_start + args.delay
            next_block = auction_open
            events.clear()
            deposits.clear()

            # Average price/depth from the *previous* epoch
            prev_start, prev_end = max(0, epoch_start - epoch_len), epoch_start - 1
            pricing_provider = _make_pricing_provider(st, prev_start, prev_end)
            depth_provider = _make_depth_provider(st, prev_start, prev_end)

        # Wait until auction officially opens
        if head < auction_open:
            console.print(
                f"[bold yellow]Auction waiting[/] – {auction_open - head} blocks until open "
                f"(block {head}, epoch start {epoch_start})"
            )
            await asyncio.sleep(args.interval)
            continue

        # ─── pull any new α‑transfer events ───
        if next_block <= head:
            raw = await scanner.scan(next_block, head)
            for ev in raw:
                a_tao = Decimal(ev.amount_rao) / RAO_PER_TAO
                deposits[ev.subnet_id] += a_tao
                events.append(
                    TransferEvent(
                        src_coldkey=ev.src_coldkey,
                        dest_coldkey=ev.dest_coldkey or args.treasury,
                        subnet_id=ev.subnet_id,
                        amount_rao=ev.amount_rao,
                    )
                )
            next_block = head + 1

        # ─── compute rewards so far ───
        meta = await st.metagraph(args.meta_netuid)
        uid_of = {ck: uid for uid, ck in enumerate(meta.coldkeys)}.get

        async def _uid_of_ck(ck):  # type: ignore
            return uid_of(ck)

        rewards = await compute_epoch_rewards(
            miner_uids=list(range(len(meta.coldkeys))),
            events=events,
            pricing=pricing_provider,
            uid_of_coldkey=_uid_of_ck,
            start_block=auction_open,
            end_block=head,
            pool_depth_of=depth_provider,
        )
        tao_by_uid = {uid: Decimal(r) for uid, r in enumerate(rewards)}
        total_tao = sum(tao_by_uid.values())

        # ─── render Rich table ───
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("UID", justify="right")
        table.add_column("Coldkey")
        table.add_column("Hotkey")
        table.add_column("Reward (TAO)", justify="right")

        # iterate once to collect; sort by reward desc
        rows = [
            (uid, ck, hk, tao)
            for uid, (ck, hk, tao) in enumerate(zip(meta.coldkeys, meta.hotkeys, rewards))
            if tao > 0
        ]
        rows.sort(key=lambda x: x[3], reverse=True)

        for uid, ck, hk, tao in rows:
            style = "bold yellow" if highlight_ck and ck == highlight_ck else ""
            table.add_row(
                str(uid),
                ck[:12] + "…",
                hk[:12] + "…",
                f"{Decimal(tao).normalize():f}",
                style=style,
            )

        # Add totals row
        table.add_row(
            "",
            "",
            "[bold]TOTAL[/]",
            f"[bold]{total_tao.normalize():f}[/]",
            style="bold magenta",
        )

        # ─── clear & print ───
        console.clear()
        blocks_left = epoch_start + epoch_len - 1 - head
        console.rule(
            f"[green]Subnet {args.netuid} Auction[/] – block {head} · "
            f"Epoch {head // epoch_len} [{epoch_start}-{epoch_start + epoch_len - 1}] · "
            f"{blocks_left} blocks left"
        )
        console.print(table)

        await asyncio.sleep(args.interval)


# ───────────────────────────── entrypoint ──────────────────────────────── #


def main() -> None:
    asyncio.run(_monitor(_arg_parser().parse_args()))


if __name__ == "__main__":
    main()
