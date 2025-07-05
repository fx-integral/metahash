#!/usr/bin/env python3
# --------------------------------------------------------------------------- #
#  auction_watch.py 
# --------------------------------------------------------------------------- #


from __future__ import annotations

import argparse
import asyncio
import json
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
    subnet_price,      # spot SN‑73 price
)
from metahash.validator.rewards import compute_epoch_rewards, TransferEvent
from metahash.utils.bond_utils import quote_alpha_cost
from metahash.utils.wallet_utils import load_wallet, transfer_alpha

# ───────────────────────── precision / constants ────────────────────────── #
getcontext().prec = 60
RAO_PER_TAO = Decimal(10) ** 9
D_START = Decimal(str(_D_START_FLOAT))
bt.logging.set_info()

# ───────────────────────────── CLI ────────────────────────────── #


def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Subnet auction monitor with live reward forecast and "
        "optional automatic α→TAO bidding (no bond‑curve).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # network / subnets
    p.add_argument("--network", default=DEFAULT_BITTENSOR_NETWORK)
    p.add_argument("--netuid", type=int, required=True, help="Auction subnet uid")
    p.add_argument(
        "--meta-netuid",
        type=int,
        default=73,
        help="Subnet uid used for UID look‑ups / reward forecast",
    )

    # timing
    p.add_argument("--delay", type=int, default=AUCTION_DELAY_BLOCKS)
    p.add_argument("--interval", type=float, default=12.0)

    # treasury
    p.add_argument("--treasury", default=TREASURY_COLDKEY)

    # output / verbosity
    p.add_argument("--json", action="store_true")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    p.add_argument("--min-display-alpha", type=Decimal, default=Decimal("0"))

    # bidding
    p.add_argument("--validator-hotkey", required=True)
    p.add_argument("--max-alpha", type=Decimal, default=Decimal("0"))
    p.add_argument("--step-alpha", type=Decimal, default=Decimal("1"))
    p.add_argument(
        "--max-discount",            # % you accept to LOSE
        dest="max_discount",
        type=Decimal,
        default=Decimal("20"),
        help="Maximum discount (loss) you tolerate, in percent. "
             "20 = you accept receiving SN‑73 worth 80 % of TAO spent.",
    )
    p.add_argument(
        "--safety-buffer",
        type=Decimal,
        default=Decimal("1.25"),
        help="Assume others add ×this TAO before epoch close when simulating a bid",
    )

    # wallet flags
    p.add_argument("--wallet.name", dest="wallet_name", default=None)
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default=None)
    return p

# ──────────────────── helpers ────────────────────── #


def _format_range(start: int, length: int) -> str:
    return f"{start}-{start + length - 1}"


def _status(head: int, open_: int, start: int, length: int) -> str:
    state = (
        f"Auction Active (started {head - open_} blocks ago)"
        if head >= open_
        else f"Auction Waiting ({open_ - head} blocks to start)"
    )
    eid = head // length
    return f"{state} │ Epoch {eid} [{_format_range(start, length)}] (Block {head})"

# ─────────────────── main coroutine ─────────────────── #


