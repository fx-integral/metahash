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

# Price oracle (for ordering and budget conversion; prices are NOT written into commitments)
from metahash.utils.subnet_utils import average_price, average_depth
from metahash.validator.valuation import (
    effective_value_tao,
    encode_value_mu,
)

EPS_ALPHA = 1e-12  # tolerance used to mark partial fills


class ClearingEngine:
    """
    Clear winners *now* for the current epoch and notify miners.
    Populates StateStore.pending_commits[e] with the exact pay window (as,de),
    so CommitmentsEngine can publish the same snapshot in e+1.

    - Budget is enforced in TAO, not α. Base budget = AUCTION_BUDGET_ALPHA (α of validator's netuid)
      * price_tao_per_alpha[base].
    - Per-CK quotas are caps in TAO (normalized reputation), enforced during allocation.
    - Ordering and consumption metric (TAO) considers slippage via pool depth:
        value_tao(α) = weight × (1 − discount) × post_slip(price, depth, α)
    - Invoices & WinSynapse carry FULL α owed; we also stash μTAO value for visibility.
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
        for sid in sids:
            out[sid] = await self._safe_price(sid, start_block, end_block)
        return out

    async def _estimate_depths_for_bids(
        self, bids: List[BidInput], start_block: int, end_block: int
    ) -> Dict[int, int]:
        sids = sorted({b.subnet_id for b in bids})
        out: Dict[int, int] = {}
        for sid in sids:
            try:
                st = await self.parent._stxn()
                d = await average_depth(sid, start_block=start_block, end_block=end_block, st=st)
                out[sid] = int(d or 0)
            except (asyncio.CancelledError, Exception) as e:
                pretty.log(f"[yellow]Depth lookup failed/cancelled for sid={sid}: {e}[/yellow]")
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
        cks = sorted({b.coldkey for b in bids})
        if not cks or my_budget_tao <= 0:
            return ({}, {})

        # Reputation comes from StateStore if present; default to 0.0
        rep_store: Dict[str, float] = getattr(self.state, "reputation", {}) or {}

        if not REPUTATION_ENABLED:
            N = max(1, len(cks))
            q = {ck: 1.0 / N for ck in cks}
            return ({ck: my_budget_tao * q[ck] for ck in cks}, q)

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
        Clear and notify winners for the current epoch epoch_to_clear.
        Populates StateStore.pending_commits and persists it.

        Budget is enforced in TAO, computed from
        (my_share * AUCTION_BUDGET_ALPHA[base]) * price_tao_per_alpha[base].
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
        if my_budget_tao <= 0:
            pretty.log("[yellow]TAO budget share is zero – skipping clear.[/yellow]")
            return False

        pretty.kv_panel(
            "Budget (TAO) — base subnet",
            [
                ("epoch", epoch_to_clear),
                ("base_sid", base_sid),
                ("base_price_tao/α", f"{base_price_tao_per_alpha:.8f}"),
                ("share", f"{share:.6f}"),
                ("AUCTION_BUDGET_ALPHA(α)", f"{float(AUCTION_BUDGET_ALPHA):.6f}"),
                ("my_budget_tao", f"{my_budget_tao:.6f}"),
            ],
            style="bold cyan",
        )

        # 2) Build allocation inputs (subnet weights already snapped when accepting the bid)
        bids_in_all: List[BidInput] = self._to_bid_inputs(bid_map)

        # Enforce forbidden subnets at clearing time as well
        bids_in: List[BidInput] = [
            b for b in bids_in_all if b.subnet_id not in FORBIDDEN_ALPHA_SUBNETS
        ]
        if not bids_in:
            pretty.log(
                "[yellow]All bids filtered out by forbidden subnet policy; nothing to clear.[/yellow]"
            )
            return False

        # 2.a Estimate prices & depths for **ordering + valuation with slippage**
        price_by_sid: Dict[int, float] = await self._estimate_prices_for_bids(bids_in, start_block, end_block)
        depth_by_sid: Dict[int, int] = await self._estimate_depths_for_bids(bids_in, start_block, end_block)

        def line_value_tao(b: BidInput, alpha: float) -> float:
            """Full line TAO value for the given α using price+slippage × weight × (1−discount)."""
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
            per_alpha = (line_val / max(1e-12, req_alpha))
            rows_bids.append(
                [
                    b.miner_uid,
                    b.coldkey[:8] + "…",
                    b.subnet_id,
                    b.weight_bps,
                    b.discount_bps,
                    f"{req_alpha:.4f} α",
                    f"{per_alpha:.10f} TAO/α(est.)",
                    f"{line_val:.6f} TAO(value)",
                ]
            )
        if rows_bids:
            pretty.table(
                "[yellow]Bids (ordered by TAO value)[/yellow]",
                ["UID", "CK", "Subnet", "W_bps", "Disc_bps", "Req α", "TAO/α", "Line TAO"],
                rows_bids,
            )

        # 3) Single-pass quotas from normalized reputation (caps in TAO)
        cap_tao_by_ck, quota_frac = self._compute_quota_caps_tao(bids_in, my_budget_tao)
        cap_rows = [
            [ck[:8] + "…", f"{quota_frac.get(ck, 0.0):.3f}", f"{cap_tao_by_ck.get(ck, 0.0):.6f} TAO"]
            for ck in sorted(cap_tao_by_ck.keys())
        ]
        if cap_rows:
            pretty.table("Per-Coldkey Quotas (normalized) → Caps(TAO)",
                        ["Coldkey", "Quota frac", "Cap TAO"], cap_rows)

        # 4) TAO-budget greedy allocator honoring per-CK TAO caps
        ordered = sorted(
            bids_in,
            key=lambda b: (line_value_tao(b, max(0.0, float(b.alpha))), float(b.alpha), -int(b.idx)),
            reverse=True,
        )
        remaining_budget_tao = float(my_budget_tao)
        cap_left_tao = {ck: float(v) for ck, v in cap_tao_by_ck.items()}

        winners: List[WinAllocation] = []
        dbg_rows = []
        req_alpha_by_win: Dict[int, float] = {}
        tao_taken_by_ck: Dict[str, float] = defaultdict(float)

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
            w = WinAllocation(**kwargs)
            req_alpha_by_win[id(w)] = float(b.alpha)
            return w

        def _solve_take_alpha_for_budget(b: BidInput, budget_left: float, cap_left: float) -> float:
            """
            Given remaining budgets (TAO-valued), find α to take (<= requested) such that
            effective_value_tao(α) <= min(budget_left, cap_left). Monotone in α → bisection.
            """
            limit = max(0.0, min(budget_left, cap_left))
            want = max(0.0, float(b.alpha))
            if limit <= 0.0 or want <= 0.0:
                return 0.0
            # If full request fits, return it
            full_val = line_value_tao(b, want)
            if full_val <= limit:
                return want
            # Otherwise bisection on [0, want]
            lo, hi = 0.0, want
            for _ in range(40):  # ~1e-12 α precision
                mid = 0.5 * (lo + hi)
                val = line_value_tao(b, mid)
                if val <= limit:
                    lo = mid
                else:
                    hi = mid
            return lo

        for b in ordered:
            if remaining_budget_tao <= 0:
                break
            ck = b.coldkey
            want_alpha = max(0.0, float(b.alpha))
            if want_alpha <= 0:
                continue

            cap_avail_tao = cap_left_tao.get(ck, remaining_budget_tao)
            if cap_avail_tao <= 0:
                continue

            # Find how much α we can take under current TAO budgets with slippage valuation
            take_alpha = _solve_take_alpha_for_budget(b, remaining_budget_tao, cap_avail_tao)
            if take_alpha <= 0:
                continue

            w = _make_winalloc(b, take_alpha)
            winners.append(w)

            # Debug row: show α vs TAO *value* and caps in TAO-value
            tao_consumed = line_value_tao(b, take_alpha)
            dbg_rows.append(
                [
                    b.miner_uid,
                    str(ck)[:8] + "…",
                    b.subnet_id,
                    f"{b.alpha:.4f} α",
                    f"{take_alpha:.4f} α",
                    f"{(tao_consumed/max(1e-12,take_alpha)):.10f} TAO/α",
                    f"{tao_consumed:.6f} TAO(value)",
                    b.discount_bps,
                    b.weight_bps,
                    f"{cap_avail_tao:.6f}→{cap_avail_tao - tao_consumed:.6f} TAO",
                ]
            )

            remaining_budget_tao -= tao_consumed
            cap_left_tao[ck] = max(0.0, cap_avail_tao - tao_consumed)
            tao_taken_by_ck[ck] += tao_consumed

        if dbg_rows:
            pretty.table(
                "Allocations — TAO Budget (caps enforced in TAO)",
                ["UID", "CK", "Subnet", "Req α", "Take α", "TAO/α", "Spend TAO", "Disc_bps", "W_bps", "Cap TAO (before→after)"],
                dbg_rows[: max(10, LOG_TOP_N)],
            )

        if not winners:
            pretty.log("[red]No winners after applying TAO quotas & budget.[/red]")
            return False

        # Winners detail computed at accepted α
        rows_w = []
        per_win_value_tao: Dict[int, float] = {}
        per_win_value_mu: Dict[int, int] = {}
        for w in winners[: max(10, LOG_TOP_N)]:
            req_alpha = req_alpha_by_win.get(id(w), getattr(w, "alpha_requested", w.alpha_accepted))
            acc_alpha = float(w.alpha_accepted)
            val_acc = effective_value_tao(
                int(round(acc_alpha * PLANCK)),
                int(w.weight_bps),
                int(w.discount_bps),
                price_by_sid.get(int(w.subnet_id), 0.0),
                depth_by_sid.get(int(w.subnet_id), 0),
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
                    f"{val_acc:.6f} TAO(value)",
                    "P" if (acc_alpha + EPS_ALPHA) < float(req_alpha) else "F",
                ]
            )
        pretty.table(
            "Winners — detail (TAO spend)",
            ["UID", "CK", "Subnet", "Req α", "Acc α", "Disc_bps", "W_bps", "Spend TAO", "Fill"],
            rows_w,
        )

        # 5) Stash pending commitment snapshot (publish later with SAME window)
        pay_epoch = int(epoch_to_clear + 1)
        epoch_len = int(self.parent.epoch_end_block - self.parent.epoch_start_block + 1)
        win_start = int(self.parent.epoch_end_block) + 1
        win_end = int(self.parent.epoch_end_block + epoch_len)

        inv_i: Dict[int, List[List[int]]] = defaultdict(list)
        for w in winners:
            req_rao_full = int(round(float(w.alpha_accepted) * PLANCK))  # FULL α owed (accepted)
            inv_i[int(w.miner_uid)].append(
                [
                    int(w.subnet_id),
                    int(w.discount_bps),
                    int(w.weight_bps),
                    int(req_rao_full),
                    int(per_win_value_mu[id(w)]),
                ]
            )

        # Persist to StateStore so CommitmentsEngine can publish in e+1
        self.state.pending_commits[str(epoch_to_clear)] = {
            "v": 3,  # payload schema label
            "e": int(epoch_to_clear),
            "pe": pay_epoch,
            "as": int(win_start),
            "de": int(win_end),
            "hk": self.parent.hotkey_ss58,
            "t": VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, ""),
            "inv": {str(uid): {"ck": ""} for uid in inv_i.keys()},
            "i": [[uid, lines] for uid, lines in inv_i.items()],
        }
        if hasattr(self.state, "save_pending_commits"):
            self.state.save_pending_commits()

        # 6) Human-friendly invoice preview
        preview_rows = []
        for w in winners:
            acc_alpha = float(w.alpha_accepted)
            acc_rao = int(round(acc_alpha * PLANCK))
            value_tao = per_win_value_tao[id(w)]
            per_alpha = value_tao / max(1e-12, acc_alpha)
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
                    f"{value_tao:.6f} TAO",
                    f"[{win_start},{win_end}]",
                    inv_id[:12],
                ]
            )
        pretty.table(
            "Invoice Preview — α to pay & value",
            ["UID", "CK", "Subnet", "α to pay", "TAO/α", "Total TAO", "Window", "InvoiceID"],
            preview_rows[: max(10, LOG_TOP_N)],
        )

        # 7) Notify winners immediately; payment window = next epoch (e+1) with **defined end**
        ack_ok = 0
        calls_sent = 0
        targets_resolved = 0
        attempts = len(winners)
        v_uid = self._hotkey_to_uid().get(self.parent.hotkey_ss58, None)

        for w in winners:
            try:
                amount_rao_full = int(round(float(w.alpha_accepted) * PLANCK))
                req_alpha = req_alpha_by_win.get(
                    id(w), getattr(w, "alpha_requested", w.alpha_accepted)
                )
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

                # For the early-win log, show both α to pay & TAO value cleanly
                val_tao = per_win_value_tao[id(w)]
                per_alpha = val_tao / max(1e-12, float(w.alpha_accepted))
                partial = (w.alpha_accepted + EPS_ALPHA) < float(req_alpha)
                pretty.log(
                    "[cyan]EARLY Win[/cyan] "
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
                        alpha=w.alpha_accepted,  # FULL α to be paid by the miner
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
                ("budget_left TAO", f"{remaining_budget_tao:.6f}"),
                ("treasury", VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, "")),
                ("attempts", attempts),
                ("targets_resolved", targets_resolved),
                ("calls_sent", calls_sent),
                ("win_acks", f"{ack_ok}/{calls_sent}"),
            ],
            style="bold green",
        )

        # Attempted to notify anyone? (conservative success flag)
        return calls_sent > 0
