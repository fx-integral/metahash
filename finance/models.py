# metahash/finance/models.py
# --------------------------------------------------------------------------- #
# Dataclasses shared across the finance tooling. These are intentionally kept
# lightweight so they can be serialised to JSON caches and reused in tests
# without pulling in heavy blockchain dependencies.
# --------------------------------------------------------------------------- #

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Literal, Optional, Tuple

from metahash.config import PLANCK
from metahash.utils.valuation import decode_value_mu

# --------------------------------------------------------------------------- #
# Transaction tracking
# --------------------------------------------------------------------------- #

Direction = Literal["in", "out", "internal"]


@dataclass(slots=True)
class TransactionRecord:
    """
    Representation of a stake transfer touching the treasury.

    Attributes:
        block: Chain block number the transfer occurred in.
        subnet_id: Subnet identifier of the alpha being transferred.
        amount_rao: Transfer amount in planck/rao units.
        src: Source coldkey SS58.
        dest: Destination coldkey SS58.
        direction: Perspective of the treasury (`in`, `out`, or `internal`).
        extrinsic_idx: Optional index within the block for traceability.
    """

    block: int
    subnet_id: int
    amount_rao: int
    src: str
    dest: str
    direction: Direction
    extrinsic_idx: Optional[int] = None

    @property
    def amount_alpha(self) -> float:
        """Amount expressed in alpha units."""
        return self.amount_rao / PLANCK

    def to_dict(self) -> Dict[str, object]:
        """JSON-friendly representation."""
        data = asdict(self)
        data["amount_alpha"] = self.amount_alpha
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "TransactionRecord":
        return cls(
            block=int(data["block"]),
            subnet_id=int(data["subnet_id"]),
            amount_rao=int(data["amount_rao"]),
            src=str(data["src"]),
            dest=str(data["dest"]),
            direction=str(data["direction"]),  # type: ignore[arg-type]
            extrinsic_idx=(
                int(data["extrinsic_idx"])
                if data.get("extrinsic_idx") is not None
                else None
            ),
        )


