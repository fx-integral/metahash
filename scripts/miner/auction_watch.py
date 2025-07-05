#!/usr/bin/env python3
# --------------------------------------------------------------------------- #
#  auction_watch.py – Subnet auction monitor with optional automatic bidding  #
#
#  2025‑07‑05 • wallet loading uses --wallet.name / --wallet.hotkey           #
#             • cold‑key defaults to wallet.coldkey.ss58                      #
#             • validator‑hotkey defaults to wallet.hotkey.ss58               #
#             • NEW: seeds the auction automatically at baseline discount     #
#             • 2025‑07‑05 (FIX) all α amounts are TAO, not RAO; transfers    #
#               use bt.Balance.from_rao(...)                                  #
#             • 2025‑07‑05 (CHANGE) --max-discount is now given in PERCENT    #
#               (e.g. 10 == 10 %), not as a fraction (0.1)                    #
#             • 2025‑07‑05 (UI) status line now shows                         #
#               <auction‑state> | Epoch <id> [start–end] (Block <head>)       #
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
    D_START as _D_START_FLOAT,
    BAG_SN73,
)
from metahash.utils.colors import ColoredLogger as clog
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.utils.bond_utils import get_bond_curve, quote_alpha_cost
from metahash.utils.wallet_utils import load_wallet, transfer_alpha
from metahash.config import DEFAULT_BITTENSOR_NETWORK


# ───────────────────────── precision / constants ────────────────────────── #

getcontext().prec = 60
D_START = Decimal(str(_D_START_FLOAT))        # float → exact Decimal (fraction)
RAO_PER_TAO = Decimal(10) ** 9                # 1 TAO = 10⁹ rao
bt.logging.set_info()

# ───────────────────────────── CLI ────────────────────────────── #


