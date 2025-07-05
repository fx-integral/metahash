#!/usr/bin/env python3
# --------------------------------------------------------------------------- #
#  auction_watch.py – Subnet auction monitor with optional automatic bidding  #
#
#  2025‑07‑05 • rewritten for “direct‑value” rewards (no bond curve)          #
#             • discount = 1 – TAO_spent / TAO_value_of_expected_SN‑73        #
#             • SN‑73 price fetched each loop with subnet_utils.subnet_price  #
#             • new flag --safety-buffer,                                     #
#               --max-discount kept (alias of --target-discount)              #
# --------------------------------------------------------------------------- #

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from decimal import Decimal, getcontext
from typing import Dict, List, Optional, Tuple

import bittensor as bt
from metahash.config import (
    AUCTION_DELAY_BLOCKS,
    TREASURY_COLDKEY,
    BAG_SN73,
    D_START as _D_START_FLOAT,
    DEFAULT_BITTENSOR_NETWORK,
)
from metahash.utils.colors import ColoredLogger as clog
from metahash.utils.subnet_utils import (
    average_price,
    average_depth,
    subnet_price,         # ← spot SN‑73 price
)
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.utils.bond_utils import quote_alpha_cost
from metahash.utils.wallet_utils import load_wallet, transfer_alpha

# ───────────────────────── precision / constants ────────────────────────── #

getcontext().prec = 60
D_START = Decimal(str(_D_START_FLOAT))        # float → exact Decimal
RAO_PER_TAO = Decimal(10) ** 9                # 1 TAO = 10⁹ rao
bt.logging.set_info()

# ───────────────────────────── CLI ────────────────────────────── #