# --------------------------------------------------------------------------- #
# Holdings snapshots
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class HoldingsSnapshot:
    """
    Snapshot of treasury stake at a particular block.

    Attributes:
        block: Block number the snapshot was taken at.
        per_subnet_alpha: Mapping of netuid -> alpha holdings.
    """

    block: int
    per_subnet_alpha: Dict[int, float] = field(default_factory=dict)

    @property
    def total_alpha(self) -> float:
        return sum(self.per_subnet_alpha.values())

    def to_dict(self) -> Dict[str, object]:
        return {
            "block": self.block,
            "per_subnet_alpha": {int(k): float(v) for k, v in self.per_subnet_alpha.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "HoldingsSnapshot":
        per_subnet = {
            int(net): float(amount)
            for net, amount in (data.get("per_subnet_alpha") or {}).items()
        }
        return cls(block=int(data["block"]), per_subnet_alpha=per_subnet)

    def copy(self) -> "HoldingsSnapshot":
        return HoldingsSnapshot(self.block, dict(self.per_subnet_alpha))

    def add_alpha(self, subnet_id: int, amount_alpha: float) -> None:
        self.per_subnet_alpha[subnet_id] = (
            self.per_subnet_alpha.get(subnet_id, 0.0) + amount_alpha
        )

    def nets(self) -> Iterable[int]:
        return self.per_subnet_alpha.keys()


# --------------------------------------------------------------------------- #
# Commitments & auction diagnostics
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class WinnerLine:
    miner_uid: int
    subnet_id: int
    discount_bps: int
    weight_bps: int
    accepted_alpha: float
    value_tao: float

    def to_dict(self) -> Dict[str, object]:
        return {
            "miner_uid": self.miner_uid,
            "subnet_id": self.subnet_id,
            "discount_bps": self.discount_bps,
            "weight_bps": self.weight_bps,
            "accepted_alpha": self.accepted_alpha,
            "value_tao": self.value_tao,
        }


@dataclass(slots=True)
class RequestedLine:
    miner_uid: int
    subnet_id: int
    discount_bps: int
    weight_bps: int
    requested_alpha: float
    estimated_value_tao: float

    def to_dict(self) -> Dict[str, object]:
        return {
            "miner_uid": self.miner_uid,
            "subnet_id": self.subnet_id,
            "discount_bps": self.discount_bps,
            "weight_bps": self.weight_bps,
            "requested_alpha": self.requested_alpha,
            "estimated_value_tao": self.estimated_value_tao,
        }


@dataclass(slots=True)
class CommitmentSnapshot:
    epoch: int
    pay_epoch: int
    auction_start_block: int
    auction_end_block: int
    block_sampled: int
    budget_total_tao: float
    budget_leftover_tao: float
    winner_lines: List[WinnerLine] = field(default_factory=list)
    requested_lines: List[RequestedLine] = field(default_factory=list)

    @property
    def budget_used_tao(self) -> float:
        return max(0.0, self.budget_total_tao - self.budget_leftover_tao)

    @property
    def budget_used_pct(self) -> float:
        if self.budget_total_tao <= 0:
            return 0.0
        return min(1.0, self.budget_used_tao / self.budget_total_tao)

    def to_dict(self) -> Dict[str, object]:
        return {
            "epoch": self.epoch,
            "pay_epoch": self.pay_epoch,
            "auction_start_block": self.auction_start_block,
            "auction_end_block": self.auction_end_block,
            "block_sampled": self.block_sampled,
            "budget_total_tao": self.budget_total_tao,
            "budget_leftover_tao": self.budget_leftover_tao,
            "winner_lines": [w.to_dict() for w in self.winner_lines],
            "requested_lines": [r.to_dict() for r in self.requested_lines],
        }

    @classmethod
    def from_payload(
        cls,
        payload: Dict[str, object],
        *,
        block_sampled: int,
    ) -> Optional["CommitmentSnapshot"]:
        try:
            epoch = int(payload["e"])
            pay_epoch = int(payload.get("pe", epoch + 1))
            auction_start_block = int(payload.get("as", block_sampled))
            auction_end_block = int(payload.get("de", auction_start_block))
            budget_total_tao = float(payload.get("bt_tao", 0.0))
            budget_leftover_tao = float(payload.get("bl_tao", 0.0))
        except Exception:
            return None

        winners_raw: List = payload.get("i") or []
        requests_raw: List = payload.get("b") or []

        winner_lines: List[WinnerLine] = []
        for entry in winners_raw:
            try:
                uid, lines = entry
                for ln in lines:
                    subnet_id, discount_bps, weight_bps, acc_rao, value_mu = ln
                    winner_lines.append(
                        WinnerLine(
                            miner_uid=int(uid),
                            subnet_id=int(subnet_id),
                            discount_bps=int(discount_bps),
                            weight_bps=int(weight_bps),
                            accepted_alpha=int(acc_rao) / PLANCK,
                            value_tao=decode_value_mu(int(value_mu)),
                        )
                    )
            except Exception:
                continue

        requested_lines: List[RequestedLine] = []
        for entry in requests_raw:
            try:
                uid, lines = entry
                for ln in lines:
                    subnet_id, discount_bps, weight_bps, req_rao, value_mu = ln
                    requested_lines.append(
                        RequestedLine(
                            miner_uid=int(uid),
                            subnet_id=int(subnet_id),
                            discount_bps=int(discount_bps),
                            weight_bps=int(weight_bps),
                            requested_alpha=int(req_rao) / PLANCK,
                            estimated_value_tao=decode_value_mu(int(value_mu)),
                        )
                    )
            except Exception:
                continue

        return cls(
            epoch=epoch,
            pay_epoch=pay_epoch,
            auction_start_block=auction_start_block,
            auction_end_block=auction_end_block,
            block_sampled=block_sampled,
            budget_total_tao=budget_total_tao,
            budget_leftover_tao=budget_leftover_tao,
            winner_lines=winner_lines,
            requested_lines=requested_lines,
        )

