#!/usr/bin/env python3
# --------------------------------------------------------------------------- #
#  auction_watch.py – v8  (Rich table output)                                 #
# --------------------------------------------------------------------------- #
from __future__ import annotations

import argparse
import asyncio
import math
from decimal import Decimal, getcontext
from typing import List, Optional

import bittensor as bt
from metahash.config import (
    AUCTION_DELAY_BLOCKS,
    TREASURY_COLDKEY,
    BAG_SN73,
    DEFAULT_BITTENSOR_NETWORK,
)
from metahash.utils.colors import ColoredLogger as clog
from metahash.utils.subnet_utils import average_price, average_depth, subnet_price
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.utils.wallet_utils import load_wallet, transfer_alpha

# ───── Rich for pretty tables ───── #
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()

# ───────────────────────── precision / constants ────────────────────────── #
getcontext().prec = 60
RAO_PER_TAO = Decimal(10) ** 9
MIN_TAO_ONCHAIN = Decimal("0.0005")           # 500 000 RAO
DEFAULT_STEP_ALPHA = Decimal("0.01")

bt.logging.set_info()

# ───────────────────────────── CLI ────────────────────────────── #


def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Subnet auction monitor with profit‑maximising automatic "
                    "α→TAO bidding (flat cost).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # network / subnets
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK)
    p.add_argument("--netuid", type=int, required=True, help="Subnet to send alpha from")
    p.add_argument("--meta-netuid", type=int, default=73,
                   help="Subnet uid for UID look‑ups / reward forecast")

    # timing
    p.add_argument("--delay", type=int, default=AUCTION_DELAY_BLOCKS)
    p.add_argument("--interval", type=float, default=12.0)

    # treasury
    p.add_argument("--treasury", default=TREASURY_COLDKEY)

    # bidding
    p.add_argument("--source-hotkey", required=True,
                   help="Hotkey where alpha will be transferred from.")
    p.add_argument("--max-alpha", type=Decimal, default=Decimal("0"),
                   help="Absolute cap on α you are willing to spend this epoch.")
    p.add_argument("--step-alpha", type=Decimal, default=DEFAULT_STEP_ALPHA,
                   help="Smallest α that can be sent in a single bid.")
    p.add_argument("--max-discount", type=Decimal, default=Decimal("20"),
                   help="Maximum loss (discount) you tolerate, in percent.")
    p.add_argument("--safety-buffer", type=Decimal, default=Decimal("1.25"),
                   help="Assume others add ×this TAO before epoch close.")

    # wallet
    p.add_argument("--wallet.name", dest="wallet_name", default=None)
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default=None)
    return p

# ──────────────────── helpers ────────────────────── #


def _format_range(start: int, length: int) -> str:
    return f"{start}-{start + length - 1}"


def _status(head: int, open_: int, start: int, length: int) -> str:
    epoch_end = start + length - 1
    blocks_left = epoch_end - head
    state = (
        f"Auction Active (started {head - open_} blocks ago)"
        if head >= open_
        else f"Auction Waiting ({open_ - head} blocks to start)"
    )
    eid = head // length
    return (f"{state} │ Epoch {eid} [{_format_range(start, length)}] "
            f"(Block {head}) │ {blocks_left} blk left")


def _fmt_margin(m: Decimal, colour=True) -> str:
    if m >= 0:
        text = f"profit {m:.2%}"
        return f"[green]{text}[/green]" if colour else text
    text = f"discount {(-m):.2%}"
    return f"[red]{text}[/red]" if colour else text

# ─────────────────── providers (for rewards calc) ─────────────────── #


def _make_pricing_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _pricing(sid: int, *_):
        return await average_price(sid, start_block=start, end_block=end, st=st)
    return _pricing


def _make_depth_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _depth(sid: int):
        d = await average_depth(sid, start_block=start, end_block=end, st=st)
        return int(getattr(d, "rao", d or 0))
    return _depth

# ─────────────────── main coroutine ─────────────────── #


