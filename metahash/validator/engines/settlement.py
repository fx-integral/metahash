# metahash/validator/engines/settlement.py — Modular settlement (step-by-step, VALUE-based, with rich debug)
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from decimal import Decimal, getcontext
from typing import Dict, List, Optional, Set, Tuple

from bittensor import BLOCKTIME
from metahash.utils.pretty_logs import pretty
from metahash.utils.ipfs import aget_json
from metahash.utils.commitments import read_all_plain_commitments
from metahash.validator.alpha_transfers import AlphaTransfersScanner, TransferEvent
from metahash.treasuries import VALIDATOR_TREASURIES
from metahash.validator.valuation import decode_value_mu
from metahash.validator.state import StateStore
from metahash.utils.helpers import safe_json_loads, safe_int

from metahash.config import (
    TESTING,                        # single switch for "no real set_weights"
    POST_PAYMENT_CHECK_DELAY_BLOCKS,
    FORBIDDEN_ALPHA_SUBNETS,
    LOG_TOP_N,
    FORCE_BURN_WEIGHTS,
)

# ---------------- precision / toggles ----------------
getcontext().prec = 60
_1e9 = Decimal(10) ** 9

# Debugging UX
VERBOSE_DUMPS = True
PAUSE_ON_CHECKPOINTS = True  # auto-disabled when not TTY

# Payment check policy:
# If True: require per-subnet paid_rao >= required_rao for *every* line of a miner (strict).
# If False: require miner_total_paid_rao >= miner_total_required_rao (looser, cross-subnet OK).
STRICT_PER_SUBNET = True


def _isatty() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _pause(msg: str):
    """Optional interactive pause."""
    if not PAUSE_ON_CHECKPOINTS or not _isatty():
        return
    try:
        input(f"\n[PAUSE] {msg} — press <enter> to continue...")
    except Exception:
        pass


def _j(obj) -> str:
    """Compact JSON for logs (falls back to str)."""
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        try:
            return str(obj)
        except Exception:
            return "<unprintable>"


def _kv_preview(d: dict, n=8) -> dict:
    out = {}
    for i, (k, v) in enumerate(d.items()):
        if i >= n:
            out["…"] = f"+{len(d)-n} more"
            break
        out[k] = v
    return out


