# metahash/finance/cli.py
# --------------------------------------------------------------------------- #
# Command-line entry point that updates the treasury transaction cache and
# prints portfolio metrics. Designed to be cron/pm2 friendly.
# --------------------------------------------------------------------------- #

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from metahash.treasuries import VALIDATOR_TREASURIES

from .constants import (
    BASE_SUBNET_ID,
    BLOCKS_PER_DAY,
    DEFAULT_HISTORY_DAYS,
)
from .models import HoldingsSnapshot, TransactionRecord
from .treasury_cache import TreasuryTransactionCache
from .treasury_chain import (
    fetch_commitment_history,
    fetch_holdings_snapshot,
    fetch_prices,
    get_current_block,
    get_tempo,
    open_subtensor,
)
from .treasury_metrics import TreasuryMetricsCalculator


def _parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m finance.cli",
        description="Update treasury caches and compute portfolio metrics.",
    )
    parser.add_argument(
        "--network",
        default="archive",
        help="Bittensor network to use (default: archive)",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=Path("cache/treasury_transactions.json"),
        help="Path to the treasury transaction cache JSON file.",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=DEFAULT_HISTORY_DAYS,
        help="How many days of history to backfill on the first run.",
    )
    parser.add_argument(
        "--treasury",
        action="append",
        dest="treasuries",
        help="Explicit treasury coldkey to track (can be provided multiple times). "
        "Defaults to the values in metahash/treasuries.py.",
    )
    parser.add_argument(
        "--hotkey",
        action="append",
        dest="hotkeys",
        help="Validator hotkey to inspect for commitment data (defaults to treasuries' hotkey mapping).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write the metrics summary as JSON.",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the metrics summary JSON to stdout.",
    )
    parser.add_argument(
        "--max-commitment-epochs",
        type=int,
        default=120,
        help="Maximum number of commitment epochs to backfill (default: 120).",
    )
    parser.add_argument(
        "--no-update",
        action="store_true",
        help="Skip scanning the chain for new transactions (use cached data only).",
    )
    parser.add_argument(
        "--scan-chunk",
        type=int,
        default=600,
        help="Number of blocks per transfer scan batch (default: 600).",
    )
    parser.add_argument(
        "--scan-parallel",
        type=int,
        default=1,
        help="Reserved for future parallel scanning (currently informational only).",
    )
    parser.add_argument(
        "--start-block",
        type=int,
        help="Optional explicit block number to start scanning from (overrides --days-back).",
    )
    parser.add_argument(
        "--end-block",
        type=int,
        help="Optional explicit block number to stop scanning at (defaults to chain head).",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


async def _gather_historical_snapshots(
    subtensor,
    *,
    coldkeys: List[str],
    current_block: int,
    horizon_days: int,
) -> Dict[int, HoldingsSnapshot]:
    snapshots: Dict[int, HoldingsSnapshot] = {}
    tasks = []
    blocks = []
    for day in range(1, horizon_days + 1):
        block = current_block - day * BLOCKS_PER_DAY
        if block <= 0:
            continue
        blocks.append(block)
        tasks.append(fetch_holdings_snapshot(subtensor, coldkeys=coldkeys, block=block))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for block, result in zip(blocks, results):
        if isinstance(result, Exception):
            continue
        snapshots[block] = result
    return snapshots


async def run(argv: Optional[Iterable[str]] = None) -> Dict[str, object]:
    args = _parse_args(argv)

    treasuries = list(dict.fromkeys(args.treasuries or VALIDATOR_TREASURIES.values()))
    hotkeys = list(dict.fromkeys(args.hotkeys or VALIDATOR_TREASURIES.keys()))
    if not treasuries:
        raise ValueError("No treasury coldkeys configured.")

    print(f"Connecting to Bittensor network '{args.network}'…", flush=True)
    cache = TreasuryTransactionCache(
        args.cache_file,
        treasuries=treasuries,
        network=args.network,
    )

    async with open_subtensor(args.network) as subtensor:
        current_block = await get_current_block(subtensor)
        if args.start_block is not None:
            start_block = max(0, min(args.start_block, current_block))
        else:
            start_block = max(0, current_block - args.days_back * BLOCKS_PER_DAY)

        if args.end_block is not None:
            end_block = max(start_block, min(args.end_block, current_block))
        else:
            end_block = current_block

        if args.start_block is not None or args.end_block is not None:
            print(
                f"Connected. Current block: {current_block} (scanning from {start_block} to {end_block})",
                flush=True,
            )
        else:
            print(
                f"Connected. Current block: {current_block} (scanning back {args.days_back} days)",
                flush=True,
            )

        if not args.no_update:
            total_blocks = max(0, end_block - start_block)
            total_batches = (total_blocks // args.scan_chunk) + 1
            if total_blocks == 0:
                print("No new blocks to scan.", flush=True)
            else:
                print(
                    f"Scanning approximately {total_blocks} blocks in {total_batches} batches "
                    f"(chunk size {args.scan_chunk})…",
                    flush=True,
                )

            def _progress(done: int, total: int) -> None:
                if total == 0:
                    return
                step = max(1, total // 20)
                if done == 1 or done == total or done % step == 0:
                    print(f"  progress: {done}/{total} batches", flush=True)

            await cache.update(
                subtensor,
                start_block=start_block,
                end_block=end_block,
                chunk_size=args.scan_chunk,
                progress_callback=_progress if not args.no_update and total_blocks > 0 else None,
                progress_label="Transfer scan",
            )
            if total_blocks > 0:
                print("Transfer scan complete.", flush=True)

        transactions = cache.records

        holdings_now = await fetch_holdings_snapshot(
            subtensor,
            coldkeys=treasuries,
            block=current_block,
        )
        holdings_day_ago = await fetch_holdings_snapshot(
            subtensor,
            coldkeys=treasuries,
            block=max(0, current_block - BLOCKS_PER_DAY),
        )

        historical_snapshots = await _gather_historical_snapshots(
            subtensor,
            coldkeys=treasuries,
            current_block=current_block,
            horizon_days=7,
        )
        historical_snapshots[current_block] = holdings_now

        nets_of_interest = {
            tx.subnet_id for tx in transactions
        } | set(holdings_now.per_subnet_alpha.keys()) | {BASE_SUBNET_ID}

        prices = await fetch_prices(subtensor, nets_of_interest)

        tempo = await get_tempo(subtensor, BASE_SUBNET_ID)
        commitments = await fetch_commitment_history(
            subtensor,
            netuid=BASE_SUBNET_ID,
            hotkeys=hotkeys,
            start_block=start_block,
            end_block=current_block,
            tempo=tempo,
            max_epochs=args.max_commitment_epochs,
        )

    # Prepare snapshots map keyed by block for calculator
    snapshot_map = {snap.block: snap for snap in historical_snapshots.values()}

    calculator = TreasuryMetricsCalculator(
        transactions=transactions,
        current_block=current_block,
        prices=prices,
        holdings_current=holdings_now,
        holdings_day_ago=holdings_day_ago,
        historical_snapshots=snapshot_map,
        commitments=commitments,
    )
    metrics = calculator.compute()

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2)

    if args.print_json or not args.output:
        print(json.dumps(metrics, indent=2))  # pragma: no cover - CLI side effect

    return metrics


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    main()
