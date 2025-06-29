#!/usr/bin/env python3
# ====================================================================== #
# auction_watch.py                                                       #
# ====================================================================== #
"""
Real‑time auction dashboard (stdout or JSON).

Key upgrades
------------
1.   **Colourised logging** via ``ColoredLogger``.
2.   **Per‑subnet filtering** – honours ``--subnet`` for metrics.
3.   **Tempo safety** – a fresh tempo is fetched every loop.
4.   **Incremental scanning** – only new blocks are queried.
5.   **Full‑precision math** – displays floats, keeps ``Decimal`` inside.
6.   **Optional JSON output** – ``--json`` pipes raw metrics downstream.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import List

import bittensor as bt

from metahash.config import BAG_SN73
from metahash.utils.bond_utils import get_bond_curve
from metahash.validator.alpha_transfers import AlphaTransfersScanner
from metahash.validator.rewards import (
    TransferEvent,
    cast_events,
    attach_prices,
    apply_slippage,
    allocate_bond_curve,
)

from metahash.utils.epoch_metrics import (
    summarize_sn73_rewards,
    summarize_alpha_deposits,
)
from metahash.utils.colors import ColoredLogger as log


# ───────────────────────────── CLI ─────────────────────────────── #

def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Live SN‑73 / α auction monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--subnet", type=int, default=73,
                   help="Subnet ID to monitor")
    p.add_argument("--network", default="finney",
                   help="Bittensor network (finney, nakamoto, …)")
    p.add_argument("--treasury", default=None,
                   help="Treasury cold‑key (auto‑detect if omitted)")
    p.add_argument("--interval", type=float, default=12.0,
                   help="Polling interval in seconds (≈ one block)")
    p.add_argument("--exclude-coldkey", action="append", default=[],
                   help="Your own cold‑keys to exclude from α totals "
                        "(may be given multiple times)")
    p.add_argument("--json", action="store_true",
                   help="Print full JSON instead of human text")
    return p


# ────────────────────────── helpers ────────────────────────────── #

def _epoch_start(block: int, tempo: int) -> int:
    """Epoch heads are spaced **tempo + 1** blocks apart."""
    epoch_len = tempo + 1
    return block - (block % epoch_len)


async def _pricing_provider(
    st: bt.AsyncSubtensor, subnet_id: int, start: int, end: int
):
    spot = await st.alpha_tao_avg_price(subnet_id=subnet_id,
                                        start=start, end=end)
    # tiny shim matching the interface expected by attach_prices()
    return type("Price", (), {"tao": spot, "rao": None})


async def _pool_depth(st: bt.AsyncSubtensor, subnet_id: int) -> int:
    return await st.alpha_pool_depth(subnet_id=subnet_id)


# ────────────────────────── main logic ─────────────────────────── #

async def _monitor(args) -> None:
    # 1. chain connection ------------------------------------------------
    st = bt.AsyncSubtensor(network=args.network)
    await st.initialize()
    log.success(f"Connected to network “{args.network}”")

    # 2. curve params (β, c0, r_min) ------------------------------------
    curve = get_bond_curve()
    beta, c0, r_min = curve.beta, curve.c0, curve.r_min

    # 3. alpha scanner ---------------------------------------------------
    scanner = AlphaTransfersScanner(st, dest_coldkey=args.treasury)

    # 4. epoch setup -----------------------------------------------------
    head = await st.get_current_block()
    tempo = await st.tempo(args.subnet)           # tempo for monitored subnet
    epoch_start = _epoch_start(head, tempo)

    next_block = epoch_start                      # incremental cursor
    deposits: List = []                           # full epoch so far

    # 5. main loop -------------------------------------------------------
    while True:
        try:
            head = await st.get_current_block()
            if next_block > head:                 # nothing new
                await asyncio.sleep(args.interval)
                continue

            # ensure tempo stays correct even if the subnet upgrades it
            tempo = await st.tempo(args.subnet)
            epoch_start = _epoch_start(head, tempo)

            # -------- scan only the new range --------------------------
            raw_events = await scanner.scan(next_block, head)
            new_events: List[TransferEvent] = [
                TransferEvent(
                    coldkey=ev.dest_coldkey or "",
                    subnet_id=ev.subnet_id,
                    amount_rao=ev.amount_rao,
                )
                for ev in raw_events
                if ev.subnet_id == args.subnet                     # filter
            ]
            if not new_events:
                next_block = head + 1
                await asyncio.sleep(args.interval)
                continue

            new_deposits = cast_events(new_events)

            # -------- enrich price + slippage --------------------------
            await attach_prices(
                new_deposits,
                pricing=lambda sid, s, e: _pricing_provider(st, sid, s, e),
                epoch_start=epoch_start,
                epoch_end=head,
            )
            await apply_slippage(
                new_deposits,
                pool_depth_of=lambda sid: _pool_depth(st, sid),
            )

            # -------- bond‑curve minting for these deposits ------------
            allocate_bond_curve(
                deposits=new_deposits,
                bag_sn73=BAG_SN73,
                c0=c0,
                beta=beta,
                r_min=r_min,
            )

            # -------- merge into epoch state ---------------------------
            deposits.extend(new_deposits)
            next_block = head + 1

            # -------- metrics -----------------------------------------
            rewards = summarize_sn73_rewards(deposits)
            other_alpha = summarize_alpha_deposits(
                deposits,
                exclude_coldkeys=set(args.exclude_coldkey),
            )

            # -------- output ------------------------------------------
            if args.json:
                payload = {
                    "timestamp": int(time.time()),
                    "block": head,
                    "epoch_start": epoch_start,
                    "subnet": args.subnet,
                    "sn73_rewards": {
                        "aggregate": {
                            k: str(v) for k, v in rewards["aggregate"].items()
                        }
                    },
                    "alpha_from_others": {
                        "aggregate": {
                            k: str(v) for k, v in other_alpha["aggregate"].items()
                        }
                    },
                }
                print(json.dumps(payload, separators=(",", ":")))
                sys.stdout.flush()
                continue

            # human text ------------------------------------------------
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            bar = "-" * 60
            log.info(f"\n[{ts}]  block {head}  (epoch start {epoch_start})")
            log.info(bar, color="gray")

            agg_r = rewards["aggregate"]
            log.success(f"SN‑73 minted : {float(agg_r['sn73']):.2f}")
            log.info(f"α deposited  : {float(agg_r['alpha']):.3f}")
            log.info(f"TAO value    : {float(agg_r['tao_value']):.3f}  "
                     f"(post‑slip {float(agg_r['tao_post_slip']):.3f})")

            agg_a = other_alpha["aggregate"]
            log.info("\nα from *others* this epoch:", color="cyan")
            log.info(f"  α          {float(agg_a['alpha']):.3f}")
            log.info(f"  TAO value  {float(agg_a['tao_value']):.3f}  "
                     f"(post‑slip {float(agg_a['tao_post_slip']):.3f})")

            # verbose per‑subnet breakdown (only the monitored subnet here)
            m = rewards["per_subnet"].get(args.subnet)
            if m:
                log.info(f"  · subnet {args.subnet}: "
                         f"{float(m['sn73']):.2f} SN‑73, "
                         f"{float(m['alpha']):.3f} α")

            sys.stdout.flush()

        except Exception as err:
            log.error(f"[ERROR] {err}")
            await asyncio.sleep(args.interval)


def main() -> None:
    asyncio.run(_monitor(_arg_parser().parse_args()))


# ----------------------------------------------------------------------- #
if __name__ == "__main__":
    main()
