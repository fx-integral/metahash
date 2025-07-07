#!/usr/bin/env python3
# --------------------------------------------------------------------------- #
#  auction_leaderboard.py – v2.2 (07 Jul 2025)                                #
#                                                                              #
#  Realtime subnet‑auction dashboard (read‑only)                               #
#  • Better Rich formatting: clearer panels, bold labels, zebra rows           #
#  • Still auto‑detects auction subnet; no --netuid flag                       #
# --------------------------------------------------------------------------- #
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal, getcontext
from typing import Dict, List, Optional

import bittensor as bt
from rich import box
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.style import Style

from metahash.config import (
    AUCTION_DELAY_BLOCKS,
    BAG_SN73,
    DEFAULT_BITTENSOR_NETWORK,
    TREASURY_COLDKEY,
)
from metahash.utils.subnet_utils import average_price, average_depth, subnet_price
from metahash.utils.wallet_utils import load_wallet

from metahash.validator.alpha_transfers import AlphaTransfersScanner, TransferEvent
from metahash.validator.rewards import compute_epoch_rewards

# ─────────── precision & console ─────────── #
getcontext().prec = 60
console = Console()
bt.logging.set_warning()                 # suppress bittensor debug chatter


# ═════════════════ CLI ═════════════════ #
def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Realtime subnet‑auction leaderboard (read‑only)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK,
                   help="Bittensor network name or websocket endpoint")
    p.add_argument("--meta-netuid", type=int, default=73,
                   help="Subnet whose bag is distributed to winners (default 73)")
    p.add_argument("--delay", type=int, default=AUCTION_DELAY_BLOCKS,
                   help="Blocks from epoch start until auction opens")
    p.add_argument("--interval", type=float, default=12.0,
                   help="Refresh interval when --watch is set (seconds)")
    p.add_argument("--watch", action="store_true",
                   help="Continuously refresh until Ctrl‑C")
    # highlight
    p.add_argument("--treasury", default=TREASURY_COLDKEY,
                   help="Coldkey that receives α bids (treasury)")
    p.add_argument("--my-coldkey", default=None,
                   help="Highlight this coldkey’s row")
    # wallet (optional autodetect of --my-coldkey)
    p.add_argument("--wallet.name", dest="wallet_name", default=None)
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default=None)
    return p


# ═════════════ helpers ═════════════ #
def _range_str(start: int, length: int) -> str:
    return f"{start}-{start + length - 1}"


def _fmt_num(v: Decimal | int, prec: int = 6) -> str:
    return f"{Decimal(v):>{prec + 3}.{prec}f}"  # pad for alignment


def _pct(x: Decimal) -> str:
    return f"{x * 100:>6.2f}%"


def _margin_txt(m: Decimal) -> Text:
    t = Text(f"{m * 100:+6.2f}%")
    t.style = "green" if m >= 0 else "red"
    return t


def _short(addr: str, n: int = 6) -> str:
    return addr[:n] + "…" + addr[-n:]


# provider factories (no async lambda allowed)
def _pricing_provider(st: bt.AsyncSubtensor, b0: int, b1: int):
    async def _p(sid: int, *_):
        return await average_price(sid, start_block=b0, end_block=b1, st=st)
    return _p


def _depth_provider(st: bt.AsyncSubtensor, b0: int, b1: int):
    async def _d(sid: int):
        d = await average_depth(sid, start_block=b0, end_block=b1, st=st)
        return int(getattr(d, "rao", d or 0))
    return _d


