from __future__ import annotations

from finance.models import CommitmentSnapshot, HoldingsSnapshot, TransactionRecord, WinnerLine
from finance.treasury_metrics import TreasuryMetricsCalculator


def _tx(block: int, amount_alpha: float, direction: str = "in") -> TransactionRecord:
    return TransactionRecord(
        block=block,
        subnet_id=5,
        amount_rao=int(round(amount_alpha * 10**9)),
        src="src",
        dest="dest",
        direction=direction,
    )


def test_metrics_basic_flow():
    current_block = 10_000
    # Transactions: two historical deposits + a recent locked one.
    txs = [
        _tx(100, 10),
        _tx(200, 5),
        _tx(9_900, 2),
    ]

    holdings_now = HoldingsSnapshot(block=current_block, per_subnet_alpha={5: 18.0})
    holdings_day_ago = HoldingsSnapshot(block=current_block - 7200, per_subnet_alpha={5: 15.0})

    # Historical snapshots (only day 1 needed for averages in this test)
    historical = {
        current_block - 7200: holdings_day_ago,
    }

    commitment = CommitmentSnapshot(
        epoch=1,
        pay_epoch=2,
        auction_start_block=9500,
        auction_end_block=9600,
        block_sampled=9600,
        budget_total_tao=10.0,
        budget_leftover_tao=2.0,
        winner_lines=[WinnerLine(miner_uid=1, subnet_id=5, discount_bps=500, weight_bps=10_000, accepted_alpha=1.0, value_tao=0.5)],
        requested_lines=[],
    )

    calculator = TreasuryMetricsCalculator(
        transactions=txs,
        current_block=current_block,
        prices={5: 2.0},
        holdings_current=holdings_now,
        holdings_day_ago=holdings_day_ago,
        historical_snapshots=historical,
        commitments=[commitment],
        lock_period_blocks=500,  # shorten for the test
    )

    metrics = calculator.compute()

    holdings = metrics["holdings"]
    assert abs(holdings["locked_alpha"][5] - 2.0) < 1e-9
    assert abs(holdings["liquid_alpha"][5] - 16.0) < 1e-9

    revenue = metrics["revenue"]["daily"]
    # Daily staking reward: current (18) - previous (15) - net flow (2) = 1
    assert abs(revenue["total_alpha"] - 1.0) < 1e-9
    # TAO value with price 2.0
    assert abs(revenue["total_tao"] - 2.0) < 1e-9

    auctions = metrics["auctions"]
    assert auctions["epochs_tracked"] == 1
    assert abs(auctions["avg_budget_used_pct"] - 0.8) < 1e-9
    assert abs(auctions["avg_margin_pct"] - 5.0) < 1e-9  # 500bps -> 5%
