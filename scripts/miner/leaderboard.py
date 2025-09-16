#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────── #
#  scripts/leaderboard.py – v1.2                                          #
#                                                                          #
#  Compact, real‑time Rich dashboard for the Bittensor subnet auction.     #
#  • Updated 2025‑07‑07 to support the new TransferEvent signature         #
#    introduced in alpha_transfers.py (cross‑subnet α‑swap patch).         #
#                                                                          #
#  Author: <you> – 07 Jul 2025                                             #
# ─────────────────────────────────────────────────────────────────────── #
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal, getcontext
from typing import Dict, List, Optional

import bittensor as bt
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from metahash.config import (
    AUCTION_DELAY_BLOCKS,
    BAG_SN73,
    DEFAULT_BITTENSOR_NETWORK,
    TREASURY_COLDKEY,
)
from metahash.utils.subnet_utils import average_price, average_depth, subnet_price
from metahash.utils.wallet_utils import load_wallet
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.validator.alpha_transfers import AlphaTransfersScanner

# ───────────────────────── precision & constants ─────────────────────── #
getcontext().prec = 60
RAO_PER_TAO = Decimal(10) ** 9  # 1 TAO  = 1e9 RAO

console = Console()
bt.logging.set_info()            # use set_debug() for verbose tracing


# ═════════════════════════ CLI ═════════════════════════════════════════ #
def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Realtime Bittensor subnet‑auction dashboard",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # network / subnets
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK)
    p.add_argument("--meta-netuid", type=int, default=73,
                   help="Subnet whose bag is auctioned (SN‑73 by default)")
    # timing
    p.add_argument("--delay", type=int, default=AUCTION_DELAY_BLOCKS,
                   help="Blocks from epoch start until auction opens")
    p.add_argument("--interval", type=float, default=12.0,
                   help="Refresh seconds when --watch is set")
    p.add_argument("--watch", action="store_true",
                   help="Continuously refresh until interrupted")
    # treasury & wallet / highlighting
    p.add_argument("--treasury", default=TREASURY_COLDKEY)
    p.add_argument("--my-coldkey", default=None,
                   help="Coldkey to highlight (auto‑detected from wallet if omitted)")
    p.add_argument("--wallet.name", dest="wallet_name", default=None)
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default=None)
    return p


# ═══════════════════ helper formatting ═════════════════════════════════ #
def _format_range(start: int, length: int) -> str:
    return f"{start}-{start + length - 1}"


def _fmt_tao(v: Decimal | int, prec: int = 6) -> str:
    """
    Pretty‑print TAO (or token counts) with `prec` decimal places.
    Use prec=0 for integer‑only display (e.g. BAG_SN73).
    """
    return f"{Decimal(v):.{prec}f}"


def _fmt_pct(x: Decimal, width: int = 7) -> str:
    return f"{x * 100:>{width}.2f}%"


def _fmt_margin_table(m: Decimal) -> Text:
    """
    Margin cell for the *leaderboard* (numeric only, colour coded).
    """
    txt = Text(f"{m * 100:+.2f}%")
    txt.style = "green" if m >= 0 else "red"
    return txt


def _fmt_margin_overview(m: Decimal) -> Text:
    """
    Margin for the *overview* with “profit” / “discount” wording.
    """
    txt = Text()
    label = "profit" if m >= 0 else "discount"
    txt.append(f"{m * 100:+.2f}% {label}")
    txt.style = "green" if m >= 0 else "red"
    return txt


def _ellips(addr: str, n: int = 8) -> str:
    """Abbreviate an ss58 address to `5Gw6xj…Ptao` style."""
    return addr if len(addr) <= 2 * n + 1 else f"{addr[:n]}…{addr[-n:]}"


# ═════════ provider factories (async lambdas are illegal) ══════════════ #
def _make_pricing_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _pricing(sid: int, *_):
        return await average_price(sid, start_block=start, end_block=end, st=st)

    return _pricing


def _make_depth_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _depth(sid: int):
        d = await average_depth(sid, start_block=start, end_block=end, st=st)
        return int(getattr(d, "rao", d or 0))

    return _depth


