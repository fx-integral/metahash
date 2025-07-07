#!/usr/bin/env python3
# ╭────────────────────────────────────────────────────────────────────────────╮
#  auto_bidder_basic.py  ·  v2.0  ·  07 Jul 2025                             #
# ╰────────────────────────────────────────────────────────────────────────────╯
"""
A **verbose α→TAO auction helper** for Bittensor.

Changes compared to *auto_bidder_basic.py*
──────────────────────────────────────────
1.  Richer CLI:
      • --top‑miners N       – how many miners to list (default 10)
      • --all‑miners-table   – list every miner (can get large)
2.  Extended dashboard printed every --interval seconds:
      • Chain / epoch status line
      • Auction snapshot (total TAO, bag value, margin, discount, α spent)
      • Price inputs (SN‑73 oracle price, bag constant)
      • Top‑N miners by TAO contributed so far this epoch
3.  Completely self‑contained – same dependencies as the original script.
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal, getcontext
from typing import List

import bittensor as bt
from rich import box
from rich.console import Console
from rich.table import Table

# ───────────────── project‑local imports ────────────────── #
from metahash.config import (            # unchanged
    AUCTION_DELAY_BLOCKS,
    BAG_SN73,
    DEFAULT_BITTENSOR_NETWORK,
    TREASURY_COLDKEY,
)
from metahash.utils.colors import ColoredLogger as clog
from metahash.utils.subnet_utils import average_price, average_depth, subnet_price
from metahash.utils.wallet_utils import load_wallet, transfer_alpha
from metahash.validator.alpha_transfers import AlphaTransfersScanner, TransferEvent
from metahash.validator.rewards import compute_epoch_rewards

# ─── maths / display settings ───────────────────────────── #
getcontext().prec = 60
MIN_TAO_ONCHAIN = Decimal("0.0005")         # 500 000 RAO dust limit
console = Console()
bt.logging.set_info()                       # quieter than DEBUG


# ╭──────────────────────── CLI ─────────────────────────╮ #
def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Verbose α auto‑bidder: send a fixed α amount whenever "
        "the projected discount does **not** exceed --max-discount and print "
        "a detailed dashboard each cycle.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # network / subnets
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK)
    p.add_argument("--netuid", type=int, required=True,
                   help="Subnet uid where α is spent (auction subnet)")
    p.add_argument("--meta-netuid", type=int, default=73,
                   help="Subnet uid used for SN‑73 bag valuation")

    # timing
    p.add_argument("--delay", type=int, default=AUCTION_DELAY_BLOCKS,
                   help="Blocks between epoch start and auction opening")
    p.add_argument("--interval", type=float, default=12.0,
                   help="Seconds between auction checks")

    # treasury
    p.add_argument("--treasury", default=TREASURY_COLDKEY,
                   help="Treasury cold‑key that receives α bids")

    # bidding rule
    p.add_argument("--bid-alpha", type=Decimal, required=True,
                   help="α tokens to send on **each** qualifying bid")
    p.add_argument("--max-alpha", type=Decimal, default=Decimal("0"),
                   help="Stop after this much α spent in the epoch (0 = unlimited)")
    p.add_argument("--max-discount", type=Decimal, default=Decimal("20"),
                   help="Maximum loss you tolerate (e.g. 20 = 20 %)")

    # wallet / key
    p.add_argument("--wallet.name", dest="wallet_name", default=None)
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default=None)
    p.add_argument("--source-hotkey", required=True,
                   help="Validator hot‑key that signs the bid extrinsic")

    # extra verbosity
    p.add_argument("--top-miners", type=int, default=10,
                   help="Show the N miners with the largest TAO sent so far")
    p.add_argument("--all-miners-table", action="store_true",
                   help="Print a table line for *every* miner (can be large)")

    return p


# ╭────────────────── helper fns ───────────────────╮ #
def _format_range(start: int, length: int) -> str:
    return f"{start}-{start + length - 1}"


def _status_line(head: int, open_: int, epoch_start: int, epoch_len: int) -> str:
    epoch_end = epoch_start + epoch_len - 1
    blocks_left = epoch_end - head
    state = "⏳ pending" if head < open_ else "🟢 active "
    eid = head // epoch_len
    return (
        f"{state}│ Epoch {eid} [{_format_range(epoch_start, epoch_len)}] │ "
        f"Block {head} │ {blocks_left} blk left"
    )


def _make_pricing_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _pricing(sid: int, *_):
        return await average_price(sid, start_block=start, end_block=end, st=st)
    return _pricing


def _make_depth_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _depth(sid: int):
        d = await average_depth(sid, start_block=start, end_block=end, st=st)
        return int(getattr(d, "rao", d or 0))
    return _depth


def _should_bid(
    discount: Decimal,
    spent_alpha: Decimal,
    max_alpha: Decimal,
    max_discount: Decimal,
) -> bool:
    """Return True if we should send another bid now."""
    if discount > max_discount:
        return False
    if max_alpha and spent_alpha >= max_alpha:
        return False
    return True


# ╭────────────────── dashboard ───────────────────╮ #
def _print_dashboard(                         # noqa: PLR0913
    head: int,
    auction_open: int,
    epoch_start: int,
    epoch_len: int,
    total_tao: Decimal,
    bag_value: Decimal,
    margin: Decimal,
    discount: Decimal,
    spent_alpha: Decimal,
    args: argparse.Namespace,
    sn73_price: Decimal,
    rewards: List[Decimal],
    coldkeys: List[str],
):
    """Clear screen and print all relevant information nicely formatted."""
    console.clear()

    # status line
    console.rule(_status_line(head, auction_open, epoch_start, epoch_len))

    # auction snapshot table
    tbl = Table(title="Auction snapshot", box=box.SIMPLE_HEAVY, expand=False)
    tbl.add_column("metric", style="bold")
    tbl.add_column("value", justify="right")
    tbl.add_row("total TAO", f"{total_tao:.6f}")
    tbl.add_row("bag value", f"{bag_value:.6f}")
    tbl.add_row("margin", f"{margin:.2%}")
    tbl.add_row("discount", f"{discount:.2%}")
    tbl.add_row("spent α", f"{spent_alpha} / "
                           f"{'∞' if args.max_alpha == 0 else args.max_alpha}")
    tbl.add_row("next bid rule",
                f"{args.bid_alpha} α if disc ≤ {args.max_discount * 100:.2f}%")
    console.print(tbl)

    # price inputs table
    pin = Table(title="Price inputs", box=box.SIMPLE_HEAVY, expand=False)
    pin.add_column("parameter")
    pin.add_column("value", justify="right")
    pin.add_row("SN‑73 oracle price", f"{sn73_price:.6f} TAO")
    pin.add_row("Bag constant", f"{BAG_SN73} SN‑73")
    console.print(pin)

    # miner leaderboard
    miners = [(uid, ck, r) for uid, (ck, r) in
              enumerate(zip(coldkeys, rewards)) if r > 0]
    miners.sort(key=lambda tup: tup[2], reverse=True)

    show_all = args.all_miners_table
    top_n = len(miners) if show_all else min(args.top_miners, len(miners))
    if top_n:
        board = Table(title=f"Top {top_n} miners by TAO sent"
                      + (" (all miners)" if show_all else ""),
                      box=box.SIMPLE_HEAVY, expand=False)
        board.add_column("#", justify="right")
        board.add_column("uid", justify="right")
        board.add_column("coldkey")
        board.add_column("TAO sent", justify="right")
        for rank, (uid, ck, r) in enumerate(miners[:top_n], start=1):
            board.add_row(str(rank), str(uid), ck, f"{r:.6f}")
        console.print(board)

    console.rule()  # horizontal line at bottom


# ╭────────────────── main loop ───────────────────╮ #
async def _monitor(args: argparse.Namespace):
    args.max_discount = args.max_discount / Decimal(100)   # pct → fraction

    wallet = load_wallet(coldkey_name=args.wallet_name, hotkey_name=args.wallet_hotkey)
    if not wallet:
        clog.error("Wallet not found – cannot bid.")
        return

    st = bt.AsyncSubtensor(network=args.network)
    await st.initialize()
    scanner = AlphaTransfersScanner(st, dest_coldkey=args.treasury)

    # epoch state
    epoch_start = auction_open = next_block = None
    pricing_provider = depth_provider = None
    events: List[TransferEvent] = []
    spent_alpha = Decimal(0)

    clog.info("Verbose auto‑bidder started", color="cyan")
    clog.info(
        f"Bid rule: {args.bid_alpha} α when discount ≤ {args.max_discount * 100:.2f} %",
        color="cyan",
    )

    while True:
        head = await st.get_current_block()
        tempo = await st.tempo(args.meta_netuid)
        epoch_len = tempo + 1
        epoch_start_now = head - (head % epoch_len)

        # -------- epoch rollover --------
        if epoch_start != epoch_start_now:
            epoch_start = epoch_start_now
            auction_open = epoch_start + args.delay
            next_block = auction_open
            events.clear()
            spent_alpha = Decimal(0)

            # providers use previous epoch
            start_prev, end_prev = max(0, epoch_start - epoch_len), epoch_start - 1
            pricing_provider = _make_pricing_provider(st, start_prev, end_prev)
            depth_provider = _make_depth_provider(st, start_prev, end_prev)

            clog.info(f"--- NEW EPOCH {epoch_start // epoch_len} "
                      f"[{_format_range(epoch_start, epoch_len)}]", color="cyan")

        # wait until auction opens
        if head < auction_open:
            _print_dashboard(
                head, auction_open, epoch_start, epoch_len,
                total_tao=Decimal(0), bag_value=Decimal(0),
                margin=Decimal(0), discount=Decimal(0),
                spent_alpha=spent_alpha, args=args,
                sn73_price=Decimal(0), rewards=[],
                coldkeys=[],
            )
            await asyncio.sleep(args.interval)
            continue

        # -------- scan chain --------
        if next_block <= head:
            events.extend(await scanner.scan(next_block, head))
            next_block = head + 1

        # -------- recompute auction numbers --------
        meta = await st.metagraph(args.meta_netuid)
        uid_of = {ck: uid for uid, ck in enumerate(meta.coldkeys)}.get

        async def _uid_of_ck(ck):      # noqa: ANN001
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

        total_tao = Decimal(sum(rewards))
        price_bal = await subnet_price(args.meta_netuid, st=st)
        sn73_price = Decimal(str(price_bal.tao)) if price_bal else Decimal(0)
        bag_value = BAG_SN73 * sn73_price

        margin = (bag_value / total_tao - 1) if total_tao else Decimal("Infinity")
        discount = max(Decimal(0), -margin) if margin != Decimal("Infinity") else Decimal(0)

        # ---------- dashboard ----------
        _print_dashboard(
            head, auction_open, epoch_start, epoch_len,
            total_tao, bag_value, margin, discount,
            spent_alpha, args, sn73_price, rewards, meta.coldkeys,
        )

        # ---------- maybe bid ----------
        if _should_bid(discount, spent_alpha, args.max_alpha, args.max_discount):
            try:
                ok = await transfer_alpha(
                    subtensor=st,
                    wallet=wallet,
                    hotkey_ss58=args.source_hotkey,
                    origin_and_dest_netuid=args.netuid,
                    dest_coldkey_ss58=args.treasury,
                    amount=bt.Balance.from_tao(args.bid_alpha),
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                )
            except Exception as exc:   # noqa: BLE001
                clog.warning(f"transfer_alpha failed: {exc}")
                ok = False
            if ok:
                spent_alpha += args.bid_alpha
                clog.success(f"BID sent {args.bid_alpha} α (epoch total {spent_alpha})")
        else:
            clog.debug("Conditions not met – no bid this round.")

        await asyncio.sleep(args.interval)


# ╭────────────────── entrypoint ───────────────────╮ #
def main() -> None:
    """CLI entrypoint."""
    asyncio.run(_monitor(_arg_parser().parse_args()))


if __name__ == "__main__":
    main()