class SettlementEngine:
    """
    Modular settlement flow:
      1) Load snapshots from commitments (v4 → IPFS payloads; legacy inline supported).
      2) Merge payment window [as, de] across masters; wait until closed (de + delay).
      3) Scan α transfers (miners → master treasuries) in the merged window.
      4) Build paid index (uid, treasury, subnet) → paid_rao (drop forbidden / cross-subnet).
      5) Score miners using VALUE stored in payload lines, gated by payment checks:
           - STRICT_PER_SUBNET=True → every line's required_rao must be paid on that subnet.
           - else                     total paid per miner must cover total required across subnets.
      6) Apply weights or preview (TESTING=True suppresses on-chain set_weights).

    Notes:
      • This module *does not* re-compute VALUE; we trust `value_mu` (μTAO) from payload.
      • If any miner fails the payment gate, they get 0 from that master's snapshot.
      • Aggregation: scores from *all masters' snapshots* are summed per miner.
    """

    def __init__(self, parent, state: StateStore):
        self.parent = parent
        self.state = state
        self._scanner: Optional[AlphaTransfersScanner] = None
        self._rpc_lock: asyncio.Lock = parent._rpc_lock  # reuse parent's lock

    # ───────────────────────── public API ─────────────────────────
    async def settle_and_set_weights_all_masters(self, epoch_to_settle: int):
        """
        Top-level driver for settlement of epoch (e−2).
        """
        if epoch_to_settle < 0 or epoch_to_settle in self.state.validated_epochs:
            return

        pretty.rule("[bold cyan]SETTLEMENT — BEGIN[/bold cyan]")
        pretty.kv_panel(
            "Settlement kickoff",
            [("epoch_to_settle (e−2)", epoch_to_settle),
             ("testing", str(TESTING).lower()),
             ("force_burn", str(bool(FORCE_BURN_WEIGHTS)).lower()),
             ("strict_per_subnet", str(bool(STRICT_PER_SUBNET)).lower())],
            style="bold cyan",
        )

        # 1) Load snapshots (payloads) from commitments (IPFS v4 or legacy inline)
        snapshots = await self._load_snapshots_from_commitments(epoch_to_settle)
        if not snapshots:
            self._fallback_burn_all(epoch_to_settle, reason="no commitments")
            return

        # 2) Merge payment window (and ensure closed)
        ok, start_block, end_block = self._merge_payment_window(epoch_to_settle, snapshots)
        if not ok:
            return  # postponed; logs already printed

        # 3) Scan α transfers in [as, de] (optionally you could add +delay here; we already extended end in step 2)
        events = await self._scan_alpha_transfers(start_block, end_block)
        if events is None:
            pretty.kv_panel("Settlement postponed", [("epoch", epoch_to_settle), ("reason", "scanner failed")], style="bold yellow")
            return

        # 4) Build paid α index (uid, treasury, subnet) → paid_rao
        paid_idx, drop_stats = self._build_paid_rao_index(snapshots, events)
        self._log_paid_index_stats(paid_idx, drop_stats)

        # 5) Score miners from snapshots using VALUE (payload) gated by payments
        final_scores, credit_rows = self._score_miners_from_snapshots_value_only(snapshots, paid_idx)
        self._log_credit_rows(credit_rows)

        # 6) Apply weights (or preview in TESTING)
        self._apply_weights_or_preview(epoch_to_settle, final_scores)

        pretty.rule("[bold cyan]SETTLEMENT — END[/bold cyan]")

    # ──────────────────────── Step 1: snapshots ────────────────────────
    async def _load_snapshots_from_commitments(self, epoch_to_settle: int) -> List[Dict]:
        st = await self.parent._stxn()
        try:
            commits = await read_all_plain_commitments(st, netuid=self.parent.config.netuid, block=None)
        except Exception as e:
            pretty.log(f"[red]Commitment read_all failed: {e}[/red]")
            return []

        masters_hotkeys: Set[str] = set(VALIDATOR_TREASURIES.keys())
        masters_treasuries: Set[str] = set(VALIDATOR_TREASURIES.values())
        snapshots: List[Dict] = []

        pretty.kv_panel("Commitments fetched", [("#hotkeys_in_commit_map", len(commits or {}))], style="bold cyan")

        def _decode_commit_entry(entry):
            if entry is None:
                return None
            if isinstance(entry, dict):
                raw = entry.get("raw")
                parsed = safe_json_loads(raw) if isinstance(raw, (str, bytes, bytearray, dict, list)) else None
                return parsed if parsed is not None else entry
            if isinstance(entry, (str, bytes, bytearray)):
                return safe_json_loads(entry)
            return None

        for hk_key, data_raw in (commits or {}).items():
            data_decoded = _decode_commit_entry(data_raw)
            candidates: List[Dict] = []

            if isinstance(data_decoded, dict) and isinstance(data_decoded.get("sn"), list):
                for s in data_decoded["sn"]:
                    s_dec = _decode_commit_entry(s)
                    if isinstance(s_dec, dict):
                        candidates.append(s_dec)
            elif isinstance(data_decoded, dict):
                candidates = [data_decoded]
            else:
                continue

            chosen = None
            for s in candidates:
                # v4 compact (CID-only)
                if s.get("v") == 4 and s.get("e") == epoch_to_settle and s.get("pe") == (epoch_to_settle + 1):
                    cid = s.get("c", "")
                    if isinstance(cid, str) and cid:
                        try:
                            raw, _norm_bytes, _h = await aget_json(cid)
                            payload = safe_json_loads(raw)
                            if not isinstance(payload, dict) or "as" not in payload or "de" not in payload:
                                continue
                            payload.setdefault("e", s.get("e"))
                            payload.setdefault("pe", s.get("pe"))
                            payload.setdefault("hk", hk_key)
                            payload.setdefault("t", VALIDATOR_TREASURIES.get(hk_key, ""))
                            chosen = dict(payload)

                            # Human visibility
                            inv = payload.get("inv") or payload.get("i")
                            pretty.kv_panel(
                                "Payload fetched (IPFS)",
                                [
                                    ("hk", hk_key[:10] + "…"),
                                    ("cid", cid[:46] + ("…" if len(cid) > 46 else "")),
                                    ("e", payload.get("e")),
                                    ("pe", payload.get("pe")),
                                    ("as", payload.get("as")),
                                    ("de", payload.get("de")),
                                    ("has_inv", str(bool(inv)).lower()),
                                ],
                                style="bold cyan",
                            )
                            break
                        except Exception as ipfs_exc:
                            pretty.log(f"[yellow]Skip hk={hk_key[:6]}… — failed to fetch CID: {ipfs_exc}[/yellow]")
                            continue

                # Legacy inline (for completeness)
                if s.get("e") == epoch_to_settle and s.get("pe") == (epoch_to_settle + 1) and "as" in s and "de" in s:
                    chosen = dict(s)
                    chosen.setdefault("hk", hk_key)
                    chosen.setdefault("t", VALIDATOR_TREASURIES.get(hk_key, ""))
                    pretty.kv_panel(
                        "Payload fetched (inline legacy)",
                        [
                            ("hk", hk_key[:10] + "…"),
                            ("e", s.get("e")),
                            ("pe", s.get("pe")),
                            ("as", s.get("as")),
                            ("de", s.get("de")),
                            ("has_inv", str("inv" in s or "i" in s).lower()),
                        ],
                        style="bold cyan",
                    )
                    break

            if not chosen:
                continue

            tre = chosen.get("t", "")
            if hk_key not in masters_hotkeys and tre not in masters_treasuries:
                continue

            snapshots.append({"hk": hk_key, **chosen})

        if not snapshots:
            pretty.log("[grey]No usable snapshots found in commitments.[/grey]")
        else:
            pretty.kv_panel("Snapshots ready", [("#masters", len(snapshots))], style="bold cyan")
        _pause("After fetching payloads")
        return snapshots

    # ───────────────────── Step 2: window merge ───────────────────────
    def _merge_payment_window(self, epoch_to_settle: int, snapshots: List[Dict]) -> Tuple[bool, int, int]:
        as_vals: List[int] = []
        de_vals: List[int] = []
        clean: List[Dict] = []

        for s in snapshots:
            inv = self._normalize_inv(s)
            if inv is None:
                pretty.log(f"[yellow]Snapshot hk={s.get('hk','')[:8]}… has no usable inventory — skipping.[/yellow]")
                continue
            s["inv"] = inv
            a = safe_int(s.get("as"))
            d = safe_int(s.get("de"))
            if a is None or d is None:
                pretty.log(f"[yellow]Snapshot hk={s.get('hk','')[:8]}… missing as/de — skipping.[/yellow]")
                continue
            as_vals.append(a); de_vals.append(d)
            clean.append(s)

        if not as_vals or not de_vals:
            pretty.log("[yellow]No valid windows extracted from commitments — postponing settlement this epoch.[/yellow]")
            return False, 0, 0

        start_block = min(as_vals)
        end_block = max(de_vals)

        pretty.show_settlement_window(
            epoch_to_settle, start_block, end_block, POST_PAYMENT_CHECK_DELAY_BLOCKS, len(clean)
        )

        # extend end by post-payment delay
        end_block += max(0, int(POST_PAYMENT_CHECK_DELAY_BLOCKS or 0))
        _pause("After windows merged (as/de)")

        # Ensure pay window is closed
        if self.parent.block < end_block:
            remain = end_block - self.parent.block
            eta_s = remain * BLOCKTIME
            pretty.kv_panel(
                "Waiting for payment window to close",
                [
                    ("settle epoch (e−2)", epoch_to_settle),
                    ("payment epoch scanned (e−1+1)", epoch_to_settle + 1),
                    ("blocks_left", remain),
                    ("~eta", f"{int(eta_s//60)}m{int(eta_s%60):02d}s"),
                ],
                style="bold yellow",
            )
            return False, 0, 0

        snapshots.clear()
        snapshots.extend(clean)
        return True, start_block, end_block

    # ─────────────────── Step 3: scan α transfers ─────────────────────
    async def _scan_alpha_transfers(self, start_block: int, end_block: int) -> Optional[List[TransferEvent]]:
        pretty.rule("[bold cyan]SCAN α TRANSFERS[/bold cyan]")

        if self._scanner is None:
            self._scanner = AlphaTransfersScanner(await self.parent._stxn(), dest_coldkey=None, rpc_lock=self._rpc_lock)

        try:
            events_raw = await self._scanner.scan(start_block, end_block)
        except Exception as scan_exc:
            pretty.log(f"[yellow]Scanner failed: {scan_exc}[/yellow]")
            return None

        def _coerce(ev) -> TransferEvent | None:
            if isinstance(ev, TransferEvent):
                return ev
            if isinstance(ev, dict):
                try:
                    return TransferEvent(
                        block=int(ev.get("block", -1)),
                        from_uid=int(ev.get("from_uid", -1)),
                        to_uid=int(ev.get("to_uid", -1)),
                        subnet_id=int(ev.get("subnet_id")),
                        amount_rao=int(ev.get("amount_rao", 0)),
                        src_coldkey=ev.get("src_coldkey"),
                        dest_coldkey=ev.get("dest_coldkey"),
                        src_coldkey_raw=ev.get("src_coldkey_raw"),
                        dest_coldkey_raw=ev.get("dest_coldkey_raw"),
                        src_subnet_id=(None if ev.get("src_subnet_id") is None else int(ev.get("src_subnet_id"))),
                    )
                except Exception:
                    return None
            return None

        events: List[TransferEvent] = []
        if isinstance(events_raw, list):
            for ev in events_raw:
                c = _coerce(ev)
                if c:
                    events.append(c)

        pretty.kv_panel("Scanner result",
                        [("#events_raw", len(events_raw or [])),
                         ("#events (coerced)", len(events)),
                         ("range", f"[{start_block},{end_block}]")],
                        style="bold cyan")

        if VERBOSE_DUMPS and events:
            sample = []
            for e in events[:5]:
                sample.append(
                    {
                        "blk": e.block,
                        "src_ck": (e.src_coldkey[:8] + "…") if e.src_coldkey else None,
                        "dst_ck": (e.dest_coldkey[:8] + "…") if e.dest_coldkey else None,
                        "sid": e.subnet_id,
                        "src_sid": e.src_subnet_id,
                        "amt(rao)": e.amount_rao,
                    }
                )
            pretty.log("[dim]events(sample): " + _j(sample) + (" …" if len(events) > 5 else "") + "[/dim]")

        _pause("After scanning & coercing events")
        return events

    # ───────── Step 4: build paid α index & stats ─────────
    def _build_paid_rao_index(
        self, snapshots: List[Dict], events: List[TransferEvent]
    ) -> Tuple[Dict[Tuple[int, str, int], int], Dict[str, int]]:
        master_treasuries: Set[str] = {VALIDATOR_TREASURIES.get(s.get("hk", ""), s.get("t", "")) for s in snapshots}

        # UIDs present (limit ck→uid mapping)
        uids_present: Set[int] = set()
        for s in snapshots:
            inv = s.get("inv", {}) or {}
            for uid_s in inv.keys():
                try:
                    uids_present.add(int(uid_s))
                except Exception:
                    pass

        ck_to_uid: Dict[str, int] = {}
        try:
            for uid, ck in enumerate(self.parent.metagraph.coldkeys):
                if uid in uids_present:
                    ck_to_uid[ck] = uid
        except Exception:
            pass

        paid_idx: Dict[Tuple[int, str, int], int] = defaultdict(int)
        drop_stats = dict(
            not_master=0, forbidden=0, uid_missing=0, cross_subnet=0,
        )

        for ev in (events or []):
            try:
                if ev.dest_coldkey not in master_treasuries:
                    drop_stats["not_master"] += 1
                    continue
                if ev.subnet_id in FORBIDDEN_ALPHA_SUBNETS:
                    drop_stats["forbidden"] += 1
                    continue
                uid = ck_to_uid.get(ev.src_coldkey)
                if uid is None or uid not in uids_present:
                    drop_stats["uid_missing"] += 1
                    continue
                src_sid = ev.src_subnet_id if getattr(ev, "src_subnet_id", None) is not None else ev.subnet_id
                if src_sid != ev.subnet_id:
                    # strict: require src=dest subnet (prevents cross-subnet spoof)
                    drop_stats["cross_subnet"] += 1
                    continue
                paid_idx[(uid, ev.dest_coldkey, ev.subnet_id)] += int(ev.amount_rao)
            except Exception:
                continue

        return paid_idx, drop_stats

    def _log_paid_index_stats(self, paid_idx: Dict[Tuple[int, str, int], int], drop_stats: Dict[str, int]):
        pretty.kv_panel(
            "Paid α aggregation",
            [
                ("#paid_keys", len(paid_idx)),
                ("dropped_not_master", drop_stats.get("not_master", 0)),
                ("dropped_forbidden", drop_stats.get("forbidden", 0)),
                ("dropped_uid_missing", drop_stats.get("uid_missing", 0)),
                ("dropped_cross_subnet", drop_stats.get("cross_subnet", 0)),
            ],
            style="bold cyan",
        )
        if VERBOSE_DUMPS and paid_idx:
            sample = []
            for i, (k, v) in enumerate(paid_idx.items()):
                if i >= 6:
                    break
                uid, tre, sid = k
                sample.append({"uid": uid, "tre": tre[:10] + "…", "sid": sid, "paid_rao": v})
            pretty.log("[dim]paid(sample): " + _j(sample) + (" …" if len(paid_idx) > 6 else "") + "[/dim]")
        _pause("After paid α aggregation")

    # ───────── Step 5: scoring using VALUE in payload only ─────────
    def _score_miners_from_snapshots_value_only(
        self,
        snapshots: List[Dict],
        paid_idx: Dict[Tuple[int, str, int], int],
    ) -> Tuple[List[float], List[List[str | int | float]]]:
        """
        Scoring rule (per master snapshot):
          * Collect a miner's winning lines (each has [sid, disc_bps, w_bps, required_rao, value_mu]).
          * Gate: if STRICT_PER_SUBNET → for EVERY subnet: paid_rao(uid, tre, sid) >= sum(required_rao on that sid).
                  else               → sum_paid_rao(uid, tre, ANY sid) >= sum_all_required_rao.
          * If gate fails → miner gets 0 from that snapshot.
          * Else reward = sum(decode(value_mu)) across all lines.
        Aggregate rewards across all masters.
        """
        scores_by_uid: Dict[int, float] = defaultdict(float)
        credit_rows: List[List[str | int | float]] = []  # per-miner summary rows

        # stable ordering
        miner_uids_all: List[int] = list(self.parent.get_miner_uids())

        for s in snapshots:
            tre: str = s.get("t", "") or ""
            inv: Dict[str, Dict] = s.get("inv", {}) or {}

            # Build per-miner required/pay/value
            for uid_s, inv_d in inv.items():
                try:
                    uid = int(uid_s)
                except Exception:
                    continue

                # Gather required and values by subnet
                by_sid_required: Dict[int, int] = defaultdict(int)
                total_required = 0
                total_value_tao = 0.0

                # Lines may be in s["i"] (compact) rather than inv_d, normalize both:
                # Preferred source: compact "i" pairs
                lines_compact: List[List[int]] = []
                if "i" in s and isinstance(s["i"], list):
                    for pair in s["i"]:
                        try:
                            uid2, lines = pair
                        except Exception:
                            continue
                        if int(uid2) != uid:
                            continue
                        if isinstance(lines, list):
                            for ln in lines:
                                if isinstance(ln, list) and len(ln) >= 4:
                                    lines_compact.append(ln)
                # Fallback: inv_d["ln"] style if present
                if not lines_compact and isinstance(inv_d, dict) and isinstance(inv_d.get("ln"), list):
                    for ln in inv_d.get("ln", []):
                        if isinstance(ln, list) and len(ln) >= 4:
                            lines_compact.append(ln)

                for ln in lines_compact:
                    # Expected normalized order in our payload: [sid, disc_bps, w_bps, required_rao, value_mu?]
                    try:
                        sid = int(ln[0]); req_rao = int(ln[3])
                    except Exception:
                        continue
                    if req_rao <= 0:
                        continue
                    by_sid_required[sid] += req_rao
                    total_required += req_rao

                    # VALUE metric: μTAO if present (int)
                    val_mu = int(ln[4]) if len(ln) >= 5 else 0
                    if val_mu > 0:
                        total_value_tao += decode_value_mu(val_mu)

                if total_required <= 0:
                    # no actual requirement -> no credit (or skip)
                    continue

                # Determine paid α for this miner to THIS master's treasury (strictly per-subnet or total)
                if STRICT_PER_SUBNET:
                    fully_paid = True
                    for sid, need in by_sid_required.items():
                        got = int(paid_idx.get((uid, tre, sid), 0))
                        if got < need:
                            fully_paid = False
                            break
                else:
                    total_paid = 0
                    # sum paid to this treasury on any subnet (only those we considered)
                    for sid in by_sid_required.keys():
                        total_paid += int(paid_idx.get((uid, tre, sid), 0))
                    fully_paid = (total_paid >= total_required)

                credit_rows.append([
                    uid, tre[:8] + "…", ("strict" if STRICT_PER_SUBNET else "total"),
                    total_required, (None if STRICT_PER_SUBNET else total_paid if total_required > 0 else 0),
                    "OK" if fully_paid else "NO", f"{total_value_tao:.6f}"
                ])

                if fully_paid and total_value_tao > 0:
                    scores_by_uid[uid] += total_value_tao

        final_scores = [float(scores_by_uid.get(uid, 0.0)) for uid in miner_uids_all]
        return final_scores, credit_rows

    def _log_credit_rows(self, rows: List[List[str | int | float]]):
        if rows:
            pretty.table(
                "[cyan]Miner credit (per snapshot) — Required α vs Paid α → VALUE sum[/cyan]",
                ["UID", "Treasury", "Gate", "α_required(rao)", "α_paid(rao?)", "Credit?", "VALUE_sum(TAO)"],
                rows[: max(12, int(LOG_TOP_N))],
            )
            extra = len(rows) - max(12, int(LOG_TOP_N))
            if extra > 0:
                pretty.log(f"[dim]… +{extra} more rows[/dim]")

    # ───────── Step 6: apply weights or preview ─────────
    def _apply_weights_or_preview(self, epoch_to_settle: int, final_scores: List[float]):
        miner_uids_all = list(self.parent.get_miner_uids())

        burn_reason = None
        burn_all = FORCE_BURN_WEIGHTS or (not any(final_scores))
        if FORCE_BURN_WEIGHTS:
            burn_reason = "FORCE_BURN_WEIGHTS"
        elif not any(final_scores):
            burn_reason = "NO_POSITIVE_SCORES"

        self._log_final_scores_table(final_scores, miner_uids_all, reason=burn_reason)
        self._log_weights_preview(final_scores, miner_uids_all, mode="burn-all" if burn_all else "normal")
        _pause("Before applying weights (or preview in TESTING)")

        # Burn-all substitution if needed (and not testing)
        if burn_all and not TESTING:
            pretty.log(f"[red]Burn-all triggered – reason: {burn_reason}.[/red]")
            final_scores = [1.0 if uid == 0 else 0.0 for uid in miner_uids_all]

        if not TESTING:
            pretty.log("[green]Applying weights on-chain.[/green]")
            self.parent.update_scores(final_scores, miner_uids_all)
            self.parent.set_weights()
        else:
            pretty.kv_panel("TESTING — on-chain set_weights() suppressed",
                            [("nonzero_miners", sum(1 for s in final_scores if s > 0)),
                             ("sum(scores)", f"{sum(final_scores):.6f}"),
                             ("mode", "burn-all" if (FORCE_BURN_WEIGHTS or not any(final_scores)) else "normal")],
                            style="bold magenta")

        self.state.validated_epochs.add(epoch_to_settle)
        self.state.save_validated_epochs()
        pretty.kv_panel("Settlement Complete",
                        [("epoch_settled (e−2)", epoch_to_settle),
                         ("miners_scored", sum(1 for x in final_scores if x > 0))],
                        style="bold green")

    # ───────────────────────── helpers ─────────────────────────
    def _normalize_inv(self, s: Dict) -> Optional[Dict[str, Dict]]:
        """
        Normalize inventory to:
          inv = {
            "<uid>": { "ck": "<coldkey|empty>", "ln": [[sid, disc_bps, weight_bps, required_rao, value_mu?], ...] },
            ...
          }
        Accepts our compact `i: [[uid, [[sid, disc_bps, w_bps, rao, value_mu?], ...]], ...]`
        and legacy variants (ln with [sid, w_bps, rao, disc?, value_mu?]).
        """
        inv: Dict[str, Dict] | None = s.get("inv")  # type: ignore[assignment]
        if isinstance(inv, dict) and inv:
            if VERBOSE_DUMPS:
                sample_uid = next(iter(inv.keys()), None)
                pretty.kv_panel("inv pre-normalized (dict)",
                                [("uids", len(inv)), ("sample_uid", str(sample_uid))],
                                style="bold cyan")
            return inv

        # fall back to legacy/compact "i"
        inv_tmp: Dict[str, Dict] = {}
        if "i" in s and isinstance(s["i"], list):
            for item in s["i"]:
                if not (isinstance(item, list) and len(item) == 2):
                    continue
                uid, lines = item
                try:
                    uid_int = int(uid)
                except Exception:
                    continue
                # coldkey not strictly required in inventory for settlement gate
                ln_list: List[List[int]] = []
                for ln in (lines or []):
                    if not (isinstance(ln, list) and len(ln) >= 4):
                        continue
                    try:
                        sid = int(ln[0])
                        disc = int(ln[1])
                        w_bps = int(ln[2])
                        rao = int(ln[3])
                        val_mu = int(ln[4]) if len(ln) >= 5 else 0
                    except Exception:
                        continue
                    if val_mu:
                        ln_list.append([sid, disc, w_bps, rao, val_mu])
                    else:
                        ln_list.append([sid, disc, w_bps, rao])
                inv_tmp[str(uid_int)] = {"ck": "", "ln": ln_list}

        if VERBOSE_DUMPS and inv_tmp:
            any_uid = next(iter(inv_tmp.keys()), None)
            ln_samp = inv_tmp.get(any_uid, {}).get("ln", [])[:3]
            pretty.kv_panel("inv normalized (compact→dict)",
                            [("#uids", len(inv_tmp)), ("sample_uid", str(any_uid)), ("sample_lines", _j(ln_samp))],
                            style="bold cyan")
        return inv_tmp if inv_tmp else None

    def _log_final_scores_table(self, scores: List[float], uids: List[int], reason: str | None = None):
        pairs = list(zip(uids, scores))
        pairs.sort(key=lambda x: x[1], reverse=True)
        top_n = max(1, int(LOG_TOP_N))
        head = pairs[:top_n]
        total = sum(scores)
        nonzero = sum(1 for _, s in pairs if s > 0)
        lines = []
        lines.append("┌──────────── Final Scores (top {}) ────────────┐".format(min(top_n, len(pairs))))
        lines.append("│ {:>6} │ {:>14} │".format("UID", "score (TAO)"))
        lines.append("├─────────┼────────────────┤")
        for uid, sc in head:
            lines.append("│ {:>6} │ {:>14.6f} │".format(uid, sc))
        if len(pairs) > top_n:
            lines.append("│ ... │ ({} more) │".format(len(pairs) - top_n))
        lines.append("├─────────┴────────────────┤")
        lines.append("│ total: {:>10.6f} | nonzero: {:>4} │".format(total, nonzero))
        if reason:
            lines.append("│ reason: {:<28} │".format(reason))
        lines.append("└───────────────────────────┘")
        pretty.log("\n".join(lines))

    def _log_weights_preview(self, scores: List[float], uids: List[int], mode: str):
        total = sum(scores) or 1.0
        weights = [s / total for s in scores]
        pairs = list(zip(uids, weights))
        pairs.sort(key=lambda x: x[1], reverse=True)
        top_n = max(1, int(LOG_TOP_N))
        rows = [[uid, f"{w:.6f}", f"{(w*100):.2f}%"] for uid, w in pairs[:top_n]]
        pretty.table(
            f"[magenta]WEIGHTS PREVIEW — mode={mode}[/magenta]",
            ["UID", "Weight", "% of total"],
            rows
        )
        pretty.kv_panel("Weights Summary",
                        [("nonzero", sum(1 for w in weights if w > 0)),
                         ("sum(weights)", f"{sum(weights):.6f}"),
                         ("max", f"{max(weights) if weights else 0:.6f}")],
                        style="bold magenta")

    # ─────────────── burn-all fallback when empty ───────────────
    def _fallback_burn_all(self, epoch_to_settle: int, reason: str):
        pretty.log(f"[grey]No master commitments found — applying burn-all weights ({reason}).[/grey]")
        miner_uids_all: List[int] = list(self.parent.get_miner_uids())
        final_scores = [1.0 if uid == 0 else 0.0 for uid in miner_uids_all]
        self._log_final_scores_table(final_scores, miner_uids_all, reason=reason)
        self._log_weights_preview(final_scores, miner_uids_all, mode="burn-all (no snapshots)")
        if not TESTING:
            self.parent.update_scores(final_scores, miner_uids_all)
            self.parent.set_weights()
        else:
            pretty.log("[yellow]TESTING: skipping on-chain set_weights().[/yellow]")
        self.state.validated_epochs.add(epoch_to_settle)
        self.state.save_validated_epochs()
        pretty.rule("[bold cyan]SETTLEMENT — END[/bold cyan]")
