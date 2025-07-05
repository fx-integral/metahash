#!/usr/bin/env python3
# --------------------------------------------------------------------------- #
#  leaderboard.py – v1.2                                                      #
#  Realtime subnet–auction dashboard (Rich)                                   #
#                                                                             #
#  Changes (v1.1 → v1.2):                                                     #
#    • Added lightweight in‑memory cache so we *only* scan new blocks,        #
#      avoiding expensive rescans.                                            #
#    • Re‑used AsyncSubtensor and scanner across refreshes.                   #
#    • Minor refactor: moved snapshot‑state variables into a tiny dict.       #
# --------------------------------------------------------------------------- #
from __future__ import annotations

import argparse
import asyncio
import statistics
from decimal import Decimal, getcontext
from typing import Dict, List, Optional

import bittensor as bt
from metahash.config import (
    AUCTION_DELAY_BLOCKS,
    BAG_SN73,
    DEFAULT_BITTENSOR_NETWORK,
    TREASURY_COLDKEY,
)
from metahash.utils.colors import ColoredLogger as clog
from metahash.utils.subnet_utils import average_price, average_depth, subnet_price
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.utils.wallet_utils import load_wallet
from metahash.validator.alpha_transfers import AlphaTransfersScanner

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.box import MINIMAL_HEAVY_HEAD
from rich.text import Text

# ───────────────────── precision & misc ────────────────────── #
getcontext().prec = 60
RAO_PER_TAO = Decimal(10) ** 9

console = Console()
bt.logging.set_info()


# ═══════════════════════════ CLI ══════════════════════════════ #
def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Realtime subnet‑auction leaderboard",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # network / subnets
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK,
                   help="Bittensor network name or endpoint")
    p.add_argument("--meta-netuid", type=int, default=73,
                   help="Subnet whose bag is distributed to winners (default 73)")
    # timing
    p.add_argument("--delay", type=int, default=AUCTION_DELAY_BLOCKS,
                   help="Blocks from epoch start until auction opens")
    p.add_argument("--interval", type=float, default=12.0,
                   help="Refresh interval when --watch is set (seconds)")
    p.add_argument("--watch", action="store_true",
                   help="Continuously refresh the dashboard")
    # treasury & wallet / highlighting
    p.add_argument("--treasury", default=TREASURY_COLDKEY,
                   help="Coldkey that receives α bids (treasury)")
    p.add_argument("--my-coldkey", default=None,
                   help="Highlight this coldkey’s row (auto‑detected from wallet)")
    p.add_argument("--wallet.name", dest="wallet_name", default=None,
                   help="Wallet coldkey name (for highlight)")
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default=None,
                   help="Wallet hotkey name (unused here but convenient)")

    return p


# ═════════════════════════ helpers ════════════════════════════ #
def _format_range(start: int, length: int) -> str:
    return f"{start}-{start + length - 1}"


def _fmt_tao(v: Decimal | int, prec: int = 6) -> str:
    return f"{Decimal(v):.{prec}f}"


def _fmt_pct(x: Decimal, width: int = 7) -> str:
    return f"{x * 100:>{width}.2f}%"


def _fmt_margin(m: Decimal) -> Text:
    txt = Text(f"{m * 100:+.2f}%")
    txt.style = "green" if m >= 0 else "red"
    return txt


def _short(addr: str, n: int = 6) -> str:
    """Compress an ss58 address for table display."""
    return addr[:n] + "…" + addr[-n:]


# ═══════════ provider factories (async lambdas are illegal) ═══════════ #
def _make_pricing_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _pricing(sid: int, *_):
        return await average_price(sid, start_block=start, end_block=end, st=st)

    return _pricing


def _make_depth_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _depth(sid: int):
        d = await average_depth(sid, start_block=start, end_block=end, st=st)
        return int(getattr(d, "rao", d or 0))

    return _depth


