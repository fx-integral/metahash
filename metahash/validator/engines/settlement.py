# metahash/validator/engines/settlement.py — Simple settlement (TESTING-aware, verbose)

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
from metahash.validator.alpha_transfers import AlphaTransfersScanner
from metahash.validator.alpha_transfers import TransferEvent
from metahash.treasuries import VALIDATOR_TREASURIES
from metahash.validator.valuation import decode_value_mu, effective_value_tao  # NEW

from metahash.validator.state import StateStore
from metahash.utils.helpers import safe_json_loads, safe_int

from metahash.config import (
    TESTING,  # ← single switch for "no real set_weights"
    POST_PAYMENT_CHECK_DELAY_BLOCKS,
    FORBIDDEN_ALPHA_SUBNETS,
    LOG_TOP_N,
    FORCE_BURN_WEIGHTS,
    K_SLIP,
    SLIP_TOLERANCE,
)

# ---------- Precision & constants ----------
getcontext().prec = 60
_1e9 = Decimal(10) ** 9
K_SLIP_D = Decimal(str(K_SLIP))
SLIP_TOLERANCE_D = Decimal(str(SLIP_TOLERANCE))

# ---------- Local toggles (safe defaults) ----------
VERBOSE_DUMPS = True
PAUSE_ON_CHECKPOINTS = True  # we auto-disable if not a TTY


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
    """Compact JSON dump for logs."""
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        try:
            return str(obj)
        except Exception:
            return "<unprintable>"