async def _monitor(args: argparse.Namespace):
    args.max_discount = args.max_discount / Decimal(100)

    def warn(m): return clog.warning("[auction] " + m)
    def info(m): return clog.info("[auction] " + m, color="cyan")

    if args.max_alpha and 0 < args.max_alpha < args.step_alpha:
        warn("--step-alpha larger than --max-alpha; reducing step-alpha.")
        args.step_alpha = args.max_alpha

    wallet = load_wallet(coldkey_name=args.wallet_name, hotkey_name=args.wallet_hotkey)
    autobid = bool(wallet and args.source_hotkey)

    st = bt.AsyncSubtensor(network=args.network)
    await st.initialize()

    from metahash.validator.alpha_transfers import AlphaTransfersScanner
    scanner = AlphaTransfersScanner(st, dest_coldkey=args.treasury)

    epoch_start = auction_open = next_block = None
    pricing_provider = depth_provider = None
    events: List[TransferEvent] = []
    my_uid: Optional[int] = None
    alpha_sent = Decimal(0)

    info(f"Auction subnet = {args.netuid}  |  meta subnet = {args.meta_netuid}")
    if autobid:
        info(f"Auto‑bid ON  min={args.step_alpha} α, "
             f"max={args.max_alpha or '∞'} α, "
             f"max_loss={args.max_discount:.2%}, "
             f"buffer={args.safety_buffer}")

    def _min_allowed_alpha() -> Decimal:
        return max(args.step_alpha, MIN_TAO_ONCHAIN)

    while True:
        print()
        head = await st.get_current_block()
        tempo = await st.tempo(args.meta_netuid)
        epoch_len = tempo + 1
        epoch_start_now = head - (head % epoch_len)

        # ───────── epoch rollover ─────────
        if epoch_start != epoch_start_now:
            epoch_start = epoch_start_now
            auction_open = epoch_start + args.delay
            next_block = auction_open
            events.clear()
            alpha_sent = Decimal(0)

            start_prev, end_prev = max(0, epoch_start - epoch_len), epoch_start - 1
            pricing_provider = _make_pricing_provider(st, start_prev, end_prev)
            depth_provider = _make_depth_provider(st, start_prev, end_prev)

            meta = await st.metagraph(args.meta_netuid)
            uid_lookup = {ck: uid for uid, ck in enumerate(meta.coldkeys)}
            my_uid = uid_lookup.get(wallet.coldkey.ss58_address) if wallet.coldkey.ss58_address else None
            info(f"⟫ CURRENT EPOCH {epoch_start // epoch_len} "
                 f"[{_format_range(epoch_start, epoch_len)}]")

        info(_status(head, auction_open, epoch_start, epoch_len))

        if head < auction_open:
            await asyncio.sleep(args.interval)
            continue

        # ───────── scan unprocessed blocks ─────────
        if next_block <= head:
            info(f"Scanner: frm={next_block}  to={head}")
            raw = await scanner.scan(next_block, head)
            events.extend(
                TransferEvent(
                    src_coldkey=ev.src_coldkey,
                    dest_coldkey=ev.dest_coldkey or args.treasury,
                    subnet_id=ev.subnet_id,
                    amount_rao=ev.amount_rao,
                )
                for ev in raw
            )
            next_block = head + 1
            info(f"… scanned {len(raw)} new α‑transfer(s)")

        # ───────── recompute rewards & margins ─────────
        meta = await st.metagraph(args.meta_netuid)
        uid_of = {ck: uid for uid, ck in enumerate(meta.coldkeys)}.get

        async def _uid_of_ck(ck): return uid_of(ck)

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
        my_tao_spent = tao_by_uid.get(my_uid, Decimal(0)) if my_uid is not None else Decimal(0)

        price_bal = await subnet_price(args.meta_netuid, st=st)
        sn73_price = Decimal(str(price_bal.tao)) if price_bal else Decimal(0)
        bag_value = BAG_SN73 * sn73_price

        global_margin = (bag_value / total_tao - 1) if total_tao else Decimal(0)
        my_reward_tau = bag_value * (my_tao_spent / total_tao) if total_tao else Decimal(0)
        my_margin = (my_reward_tau / my_tao_spent - 1) if my_tao_spent else Decimal(0)

        # ───────── optimal α calculation ─────────
        def _optimal_extra_alpha() -> Decimal:
            others_now = total_tao - my_tao_spent
            others_future = others_now * args.safety_buffer

            if others_future == 0:
                return _min_allowed_alpha() if my_tao_spent == 0 else Decimal(0)

            m_star = Decimal(math.sqrt(bag_value * others_future)) - others_future
            if m_star < 0:
                m_star = Decimal(0)

            m_disc = (bag_value / (1 - args.max_discount)
                      - others_future) if args.max_discount < 1 else Decimal("Infinity")
            if m_disc < 0:
                m_disc = Decimal(0)

            target = min(m_star, m_disc)
            if target <= my_tao_spent:
                return Decimal(0)

            delta = target - my_tao_spent
            delta = max(delta, _min_allowed_alpha())
            if args.max_alpha and alpha_sent + delta > args.max_alpha:
                return Decimal(0)
            return delta

        extra_alpha = _optimal_extra_alpha()
        future_total = total_tao + extra_alpha
        future_margin = (bag_value / future_total - 1) if future_total else global_margin
        loss_after = max(Decimal(0), 1 - bag_value / future_total) if future_total else Decimal(0)

        # ───────── rich table output ─────────
        table = Table(
            title=f"Subnet {args.netuid}  |  Block {head}",
            box=box.MINIMAL_HEAVY_HEAD,
            show_header=True,
            header_style="bold magenta",
        )
        table.add_column("Category", justify="left")
        table.add_column("Sent (TAO)", justify="right")
        table.add_column("Auction Value (TAO)", justify="right")
        table.add_column("Margin", justify="right")

        table.add_row(
            "GLOBAL",
            f"{total_tao:.6f}",
            f"{bag_value:.6f}",
            _fmt_margin(global_margin),
        )
        table.add_row(
            "ME",
            f"{my_tao_spent:.6f}",
            f"{my_reward_tau:.6f}",
            _fmt_margin(my_margin),
        )
        console.print(table)
        console.print(
            f"[cyan]OPTIMAL extra α:[/] {extra_alpha}    "
            f"(future {_fmt_margin(future_margin)})\n"
        )

        # ───────── maybe bid ─────────
        if autobid and extra_alpha > 0 and loss_after <= args.max_discount:
            try:
                ok = await transfer_alpha(
                    subtensor=st,
                    wallet=wallet,
                    hotkey_ss58=args.source_hotkey,
                    origin_and_dest_netuid=args.netuid,
                    dest_coldkey_ss58=args.treasury,
                    amount=bt.Balance.from_tao(extra_alpha),
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                )
            except Exception as exc:
                warn(f"transfer_alpha failed: {exc}")
                ok = False
            if ok:
                alpha_sent += extra_alpha
                clog.success(f"AUTO‑BID sent {extra_alpha} α "
                             f"(cum {alpha_sent}) "
                             f"{_fmt_margin(future_margin, colour=False)}")

        await asyncio.sleep(args.interval)

# ─────────────────── entrypoint ─────────────────── #


def main() -> None:
    asyncio.run(_monitor(_arg_parser().parse_args()))


if __name__ == "__main__":
    main()