#!/usr/bin/env python3
# --------------------------------------------------------------------------- #
#  auction_watch.py – Incremental SN-73 auction monitor (live forecast)       #
#                                                                             #
#  2025-07-03 • unified discount math via bond_utils.quote_alpha_cost()       #
#             • Decimal-safe D_START (no float/Decimal mix crash)             #
#             • same slim log format + “started … blk ago”                   #
# --------------------------------------------------------------------------- #

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import defaultdict
from decimal import Decimal, getcontext
from typing import Dict, List, Optional, Tuple

import bittensor as bt
from metahash.config import (
    AUCTION_DELAY_BLOCKS,
    TREASURY_COLDKEY,
    D_START as _D_START_FLOAT,   # float in config
    BAG_SN73,
)
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.validator.rewards import (
    compute_epoch_rewards,
    TransferEvent,
)
from metahash.utils.bond_utils import (
    get_bond_curve,
    quote_alpha_cost,           # ← shared α-cost helper you added here
)

# ───────────────────────── precision / constants ───────────────────────── #

getcontext().prec = 60                     # keep sub-planck accuracy
D_START = Decimal(str(_D_START_FLOAT))     # convert float 0.1 → exact Decimal
PLANCK_D = Decimal(10) ** 9

# ───────────────────────────── CLI ────────────────────────────── #