# ════════════════ snapshot (one render) ═════════════════ #
async def _snapshot(
    st: bt.AsyncSubtensor,
    cache: Dict[str, object],
    args: argparse.Namespace,
):
    """
    Pull on‑chain data once and render the summary + leaderboard.
    A small cache (events, last_scanned, etc.) is kept in memory so that
    we *only* query blocks we have not seen yet.
    """
    # ---------- chain state ----------
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
            # we’ll fill these lazily ↓
            "scanner": None,
            "meta": None,
        })

    # ---------- display state banner ----------
    state = ("🟢 Auction active"
             if head >= auction_open
             else "⏳ Auction waiting")
    console.rule(Text(f"{state} · Epoch {eid} [{_format_range(epoch_start, epoch_len)}] · Block {head}",
                      style="cyan"))

    if head < auction_open:
        console.print(Panel("Auction has not opened yet.", style="yellow"))
        return

    # ---------- scanner (re‑use across refreshes) ----------
    if cache["scanner"] is None:
        cache["scanner"] = AlphaTransfersScanner(st, dest_coldkey=args.treasury)
    scanner: AlphaTransfersScanner = cache["scanner"]  # type: ignore

    # ---------- scan only NEW blocks ----------
    start_blk = cache["last_scanned"] + 1
    if start_blk <= head:
        new_raw = await scanner.scan(start_blk, head)
        new_events = [
            TransferEvent(
                src_coldkey=ev.src_coldkey,
                dest_coldkey=ev.dest_coldkey or args.treasury,
                subnet_id=ev.subnet_id,
                amount_rao=ev.amount_rao,
            )
            for ev in new_raw
        ]
        cache["events"].extend(new_events)
        cache["last_scanned"] = head

    events: List[TransferEvent] = cache["events"]

    # ---------- providers (average over previous epoch) ----------
    start_prev, end_prev = max(0, epoch_start - epoch_len), epoch_start - 1
    pricing_provider = _make_pricing_provider(st, start_prev, end_prev)
    depth_provider = _make_depth_provider(st, start_prev, end_prev)

    # ---------- metagraph & reward computation ----------
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
    total_tao = sum(tao_by_uid.values())

    # ---------- financials ----------
    price_bal = await subnet_price(args.meta_netuid, st=st)
    sn73_price = Decimal(str(price_bal.tao)) if price_bal else Decimal(0)
    bag_value = BAG_SN73 * sn73_price
    global_margin = (bag_value / total_tao - 1) if total_tao else Decimal(0)

    # ---------- summary panel ----------
    summary = Table.grid(expand=False)
    summary.add_column(justify="right")
    summary.add_column(justify="left")
    summary.add_row("α total:", _fmt_tao(total_tao))
    summary.add_row("# bidders:", str(len(tao_by_uid)))
    summary.add_row("Bag value:", _fmt_tao(bag_value))
    summary.add_row("Global margin:", _fmt_margin(global_margin))

    console.print(Panel(summary, title="Auction Overview", box=MINIMAL_HEAVY_HEAD))

    if not tao_by_uid:
        console.print("No bids recorded yet.", style="yellow")
        return

    # ---------- leaderboard ----------
    lb = Table(
        title=f"Subnet {args.meta_netuid} Leaderboard – Epoch {eid}",
        header_style="bold magenta",
        box=MINIMAL_HEAVY_HEAD,
        show_lines=False,
    )
    lb.add_column("#", justify="right")
    lb.add_column("UID", justify="right")
    lb.add_column("Hotkey", justify="left")
    lb.add_column("Coldkey", justify="left")
    lb.add_column("α sent", justify="right")
    lb.add_column("Share", justify="right")
    lb.add_column("Margin", justify="right")

    rows = sorted(tao_by_uid.items(), key=lambda kv: kv[1], reverse=True)
    for rank, (uid, tao) in enumerate(rows, start=1):
        share = tao / total_tao
        margin = (bag_value * share / tao - 1) if tao else Decimal(0)
        style = "bold green" if meta.coldkeys[uid] == args.my_coldkey else None

        lb.add_row(
            str(rank),
            str(uid),
            _short(meta.hotkeys[uid]),
            _short(meta.coldkeys[uid]),
            _fmt_tao(tao),
            _fmt_pct(share),
            _fmt_margin(margin),
            style=style,
        )

    console.print(lb)


# ═══════════════ runner (loop if --watch) ═══════════════ #
async def _runner(args: argparse.Namespace):
    # Highlight your own coldkey automatically if wallet provided
    if not args.my_coldkey and (args.wallet_name or args.wallet_hotkey):
        try:
            w = load_wallet(args.wallet_name, args.wallet_hotkey)
            if w and w.coldkey:
                args.my_coldkey = w.coldkey.ss58_address
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


# ═════════════════════ entry-point ═════════════════════ #
def main() -> None:
    asyncio.run(_runner(_arg_parser().parse_args()))


if __name__ == "__main__":
    main()