# ═════════════════ snapshot (single render) ════════════════════════════ #
async def _snapshot(
    st: bt.AsyncSubtensor,
    cache: Dict[str, object],
    args: argparse.Namespace,
):
    """
    Pull on‑chain data **once** and render banner, overview, and leaderboard.
    A tiny in‑mem cache ensures we *only* query blocks we haven’t seen yet.
    """
    # ───── chain state ───── #
    head = await st.get_current_block()
    tempo = await st.tempo(args.meta_netuid)
    epoch_len = tempo + 1
    epoch_start = head - (head % epoch_len)
    auction_open = epoch_start + args.delay
    eid = epoch_start // epoch_len

    # New epoch? → reset cache
    if cache.get("epoch_start") != epoch_start:
        cache.clear()
        cache.update({
            "epoch_start": epoch_start,
            "auction_open": auction_open,
            "events": [],
            "last_scanned": auction_open - 1,
            "scanner": None,
            "meta": None,
        })

    # ───── status banner ───── #
    status_label = "Active" if head >= auction_open else "Pending"
    banner_txt = (
        f"{status_label}  "
        f"Epoch {eid}  "
        f"[{_format_range(epoch_start, epoch_len)}]  ·  "
        f"Block {head}"
    )
    console.rule(Text(banner_txt, style="cyan"))

    if head < auction_open:
        console.print(Panel("Auction has not opened yet.", style="yellow"))
        return

    # ───── scanner (re‑use across refreshes) ───── #
    if cache["scanner"] is None:
        cache["scanner"] = AlphaTransfersScanner(st, dest_coldkey=args.treasury)
    scanner: AlphaTransfersScanner = cache["scanner"]  # type: ignore

    # ───── scan only NEW blocks ───── #
    start_blk = cache["last_scanned"] + 1
    if start_blk <= head:
        # Raw events from the patched scanner
        new_raw = await scanner.scan(start_blk, head)

        # Re-wrap into the *rewards* TransferEvent dataclass
        # (compute_epoch_rewards expects this type)
        new_events = [
            TransferEvent(
                block=ev.block,
                from_uid=ev.from_uid,
                to_uid=ev.to_uid,
                subnet_id=ev.subnet_id,
                amount_rao=ev.amount_rao,
                src_coldkey=ev.src_coldkey,
                dest_coldkey=ev.dest_coldkey or args.treasury,
                src_coldkey_raw=ev.src_coldkey_raw,
                dest_coldkey_raw=ev.dest_coldkey_raw,
                src_subnet_id=ev.src_subnet_id,
            )
            for ev in new_raw
        ]

        cache["events"].extend(new_events)
        cache["last_scanned"] = head

    events: List[TransferEvent] = cache["events"]

    # ───── providers (avg over previous epoch) ───── #
    start_prev, end_prev = max(0, epoch_start - epoch_len), epoch_start - 1
    pricing_provider = _make_pricing_provider(st, start_prev, end_prev)
    depth_provider = _make_depth_provider(st, start_prev, end_prev)

    # ───── metagraph & rewards ───── #
    if cache["meta"] is None:
        cache["meta"] = await st.metagraph(args.meta_netuid)
    meta = cache["meta"]  # type: ignore
    uid_of = {ck: uid for uid, ck in enumerate(meta.coldkeys)}.get

    async def _uid_of_ck(ck):  # type: ignore[return-value]
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
    tao_by_uid = {uid: Decimal(r) for uid, r in enumerate(rewards) if r}
    total_tao = sum(tao_by_uid.values())        # “Total value sent”
    bidders = len(tao_by_uid)

    # ───── price feed & bag value ───── #
    price_bal = await subnet_price(args.meta_netuid, st=st)
    sn73_price = Decimal(str(price_bal.tao)) if price_bal else Decimal(0)
    bag_value = Decimal(BAG_SN73) * sn73_price
    margin = (bag_value / total_tao - 1) if total_tao else Decimal(0)

    # ═════════ Overview panel ═════════ #
    overview = Table.grid(expand=False)
    overview.add_column(justify="right")
    overview.add_column(justify="left")

    overview.add_row("Bag of SN‑73 α:", _fmt_tao(BAG_SN73, prec=0))
    overview.add_row("Bag value (TAO):", _fmt_tao(bag_value))
    overview.add_row("Total value sent:", _fmt_tao(total_tao))
    overview.add_row("Margin:", _fmt_margin_overview(margin))
    overview.add_row("# bidders:", str(bidders))

    console.print(Panel(overview, title="Auction Overview", box=box.SIMPLE_HEAVY))

    if not tao_by_uid:
        console.print("No bids recorded yet.", style="yellow")
        return

    # ═════════ leaderboard table ══════ #
    lb = Table(
        title=f"Subnet {args.meta_netuid} Leaderboard – Epoch {eid}",
        header_style="bold magenta",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        expand=True,
    )
    lb.add_column("#", justify="right")
    lb.add_column("UID", justify="right")
    lb.add_column("Hotkey", overflow="ellipsis")
    lb.add_column("Coldkey", overflow="ellipsis")
    lb.add_column("Spent TAO", justify="right")
    lb.add_column("Share", justify="right")
    lb.add_column("Margin", justify="right")

    rows = sorted(tao_by_uid.items(), key=lambda kv: kv[1], reverse=True)
    for rank, (uid, tao) in enumerate(rows, start=1):
        share = tao / total_tao
        row_margin = (bag_value * share / tao - 1) if tao else Decimal(0)
        style = "bold cyan" if meta.coldkeys[uid] == args.my_coldkey else ""

        lb.add_row(
            str(rank),
            str(uid),
            _ellips(meta.hotkeys[uid]),
            _ellips(meta.coldkeys[uid]),
            _fmt_tao(tao),
            _fmt_pct(share),
            _fmt_margin_table(row_margin),
            style=style,
        )

    console.print(lb)


# ═════════════════════ runner (loop if --watch) ════════════════════════ #
async def _runner(args: argparse.Namespace):
    # Highlight your own coldkey automatically if wallet provided
    if not args.my_coldkey and (args.wallet_name or args.wallet_hotkey):
        try:
            w = load_wallet(args.wallet_name, args.wallet_hotkey, unlock=False)
            if w and w.coldkeypub:
                args.my_coldkey = w.coldkeypub
        except Exception:
            pass  # ignore wallet load errors

    st = bt.AsyncSubtensor(network=args.network)
    await st.initialize()
    cache: Dict[str, object] = {}

    while True:
        console.clear()
        await _snapshot(st, cache, args)
        if not args.watch:
            break
        await asyncio.sleep(args.interval)


# ═══════════════════ entry‑point ═══════════════════════════════════════ #
def main() -> None:
    asyncio.run(_runner(_arg_parser().parse_args()))


if __name__ == "__main__":
    main()
