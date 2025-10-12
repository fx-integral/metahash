# metahash/finance/treasury_metrics.py
# --------------------------------------------------------------------------- #
# High-level metric aggregation for the MetaHash treasury.  This module is
# intentionally pure: all inputs are plain dataclasses/structures so the logic
# can be unit-tested without requiring live chain access.
# --------------------------------------------------------------------------- #

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable, List, Mapping, Optional, Tuple

from .constants import (
    BASE_AUCTION_EMISSION_ALPHA,
    BASE_SUBNET_ID,
    BLOCKS_PER_DAY,
    LOCK_PERIOD_BLOCKS,
)
from .models import (
    CommitmentSnapshot,
    HoldingsSnapshot,
    TransactionRecord,
)


# --------------------------------------------------------------------------- #
# Helper data structures
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DailyReward:
    """Helper structure for daily reward calculations."""

    per_subnet_alpha: Dict[int, float]
    per_subnet_tao: Dict[int, float]
    total_alpha: float
    total_tao: float
    apr_alpha_per_subnet: Dict[int, float]


# --------------------------------------------------------------------------- #
# Utility functions
# --------------------------------------------------------------------------- #


def _ordered_transactions(transactions: Iterable[TransactionRecord]) -> List[TransactionRecord]:
    return sorted(transactions, key=lambda tx: (tx.block, tx.amount_rao, tx.src, tx.dest))


def _net_flows_between(
    transactions: Iterable[TransactionRecord],
    *,
    start_block_exclusive: int,
    end_block_inclusive: int,
) -> Dict[int, float]:
    flows: Dict[int, float] = defaultdict(float)
    for tx in transactions:
        if tx.direction == "internal":
            continue
        if tx.block <= start_block_exclusive:
            continue
        if tx.block > end_block_inclusive:
            continue
        delta = tx.amount_alpha if tx.direction == "in" else -tx.amount_alpha
        flows[tx.subnet_id] += delta
    return flows


def _aggregate_totals(values: Mapping[int, float]) -> float:
    return float(sum(values.values()))


def _convert_alpha_to_tao(alpha_by_net: Mapping[int, float], prices: Mapping[int, float]) -> Dict[int, float]:
    return {net: float(alpha) * float(prices.get(net, 0.0)) for net, alpha in alpha_by_net.items()}


def _safe_divide(numerator: float, denominator: float) -> float:
    if abs(denominator) <= 1e-18:
        return 0.0
    return numerator / denominator