# ═════════════ snapshot ═════════════ #
async def _snapshot(st: bt.AsyncSubtensor, cache: Dict[str, object], args):
    head = await st.get_current_block()
    tempo = await st.tempo(args.meta_netuid)
    epoch_len = tempo + 1
    epoch_start = head - (head % epoch_len)
    auction_open = epoch_start + args.delay
    eid = epoch_start // epoch_len
    blocks_left = epoch_start + epoch_len - head - 1

    # reset on new epoch
    if cache.get("epoch_start") != epoch_start:
        cache.clear()
        cache.update({
            "epoch_start": epoch_start,
            "auction_open": auction_open,
            "events": [],
            "last_scanned": auction_open - 1,
            "scanner": None,
            "meta": None,
            "auction_netuid": None,
        })

    # banner
    banner_style = "green" if head >= auction_open else "yellow"
    console.rule(Text(
        f"{'🟢' if head >= auction_open else '⏳'} "
        f"Epoch {eid} [{_range_str(epoch_start, epoch_len)}] · "
        f"Block {head} (left {blocks_left})",
        style=banner_style,
    ))

    if head < auction_open:
        console.print("Auction has not opened yet.", style="yellow")
        return

    # scanner
    if cache["scanner"] is None:
        cache["scanner"] = AlphaTransfersScanner(st, dest_coldkey=args.treasury)
    scanner: AlphaTransfersScanner = cache["scanner"]  # type: ignore

    # scan new blocks only
    start_blk = cache["last_scanned"] + 1
    if start_blk <= head:
        fresh = await scanner.scan(start_blk, head)
        cache["events"].extend(fresh)
        cache["last_scanned"] = head
        if fresh and cache["auction_netuid"] is None:
            cache["auction_netuid"] = fresh[0].subnet_id

    events: List[TransferEvent] = cache["events"]
    auction_netuid: Optional[int] = cache.get("auction_netuid")

    # providers for previous epoch averages
    prev_start, prev_end = max(0, epoch_start - epoch_len), epoch_start - 1
    pricing = _pricing_provider(st, prev_start, prev_end)
    depth = _depth_provider(st, prev_start, prev_end)

    # metagraph
    if cache["meta"] is None:
        cache["meta"] = await st.metagraph(args.meta_netuid)
    meta = cache["meta"]  # type: ignore
    uid_of = {ck: uid for uid, ck in enumerate(meta.coldkeys)}.get

    async def _uid_of_ck(ck):  # type: ignore[return-value]
        return uid_of(ck)

    rewards = await compute_epoch_rewards(
        miner_uids=list(range(len(meta.coldkeys))),
        events=events,
        pricing=pricing,
        uid_of_coldkey=_uid_of_ck,
        start_block=auction_open,
        end_block=head,
        pool_depth_of=depth,
    )
    tao_by_uid = {u: Decimal(r) for u, r in enumerate(rewards) if r}
    total_tao = sum(tao_by_uid.values())

    # prices
    alpha_price = Decimal(0)
    if auction_netuid is not None:
        p = await subnet_price(auction_netuid, st=st)
        alpha_price = Decimal(str(p.tao)) if p else Decimal(0)
    sn73_bal = await subnet_price(args.meta_netuid, st=st)
    sn73_price = Decimal(str(sn73_bal.tao)) if sn73_bal else Decimal(0)

    bag_value = BAG_SN73 * sn73_price
    global_margin = (bag_value / total_tao - 1) if total_tao else Decimal(0)
    total_alpha = (total_tao / alpha_price) if alpha_price else None

    # ── overview panel ── #
    overview = Table.grid(padding=(0, 2))
    overview.add_column(justify="right", style="bold bright_cyan")
    overview.add_column(justify="left", style="white")
    overview.add_row("α total (TAO):", _fmt_num(total_tao))
    overview.add_row("α total (tokens):",
                     _fmt_num(total_alpha) if total_alpha else "—")
    overview.add_row("# bidders:", f"{len(tao_by_uid):>6}")
    overview.add_row("α price (TAO):",
                     _fmt_num(alpha_price, 8) if alpha_price else "—")
    overview.add_row("SN‑73 price:", _fmt_num(sn73_price, 8))
    overview.add_row("Bag value:", _fmt_num(bag_value))
    overview.add_row("Global margin:", _margin_txt(global_margin))
    if auction_netuid is not None:
        overview.add_row("Auction subnet:", str(auction_netuid))

    print()
    print("==============================================")
    print()
    console.print(Panel(overview, title="Auction Overview",
                        title_align="left", box=box.ROUNDED))

    if not tao_by_uid:
        console.print("No bids recorded yet.", style="yellow")
        return

    # ── leaderboard ── #
    lb = Table(
        title=f"Subnet {args.meta_netuid} Leaderboard – Epoch {eid}",
        box=box.ROUNDED,
        header_style="bold magenta",
        show_edge=False,
        padding=(0, 1),
        row_styles=[Style(dim=False), Style(dim=True)],   # zebra stripes
    )
    lb.add_column("#", justify="right", style="bright_cyan", no_wrap=True)
    lb.add_column("UID", justify="right", style="bright_cyan", no_wrap=True)
    lb.add_column("Hotkey", justify="left", no_wrap=True)
    lb.add_column("Coldkey", justify="left", no_wrap=True)
    lb.add_column("Spent TAO", justify="right", style="white", no_wrap=True)
    lb.add_column("Share", justify="right", style="white", no_wrap=True)
    lb.add_column("Margin", justify="right", no_wrap=True)

    rows = sorted(tao_by_uid.items(), key=lambda kv: kv[1], reverse=True)
    for rk, (uid, tao) in enumerate(rows, 1):
        share = tao / total_tao
        margin = (bag_value * share / tao - 1) if tao else Decimal(0)
        highlight = meta.coldkeys[uid] == args.my_coldkey
        lb.add_row(
            str(rk),
            str(uid),
            _short(meta.hotkeys[uid]),
            _short(meta.coldkeys[uid]),
            _fmt_num(tao),
            _pct(share),
            _margin_txt(margin),
            style="bold yellow" if highlight else "",
        )
    console.print(lb)


# ═════════════ runner ═════════════ #
async def _runner(args):
    if not args.my_coldkey and (args.wallet_name or args.wallet_hotkey):
        try:
            w = load_wallet(args.wallet_name, args.wallet_hotkey, unlock=False)
            if w and w.coldkey:
                args.my_coldkey = w.coldkey.ss58_address
        except Exception:
            pass

    st = bt.AsyncSubtensor(network=args.network)
    await st.initialize()

    cache: Dict[str, object] = {}
    while True:
        console.clear()
        await _snapshot(st, cache, args)
        if not args.watch:
            break
        await asyncio.sleep(args.interval)


# ═════════ entry‑point ═════════ #
def main() -> None:
    asyncio.run(_runner(_arg_parser().parse_args()))


if __name__ == "__main__":
    main()