def _head(items: list, n=3):
    return items[: min(n, len(items))]


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
    Simplified settlement across ALL masters for epoch e−2.
    TESTING=True → compute everything, show weights, skip on-chain set_weights().

    Flow:
      [Commitments] → [Windows] → [Scanner] → [Prices/Depths]* → [Scores] → [Weights]
      * Only fetched if stored μTAO values are missing in snapshots (fallback).
    """

    def __init__(self, parent, state: StateStore):
        self.parent = parent
        self.state = state
        self._scanner: Optional[AlphaTransfersScanner] = None
        self._rpc_lock: asyncio.Lock = parent._rpc_lock  # reuse parent's lock

    # ---------- public API ----------
    async def settle_and_set_weights_all_masters(self, epoch_to_settle: int):
        if epoch_to_settle < 0 or epoch_to_settle in self.state.validated_epochs:
            return

        pretty.rule("[bold cyan]DBG: BEGIN SETTLEMENT[/bold cyan]")
        pretty.kv_panel(
            "DBG: Settlement kickoff",
            [("epoch_to_settle (e-2)", epoch_to_settle),
             ("testing", str(TESTING).lower()),
             ("force_burn", str(bool(FORCE_BURN_WEIGHTS)).lower())],
            style="bold cyan",
        )

        st = await self.parent._stxn()
        try:
            commits = await read_all_plain_commitments(
                st, netuid=self.parent.config.netuid, block=None
            )
            cnt = len(commits or {})
            pretty.kv_panel("DBG: Commitments fetched", [("#hotkeys_in_commit_map", cnt)], style="bold cyan")
            if VERBOSE_DUMPS and cnt:
                pretty.log("[dim]commit hotkeys sample: "
                           + ", ".join(_head(list(commits.keys()), 5)) + (" …" if cnt > 5 else "") + "[/dim]")
        except Exception as e:
            pretty.log(f"[red]Commitment read_all failed: {e}[/red]")
            return

        masters_hotkeys: Set[str] = set(VALIDATOR_TREASURIES.keys())
        masters_treasuries: Set[str] = set(VALIDATOR_TREASURIES.values())
        snapshots: List[Dict] = []

        # ── 1) Extract v4 snapshots (CID) or inline legacy for this epoch ──
        pretty.rule("[bold cyan]DBG: PARSE COMMITMENTS → SNAPSHOTS[/bold cyan]")

        for hk_key, data_raw in (commits or {}).items():
            data_decoded = self._decode_commit_entry(data_raw)
            cand_list: List[Dict] = []

            if isinstance(data_decoded, dict) and "sn" in data_decoded and isinstance(data_decoded["sn"], list):
                for s in data_decoded["sn"]:
                    s_dec = self._decode_commit_entry(s)
                    if isinstance(s_dec, dict):
                        cand_list.append(s_dec)
            elif isinstance(data_decoded, dict):
                cand_list = [data_decoded]
            else:
                continue

            chosen = None
            for s in cand_list:
                # v4 compact (CID-only)
                if s.get("v") == 4 and s.get("e") == epoch_to_settle and s.get("pe") == (epoch_to_settle + 1):
                    cid = s.get("c", "")
                    if isinstance(cid, str) and cid:
                        try:
                            full, _norm_bytes, _h = await aget_json(cid)
                            full_parsed = safe_json_loads(full)
                            if not isinstance(full_parsed, dict):
                                continue
                            if "as" not in full_parsed or "de" not in full_parsed:
                                continue
                            full_parsed.setdefault("e", s.get("e"))
                            full_parsed.setdefault("pe", s.get("pe"))
                            full_parsed.setdefault("hk", hk_key)
                            full_parsed.setdefault("t", VALIDATOR_TREASURIES.get(hk_key, ""))
                            chosen = dict(full_parsed)
                            if VERBOSE_DUMPS:
                                pretty.kv_panel(
                                    "DBG: Loaded v4 snapshot via IPFS",
                                    [("hk", hk_key[:10] + "…"),
                                     ("cid", cid[:12] + "…"),
                                     ("as", str(full_parsed.get("as"))),
                                     ("de", str(full_parsed.get("de"))),
                                     ("has_inv", str("inv" in full_parsed).lower())],
                                    style="bold cyan",
                                )
                            break
                        except Exception as ipfs_exc:
                            # Dev fallback: if this is our own hk and we still have local pending payload, use it.
                            if hk_key == self.parent.hotkey_ss58:
                                local = self.state.pending_commits.get(str(epoch_to_settle))
                                if isinstance(local, dict) and int(local.get("pe", epoch_to_settle + 1)) == epoch_to_settle + 1:
                                    chosen = dict(local)
                                    chosen.setdefault("e", epoch_to_settle)
                                    chosen.setdefault("pe", epoch_to_settle + 1)
                                    chosen.setdefault("hk", hk_key)
                                    chosen.setdefault("t", VALIDATOR_TREASURIES.get(hk_key, ""))
                                    pretty.log("[magenta]Using local pending payload as fallback for our own v4 commit (IPFS fetch failed).[/magenta]")
                                    break
                            pretty.log(f"[yellow]Skip hk={hk_key[:6]}… — failed to fetch CID: {ipfs_exc}[/yellow]")
                            continue

                # Legacy inline (rare)
                if s.get("e") == epoch_to_settle and s.get("pe") == (epoch_to_settle + 1) and "as" in s and "de" in s:
                    chosen = dict(s)
                    chosen.setdefault("hk", hk_key)
                    chosen.setdefault("t", VALIDATOR_TREASURIES.get(hk_key, ""))
                    if VERBOSE_DUMPS:
                        pretty.kv_panel(
                            "DBG: Using legacy inline snapshot",
                            [("hk", hk_key[:10] + "…"),
                             ("as", str(s.get("as"))),
                             ("de", str(s.get("de"))),
                             ("has_inv", str("inv" in s).lower())],
                            style="bold cyan",
                        )
                    break

            if not chosen:
                continue

            tre = chosen.get("t", "")
            if hk_key not in masters_hotkeys and tre not in masters_treasuries:
                continue

            snapshots.append({"hk": hk_key, **chosen})

        miner_uids_all: List[int] = list(self.parent.get_miner_uids())

        if not snapshots:
            pretty.log("[grey]No master commitments found — applying burn-all weights (no payments to account).[/grey]")
            final_scores = [1.0 if uid == 0 else 0.0 for uid in miner_uids_all]
            self._log_final_scores_table(final_scores, miner_uids_all, reason="no commitments → burn-all")
            self._log_weights_preview(final_scores, miner_uids_all, mode="burn-all (no snapshots)")

            if not TESTING:
                self.parent.update_scores(final_scores, miner_uids_all)
                self.parent.set_weights()
            else:
                pretty.log("[yellow]TESTING: skipping on-chain set_weights().[/yellow]")

            self.state.validated_epochs.add(epoch_to_settle)
            self.state.save_validated_epochs()
            pretty.rule("[bold cyan]DBG: END SETTLEMENT[/bold cyan]")
            return

        # ── 2) Normalize snapshots & union payment window ──
        pretty.rule("[bold cyan]DBG: NORMALIZE INVENTORY & MERGE WINDOWS[/bold cyan]")

        as_vals: List[int] = []
        de_vals: List[int] = []
        clean_snapshots: List[Dict] = []
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
            as_vals.append(a)
            de_vals.append(d)
            clean_snapshots.append(s)

        if not as_vals or not de_vals:
            pretty.log("[yellow]No valid windows extracted from commitments — postponing settlement this epoch.[/yellow]")
            return

        start_block = min(as_vals)
        end_block = max(de_vals)
        pretty.show_settlement_window(
            epoch_to_settle, start_block, end_block, POST_PAYMENT_CHECK_DELAY_BLOCKS, len(clean_snapshots)
        )
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
            return

        # ── 3) Scan events and build paid α by (uid, treasury, subnet) ──
        pretty.rule("[bold cyan]DBG: SCAN α TRANSFERS[/bold cyan]")

        if self._scanner is None:
            self._scanner = AlphaTransfersScanner(await self.parent._stxn(), dest_coldkey=None, rpc_lock=self._rpc_lock)

        try:
            events_raw = await self._scanner.scan(start_block, end_block)
            n_raw = len(events_raw or [])
            pretty.kv_panel("DBG: Scanner returned", [("#events_raw", n_raw), ("range", f"[{start_block},{end_block}]")], style="bold cyan")
            if VERBOSE_DUMPS and n_raw:
                pretty.log("[dim]events_raw sample: " + _j(_head(events_raw, 3)) + (" …" if n_raw > 3 else "") + "[/dim]")
        except Exception as scan_exc:
            pretty.kv_panel("Settlement postponed", [("epoch", epoch_to_settle), ("reason", f"scanner failed: {scan_exc}")], style="bold yellow")
            return

        # Coerce scanner output into a clean List[TransferEvent]
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
                coerced = _coerce(ev)
                if coerced:
                    events.append(coerced)

        pretty.kv_panel("DBG: Events coerced → TransferEvent", [("#events", len(events))], style="bold cyan")
        if VERBOSE_DUMPS and events:
            show = []
            for e in _head(events, 5):
                show.append(
                    {
                        "blk": e.block,
                        "src_ck": (e.src_coldkey[:8] + "…") if e.src_coldkey else None,
                        "dst_ck": (e.dest_coldkey[:8] + "…") if e.dest_coldkey else None,
                        "sid": e.subnet_id,
                        "src_sid": e.src_subnet_id,
                        "amt": e.amount_rao,
                    }
                )
            pretty.log("[dim]" + _j(show) + (" …" if len(events) > 5 else "") + "[/dim]")
        _pause("After scanning & coercing events")

        master_treasuries: Set[str] = {VALIDATOR_TREASURIES.get(s.get("hk", ""), s.get("t", "")) for s in clean_snapshots}
        uids_present: Set[int] = set()
        subnets_needed: Set[int] = set()
        need_oracle = False  # NEW: only fetch prices/depths if some lines lack μTAO

        # Collect UIDs present in payloads & all subnets referenced; detect μTAO presence
        for s in clean_snapshots:
            inv: Dict[str, Dict] = s.get("inv", {}) or {}
            for uid_s, inv_d in inv.items():
                try:
                    uid = int(uid_s)
                except Exception:
                    continue
                uids_present.add(uid)
                for ln in inv_d.get("ln", []) or []:
                    if isinstance(ln, list) and len(ln) >= 4:
                        subnets_needed.add(int(ln[0]))
                        if len(ln) < 5:
                            need_oracle = True  # fallback path required

        pretty.kv_panel(
            "DBG: Payload coverage",
            [("#masters", len(clean_snapshots)),
             ("#uids_present", len(uids_present)),
             ("#subnets_needed", len(subnets_needed)),
             ("need_oracle_fallback", str(need_oracle).lower())],
            style="bold cyan",
        )
        _pause("After inventory normalization overview")

        # Build coldkey→uid map limited to present UIDs
        ck_to_uid: Dict[str, int] = {}
        try:
            for uid, ck in enumerate(self.parent.metagraph.coldkeys):
                if uid in uids_present:
                    ck_to_uid[ck] = uid
        except Exception:
            pass

        # Sum α actually paid per (miner uid, treasury, subnet)
        paid_rao_by_uid_tre_sid: Dict[Tuple[int, str, int], int] = defaultdict(int)
        dropped_cross_subnet = 0
        dropped_forbidden = 0
        dropped_not_master = 0
        dropped_uid_missing = 0

        for ev in (events or []):
            try:
                if ev.dest_coldkey not in master_treasuries:
                    dropped_not_master += 1
                    continue
                if ev.subnet_id in FORBIDDEN_ALPHA_SUBNETS:
                    dropped_forbidden += 1
                    continue
                uid = ck_to_uid.get(ev.src_coldkey)
                if uid is None or uid not in uids_present:
                    dropped_uid_missing += 1
                    continue
                src_sid = ev.src_subnet_id if getattr(ev, "src_subnet_id", None) is not None else ev.subnet_id
                if src_sid != ev.subnet_id:
                    dropped_cross_subnet += 1
                    continue
                paid_rao_by_uid_tre_sid[(uid, ev.dest_coldkey, ev.subnet_id)] += int(ev.amount_rao)
            except Exception:
                # ignore malformed event
                continue

        pretty.kv_panel(
            "DBG: Paid α aggregation",
            [
                ("#paid_keys", len(paid_rao_by_uid_tre_sid)),
                ("dropped_not_master", dropped_not_master),
                ("dropped_forbidden", dropped_forbidden),
                ("dropped_uid_missing", dropped_uid_missing),
                ("dropped_cross_subnet", dropped_cross_subnet),
            ],
            style="bold cyan",
        )
        if VERBOSE_DUMPS and paid_rao_by_uid_tre_sid:
            sample = []
            for i, (k, v) in enumerate(paid_rao_by_uid_tre_sid.items()):
                if i >= 6:
                    break
                uid, tre, sid = k
                sample.append({"uid": uid, "tre": tre[:10] + "…", "sid": sid, "paid_rao": v})
            pretty.log("[dim]paid sample: " + _j(sample) + (" …" if len(paid_rao_by_uid_tre_sid) > 6 else "") + "[/dim]")
        _pause("After paid α aggregation")

        # ── 4) Price & depth caches (only if needed) ──
        price_cache: Dict[int, float] = {}
        depth_cache: Dict[int, int] = {}
        if need_oracle:
            pretty.rule("[bold cyan]DBG: ORACLE — PRICES & DEPTHS (fallback only)[/bold cyan]")
            await self._populate_price_and_depth_caches(subnets_needed, start_block, end_block, price_cache, depth_cache)
            pretty.kv_panel(
                "DBG: Oracle snapshots",
                [("#prices", len(price_cache)), ("#depths", len(depth_cache)),
                 ("prices(sample)", _j(_kv_preview(price_cache, 5))),
                 ("depths(sample)", _j(_kv_preview(depth_cache, 5)))],
                style="bold cyan",
            )
            _pause("After oracle fill (fallback)")

        # ── 5) Score miners using stored line values ──
        pretty.rule("[bold cyan]DBG: SCORING (use stored line values)[/bold cyan]")

        scores_by_uid: Dict[int, float] = defaultdict(float)
        burn_total_value = 0.0  # track burned value if subnets unpaid

        # Per-subnet outcome table (who got credit or not)
        per_subnet_credit_rows: List[List[str | int | float]] = []

        for s in clean_snapshots:
            tre: str = s.get("t", "") or ""
            inv: Dict[str, Dict] = s.get("inv", {}) or {}

            for uid_s, inv_d in inv.items():
                try:
                    uid = int(uid_s)
                except Exception:
                    continue

                # Group lines by subnet:
                # normalized line shape now allows optional 5th element μTAO
                # [sid, disc_bps, weight_bps, required_rao, value_mu?]
                lines_by_sid: Dict[int, List[List[int]]] = defaultdict(list)
                for ln in inv_d.get("ln", []) or []:
                    if not (isinstance(ln, list) and len(ln) >= 4):
                        continue
                    sid2 = int(ln[0]); disc_bps = int(ln[1]); w_bps = int(ln[2]); req_rao = int(ln[3])
                    if req_rao > 0:
                        # keep entire list to preserve optional μTAO (index 4)
                        lines_by_sid[sid2].append([sid2, disc_bps, w_bps, req_rao] + ([int(ln[4])] if len(ln) >= 5 else []))

                for sid, lines in lines_by_sid.items():
                    required_rao_total = sum(int(ln[3]) for ln in lines)
                    if required_rao_total <= 0:
                        continue

                    paid = int(paid_rao_by_uid_tre_sid.get((uid, tre, sid), 0))
                    got_credit = paid >= required_rao_total
                    per_subnet_credit_rows.append([uid, tre[:8] + "…", sid, required_rao_total, paid, "OK" if got_credit else "NO"])

                    # Walk each line; use stored μTAO if present; fallback compute if needed.
                    for ln in lines:
                        sid2, disc_bps, w_bps, req_rao = int(ln[0]), int(ln[1]), int(ln[2]), int(ln[3])
                        val_mu = int(ln[4]) if len(ln) >= 5 else 0

                        if val_mu <= 0 and need_oracle:
                            # Fallback: compute TAO value for this line using oracle and μTAO scaling.
                            price = float(price_cache.get(sid2, 0.0))
                            depth = int(depth_cache.get(sid2, 0))
                            # effective_value_tao applies weight & discount already
                            fallback_val = effective_value_tao(req_rao, w_bps, disc_bps, price, depth)
                            val_mu = int(round(fallback_val * 1_000_000.0))

                        val_tao = decode_value_mu(val_mu) if val_mu > 0 else 0.0
                        if got_credit:
                            scores_by_uid[uid] += val_tao
                        else:
                            burn_total_value += val_tao

        # Show subnet credit summary table
        if per_subnet_credit_rows:
            pretty.table(
                "[cyan]DBG: Subnet credit (per miner/subnet)[/cyan]",
                ["UID", "Treasury", "Subnet", "α_required(rao)", "α_paid(rao)", "Credit?"],
                per_subnet_credit_rows[: max(10, int(LOG_TOP_N))],
            )
            if len(per_subnet_credit_rows) > max(10, int(LOG_TOP_N)):
                pretty.log(f"[dim]… +{len(per_subnet_credit_rows) - max(10, int(LOG_TOP_N))} more rows[/dim]")

        # ── 6) Produce final scores & set weights (or TESTING preview) ──
        miner_uids_all = list(self.parent.get_miner_uids())
        final_scores: List[float] = [float(scores_by_uid.get(uid, 0.0)) for uid in miner_uids_all]

        burn_reason = None
        burn_all = FORCE_BURN_WEIGHTS or (not any(final_scores))
        if FORCE_BURN_WEIGHTS:
            burn_reason = "FORCE_BURN_WEIGHTS"
        elif not any(final_scores):
            burn_reason = "NO_POSITIVE_SCORES"

        self._log_final_scores_table(final_scores, miner_uids_all, reason=burn_reason)
        self._log_weights_preview(final_scores, miner_uids_all, mode="burn-all" if burn_all else "normal")
        _pause("Before applying weights (or preview in TESTING)")

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
                             ("mode", "burn-all" if burn_all else "normal")],
                            style="bold magenta")

        self.state.validated_epochs.add(epoch_to_settle)
        self.state.save_validated_epochs()
        pretty.kv_panel("Settlement Complete",
                        [("epoch_settled (e−2)", epoch_to_settle),
                         ("miners_scored", sum(1 for x in final_scores if x > 0)),
                         ("snapshots_used", len(clean_snapshots)),
                         ("testing", str(TESTING).lower())],
                        style="bold green")
        pretty.rule("[bold cyan]DBG: END SETTLEMENT[/bold cyan]")

    # ---------- helpers ----------

    def _decode_commit_entry(self, entry):
        if entry is None:
            return None
        if isinstance(entry, dict):
            raw = entry.get("raw")
            parsed = safe_json_loads(raw) if isinstance(raw, (str, bytes, bytearray, dict, list)) else None
            if parsed is not None:
                return parsed
            return entry
        if isinstance(entry, (str, bytes, bytearray)):
            parsed = safe_json_loads(entry)
            if parsed is not None:
                return parsed
            return None
        return None

    def _normalize_inv(self, s: Dict) -> Optional[Dict[str, Dict]]:
        """
        Normalize inventory shape to:
          inv = {
            "<uid>": { "ck": "<coldkey>", "ln": [[sid, disc_bps, weight_bps, required_rao, value_mu?], ...] },
            ...
          }
        Accepts legacy `i: [[uid, [[sid, weight_bps, required_rao, disc_bps?, value_mu?], ...]], ...]`.
        """
        inv: Dict[str, Dict] | None = s.get("inv")  # type: ignore[assignment]
        if isinstance(inv, dict) and inv:
            if VERBOSE_DUMPS:
                sample_uid = next(iter(inv.keys()), None)
                pretty.kv_panel("DBG: inv pre-normalized (dict)",
                                [("uids", len(inv)), ("sample_uid", str(sample_uid))],
                                style="bold cyan")
            return inv

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
                ck = ""
                try:
                    if 0 <= uid_int < len(self.parent.metagraph.coldkeys):
                        ck = self.parent.metagraph.coldkeys[uid_int]
                except Exception:
                    ck = ""
                ln_list: List[List[int]] = []
                for ln in (lines or []):
                    if not (isinstance(ln, list) and len(ln) >= 3):
                        continue
                    try:
                        sid = int(ln[0])
                        w_bps = int(ln[1])
                        rao = int(ln[2])
                        disc = int(ln[3]) if len(ln) >= 4 else 0
                        val_mu = int(ln[4]) if len(ln) >= 5 else 0
                    except Exception:
                        continue
                    # normalized line: [sid, disc_bps, weight_bps, required_rao, value_mu?]
                    if val_mu:
                        ln_list.append([sid, disc, w_bps, rao, val_mu])
                    else:
                        ln_list.append([sid, disc, w_bps, rao])
                inv_tmp[str(uid_int)] = {"ck": ck, "ln": ln_list}

        if VERBOSE_DUMPS and inv_tmp:
            any_uid = next(iter(inv_tmp.keys()), None)
            ln_samp = inv_tmp.get(any_uid, {}).get("ln", [])[:3]
            pretty.kv_panel("DBG: inv normalized (legacy→dict)",
                            [("#uids", len(inv_tmp)), ("sample_uid", str(any_uid)), ("sample_lines", _j(ln_samp))],
                            style="bold cyan")
        return inv_tmp if inv_tmp else None

    async def _populate_price_and_depth_caches(
        self,
        subnets_needed: Set[int],
        start: int,
        end: int,
        price_cache_out: Dict[int, float],
        depth_cache_out: Dict[int, int],
    ) -> None:
        from metahash.utils.subnet_utils import average_price, average_depth

        async def _fetch_price(sid: int) -> Tuple[int, float]:
            p = await average_price(sid, start_block=start, end_block=end, st=await self.parent._stxn())
            return sid, float(getattr(p, "tao", 0.0) or 0.0)

        async def _fetch_depth(sid: int) -> Tuple[int, int]:
            try:
                d = await average_depth(sid, start_block=start, end_block=end, st=await self.parent._stxn())
                return sid, int(d or 0)
            except Exception:
                return sid, 0

        if not subnets_needed:
            return

        price_pairs, depth_pairs = await asyncio.gather(
            asyncio.gather(*(_fetch_price(s) for s in sorted(subnets_needed))),
            asyncio.gather(*(_fetch_depth(s) for s in sorted(subnets_needed))),
        )
        price_cache_out.update(dict(price_pairs))
        depth_cache_out.update(dict(depth_pairs))

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
        # FIX: use f-string, not f(…)
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