def _round(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


# --------------------------------------------------------------------------- #
# Locked amount calculations
# --------------------------------------------------------------------------- #


def _compute_locked_breakdown(
    transactions: Iterable[TransactionRecord],
    *,
    current_block: int,
    lock_period_blocks: int = LOCK_PERIOD_BLOCKS,
) -> Tuple[Dict[int, float], Dict[int, float], Dict[int, float], Dict[int, float]]:
    """
    Returns (total_in, total_out, locked_alpha_by_net, unlocked_alpha_by_net).
    """
    deposits: Dict[int, Deque[Tuple[int, float]]] = defaultdict(deque)
    total_in: Dict[int, float] = defaultdict(float)
    total_out: Dict[int, float] = defaultdict(float)

    for tx in _ordered_transactions(transactions):
        amount = tx.amount_alpha
        if tx.direction == "in":
            deposits[tx.subnet_id].append((tx.block, amount))
            total_in[tx.subnet_id] += amount
        elif tx.direction == "out":
            total_out[tx.subnet_id] += amount
            queue = deposits[tx.subnet_id]
            remaining = amount
            while queue and remaining > 0:
                blk, chunk = queue[0]
                consume = min(chunk, remaining)
                chunk -= consume
                remaining -= consume
                if chunk <= 1e-18:
                    queue.popleft()
                else:
                    queue[0] = (blk, chunk)
                    break
            # If remaining > 0 the treasury sent more than historical inflows; we let it go negative
            if remaining > 1e-12 and not queue:
                # Negative inventory; store as negative chunk at current block so locked accounting shows deficit.
                queue.append((current_block, -remaining))
        else:
            # internal transfers – no effect on liquidity
            continue

    locked: Dict[int, float] = defaultdict(float)
    unlocked: Dict[int, float] = defaultdict(float)

    for net, queue in deposits.items():
        for blk, amount in queue:
            if abs(amount) <= 1e-18:
                continue
            age = current_block - blk
            if amount < 0:
                # Debt/outstanding negative balance considered unlocked deficit
                unlocked[net] += amount
            elif age < lock_period_blocks:
                locked[net] += amount
            else:
                unlocked[net] += amount

    return dict(total_in), dict(total_out), dict(locked), dict(unlocked)


# --------------------------------------------------------------------------- #
# Main calculator
# --------------------------------------------------------------------------- #


class TreasuryMetricsCalculator:
    def __init__(
        self,
        *,
        transactions: List[TransactionRecord],
        current_block: int,
        prices: Mapping[int, float],
        holdings_current: HoldingsSnapshot,
        holdings_day_ago: HoldingsSnapshot,
        historical_snapshots: Mapping[int, HoldingsSnapshot],
        commitments: List[CommitmentSnapshot],
        lock_period_blocks: int = LOCK_PERIOD_BLOCKS,
    ) -> None:
        self.transactions = _ordered_transactions(transactions)
        self.current_block = int(current_block)
        self.prices = dict(prices)
        self.holdings_current = holdings_current
        self.holdings_day_ago = holdings_day_ago
        self.historical_snapshots = historical_snapshots
        self.commitments = commitments
        self.lock_period_blocks = lock_period_blocks

        self._nets = sorted(
            set(list(self.prices.keys()))
            | set(self.holdings_current.nets())
            | {tx.subnet_id for tx in self.transactions}
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def compute(self) -> Dict[str, object]:
        locked_data = self._compute_locked()
        revenue_daily = self._compute_daily_rewards(days=1)
        revenue_week = self._compute_multi_day_rewards(days=7)
        auction_metrics = self._compute_auction_metrics()
        buyback_metrics = self._compute_buyback_metrics()

        holdings_alpha = {
            net: self.holdings_current.per_subnet_alpha.get(net, 0.0) for net in self._nets
        }
        holdings_tao = _convert_alpha_to_tao(holdings_alpha, self.prices)
        total_alpha = _aggregate_totals(holdings_alpha)
        total_tao = _aggregate_totals(holdings_tao)

        locked_alpha = locked_data["locked_alpha"]
        locked_tao = _convert_alpha_to_tao(locked_alpha, self.prices)
        locked_total_alpha = _aggregate_totals(locked_alpha)
        locked_total_tao = _aggregate_totals(locked_tao)
        locked_pct = _safe_divide(locked_total_alpha, total_alpha) if total_alpha else 0.0

        liquid_alpha = {
            net: holdings_alpha.get(net, 0.0) - locked_alpha.get(net, 0.0) for net in self._nets
        }
        liquid_tao = _convert_alpha_to_tao(liquid_alpha, self.prices)

        summary = {
            "holdings": {
                "current_alpha": holdings_alpha,
                "current_tao": holdings_tao,
                "total_alpha": total_alpha,
                "total_tao": total_tao,
                "locked_alpha": locked_alpha,
                "locked_tao": locked_tao,
                "locked_total_alpha": locked_total_alpha,
                "locked_total_tao": locked_total_tao,
                "locked_percentage": locked_pct,
                "liquid_alpha": liquid_alpha,
                "liquid_tao": liquid_tao,
                "total_in_alpha": locked_data["total_in"],
                "total_out_alpha": locked_data["total_out"],
            },
            "revenue": {
                "daily": {
                    "alpha": revenue_daily.per_subnet_alpha,
                    "tao": revenue_daily.per_subnet_tao,
                    "total_alpha": revenue_daily.total_alpha,
                    "total_tao": revenue_daily.total_tao,
                    "apr_alpha_per_subnet": revenue_daily.apr_alpha_per_subnet,
                    "apr_tao_total": _safe_divide(revenue_daily.total_tao, total_tao) * 365 if total_tao else 0.0,
                },
                "seven_day_average": {
                    "alpha": revenue_week.per_subnet_alpha,
                    "tao": revenue_week.per_subnet_tao,
                    "total_alpha": revenue_week.total_alpha,
                    "total_tao": revenue_week.total_tao,
                    "apr_alpha_per_subnet": revenue_week.apr_alpha_per_subnet,
                    "apr_tao_total": _safe_divide(revenue_week.total_tao, total_tao) * 365 if total_tao else 0.0,
                },
            },
            "auctions": auction_metrics,
            "buyback": buyback_metrics,
        }

        return summary

    # ------------------------------------------------------------------ #
    # Locked funds
    # ------------------------------------------------------------------ #

    def _compute_locked(self) -> Dict[str, Dict[int, float]]:
        total_in, total_out, locked_alpha, unlocked_alpha = _compute_locked_breakdown(
            self.transactions,
            current_block=self.current_block,
            lock_period_blocks=self.lock_period_blocks,
        )
        return {
            "total_in": total_in,
            "total_out": total_out,
            "locked_alpha": locked_alpha,
            "unlocked_alpha": unlocked_alpha,
        }

    # ------------------------------------------------------------------ #
    # Revenue / APR
    # ------------------------------------------------------------------ #

    def _compute_daily_rewards(self, *, days: int) -> DailyReward:
        assert days >= 1
        end_block = self.current_block
        start_block = self.current_block - days * BLOCKS_PER_DAY

        snapshot_start = self.holdings_day_ago if days == 1 else self.historical_snapshots.get(start_block)
        if snapshot_start is None:
            snapshot_start = self.holdings_day_ago

        flows = _net_flows_between(
            self.transactions,
            start_block_exclusive=start_block,
            end_block_inclusive=end_block,
        )

        per_subnet_alpha: Dict[int, float] = {}
        per_subnet_tao: Dict[int, float] = {}
        apr_alpha: Dict[int, float] = {}

        for net in self._nets:
            current = self.holdings_current.per_subnet_alpha.get(net, 0.0)
            previous = snapshot_start.per_subnet_alpha.get(net, 0.0)
            net_flow = flows.get(net, 0.0)
            staking_reward = current - previous - net_flow
            per_subnet_alpha[net] = staking_reward
            price = self.prices.get(net, 0.0)
            per_subnet_tao[net] = staking_reward * price
            apr_alpha[net] = _safe_divide(staking_reward, previous) * 365 if previous > 0 else 0.0

        total_alpha = _aggregate_totals(per_subnet_alpha)
        total_tao = _aggregate_totals(per_subnet_tao)

        return DailyReward(
            per_subnet_alpha=per_subnet_alpha,
            per_subnet_tao=per_subnet_tao,
            total_alpha=total_alpha,
            total_tao=total_tao,
            apr_alpha_per_subnet=apr_alpha,
        )

    def _compute_multi_day_rewards(self, *, days: int) -> DailyReward:
        per_subnet_alpha: Dict[int, float] = defaultdict(float)
        per_subnet_tao: Dict[int, float] = defaultdict(float)
        apr_alpha_accumulator: Dict[int, List[float]] = defaultdict(list)

        valid_days = 0
        for day in range(1, days + 1):
            start_block = self.current_block - day * BLOCKS_PER_DAY
            end_block = self.current_block - (day - 1) * BLOCKS_PER_DAY
            snapshot_start = self.historical_snapshots.get(start_block)
            snapshot_end = self.historical_snapshots.get(end_block) if day > 1 else self.holdings_current
            if snapshot_start is None or snapshot_end is None:
                continue

            flows = _net_flows_between(
                self.transactions,
                start_block_exclusive=start_block,
                end_block_inclusive=end_block,
            )

            valid_days += 1
            for net in self._nets:
                current = snapshot_end.per_subnet_alpha.get(net, 0.0)
                previous = snapshot_start.per_subnet_alpha.get(net, 0.0)
                net_flow = flows.get(net, 0.0)
                staking_reward = current - previous - net_flow
                per_subnet_alpha[net] += staking_reward
                per_subnet_tao[net] += staking_reward * self.prices.get(net, 0.0)
                if previous > 0:
                    apr_alpha_accumulator[net].append(_safe_divide(staking_reward, previous) * 365)

        if valid_days == 0:
            return DailyReward(
                per_subnet_alpha={net: 0.0 for net in self._nets},
                per_subnet_tao={net: 0.0 for net in self._nets},
                total_alpha=0.0,
                total_tao=0.0,
                apr_alpha_per_subnet={net: 0.0 for net in self._nets},
            )

        avg_alpha = {net: per_subnet_alpha.get(net, 0.0) / valid_days for net in self._nets}
        avg_tao = {net: per_subnet_tao.get(net, 0.0) / valid_days for net in self._nets}
        avg_apr = {
            net: (sum(values) / len(values)) if values else 0.0
            for net, values in apr_alpha_accumulator.items()
        }

        return DailyReward(
            per_subnet_alpha=avg_alpha,
            per_subnet_tao=avg_tao,
            total_alpha=_aggregate_totals(avg_alpha),
            total_tao=_aggregate_totals(avg_tao),
            apr_alpha_per_subnet=avg_apr,
        )

    # ------------------------------------------------------------------ #
    # Auctions
    # ------------------------------------------------------------------ #

    def _compute_auction_metrics(self) -> Dict[str, object]:
        if not self.commitments:
            return {
                "epochs_tracked": 0,
                "avg_budget_used_pct": 0.0,
                "avg_margin_pct": 0.0,
                "total_sn73_emitted_alpha": 0.0,
            }

        unique_epochs = {snap.epoch for snap in self.commitments}
        budget_used = [snap.budget_used_pct for snap in self.commitments if snap.budget_total_tao > 0]

        margin_values: List[float] = []
        for snap in self.commitments:
            for line in snap.winner_lines:
                margin_values.append(line.discount_bps / 100.0)

        avg_budget_pct = sum(budget_used) / len(budget_used) if budget_used else 0.0
        avg_margin_pct = sum(margin_values) / len(margin_values) if margin_values else 0.0

        total_emission = len(unique_epochs) * BASE_AUCTION_EMISSION_ALPHA

        return {
            "epochs_tracked": len(unique_epochs),
            "avg_budget_used_pct": avg_budget_pct,
            "avg_margin_pct": avg_margin_pct,
            "total_sn73_emitted_alpha": total_emission,
        }

    # ------------------------------------------------------------------ #
    # Buyback
    # ------------------------------------------------------------------ #

    def _compute_buyback_metrics(self) -> Dict[str, float]:
        total_alpha = 0.0
        last_day_alpha = 0.0
        last_week_alpha = 0.0

        day_threshold = self.current_block - BLOCKS_PER_DAY
        week_threshold = self.current_block - 7 * BLOCKS_PER_DAY

        for tx in self.transactions:
            if tx.direction != "out" or tx.subnet_id != BASE_SUBNET_ID:
                continue
            amount = tx.amount_alpha
            total_alpha += amount
            if tx.block > day_threshold:
                last_day_alpha += amount
            if tx.block > week_threshold:
                last_week_alpha += amount

        price_sn73 = self.prices.get(BASE_SUBNET_ID, 0.0)
        return {
            "total_alpha": total_alpha,
            "total_tao": total_alpha * price_sn73,
            "daily_alpha": last_day_alpha,
            "daily_tao": last_day_alpha * price_sn73,
            "seven_day_avg_alpha": last_week_alpha / 7 if last_week_alpha else 0.0,
            "seven_day_avg_tao": (last_week_alpha * price_sn73) / 7 if last_week_alpha else 0.0,
        }

