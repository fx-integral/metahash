# metahash/validator/engines/clearing.py
from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections import defaultdict
from typing import Dict, List, Tuple

from metahash.utils.pretty_logs import pretty
from metahash.validator.state import StateStore

# Config & utilities
from metahash.config import (
    AUCTION_BUDGET_ALPHA,
    LOG_TOP_N,
    METAHASH_SUBNET_ID,
    PLANCK,
    REPUTATION_BASELINE_CAP_FRAC,
    REPUTATION_ENABLED,
    REPUTATION_MAX_CAP_FRAC,
    REPUTATION_MIX_GAMMA,
    FORBIDDEN_ALPHA_SUBNETS,
)
from metahash.validator.engines.auction import WinAllocation, BidInput, budget_tao_from_share
from metahash.protocol import WinSynapse
from metahash.treasuries import VALIDATOR_TREASURIES

# Price oracle (for ordering & budget conversion; prices are NOT written into commitments)
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.utils.valuation import (
    effective_value_tao,
    encode_value_mu,
)

# Tolerances
EPS_ALPHA = 1e-18          # α resolution guard (we still quantize on rao later)
EPS_VALUE = 1e-12          # TAO dust threshold: stop when budget < EPS_VALUE


class ClearingEngine:
    """
    Clear winners *now* with PARTIAL FILLS to greedily consume TAO budget.
    We store the exact pay window (as,de) in StateStore.pending_commits[e]
    so CommitmentsEngine can publish the same snapshot in e+1.

    Budget & ordering are in VALUE (TAO), not α. With slippage:
        VALUE_TAO(α) = weight × (1 − discount) × post_slip(price, depth, α)

    If REPUTATION_ENABLED, we enforce per-coldkey caps in VALUE (TAO).

    NEW (budget signals for settlement burn):
      - We serialize `bt_mu` (total budget VALUE in μTAO) and
        `bl_mu` (leftover VALUE in μTAO) into the staged payload.
      - This lets Settlement infer the target budget and burn underfill to uid 0.

    FIX (v2.3.7):
      - Two-pass allocation: pass1 honors reputation caps; pass2 relaxes caps to
        consume leftover budget if any bids remain (no reason to leave VALUE idle).
      - Track accepted α per bid across passes to never exceed request.

    NEW (diagnostics snapshots in payload):
      - Include ALL accepted bids (not only winners) with requested α and estimated VALUE.
      - Include rejected bids with reasons (from AuctionEngine) if any.
      - Include reputation snapshot for involved coldkeys (+ caps/params).
      - Include jail snapshot for involved coldkeys.
      - Include weights used by this validator (bps map and optional default).
    """

    def __init__(self, parent, state: StateStore):
        self.parent = parent
        self.state = state

        # Detect WinAllocation signature once (handles schema variations)
        try:
            self._winalloc_params = set(inspect.signature(WinAllocation).parameters.keys())
        except Exception:
            self._winalloc_params = set()

    # ─────────────── Reachability & helpers ───────────────

    @staticmethod
    def _is_reachable_axon(ax) -> bool:
        """Guard against axons with host=0.0.0.0/[::] or port=0."""
        try:
            host = getattr(ax, "external_ip", None) or getattr(ax, "ip", None)
            port = getattr(ax, "external_port", None) or getattr(ax, "port", None)
            if isinstance(host, str):
                host = host.strip()
            if not host or host in ("0.0.0.0", "[::]"):
                return False
            try:
                port = int(port)
            except Exception:
                return False
            if port <= 0:
                return False
            return True
        except Exception:
            return False

    def _hotkey_to_uid(self) -> Dict[str, int]:
        mapping: Dict[str, int] = {}
        for i, ax in enumerate(self.parent.metagraph.axons):
            hk = getattr(ax, "hotkey", None)
            if hk:
                mapping[hk] = i
        if not mapping and hasattr(self.parent.metagraph, "hotkeys"):
            for i, hk in enumerate(getattr(self.parent.metagraph, "hotkeys")):
                mapping[hk] = i
        return mapping

    def _to_bid_inputs(self, bid_map) -> List[BidInput]:
        """Convert AuctionEngine._Bid entries → BidInput for allocator."""
        out: List[BidInput] = []
        for (uid, subnet_id), b in bid_map.items():
            w_bps = int(round(float(getattr(b, "weight_snap", 0.0)) * 10_000.0))
            out.append(
                BidInput(
                    miner_uid=int(getattr(b, "miner_uid", uid)),
                    coldkey=str(getattr(b, "coldkey", "")),
                    subnet_id=int(subnet_id),
                    alpha=float(getattr(b, "alpha", 0.0)),
                    discount_bps=int(getattr(b, "discount_bps", 0)),
                    weight_bps=w_bps,
                    idx=int(getattr(b, "idx", 0)),
                )
            )
        return out

    async def _safe_price(self, sid: int, start_block: int, end_block: int) -> float:
        """Cancellation-safe wrapper around pricing oracle used for ordering & budget conversion."""
        try:
            st = await self.parent._stxn()
            p = await average_price(sid, start_block=start_block, end_block=end_block, st=st)
            return float(getattr(p, "tao", 0.0) or 0.0)
        except (asyncio.CancelledError, Exception) as e:
            pretty.log(f"[yellow]Price lookup failed/cancelled for sid={sid}: {e}[/yellow]")
            return 0.0

    async def _estimate_prices_for_bids(
        self, bids: List[BidInput], start_block: int, end_block: int
    ) -> Dict[int, float]:
        sids = sorted({b.subnet_id for b in bids})
        out: Dict[int, float] = {}
        # use a single client for the loop
        try:
            st = await self.parent._new_async_subtensor()
        except Exception:
            st = None
        for sid in sids:
            try:
                p = await average_price(sid, start_block=start_block, end_block=end_block, st=st)
                out[sid] = float(getattr(p, "tao", 0.0) or 0.0)
            except Exception as e:
                pretty.log(f"[yellow]Price lookup failed for sid={sid}: {e}[/yellow]")
                out[sid] = 0.0
        return out

    async def _estimate_depths_for_bids(
        self, bids: List[BidInput], start_block: int, end_block: int
    ) -> Dict[int, int]:
        sids = sorted({b.subnet_id for b in bids})
        out: Dict[int, int] = {}
        try:
            st = await self.parent._new_async_subtensor()
        except Exception:
            st = None
        for sid in sids:
            try:
                d = await average_depth(sid, start_block=start_block, end_block=end_block, st=st)
                out[sid] = int(d or 0)
            except Exception as e:
                pretty.log(f"[yellow]Depth lookup failed for sid={sid}: {e}[/yellow]")
                out[sid] = 0
        return out

    def _compute_quota_caps_tao(
        self, bids: List[BidInput], my_budget_tao: float
    ) -> Tuple[Dict[str, float], Dict[str, float]]:
        """
        Returns:
          cap_tao_by_ck: coldkey -> cap in TAO
          quota_frac_by_ck: coldkey -> normalized fraction q (for logs)
        """
        if not REPUTATION_ENABLED or my_budget_tao <= 0:
            return ({}, {})
        cks = sorted({b.coldkey for b in bids})
        if not cks:
            return ({}, {})

        # Reputation comes from StateStore if present; default to 0.0
        rep_store: Dict[str, float] = getattr(self.state, "reputation", {}) or {}

        N = len(cks)
        rep = {ck: max(0.0, min(1.0, float(rep_store.get(ck, 0.0)))) for ck in cks}
        sum_rep = sum(rep.values())
        if sum_rep > 0:
            norm_rep = {ck: (rep[ck] / sum_rep) for ck in cks}
        else:
            norm_rep = {ck: 1.0 / N for ck in cks}

        gamma = max(0.0, min(1.0, float(REPUTATION_MIX_GAMMA)))
        baseline = max(0.0, min(1.0, float(REPUTATION_BASELINE_CAP_FRAC)))
        capmax = max(baseline, min(1.0, float(REPUTATION_MAX_CAP_FRAC)))

        q_raw = {ck: (1.0 - gamma) * (1.0 / N) + gamma * norm_rep[ck] for ck in cks}
        q_env = {ck: max(baseline, min(capmax, q_raw[ck])) for ck in cks}
        sum_env = sum(q_env.values()) or 1.0
        q = {ck: q_env[ck] / sum_env for ck in cks}  # re-normalized to sum 1

        caps = {ck: my_budget_tao * q[ck] for ck in cks}
        return caps, q

    @staticmethod
    def _make_invoice_id(
        validator_key: str,
        epoch: int,
        subnet_id: int,
        alpha: float,
        discount_bps: int,
        pay_epoch: int,
        amount_rao: int,
    ) -> str:
        payload = (
            f"{validator_key}|{epoch}|{subnet_id}|{alpha:.12f}|"
            f"{discount_bps}|{pay_epoch}|{amount_rao}"
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    # ──────────────── main entrypoint ────────────────

    async def clear_now_and_notify(self, epoch_to_clear: int) -> bool:
        """
        Clear and notify winners for the current epoch epoch_to_clear using
        PARTIAL FILLS to greedily exhaust the TAO budget (down to dust).

        FIX: two-pass:
          - pass1 honors per-CK TAO caps (reputation)
          - pass2 (if leftover) relaxes caps to consume remaining budget
        """
        if epoch_to_clear is None or epoch_to_clear < 0:
            return False

        # Must be a master to clear
        if not getattr(self.parent, "auction", None) or not self.parent.auction._is_master_now():
            return False

        bid_map = self.parent.auction._bid_book.get(epoch_to_clear, {})
        if not bid_map:
            pretty.log(
                f"[grey]No bids to clear this epoch (master). epoch(e)={epoch_to_clear}[/grey]"
            )
            return False

        # 1) Compute share and TAO budget for this epoch from master share snapshot
        share = float(self.parent.auction._my_share_by_epoch.get(epoch_to_clear, 0.0))
        start_block = int(self.parent.epoch_start_block)
        end_block = int(self.parent.epoch_end_block)

        # Base subnet = this validator's netuid (e.g., SN-73)
        base_sid = int(getattr(self.parent.config, "netuid", METAHASH_SUBNET_ID))
        base_price_tao_per_alpha = await self._safe_price(base_sid, start_block, end_block)

        my_budget_tao = budget_tao_from_share(
            share=share,
            auction_budget_alpha=AUCTION_BUDGET_ALPHA,  # α (of base subnet) per epoch
            price_tao_per_alpha_base=base_price_tao_per_alpha,
        )
        if my_budget_tao <= EPS_VALUE:
            pretty.log("[yellow]TAO budget share is zero – skipping clear.[/yellow]")
            return False

        pretty.kv_panel(
            "Budget (VALUE in TAO) — base subnet",
            [
                ("epoch", epoch_to_clear),
                ("base_sid", base_sid),
                ("base_price_tao/α", f"{base_price_tao_per_alpha:.8f}"),
                ("share", f"{share:.6f}"),
                ("AUCTION_BUDGET_ALPHA(α)", f"{float(AUCTION_BUDGET_ALPHA):.6f}"),
                ("my_budget_tao (VALUE)", f"{my_budget_tao:.6f}"),
            ],
            style="bold cyan",
        )

        # 2) Build allocation inputs (subnet weights already snapped when accepting the bid)
        bids_in_all: List[BidInput] = self._to_bid_inputs(bid_map)

        # Enforce forbidden subnets at clearing time as well (for allocation)
        bids_in: List[BidInput] = [
            b for b in bids_in_all if b.subnet_id not in FORBIDDEN_ALPHA_SUBNETS
        ]
        if not bids_in:
            pretty.log(
                "[yellow]All bids filtered out by forbidden subnet policy; nothing to clear.[/yellow]"
            )
            return False

        # 2.a Estimate prices & depths for **ordering + valuation with slippage**
        # Use *all* accepted bids for pricing so we can snapshot values for non-winners too
        price_by_sid: Dict[int, float] = await self._estimate_prices_for_bids(bids_in_all, start_block, end_block)
        depth_by_sid: Dict[int, int] = await self._estimate_depths_for_bids(bids_in_all, start_block, end_block)

        def line_value_tao(b: BidInput, alpha: float) -> float:
            """VALUE TAO for *alpha* of this bid using price+slippage × weight × (1−discount)."""
            if alpha <= 0:
                return 0.0
            rao = int(round(float(alpha) * PLANCK))
            return effective_value_tao(
                rao, b.weight_bps, b.discount_bps,
                price_by_sid.get(b.subnet_id, 0.0),
                depth_by_sid.get(b.subnet_id, 0),
            )

        # Ordered preview by estimated TAO value for operator visibility (based on requested α)
        ordered_preview = sorted(
            bids_in, key=lambda b: -line_value_tao(b, max(0.0, float(b.alpha)))
        )
        rows_bids = []
        for b in ordered_preview[: max(5, LOG_TOP_N)]:
            req_alpha = max(0.0, float(b.alpha))
            line_val = line_value_tao(b, req_alpha)
            per_alpha = (line_val / max(EPS_ALPHA, req_alpha))
            rows_bids.append(
                [
                    b.miner_uid,
                    b.coldkey[:8] + "…",
                    b.subnet_id,
                    b.weight_bps,
                    b.discount_bps,
                    f"{req_alpha:.4f} α",
                    f"{per_alpha:.10f} TAO/α(est.)",
                    depth_by_sid.get(b.subnet_id, 0),
                    f"{line_val:.6f} TAO(VALUE)",
                ]
            )
        if rows_bids:
            pretty.table(
                "[yellow]Bids — ordered by VALUE (TAO)[/yellow]",
                ["UID", "CK", "Subnet", "W_bps", "Disc_bps", "Bid α", "Price(est) TAO/α", "Depth", "VALUE TAO"],
                rows_bids,
            )

        # 3) Single-pass quotas from normalized reputation (caps in TAO)
        cap_tao_by_ck, quota_frac = self._compute_quota_caps_tao(bids_in, my_budget_tao)
        if REPUTATION_ENABLED and cap_tao_by_ck:
            cap_rows = [
                [ck[:8] + "…", f"{quota_frac.get(ck, 0.0):.3f}", f"{cap_tao_by_ck.get(ck, 0.0):.6f} TAO"]
                for ck in sorted(cap_tao_by_ck.keys())
            ]
            pretty.table("Reputation caps (TAO, per coldkey)",
                         ["Coldkey", "Quota frac", "Cap TAO"], cap_rows)

        # 4) TAO-budget greedy allocator honoring per-CK TAO caps — WITH PARTIALS
        ordered = sorted(
            bids_in,
            key=lambda b: (line_value_tao(b, max(0.0, float(b.alpha))), float(b.alpha), -int(b.idx)),
            reverse=True,
        )
        remaining_budget_tao = float(my_budget_tao)
        cap_left_tao = {ck: float(v) for ck, v in cap_tao_by_ck.items()} if cap_tao_by_ck else {}

        winners: List[WinAllocation] = []
        dbg_rows = []
        # FIX: track accepted per bid to support pass2 (relaxed)
        accepted_alpha_by_key: Dict[Tuple[int, int, int], float] = defaultdict(float)  # (uid, subnet_id, idx) -> α accepted

        # helper: solve α such that VALUE(α) ≤ limit (monotone → bisection), α ∈ [0, want]
        def _solve_take_alpha_for_limit(b: BidInput, want_alpha: float, limit_tao: float) -> float:
            if limit_tao <= EPS_VALUE or want_alpha <= EPS_ALPHA:
                return 0.0
            # quick accept – full fits
            if line_value_tao(b, want_alpha) <= limit_tao:
                return want_alpha
            lo, hi = 0.0, want_alpha
            # bisection on α (continuous), then quantize to rao (integer planck of α)
            for _ in range(60):  # high precision on α
                mid = 0.5 * (lo + hi)
                val = line_value_tao(b, mid)
                if val <= limit_tao:
                    lo = mid
                else:
                    hi = mid
            # quantize to rao
            rao = int(round(lo * PLANCK))
            if rao <= 0:
                return 0.0
            return rao / float(PLANCK)

        def _make_winalloc(b: BidInput, take_alpha: float) -> WinAllocation:
            kwargs = dict(
                miner_uid=b.miner_uid,
                coldkey=b.coldkey,
                subnet_id=b.subnet_id,
                alpha_accepted=take_alpha,
                discount_bps=b.discount_bps,
                weight_bps=b.weight_bps,
            )
            if "alpha_requested" in self._winalloc_params:
                kwargs["alpha_requested"] = float(b.alpha)
            return WinAllocation(**kwargs)

        def _consider_bid(b: BidInput, tao_limit: float, pass_label: str):
            """Inner step used by both passes."""
            nonlocal remaining_budget_tao
            if tao_limit <= EPS_VALUE:
                return
            key = (b.miner_uid, b.subnet_id, b.idx)
            already = accepted_alpha_by_key.get(key, 0.0)
            want_total = max(0.0, float(b.alpha))
            want_alpha = max(0.0, want_total - already)
            if want_alpha <= EPS_ALPHA:
                return
            take_alpha = _solve_take_alpha_for_limit(b, want_alpha, tao_limit)
            if take_alpha <= EPS_ALPHA:
                return

            w = _make_winalloc(b, take_alpha)
            winners.append(w)

            tao_consumed = line_value_tao(b, take_alpha)
            per_alpha = tao_consumed / max(EPS_ALPHA, take_alpha)

            dbg_rows.append(
                [
                    b.miner_uid,
                    str(b.coldkey)[:8] + "…",
                    b.subnet_id,
                    f"{want_total:.4f} α",
                    f"{already + take_alpha:.4f} α ({'+' if already>0 else ''}{take_alpha:.4f})",
                    f"{per_alpha:.10f} TAO/α",
                    f"{tao_consumed:.6f} TAO(VALUE)",
                    b.discount_bps,
                    b.weight_bps,
                    pass_label,
                ]
            )

            accepted_alpha_by_key[key] = already + take_alpha
            remaining_budget_tao = max(0.0, remaining_budget_tao - tao_consumed)

        # ----- PASS 1: with caps -----
        for b in ordered:
            if remaining_budget_tao <= EPS_VALUE:
                break
            ck = b.coldkey
            # TAO limit available for this allocation (global + per-CK cap)
            if cap_left_tao:
                ck_cap_left = float(cap_left_tao.get(ck, 0.0))
            else:
                ck_cap_left = float("inf")
            tao_limit = min(remaining_budget_tao, ck_cap_left)
            if tao_limit <= EPS_VALUE:
                continue
            _consider_bid(b, tao_limit, "cap")

            # Update per-CK cap if used
            if cap_left_tao and ck_cap_left != float("inf"):
                key = (b.miner_uid, b.subnet_id, b.idx)
                take_alpha = accepted_alpha_by_key[key]  # total accepted so far on this bid
                tao_consumed = line_value_tao(b, take_alpha)
                cap_left_tao[ck] = max(0.0, ck_cap_left - tao_consumed)

        # ----- PASS 2: relax caps if budget left -----
        if remaining_budget_tao > EPS_VALUE:
            for b in ordered:
                if remaining_budget_tao <= EPS_VALUE:
                    break
                # no per-CK cap in pass2
                _consider_bid(b, remaining_budget_tao, "relaxed")

        if dbg_rows:
            pretty.table(
                "Allocations — TAO Budget (caps enforced then relaxed)",
                ["UID", "CK", "Subnet", "Req α", "Acc α (cum)", "TAO/α", "Spend TAO(VALUE)", "Disc_bps", "W_bps", "Pass"],
                dbg_rows[: max(20, LOG_TOP_N)],
            )

        if remaining_budget_tao > EPS_VALUE:
            pretty.log(f"[yellow]Budget leftover (TAO) after allocation: {remaining_budget_tao:.12f} (dust or not enough bids).[/yellow]")

        if not winners:
            pretty.log("[red]No winners after applying VALUE (TAO) budget and reputation caps.[/red]")
            return False

        # Winners detail computed at accepted α
        rows_w = []
        per_win_value_tao: Dict[int, float] = {}
        per_win_value_mu: Dict[int, int] = {}
        for w in winners[: max(20, LOG_TOP_N)]:
            req_alpha = float(getattr(w, "alpha_requested", w.alpha_accepted))
            acc_alpha = float(w.alpha_accepted)
            val_acc = line_value_tao(
                BidInput(
                    miner_uid=w.miner_uid,
                    coldkey=w.coldkey,
                    subnet_id=w.subnet_id,
                    alpha=acc_alpha,
                    discount_bps=w.discount_bps,
                    weight_bps=w.weight_bps,
                ),
                acc_alpha,
            )
            per_win_value_tao[id(w)] = val_acc
            per_win_value_mu[id(w)] = int(encode_value_mu(val_acc))
            rows_w.append(
                [
                    w.miner_uid,
                    w.coldkey[:8] + "…",
                    w.subnet_id,
                    f"{req_alpha:.4f} α",
                    f"{acc_alpha:.4f} α",
                    w.discount_bps,
                    w.weight_bps,
                    f"{val_acc:.6f} TAO(VALUE)",
                    ("P" if acc_alpha + 1e-12 < req_alpha else "F"),
                ]
            )
        pretty.table(
            "Winners — acceptances (VALUE)",
            ["UID", "CK", "Subnet", "Req α", "Acc α", "Disc_bps", "W_bps", "VALUE TAO", "Fill"],
            rows_w,
        )

        # ---------- NEW: Build snapshot of ALL accepted bids (not only winners) ----------
        bids_lines_by_uid: Dict[int, List[List[int]]] = defaultdict(list)
        uids_involved = set()
        cks_involved = set()
        for b in bids_in_all:
            req_alpha = max(0.0, float(b.alpha))
            req_rao = int(round(req_alpha * PLANCK))
            val_req = line_value_tao(b, req_alpha)
            bids_lines_by_uid[int(b.miner_uid)].append(
                [
                    int(b.subnet_id),
                    int(b.discount_bps),
                    int(b.weight_bps),
                    int(req_rao),
                    int(encode_value_mu(val_req)),
                ]
            )
            uids_involved.add(int(b.miner_uid))
            cks_involved.add(str(b.coldkey))

        # rejected bids (recorded by AuctionEngine during accept stage)
        rejected_compact: List[List[object]] = []
        try:
            rej = getattr(self.parent.auction, "_rejected_bids_by_epoch", {}).get(epoch_to_clear, []) or []
        except Exception:
            rej = []
        for r in rej:
            try:
                uid = int(r.get("uid", -1))
                subnet_id = r.get("subnet_id", 0)
                subnet_id = int(subnet_id) if subnet_id is not None else 0
                alpha = float(r.get("alpha", 0.0)) if r.get("alpha") is not None else 0.0
                discount_bps = int(r.get("discount_bps", 0)) if r.get("discount_bps") is not None else 0
                reason = str(r.get("reason", ""))
                req_rao = int(round(max(0.0, alpha) * PLANCK))
                rejected_compact.append([uid, subnet_id, req_rao, discount_bps, reason])
            except Exception:
                # best-effort; skip malformed diagnostic line
                continue

        # coldkey mapping for involved UIDs
        ck_by_uid: Dict[int, str] = {}
        try:
            coldkeys = list(self.parent.metagraph.coldkeys or [])
        except Exception:
            coldkeys = []
        for uid in sorted(uids_involved):
            if 0 <= uid < len(coldkeys):
                ck_by_uid[uid] = str(coldkeys[uid])

        # reputation snapshot for involved coldkeys + caps/params
        rep_store: Dict[str, float] = getattr(self.state, "reputation", {}) or {}
        rep_snapshot = {ck: float(rep_store.get(ck, 0.0)) for ck in sorted(cks_involved)}
        rep_caps_mu = {ck: int(encode_value_mu(float(cap_tao_by_ck.get(ck, 0.0)))) for ck in sorted(cks_involved)} if cap_tao_by_ck else {}
        rep_quota_frac = {ck: float(quota_frac.get(ck, 0.0)) for ck in sorted(cks_involved)} if quota_frac else {}

        # jail snapshot for involved coldkeys
        try:
            jail_map = getattr(self.state, "ck_jail_until_epoch", {}) or {}
        except Exception:
            jail_map = {}
        jail_snapshot = {ck: int(jail_map.get(ck, -1)) for ck in sorted(cks_involved) if ck in jail_map}

        # weights snapshot (bps integer map + optional default from Strategy)
        try:
            weights_src = dict(getattr(self.parent.auction, "weights_bps", {}) or {})
        except Exception:
            weights_src = {}
        wbps = {}
        for sid_raw, val in weights_src.items():
            try:
                sid = int(sid_raw)
            except Exception:
                continue
            try:
                v = float(val)
            except Exception:
                v = 0.0
            if v <= 1.0:
                bp = int(round(v * 10_000.0))
            else:
                bp = int(round(v))
            wbps[sid] = max(0, min(10_000, bp))
        try:
            strat = getattr(self.parent, "strategy", None)
            default_val = float(getattr(strat, "default_value", None)) if strat is not None else None
        except Exception:
            default_val = None
        wbps_default = int(round(default_val * 10_000.0)) if default_val is not None else None

        # 5) Stash pending commitment snapshot (publish later with SAME window)
        pay_epoch = int(epoch_to_clear + 1)
        epoch_len = int(self.parent.epoch_end_block - self.parent.epoch_start_block + 1)
        win_start = int(self.parent.epoch_end_block) + 1
        win_end = int(self.parent.epoch_end_block + epoch_len)

        inv_i: Dict[int, List[List[int]]] = defaultdict(list)
        for w in winners:
            acc_rao_full = int(round(float(w.alpha_accepted) * PLANCK))  # accepted α in rao
            inv_i[int(w.miner_uid)].append(
                [
                    int(w.subnet_id),
                    int(w.discount_bps),
                    int(w.weight_bps),
                    int(acc_rao_full),
                    int(per_win_value_mu[id(w)]),
                ]
            )

        if not isinstance(self.state.pending_commits, dict):
            self.state.pending_commits = {}

        # === include budget signals so Settlement can burn underfill ===
        bt_mu = int(encode_value_mu(my_budget_tao))           # total budget in μTAO
        bl_mu = int(encode_value_mu(remaining_budget_tao))    # leftover in μTAO

        payload = {
            "v": 3,  # payload schema label (content-only; on-chain entry is v4 CID stub)
            "e": int(epoch_to_clear),
            "pe": pay_epoch,
            "as": int(win_start),
            "de": int(win_end),
            "hk": self.parent.hotkey_ss58,
            "t": VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, ""),
            # inventory (compact winners):
            "inv": {str(uid): {"ck": ""} for uid in inv_i.keys()},
            "i": [[uid, lines] for uid, lines in inv_i.items()],
            # budget markers
            "bt_mu": bt_mu,
            "bl_mu": bl_mu,
            "bt_tao": float(my_budget_tao),
            "bl_tao": float(remaining_budget_tao),

            # ---------- NEW snapshots ----------
            # All accepted bids this epoch (requested α), per-uid compact list:
            # each line: [subnet_id, discount_bps, weight_bps, req_rao, est_value_mu]
            "b": [[uid, lines] for uid, lines in bids_lines_by_uid.items()],
            # mapping uid -> coldkey for involved miners
            "ck_by_uid": {str(uid): ck for uid, ck in ck_by_uid.items()},
            # rejected bids with reasons: [uid, subnet_id, req_rao, discount_bps, reason]
            "rj": rejected_compact,
            # reputation snapshot and params
            "rep": {
                "enabled": bool(REPUTATION_ENABLED),
                "gamma": float(REPUTATION_MIX_GAMMA),
                "baseline": float(REPUTATION_BASELINE_CAP_FRAC),
                "capmax": float(REPUTATION_MAX_CAP_FRAC),
                "scores": rep_snapshot,           # {coldkey: score in [0,1]}
                "cap_tao_mu": rep_caps_mu,        # {coldkey: cap in μTAO for this epoch}
                "quota_frac": rep_quota_frac,     # {coldkey: fraction used to derive cap}
            },
            # jail snapshot for involved coldkeys
            "jail": jail_snapshot,                # {coldkey: jail_until_epoch}
            # weights used by this validator when accepting bids (bps integers 0..10000)
            "wbps": {
                "map": wbps,                      # {subnet_id: weight_bps}
                "default": wbps_default,          # int or null
            },
        }

        key = str(epoch_to_clear)
        self.state.pending_commits[key] = payload
        if hasattr(self.state, "save_pending_commits"):
            self.state.save_pending_commits()

        pretty.kv_panel(
            "Staged commit payload",
            [
                ("key", key),
                ("e", payload["e"]),
                ("pe", payload["pe"]),
                ("as", payload["as"]),
                ("de", payload["de"]),
                ("#miners", len(payload["inv"])),
                ("#lines_total", sum(len(v) for _, v in inv_i.items())),
                ("#bidders_all", len(bids_lines_by_uid)),
                ("#rejected", len(rejected_compact)),
                ("#rep_keys", len(rep_snapshot)),
                ("#jail_keys", len(jail_snapshot)),
                ("#wbps_sids", len(wbps)),
            ],
            style="bold magenta",
        )

        # 6) Human-friendly invoice preview (α to pay = accepted α)
        preview_rows = []
        for w in winners:
            acc_alpha = float(w.alpha_accepted)
            acc_rao = int(round(acc_alpha * PLANCK))
            value_tao = per_win_value_tao[id(w)]
            per_alpha = value_tao / max(EPS_ALPHA, acc_alpha)
            inv_id = self._make_invoice_id(
                self.parent.hotkey_ss58,
                epoch_to_clear,
                int(w.subnet_id),
                float(acc_alpha),
                int(w.discount_bps),
                pay_epoch,
                acc_rao,
            )
            preview_rows.append(
                [
                    w.miner_uid,
                    w.coldkey[:8] + "…",
                    w.subnet_id,
                    f"{acc_alpha:.4f} α",
                    f"{per_alpha:.10f} TAO/α",
                    f"{value_tao:.6f} TAO(VALUE)",
                    f"[{win_start},{win_end}]",
                    inv_id[:12],
                ]
            )
        pretty.table(
            "Invoices — α to pay (ACCEPTED)",
            ["UID", "CK", "Subnet", "α to pay", "TAO/α", "Total VALUE (TAO)", "Window", "InvoiceID"],
            preview_rows[: max(20, LOG_TOP_N)],
        )

        # 7) Notify winners immediately; payment window = next epoch (e+1)
        ack_ok = 0
        calls_sent = 0
        targets_resolved = 0
        attempts = len(winners)
        v_uid = self._hotkey_to_uid().get(self.parent.hotkey_ss58, None)

        for w in winners:
            try:
                amount_rao_full = int(round(float(w.alpha_accepted) * PLANCK))
                req_alpha = float(getattr(w, "alpha_requested", w.alpha_accepted))
                uid = int(w.miner_uid)
                if not (0 <= uid < len(self.parent.metagraph.axons)):
                    pretty.log(f"[yellow]Skip notify: invalid uid {uid} (no axon).[/yellow]")
                    continue
                target_ax = self.parent.metagraph.axons[uid]
                if not self._is_reachable_axon(target_ax):
                    pretty.log(
                        f"[yellow]Skip notify: uid {uid} axon not reachable (host/port invalid).[/yellow]"
                    )
                    continue
                targets_resolved += 1

                val_tao = per_win_value_tao[id(w)]
                per_alpha = val_tao / max(EPS_ALPHA, float(w.alpha_accepted))
                partial = (w.alpha_accepted + 1e-12) < float(req_alpha)

                pretty.log(
                    "[cyan]WIN[/cyan] "
                    f"e={epoch_to_clear}→pay_e={pay_epoch} uid={uid} subnet={w.subnet_id} "
                    f"req={float(req_alpha):.4f} α acc={float(w.alpha_accepted):.4f} α "
                    f"TAO/α={per_alpha:.10f} value={val_tao:.6f} TAO "
                    f"disc={w.discount_bps}bps w_bps={w.weight_bps} partial={partial} "
                    f"win=[{win_start},{win_end}]"
                )

                resps = await self.parent.dendrite(
                    axons=[target_ax],
                    synapse=WinSynapse(
                        subnet_id=w.subnet_id,
                        alpha=w.alpha_accepted,  # ACCEPTED α to be paid by the miner
                        clearing_discount_bps=w.discount_bps,
                        pay_window_start_block=win_start,
                        pay_window_end_block=win_end,
                        pay_epoch_index=pay_epoch,
                        validator_uid=v_uid,
                        validator_hotkey=self.parent.hotkey_ss58,
                        requested_alpha=float(req_alpha),
                        accepted_alpha=float(w.alpha_accepted),
                        was_partially_accepted=partial,
                        accepted_amount_rao=amount_rao_full,
                    ),
                    deserialize=True,
                )
                calls_sent += 1
                resp = resps[0] if isinstance(resps, list) and resps else resps
                if isinstance(resp, WinSynapse) and bool(getattr(resp, "ack", False)):
                    ack_ok += 1
            except asyncio.CancelledError as e_exc:
                pretty.log(f"[yellow]WinSynapse to uid={w.miner_uid} cancelled by RPC: {e_exc}[/yellow]")
            except Exception as e_exc:
                pretty.log(f"[yellow]WinSynapse to uid={w.miner_uid} failed: {e_exc}[/yellow]")

        pretty.kv_panel(
            "Early Clear & Notify (epoch e)",
            [
                ("epoch_cleared (e)", epoch_to_clear),
                ("pay_epoch (e+1)", pay_epoch),
                ("winners", len(winners)),
                ("budget_left VALUE (TAO)", f"{remaining_budget_tao:.6f}"),
                ("treasury", VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, "")),
                ("attempts", attempts),
                ("targets_resolved", targets_resolved),
                ("calls_sent", calls_sent),
                ("win_acks", f"{ack_ok}/{calls_sent}"),
            ],
            style="bold green",
        )

        return calls_sent > 0