async def _monitor(args: argparse.Namespace):
    args.max_discount = args.max_discount / Decimal(100)  # % → fraction

    def warn(m): return clog.warning("[auction] " + m)
    def info(m): return clog.info("[auction] " + m, color="cyan")

    # wallet
    wallet = load_wallet(coldkey_name=args.wallet_name, hotkey_name=args.wallet_hotkey)
    autobid = bool(wallet and args.validator_hotkey)

    # chain connection
    st = bt.AsyncSubtensor(network=args.network)
    await st.initialize()

    # transfer scanner
    from metahash.validator.alpha_transfers import AlphaTransfersScanner
    scanner = AlphaTransfersScanner(st, dest_coldkey=args.treasury)

    # state
    epoch_start = auction_open = next_block = None
    pricing_provider = depth_provider = None
    deposits: Dict[int, Decimal] = defaultdict(Decimal)
    events: List[TransferEvent] = []
    my_uid: Optional[int] = None
    alpha_sent_tao = Decimal(0)
    step_alpha = args.step_alpha
    max_alpha = None if args.max_alpha <= 0 else args.max_alpha

    info(f"Auction subnet = {args.netuid}  |  meta subnet = {args.meta_netuid}")
    if autobid:
        info(
            f"Auto‑bid ON  step={step_alpha} α, max={max_alpha or '∞'} α, "
            f"max_loss={args.max_discount:.2%}, buffer={args.safety_buffer}"
        )

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
            events.clear()
            alpha_sent_tao = Decimal(0)

            start_prev, end_prev = max(0, epoch_start - epoch_len), epoch_start - 1
            pricing_provider = _make_pricing_provider(st, start_prev, end_prev)
            depth_provider = _make_depth_provider(st, start_prev, end_prev)

            meta = await st.metagraph(args.meta_netuid)
            uid_of = {ck: uid for uid, ck in enumerate(meta.coldkeys)}
            my_uid = uid_of.get(wallet.coldkey.ss58_address) if wallet.coldkey.ss58_address else None
            info(f"⟫ CURRENT EPOCH {epoch_start // epoch_len} "
                 f"[{_format_range(epoch_start, epoch_len)}]")

        info(_status(head, auction_open, epoch_start, epoch_len))

        # wait until auction opens
        if head < auction_open:
            await asyncio.sleep(args.interval)
            continue

        # scan only unclutched blocks
        if next_block <= head:
            info(f"Scanner: frm={next_block}  to={head}")
            raw = await scanner.scan(next_block, head)     # <-- FIX: no “future” blocks
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
            info(f"… scanned {len(raw)} new α‑transfer(s)")

        # ► reward engine – authoritative post‑slip TAO per miner
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
            log=lambda m: info("FORECAST – " + m),
        )
        tao_by_uid = {uid: Decimal(r) for uid, r in enumerate(rewards)}
        total_tao = sum(tao_by_uid.values())
        my_tao_spent = tao_by_uid.get(my_uid, Decimal(0)) if my_uid is not None else Decimal(0)

        # live SN‑73 price
        price_bal = await subnet_price(args.meta_netuid, st=st)
        sn73_price = Decimal(str(price_bal.tao)) if price_bal else Decimal(0)

        # ► decide whether to bid
        async def _maybe_bid():
            nonlocal alpha_sent_tao
            if not autobid or sn73_price == 0:
                return

            # pessimistic future totals
            others_now = total_tao - my_tao_spent
            tao_after = my_tao_spent + step_alpha
            future_total = others_now * args.safety_buffer + tao_after
            if future_total == 0:
                return

            my_share = tao_after / future_total
            sn73_take = my_share * BAG_SN73
            value_tau = sn73_take * sn73_price

            loss = (tao_after - value_tau) / tao_after      # +ve = you lose
            info(f"Simulated bid → loss {loss:.2%} (max {args.max_discount:.2%})")

            if loss > args.max_discount:
                return
            if max_alpha is not None and alpha_sent_tao >= max_alpha:
                return
            tranche = (
                step_alpha if max_alpha is None
                else min(step_alpha, max_alpha - alpha_sent_tao)
            )
            if tranche <= 0:
                return

            try:
                ok = await transfer_alpha(
                    subtensor=st,
                    wallet=wallet,
                    hotkey_ss58=args.validator_hotkey,
                    origin_and_dest_netuid=args.netuid,
                    dest_coldkey_ss58=args.treasury,
                    amount=bt.Balance.from_tao(tranche),
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                )
            except Exception as exc:
                warn(f"transfer_alpha failed: {exc}")
                return
            if ok:
                alpha_sent_tao += tranche
                clog.success(
                    f"AUTO‑BID sent {tranche} α (cum {alpha_sent_tao}) "
                    f"loss ≤ {loss:.2%}"
                )
        # ---------- aggregates for console ---------- #
        rows: List[Tuple[int, str, str, str, str]] = []
        a_tot_tao = t_post_tot = t_pre_tot = Decimal(0)

        for sid, a_tao in sorted(deposits.items()):
            if a_tao <= 0:
                continue
            a_raw = int((a_tao * RAO_PER_TAO).to_integral_value())
            pb = await pricing_provider(sid)
            if not pb or pb.tao == 0:
                continue
            tao_post, tao_pre, slip_disc = quote_alpha_cost(
                a_raw,
                price_tao=Decimal(str(pb.tao)),
                depth_rao=await depth_provider(sid),
                c0=D_START,
            )
            if a_tao >= args.min_display_alpha:
                rows.append(
                    (
                        sid,
                        f"{a_tao.normalize():f}",
                        f"{tao_post.normalize():f}",
                        f"{tao_pre.normalize():f}",
                        f"{(slip_disc*100):.2f}%",
                    )
                )
            a_tot_tao += a_tao
            t_post_tot += tao_post
            t_pre_tot += tao_pre

        slip_tot = (Decimal(1) - t_post_tot / t_pre_tot) if t_pre_tot else D_START
        rows.insert(
            0,
            (
                -1,
                f"{a_tot_tao.normalize():f}",
                f"{t_post_tot.normalize():f}",
                f"{t_pre_tot.normalize():f}",
                f"{(slip_tot*100):.2f}%",
            ),
        )

        await _maybe_bid()

        # forecast SN‑73 for me
        my_sn73 = None
        if my_uid is not None and sn73_price > 0 and total_tao > 0:
            my_sn73 = my_tao_spent / total_tao * BAG_SN73

        # ---------- output ---------- #
        if args.json:
            print(
                json.dumps(
                    {
                        "t": int(time.time()),
                        "blk": head,
                        "epoch": epoch_start,
                        "rows": rows,
                        "sn73_price": str(sn73_price),
                        "tao_by_uid": {str(k): str(v) for k, v in tao_by_uid.items()},
                        "my_uid": my_uid,
                        "my_sn73": str(my_sn73) if my_sn73 else None,
                        "alpha_sent": str(alpha_sent_tao),
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
        else:
            total_a, total_tau, _, total_slip = rows[0][1:]
            info(f"TOTAL {total_a} α → {total_tau} TAO (slip {total_slip}) blk={head}")
            for sid, a_s, tau_post_s, _, sdisc in rows[1:]:
                info(f"  Subnet {sid:<3}: {a_s} α → {tau_post_s} TAO (slip {sdisc})")
            if wallet.coldkey.ss58_address:
                info(
                    f"MY  {wallet.coldkey.ss58_address[:12]}… "
                    f"{my_tao_spent.normalize():f} TAO post‑slip spent"
                )
            if my_sn73 is not None:
                info(
                    f"FORECAST – UID {my_uid} {my_sn73:.4f} SN‑{args.meta_netuid} "
                    f"(≈ {(my_sn73*sn73_price):.4f} TAO)"
                )
            if autobid:
                info(
                    f"AUTO‑BID status: sent {alpha_sent_tao} α "
                    f"of {max_alpha or '∞'} α limit"
                )

        await asyncio.sleep(args.interval)

# ─────────────────── providers ─────────────────── #


def _make_pricing_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _pricing(sid: int, *_):
        return await average_price(sid, start_block=start, end_block=end, st=st)
    return _pricing


def _make_depth_provider(st: bt.AsyncSubtensor, start: int, end: int):
    async def _depth(sid: int):
        d = await average_depth(sid, start_block=start, end_block=end, st=st)
        return int(getattr(d, "rao", d or 0))
    return _depth

# ─────────────────── entrypoint ─────────────────── #


def main() -> None:
    asyncio.run(_monitor(_arg_parser().parse_args()))


if __name__ == "__main__":
    main()