def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Incremental SN-73 auction monitor with live reward forecast",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--network", default="finney")
    p.add_argument("--delay", type=int, default=AUCTION_DELAY_BLOCKS)
    p.add_argument("--interval", type=float, default=12.0)
    p.add_argument("--treasury", default=TREASURY_COLDKEY)
    p.add_argument("--min-display-alpha", type=Decimal, default=Decimal("0"))
    p.add_argument("--coldkey", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    return p

# ──────────────────── local logging helpers ──────────────────── #


def _status_line(head: int, auction_open: int) -> str:
    if head >= auction_open:
        age = head - auction_open
        return f"Block {head} | Auction Active (started {age} blk ago at {auction_open})"
    delta = auction_open - head
    return f"Block {head} | Auction Waiting ({delta} blk until {auction_open})"


def _epoch_line(head: int, start: int, length: int) -> str:
    return f"Block {head} | Epoch start: {start} end: {start + length - 1}"

# ─────────────────── provider helpers (async) ─────────────────── #


def _make_pricing_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _pricing(subnet_id: int, *_):
        return await average_price(subnet_id, start_block=start, end_block=end, st=st)
    return _pricing


def _make_depth_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _depth(subnet_id: int):
        d = await average_depth(subnet_id, start_block=start, end_block=end, st=st)
        return int(getattr(d, "rao", d or 0))
    return _depth

# ─────────────────────────── monitor core ──────────────────────────── #


async def _monitor(args):
    # logging
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("watch").info
    dbg = logging.getLogger("watch").debug
    warn = logging.getLogger("watch").warning

    log("Connecting to chain…")
    st = bt.AsyncSubtensor(network=args.network)
    await st.initialize()
    log(f"Connected to “{args.network}”")

    from metahash.validator.alpha_transfers import AlphaTransfersScanner
    scanner = AlphaTransfersScanner(st, dest_coldkey=args.treasury)

    # state
    epoch_start = auction_open = next_block = None
    pricing_provider = depth_provider = None

    deposits: Dict[int, int] = defaultdict(int)
    my_deposits: Dict[int, int] = defaultdict(int)
    events: List[TransferEvent] = []

    forecast_weights: List[float] | None = None
    forecast_rewards: Dict[int, Decimal] | None = None
    my_uid: Optional[int] = None
    my_forecast_reward: Optional[Decimal] = None

    while True:
        head = await st.get_current_block()
        tempo = await st.tempo(73)
        epoch_len = tempo + 1
        epoch_start_now = head - (head % epoch_len)

        # epoch rollover
        if epoch_start != epoch_start_now:
            epoch_start = epoch_start_now
            auction_open = epoch_start + args.delay
            next_block = auction_open
            deposits.clear()
            my_deposits.clear()
            events.clear()

            # average price/depth from previous epoch
            prev_start = max(0, epoch_start - epoch_len)
            prev_end = epoch_start - 1
            pricing_provider = _make_pricing_provider(st, prev_start, prev_end)
            depth_provider = _make_depth_provider(st, prev_start, prev_end)

            # metagraph snapshot
            meta = await st.metagraph(73)
            uid_resolver = {ck: uid for uid, ck in enumerate(meta.coldkeys)}.get
            my_uid = uid_resolver(args.coldkey) if args.coldkey else None

            log(_epoch_line(head, epoch_start, epoch_len))

        log(_status_line(head, auction_open))

        # waiting
        if head < auction_open:
            await asyncio.sleep(args.interval)
            continue

        # scan chain incrementally
        if next_block <= head:
            log(f"Scanning transfers from {next_block} to {head}…")
            raw = await scanner.scan(next_block, head)
            for ev in raw:
                src_ck = getattr(ev, "src_coldkey", None) or getattr(ev, "coldkey", "")
                print(src_ck)
                input()
                deposits[ev.subnet_id] += ev.amount_rao
                events.append(TransferEvent(coldkey=src_ck,
                                            subnet_id=ev.subnet_id,
                                            amount_rao=ev.amount_rao))
                if args.coldkey and src_ck == args.coldkey:
                    my_deposits[ev.subnet_id] += ev.amount_rao
            next_block = head + 1
            log(f"Processed {len(raw)} α-transfer(s); cumulative {len(events)} this auction.")

        # provisional reward forecast
        try:
            curve = get_bond_curve()

            class _Val:
                def __init__(self, n): self._uids = list(range(n))
                def get_miner_uids(self): return self._uids
            meta = await st.metagraph(73)
            forecast = await compute_epoch_rewards(
                validator=_Val(len(meta.coldkeys)),
                events=events,
                pricing=lambda sid, *_: pricing_provider(sid),
                uid_of_coldkey=lambda ck: uid_resolver(ck),
                start_block=auction_open,
                end_block=head,
                bag_sn73=BAG_SN73,
                c0=curve.c0, beta=curve.beta, r_min=curve.r_min,
                pool_depth_of=lambda sid: depth_provider(sid),
                log=lambda m: dbg("FORECAST – " + m),
            )
            forecast_weights = forecast.weights
            forecast_rewards = forecast.rewards_per_miner
            if my_uid is not None:
                my_forecast_reward = forecast_rewards.get(my_uid, Decimal(0))
        except Exception as e:
            warn(f"FORECAST failed: {e}")

        # aggregate totals
        rows: List[Tuple[int, str, str, str, str]] = []
        a_tot_raw = t_post_tot = t_pre_tot = Decimal(0)

        for sid, a_raw in sorted(deposits.items()):
            if not a_raw:
                continue
            price_bal = await pricing_provider(sid)
            if not price_bal or price_bal.tao == 0:
                continue
            tao_post, tao_pre, disc = quote_alpha_cost(
                a_raw,
                price_tao=Decimal(str(price_bal.tao)),
                depth_rao=await depth_provider(sid),
                c0=D_START,
            )
            a_display = Decimal(a_raw) / PLANCK_D
            if a_display < args.min_display_alpha:
                continue
            rows.append((
                sid,
                f"{a_display.normalize():f}",
                f"{tao_post.normalize():f}",
                f"{tao_pre.normalize():f}",
                f"{(disc*100):.2f}%"
            ))
            a_tot_raw += Decimal(a_raw)
            t_post_tot += tao_post
            t_pre_tot += tao_pre

        disc_tot = (Decimal(1) - t_post_tot / t_pre_tot) if t_pre_tot else Decimal(D_START)
        rows.insert(0, (
            -1,
            f"{(a_tot_raw/PLANCK_D).normalize():f}",
            f"{t_post_tot.normalize():f}",
            f"{t_pre_tot.normalize():f}",
            f"{(disc_tot*100):.2f}%"
        ))

        # my deposits
        my_rows, my_a_raw, my_t_post, my_t_pre = [], Decimal(0), Decimal(0), Decimal(0)
        for sid, a_raw in sorted(my_deposits.items()):
            price_bal = await pricing_provider(sid)
            if not price_bal or price_bal.tao == 0:
                continue
            tao_post, tao_pre, _ = quote_alpha_cost(
                a_raw,
                price_tao=Decimal(str(price_bal.tao)),
                depth_rao=await depth_provider(sid),
                c0=D_START,
            )
            my_rows.append((sid,
                            f"{(Decimal(a_raw)/PLANCK_D).normalize():f}",
                            f"{tao_post.normalize():f}"))
            my_a_raw += Decimal(a_raw)
            my_t_post += tao_post
            my_t_pre += tao_pre
        my_disc_tot = (Decimal(1) - my_t_post / my_t_pre) if my_t_pre else Decimal(D_START)

        # output
        if args.json:
            print(json.dumps({
                "t": int(time.time()), "blk": head, "epoch": epoch_start,
                "rows": rows, "my_rows": my_rows,
                "my_totals": {"alpha": f"{(my_a_raw/PLANCK_D).normalize():f}",
                              "tao_post": f"{my_t_post.normalize():f}",
                              "tao_pre": f"{my_t_pre.normalize():f}",
                              "disc": f"{(my_disc_tot*100):.2f}%"},
                "forecast_weights": forecast_weights or [],
                "forecast_rewards": {str(k): str(v) for k,v in (forecast_rewards or {}).items()},
                "my_uid": my_uid,
                "my_forecast": str(my_forecast_reward) if my_forecast_reward is not None else None,
            }, separators=(",",":")), flush=True)
        else:
            sid0, a_tot, t_post, _, disc_txt = rows[0]
            log(f"TOTAL {a_tot} α → {t_post} TAO (discount {disc_txt}) at block {head}")
            for sid, a_s, tao_post_s, _, disc_s in rows[1:]:
                log(f"  Subnet {sid:<3}: {a_s} α → {tao_post_s} TAO (disc {disc_s})")
            if args.coldkey:
                if my_rows:
                    log(f"MY  {args.coldkey[:12]}… {(my_a_raw/PLANCK_D).normalize():f} α → "
                        f"{my_t_post.normalize():f} TAO (disc {(my_disc_tot*100):.2f}%)")
                    for sid, a_s, tao_post_s in my_rows:
                        log(f"    Subnet {sid:<3}: {a_s} α → {tao_post_s} TAO")
                else:
                    log(f"MY  {args.coldkey[:12]}… no deposits yet.")
                if forecast_weights is not None:
                    if my_uid is None:
                        log("FORECAST – cold-key not on metagraph.")
                    else:
                        w = forecast_weights[my_uid]
                        log(f"FORECAST – UID {my_uid} {my_forecast_reward or Decimal(0):.4f} "
                            f"SN-73 (weight {w:.2%})")

        await asyncio.sleep(args.interval)


def main():
    asyncio.run(_monitor(_arg_parser().parse_args()))


if __name__ == "__main__":
    main()
