# Finance Module – Treasury Analytics Toolkit

This directory contains the tooling used to understand the MetaHash treasury’s
portfolio.  The new stack focuses on three pillars:

1. **Transaction capture** – a persistent JSON cache of every alpha transfer
   touching the treasury coldkeys.
2. **Chain snapshots** – lightweight helpers for fetching delegated stake,
   subnet prices, and validator commitments from the archive network.
3. **Metric synthesis** – deterministic calculations for APR, locked balances,
   auction performance, and buyback progress.

Everything runs against the `archive` subtensor endpoint so historical blocks
can be revisited without re-scanning the live chain every time.

---

## Key Components

| Module | Responsibility |
| --- | --- |
| `treasury_cache.py` | Maintains `cache/treasury_transactions.json`, updating it with new stake transfers via `AlphaTransfersScanner`. |
| `treasury_chain.py` | Async helpers for delegated stake snapshots, subnet prices, and commitment history (using archive-safe RPC hygiene). |
| `treasury_metrics.py` | Pure Python aggregation of treasury metrics (APR, locked ratios, budget utilisation, buybacks, etc). |
| `cli.py` | CLI entry point tying everything together – updates caches and prints/export metrics. |

Dataclasses live in `models.py` and configuration constants in `constants.py`.

---

## Quick Start

```bash
# Update the local cache (default 120 days back) and dump metrics to stdout.
python -m finance.cli --network archive --print-json

# Write metrics to a file while keeping stdout clean.
python -m finance.cli --output cache/treasury_metrics.json

# Recompute metrics without touching the chain (use cached transfers only).
python -m finance.cli --no-update --print-json

# Focus on a custom treasury / hotkey pair.
python -m finance.cli \
  --treasury 5GW6xj5wUpLBz7jCNp38FzkdS6DfeFdUTuvUjcn6uKH5krsn \
  --hotkey 5Dnkprjf9fUrvWq3ZfFP8WrUNSjQws6UoHkbDfR1bQK8pFhW
```

Arguments of interest:

- `--days-back` – how far to backfill when the cache is empty (default 120).
- `--max-commitment-epochs` – cap the number of auction commitments to ingest (default 120 epochs).
- `--cache-file` – where to store the transaction history (`cache/treasury_transactions.json` by default).
- `--scan-chunk` – number of blocks per transfer scan batch (default 600; lower values help on slow archive nodes). Progress is now displayed via `tqdm` when available.
- `--start-block` / `--end-block` – optionally bound the scan to an explicit block range (useful for smoke tests before running a full backfill).

---

## Cache Format

Transactions are stored as plain JSON:

```json
{
  "meta": {
    "network": "archive",
    "treasuries": ["…"],
    "lock_period_days": 60,
    "blocks_per_day": 7200,
    "last_block": 5584000,
    "last_updated": "2025-01-30T12:00:00+00:00"
  },
  "transactions": [
    {
      "block": 5555555,
      "subnet_id": 5,
      "amount_rao": 1234567890,
      "amount_alpha": 1.23456789,
      "src": "5…",
      "dest": "5…",
      "direction": "in"
    }
  ]
}
```

The cache is append-only; re-running the CLI simply adds new entries and keeps
the same structure so other services can reuse it as a read-only ledger.

---

## Metrics Provided

The CLI produces a JSON payload containing:

- **Holdings** – per-subnet alpha & TAO valuations, locked vs liquid capital,
  cumulative inflows/outflows.
- **Revenue** – daily and 7-day average staking rewards (alpha & TAO) plus
  annualised APR estimates per subnet and for the overall portfolio.
- **Auctions** – average budget utilisation, mean miner discount (margin),
  total SN73 emission since inception (based on commitment history).
- **Buybacks** – lifetime SN73 alpha repurchased, daily spend, and weekly
  averages (in both alpha and TAO units).

Every number is computed deterministically from the cache + archive snapshots,
so running the tool on another host yields the same view.

---

## Extending the Toolkit

- **New metrics**: add pure functions in `treasury_metrics.py` so they can be
  unit-tested without touching the network.
- **Additional data sources**: extend `treasury_chain.py` for new RPC queries,
  keeping the async/lock discipline already present in the module.
- **Automation**: schedule `python -m finance.cli` via cron/PM2; the script is
  idempotent and safe to run frequently.

---

## Tests & Coverage

Metrics logic has been factored out of chain interaction and can be covered
with synthetic fixtures. Add tests under `tests/finance/` (or reuse your
preferred harness) to keep arithmetic regressions from slipping through.

---

## Dependencies

- `bittensor` (AsyncSubtensor + balance utilities)
- `metahash.validator.alpha_transfers` (existing scanner reused by treasury cache)
- Standard library modules only otherwise

No additional external packages are required.