def _arg_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser.

    NOTE: --max-discount is now specified in **percent** (e.g. 10, 20),
    not as a fraction (0.1, 0.2).  The value is converted to a
    fraction after parsing so all internal code still works on fractions.
    """
    p = argparse.ArgumentParser(
        description="Incremental subnet auction monitor with live reward "
        "forecast and optional automatic α→TAO bidding.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # chain / network
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK, help="Bittensor network name")

    # subnet / auction timing
    p.add_argument(
        "--netuid", type=int, default=73, help="Subnet uid whose auction you monitor / bid on"
    )
    p.add_argument(
        "--delay",
        type=int,
        default=AUCTION_DELAY_BLOCKS,
        help="Blocks after epoch start when the auction opens",
    )
    p.add_argument("--interval", type=float, default=12.0, help="Polling interval in seconds")

    # treasury & cold‑key controls
    p.add_argument("--treasury", default=TREASURY_COLDKEY, help="Treasury cold‑key that receives α")

    # output
    p.add_argument("--json", action="store_true", help="Emit machine‑readable JSON")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING"],
        help="Console verbosity",
    )
    p.add_argument(
        "--min-display-alpha",
        type=Decimal,
        default=Decimal("0"),
        help="Suppress rows with less than this α (TAO) deposited",
    )

    # ─── automatic bidding parameters ─── #
    p.add_argument(
        "--validator-hotkey",
        required=True,
        help="Validator hotkey SS58 that will receive α (defaults to wallet hotkey)",
    )
    p.add_argument(
        "--max-alpha",
        type=Decimal,
        default=Decimal("0"),
        help="Maximum total α (TAO) transferred this auction (0 = unlimited)",
    )
    p.add_argument(
        "--step-alpha",
        type=Decimal,
        default=Decimal("1"),
        help="α (TAO) amount for each transfer",
    )
    p.add_argument(
        "--max-discount",
        type=Decimal,
        default=D_START * Decimal(100),    # show same default but in percent
        help="Stop bidding when discount exceeds this percentage "
             "(e.g. 12.5 for 12.5 %)",
    )

    # wallet flags (match bittensor‑CLI style)
    p.add_argument(
        "--wallet.name",
        dest="wallet_name",
        default=None,
        help="Wallet name (defaults $BT_WALLET_NAME or 'default')",
    )
    p.add_argument(
        "--wallet.hotkey",
        dest="wallet_hotkey",
        default=None,
        help="Wallet hotkey (defaults $BT_WALLET_HOTKEY or 'default')",
    )
    return p


# ──────────────────── helper functions ────────────────────── #

def _format_epoch_range(start: int, length: int) -> str:
    """`start` and `length` → `"<start>-<end>"`"""
    return f"{start}-{start + length - 1}"


def _status_line(
    head: int,
    auction_open: int,
    epoch_start: int,
    epoch_len: int,
) -> str:
    """
    Compose a human‑readable status line:

        <Auction Waiting/Active …> │ Epoch <id> [<start>-<end>] (Block <head>)
    """
    state = (
        f"Auction Active (started {head - auction_open} blocks ago)"
        if head >= auction_open
        else f"Auction Waiting ({auction_open - head} blocks to start)"
    )
    epoch_id = head // epoch_len
    epoch_range = _format_epoch_range(epoch_start, epoch_len)
    return f"{state} │ Epoch {epoch_id} [{epoch_range}] (Block {head})"


# ─────────────────────────── main coroutine ──────────────────────────── #

async def _monitor(args: argparse.Namespace):
    # ───────── convert CLI percent → fraction ─────────
    # (if user typed 10 we want 0.10 internally)
    args.max_discount = args.max_discount / Decimal(100)

    # ───────── logging ─────────
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )
    debug = logging.getLogger("watch").debug

    def warn(m): return clog.warning("[auction] " + m)  # colored warning
    def info(m): return clog.info("[auction] " + m, color="cyan")

    # ───────── wallet ─────────
    wallet = load_wallet(coldkey_name=args.wallet_name , hotkey_name=args.wallet_hotkey)

    autobid_enabled = bool(wallet and args.validator_hotkey)

    # ───────── chain connection ─────────
    st = bt.AsyncSubtensor(network=args.network)
    await st.initialize()

    # scanner for α‑transfers into treasury
    from metahash.validator.alpha_transfers import AlphaTransfersScanner
    scanner = AlphaTransfersScanner(st, dest_coldkey=args.treasury)

    # ───────── runtime state ─────────
    epoch_start = auction_open = next_block = None
    pricing_provider = depth_provider = None

    # α deposits per subnet (TAO)
    deposits: Dict[int, Decimal] = defaultdict(lambda: Decimal(0))
    my_deposits: Dict[int, Decimal] = defaultdict(lambda: Decimal(0))

    events: List[TransferEvent] = []
    forecast_weights: List[float] | None = None
    forecast_rewards: Dict[int, Decimal] | None = None
    my_uid: Optional[int] = None
    my_forecast_reward: Optional[Decimal] = None

    # autobid bookkeeping – all in TAO
    max_alpha_tao: Optional[Decimal] = None if args.max_alpha <= 0 else args.max_alpha
    step_alpha_tao: Decimal = args.step_alpha
    alpha_sent_tao: Decimal = Decimal(0)

    info(f"Auction target subnet = {args.netuid}")
    if autobid_enabled:
        info("[auction]"
             f"Auto‑bid ON → step={step_alpha_tao} α, "
             f"max={max_alpha_tao or '∞'} α, "
             f"max_discount={args.max_discount:.2%}"
             )

    # ───────── main loop ─────────
    while True:
        print()
        head = await st.get_current_block()
        tempo = await st.tempo(args.netuid)
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
            alpha_sent_tao = Decimal(0)

            prev_start, prev_end = max(0, epoch_start - epoch_len), epoch_start - 1
            pricing_provider = _make_pricing_provider(st, prev_start, prev_end)
            depth_provider = _make_depth_provider(st, prev_start, prev_end)

            meta = await st.metagraph(args.netuid)
            print(meta.hotkeys)
            input()

            uid_resolver = {ck: uid for uid, ck in enumerate(meta.coldkeys)}.get
            my_uid = uid_resolver(wallet.coldkey.ss58_address) if wallet.coldkey.ss58_address else None
            info(f"⟫ NEW EPOCH {epoch_start // epoch_len} [{_format_epoch_range(epoch_start, epoch_len)}]")

        # combined status banner (new order & contents)
        info(_status_line(head, auction_open, epoch_start, epoch_len))

        # wait for auction open
        if head < auction_open:
            await asyncio.sleep(args.interval)
            continue

        # incremental scan
        if next_block <= head:
            debug(f"Scanning transfers {next_block}→{head}")
            raw = await scanner.scan(next_block, head + AUCTION_DELAY_BLOCKS)
            for ev in raw:
                src_ck = ev.src_coldkey or ""
                a_tao = Decimal(ev.amount_rao) / RAO_PER_TAO
                deposits[ev.subnet_id] += a_tao
                events.append(
                    TransferEvent(
                        src_coldkey=src_ck,
                        dest_coldkey=ev.dest_coldkey or args.treasury,
                        subnet_id=ev.subnet_id,
                        amount_rao=ev.amount_rao,  # keep raw value for reward calc
                    )
                )
                if wallet.coldkey.ss58_address and src_ck == wallet.coldkey.ss58_address:
                    my_deposits[ev.subnet_id] += a_tao
            next_block = head + 1
            debug(f"Processed {len(raw)} α‑transfer(s)")

        # reward forecast
        try:
            curve = get_bond_curve()

            class _Val:  # dummy validator adapter
                def __init__(self, n):
                    self._uids = list(range(n))

                def get_miner_uids(self):
                    return self._uids

            meta = await st.metagraph(args.netuid)
            uid_resolver = {ck: uid for uid, ck in enumerate(meta.coldkeys)}.get

            async def _uid_of_ck(ck):
                return uid_resolver(ck)

            forecast = await compute_epoch_rewards(
                validator=_Val(len(meta.coldkeys)),
                events=events,
                pricing=pricing_provider,
                uid_of_coldkey=_uid_of_ck,
                start_block=auction_open,
                end_block=head,
                bag_sn73=BAG_SN73,
                c0=curve.c0,
                beta=curve.beta,
                r_min=curve.r_min,
                pool_depth_of=depth_provider,
                log=lambda m: debug("FORECAST – " + m),
            )
            forecast_weights = forecast.weights
            forecast_rewards = forecast.rewards_per_miner
            my_forecast_reward = forecast_rewards.get(my_uid, Decimal(0)) if my_uid is not None else None
        except Exception as e:  # noqa: BLE001
            warn(f"FORECAST failed: {e}")

        # aggregates & optional bid
        rows: List[Tuple[int, str, str, str, str]] = []
        a_tot_tao = t_post_tot = t_pre_tot = Decimal(0)

        async def _maybe_bid(sid: int, discount: Decimal):
            nonlocal alpha_sent_tao
            if sid != args.netuid or not autobid_enabled:
                return
            if discount > args.max_discount:
                return
            if max_alpha_tao is not None and alpha_sent_tao >= max_alpha_tao:
                return

            amount_tao: Decimal = (
                step_alpha_tao
                if max_alpha_tao is None
                else min(step_alpha_tao, max_alpha_tao - alpha_sent_tao)
            )
            if amount_tao <= 0:
                return

            try:
                ok = await transfer_alpha(
                    subtensor=st,
                    wallet=wallet,
                    hotkey_ss58=args.validator_hotkey,
                    origin_and_dest_netuid=args.netuid,
                    dest_coldkey_ss58=args.treasury,
                    amount=bt.Balance.from_tao(amount_tao),
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                )
            except Exception as exc:  # noqa: BLE001
                warn(f"transfer_alpha failed: {exc}")
                return
            if ok:
                alpha_sent_tao += amount_tao
                clog.success(
                    f"AUTO‑BID sent {amount_tao} α "
                    f"(cumulative {alpha_sent_tao} α) "
                    f"at discount {discount:.2%}"
                )

        # iterate deposits
        for sid, a_tao in sorted(deposits.items()):
            if a_tao <= 0:
                continue

            a_raw = int((a_tao * RAO_PER_TAO).to_integral_value())
            price_bal = await pricing_provider(sid)
            if not price_bal or price_bal.tao == 0:
                continue

            tao_post, tao_pre, disc = quote_alpha_cost(
                a_raw,
                price_tao=Decimal(str(price_bal.tao)),
                depth_rao=await depth_provider(sid),
                c0=D_START,
            )
            await _maybe_bid(sid, disc)

            if a_tao < args.min_display_alpha:
                continue
            rows.append(
                (
                    sid,
                    f"{a_tao.normalize():f}",
                    f"{tao_post.normalize():f}",
                    f"{tao_pre.normalize():f}",
                    f"{(disc*100):.2f}%",
                )
            )
            a_tot_tao += a_tao
            t_post_tot += tao_post
            t_pre_tot += tao_pre

        disc_tot = (Decimal(1) - t_post_tot / t_pre_tot) if t_pre_tot else D_START
        rows.insert(
            0,
            (
                -1,
                f"{a_tot_tao.normalize():f}",
                f"{t_post_tot.normalize():f}",
                f"{t_pre_tot.normalize():f}",
                f"{(disc_tot*100):.2f}%",
            ),
        )

        # ────────────────────── NEW: seed auction if empty ───────────────────
        if (
            autobid_enabled
            and head >= auction_open
            and not deposits.get(args.netuid)      # no α yet in this subnet
            and alpha_sent_tao == 0                # haven’t seeded before
        ):
            await _maybe_bid(args.netuid, D_START)
        # ─────────────────────────────────────────────────────────────────────

        # personal summary
        my_rows: List[Tuple[int, str, str]] = []
        my_a_tao = my_t_post = my_t_pre = Decimal(0)
        for sid, a_tao in sorted(my_deposits.items()):
            price_bal = await pricing_provider(sid)
            if not price_bal or price_bal.tao == 0:
                continue
            a_raw = int((a_tao * RAO_PER_TAO).to_integral_value())
            tao_post, tao_pre, _ = quote_alpha_cost(
                a_raw,
                price_tao=Decimal(str(price_bal.tao)),
                depth_rao=await depth_provider(sid),
                c0=D_START,
            )
            my_rows.append((sid, f"{a_tao.normalize():f}", f"{tao_post.normalize():f}"))
            my_a_tao += a_tao
            my_t_post += tao_post
            my_t_pre += tao_pre
        my_disc_tot = (Decimal(1) - my_t_post / my_t_pre) if my_t_pre else D_START

        # ───────── output ─────────
        if args.json:
            print(
                json.dumps(
                    {
                        "t": int(time.time()),
                        "blk": head,
                        "epoch": epoch_start,
                        "rows": rows,
                        "my_rows": my_rows,
                        "my_totals": {
                            "alpha": f"{my_a_tao.normalize():f}",
                            "tao_post": f"{my_t_post.normalize():f}",
                            "tao_pre": f"{my_t_pre.normalize():f}",
                            "disc": f"{(my_disc_tot*100):.2f}%",
                        },
                        "forecast_weights": forecast_weights or [],
                        "forecast_rewards": {str(k): str(v) for k, v in (forecast_rewards or {}).items()},
                        "my_uid": my_uid,
                        "my_forecast": str(my_forecast_reward) if my_forecast_reward is not None else None,
                        "alpha_sent": str(alpha_sent_tao),
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
        else:
            total_a, total_tao, _, total_disc = rows[0][1:]
            info(f"TOTAL {total_a} α → {total_tao} TAO (discount {total_disc}) blk={head}")
            for sid, a_s, tao_post_s, _, disc_s in rows[1:]:
                info(f"  Subnet {sid:<3}: {a_s} α → {tao_post_s} TAO (disc {disc_s})")
            if wallet.coldkey.ss58_address:
                if my_rows:
                    info(
                        f"MY  {wallet.coldkey.ss58_address[:12]}… {my_a_tao.normalize():f} α → "
                        f"{my_t_post.normalize():f} TAO (disc {(my_disc_tot*100):.2f}%)"
                    )
                    for sid, a_s, tao_post_s in my_rows:
                        info(f"    Subnet {sid:<3}: {a_s} α → {tao_post_s} TAO")
                else:
                    info(f"MY  {wallet.coldkey.ss58_address[:12]}… no deposits yet.")
            if my_uid is not None and forecast_weights is not None:
                w = forecast_weights[my_uid]
                info(
                    f"FORECAST – UID {my_uid} {my_forecast_reward or Decimal(0):.4f} "
                    f"SN‑{args.netuid} (weight {w:.2%})"
                )
            if autobid_enabled:
                info(
                    f"AUTO‑BID status: sent {alpha_sent_tao} α "
                    f"of {max_alpha_tao or '∞'} α limit"
                )

        await asyncio.sleep(args.interval)


# ────────────────────────── providers (defined late) ───────────────────── #

def _make_pricing_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _pricing(subnet_id: int, *_):
        return await average_price(subnet_id, start_block=start, end_block=end, st=st)

    return _pricing


def _make_depth_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _depth(subnet_id: int):
        d = await average_depth(subnet_id, start_block=start, end_block=end, st=st)
        return int(getattr(d, "rao", d or 0))

    return _depth


# ───────────────────────── entrypoints ──────────────────────── #

def main() -> None:
    asyncio.run(_monitor(_arg_parser().parse_args()))


if __name__ == "__main__":
    main()