def _arg_parser() -> argparse.ArgumentParser:
    """
    Build the argument parser.

    --max-discount **or** --target-discount specify the minimum discount
    you are willing to accept (percent).  They are synonyms.
    """
    p = argparse.ArgumentParser(
        description="Incremental subnet auction monitor with live reward "
        "forecast and optional automatic α→TAO bidding (no bond‑curve).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # chain / network
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK, help="Bittensor network name")

    # subnet / auction timing
    p.add_argument(
        "--netuid",
        type=int,
        required=True,
        help="Subnet uid whose auction you monitor / bid in (α is sold here)",
    )
    p.add_argument(
        "--meta-netuid",
        type=int,
        default=73,
        help="Subnet uid whose metagraph is used for UID look‑ups and "
             "reward forecasting (defaults to 73).",
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
        help="Validator hotkey SS58 that will receive α",
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
    # new logic: target discount (percent)
    p.add_argument(
        "--target-discount",
        "--max-discount",          # alias for backward compatibility
        dest="target_discount",
        type=Decimal,
        default=Decimal("10"),
        help="Minimum discount you accept (percent, e.g. 12.5)",
    )
    p.add_argument(
        "--safety-buffer",
        type=Decimal,
        default=Decimal("1.25"),
        help="Assume others will add this × more TAO before epoch close "
             "(e.g. 1.25 = +25 %) when simulating a bid",
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
    """Return formatted epoch range '<start>-<end>'."""
    return f"{start}-{start + length - 1}"


def _status_line(
    head: int,
    auction_open: int,
    epoch_start: int,
    epoch_len: int,
) -> str:
    """
    Compose a human‑readable status line:

        <Auction Waiting/Active …> │ Epoch <id> [start‑end] (Block <head>)
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
    args.target_discount = args.target_discount / Decimal(100)

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
    wallet = load_wallet(coldkey_name=args.wallet_name, hotkey_name=args.wallet_hotkey)

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
    forecast_rewards: Dict[int, Decimal] | None = None
    my_uid: Optional[int] = None
    my_forecast_reward: Optional[Decimal] = None

    # autobid bookkeeping – all in TAO
    max_alpha_tao: Optional[Decimal] = None if args.max_alpha <= 0 else args.max_alpha
    step_alpha_tao: Decimal = args.step_alpha
    alpha_sent_tao: Decimal = Decimal(0)

    info(f"Auction target subnet = {args.netuid}  |  meta subnet = {args.meta_netuid}")
    if autobid_enabled:
        info(
            f"Auto‑bid ON → step={step_alpha_tao} α, "
            f"max={max_alpha_tao or '∞'} α, "
            f"target_discount={args.target_discount:.2%}, "
            f"safety_buffer={args.safety_buffer}"
        )

    # ───────── main loop ─────────
    while True:
        print()
        head = await st.get_current_block()
        tempo = await st.tempo(args.netuid)      # tempo of the **auction subnet**
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

            # ── metagraph is always taken from *meta‑netuid* ──
            meta = await st.metagraph(args.meta_netuid)
            uid_resolver = {ck: uid for uid, ck in enumerate(meta.coldkeys)}.get
            my_uid = uid_resolver(wallet.coldkey.ss58_address) if wallet.coldkey.ss58_address else None
            info(f"⟫ NEW EPOCH {epoch_start // epoch_len} "
                 f"[{_format_epoch_range(epoch_start, epoch_len)}]")

        # combined status banner
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

        # reward forecast (always on meta subnet) – no bond curve
        try:
            class _Val:  # dummy validator adapter
                def __init__(self, n):
                    self._uids = list(range(n))

                def get_miner_uids(self):
                    return self._uids

            meta = await st.metagraph(args.meta_netuid)
            uid_resolver = {ck: uid for uid, ck in enumerate(meta.coldkeys)}.get

            async def _uid_of_ck(ck):
                return uid_resolver(ck)

            rewards = await compute_epoch_rewards(
                validator=_Val(len(meta.coldkeys)),
                events=events,
                pricing=pricing_provider,
                uid_of_coldkey=_uid_of_ck,
                start_block=auction_open,
                end_block=head,
                pool_depth_of=depth_provider,
                log=lambda m: debug("FORECAST – " + m),
            )
            forecast_rewards = {uid: r for uid, r in enumerate(rewards)}
            my_forecast_reward = (
                forecast_rewards.get(my_uid, Decimal(0)) if my_uid is not None else None
            )
        except Exception as e:  # noqa: BLE001
            warn(f"FORECAST failed: {e}")

        # aggregates & optional bid
        rows: List[Tuple[int, str, str, str, str]] = []
        a_tot_tao = t_post_tot = t_pre_tot = Decimal(0)

        # spot SN‑73 price (TAO)
        spot_bal = await subnet_price(args.meta_netuid, st=st)
        sn73_price_tao: Decimal = Decimal(str(spot_bal.tao)) if spot_bal else Decimal(0)

        # ---------- inner helpers ---------- #
        async def _bid_if_worthwhile():
            """Decide whether to send one α‑tranche."""
            nonlocal alpha_sent_tao, t_post_tot, my_t_post
            if not autobid_enabled or sn73_price_tao == 0:
                return

            # pessimistic future totals
            A_i_now = my_t_post
            A_i_after = A_i_now + step_alpha_tao
            A_sigma_now = t_post_tot
            others_now = A_sigma_now - A_i_now
            A_sigma_future = others_now * args.safety_buffer + A_i_after

            if A_sigma_future == 0:
                return  # avoid div/0

            share_future = A_i_after / A_sigma_future
            sn73_future = share_future * BAG_SN73
            value_future = sn73_future * sn73_price_tao
            if value_future == 0:
                return
            discount_future = Decimal(1) - A_i_after / value_future

            debug(
                f"Simulated bid → future discount {discount_future:.2%} "
                f"(target {args.target_discount:.2%})"
            )

            if discount_future < args.target_discount:
                return  # not attractive enough

            # caps
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
                    f"at discount ≥ {discount_future:.2%}"
                )
        # ---------- end helpers ---------- #

        # iterate deposits
        my_t_post = Decimal(0)
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

            # our own discount logic uses tao_post (post‑slip spend)
            if sid == args.netuid and wallet.coldkey.ss58_address:
                my_t_post = my_deposits[sid] if sid in my_deposits else Decimal(0)

            if a_tao < args.min_display_alpha:
                # still need totals!
                a_tot_tao += a_tao
                t_post_tot += tao_post
                t_pre_tot += tao_pre
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

        # ────────────────────── AUTO‑BID decision ──────────────────────
        await _bid_if_worthwhile()

        # ───────── output ─────────
        if args.json:
            print(
                json.dumps(
                    {
                        "t": int(time.time()),
                        "blk": head,
                        "epoch": epoch_start,
                        "rows": rows,
                        "sn73_price": str(sn73_price_tao),
                        "forecast_rewards": {
                            str(k): str(v) for k, v in (forecast_rewards or {}).items()
                        },
                        "my_uid": my_uid,
                        "my_forecast": (
                            str(my_forecast_reward) if my_forecast_reward is not None else None
                        ),
                        "alpha_sent": str(alpha_sent_tao),
                        "meta_netuid": args.meta_netuid,
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
                if my_t_post > 0:
                    info(
                        f"MY  {wallet.coldkey.ss58_address[:12]}… "
                        f"{my_t_post.normalize():f} TAO spent (post‑slip)"
                    )
                else:
                    info(f"MY  {wallet.coldkey.ss58_address[:12]}… no deposits yet.")
            if my_uid is not None and forecast_rewards is not None:
                fr = forecast_rewards.get(my_uid, Decimal(0))
                info(
                    f"FORECAST – UID {my_uid} {fr:.4f} SN‑{args.meta_netuid} "
                    f"(spot value {(fr*sn73_price_tao):.4f} TAO)"
                )
            if autobid_enabled:
                info(
                    f"AUTO‑BID status: sent {alpha_sent_tao} α "
                    f"of {max_alpha_tao or '∞'} α limit"
                )

        await asyncio.sleep(args.interval)


# ───────────────────────── providers (defined late) ───────────────────── #

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
