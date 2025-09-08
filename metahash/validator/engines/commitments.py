# neurons/commitments.py
from __future__ import annotations

import json
from typing import Dict

from metahash.utils.ipfs import aadd_json, minidumps as ipfs_minidumps, IPFSError
from metahash.utils.pretty_logs import pretty
from metahash.utils.commitments import write_plain_commitment_json
from treasuries import VALIDATOR_TREASURIES

from metahash.config import ALLOW_INLINE_FALLBACK, RAW_BYTES_CEILING
from metahash.validator.state import StateStore


def _minidumps(obj: dict) -> str:
    # Minimal JSON, deterministic key order is *not* required here.
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class CommitmentsEngine:
    """
    Handles commitment publication:

      Primary (v4):
        - Store the full, rich winners payload in IPFS (no size limit enforced here).
        - Commit only a tiny CID-only object on-chain: {"v":4,"e":...,"pe":...,"c":<cid>}

      Fallback (v3, only if explicitly allowed and IPFS failed):
        - Publish a *minimal* inline payload on-chain, trimmed to fit RAW_BYTES_CEILING.
        - This is the only place where we care about byte size constraints.
    """

    def __init__(self, parent, state: StateStore):
        self.parent = parent
        self.state = state

    async def publish_commitment_for(self, epoch_cleared: int):
        """
        Publish winners payload for epoch e-1 using stored window.
        Reads payload from state.pending_commits[str(epoch_cleared)].

        Notes:
          - IPFS payload can be arbitrarily large; we do NOT trim for IPFS.
          - Only on-chain data must respect RAW_BYTES_CEILING.
        """
        if epoch_cleared < 0:
            return
        if not self._is_master_now():
            return

        key = str(epoch_cleared)
        pending = self.state.pending_commits.get(key)
        if not isinstance(pending, dict):
            pretty.log(f"[grey]No pending winners to publish for epoch {epoch_cleared}.[/grey]")
            return

        # Ensure minimal metadata present in the heavy payload (IPFS copy).
        pending.setdefault("hk", self.parent.hotkey_ss58)
        pending.setdefault("t", VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, ""))

        payload = dict(pending)
        payload.setdefault("pe", epoch_cleared + 1)

        # If window bounds absent, synthesize from the parent's epoch block info.
        if "as" not in payload or "de" not in payload:
            epoch_len = int(self.parent.epoch_end_block - self.parent.epoch_start_block + 1)
            payload["as"] = int(self.parent.epoch_end_block) + 1
            payload["de"] = int(self.parent.epoch_end_block + epoch_len)

        st = await self.parent._stxn()

        # --------------------------
        # Preferred path (v4 + IPFS)
        # --------------------------
        try:
            # Upload the full winners payload to IPFS. No size checks here.
            cid, sha_hex, byte_len = await aadd_json(
                payload,
                filename=f"commit_e{epoch_cleared}.json",
                pin=True,
                sort_keys=True,
            )

            # Compose tiny on-chain commitment: CID-only
            commit_v4 = {
                "v": 4,
                "e": int(payload.get("e", epoch_cleared)),
                "pe": int(payload.get("pe")),
                "c": str(cid),
            }
            commit_str = ipfs_minidumps(commit_v4, sort_keys=True)
            onchain_bytes = len(commit_str.encode("utf-8"))

            # Defensive guard (should always be tiny)
            if onchain_bytes > RAW_BYTES_CEILING:
                raise ValueError(
                    f"CID-only commit unexpectedly too large ({onchain_bytes}>{RAW_BYTES_CEILING})"
                )

            ok = await write_plain_commitment_json(
                st, wallet=self.parent.wallet, data=commit_str, netuid=self.parent.config.netuid
            )
            if ok:
                pretty.kv_panel(
                    "Commitment Published (v4 via IPFS, CID-only on-chain)",
                    [
                        ("epoch_cleared", epoch_cleared),
                        ("payment_epoch (pe)", payload.get("pe")),
                        ("window@ipfs", f"[{payload.get('as')}, {payload.get('de')}]"),
                        ("cid", str(cid)),
                        ("json_bytes@ipfs", byte_len),
                        ("onchain_bytes", onchain_bytes),
                        ("sha256", sha_hex),
                    ],
                    style="bold green",
                )
                self.state.pending_commits.pop(key, None)
                self.state.save_pending_commits()
                return
            else:
                pretty.log("[yellow]Commitment publish returned False (will retry next epoch).[/yellow]")
                return

        except IPFSError as ie:
            pretty.log(f"[yellow]IPFS publish failed; considering inline fallback (v3): {ie}[/yellow]")
        except Exception as e:
            pretty.log(f"[yellow]Commit via IPFS failed; considering inline fallback (v3): {e}[/yellow]")

        # ----------------------------------------------------
        # Fallback path (v3 inline) — ONLY size-limited piece
        # ----------------------------------------------------
        if not ALLOW_INLINE_FALLBACK:
            pretty.log("[yellow]Inline fallback disabled by config; will retry publishing next epoch.[/yellow]")
            return

        # Build a MINIMAL inline payload. We avoid including large metadata.
        # Keep just what downstream settlement needs to verify the window and winners.
        # Structure (example):
        #   {
        #     "v":3, "e":..., "pe":..., "as":..., "de":...,
        #     "i":[ [uid, [[sid, w_bps, rao, disc], ...]], ... ]
        #   }
        minimal_inline = {
            "v": 3,
            "e": int(payload.get("e", epoch_cleared)),
            "pe": int(payload.get("pe")),
            "as": int(payload.get("as")),
            "de": int(payload.get("de")),
        }

        # Copy winners list compactly if present.
        i_list = []
        src_i = payload.get("i", [])
        if isinstance(src_i, list):
            # Ensure only compact numeric entries make it through.
            for tup in src_i:
                try:
                    uid, lines = tup
                    compact_lines = []
                    if isinstance(lines, list):
                        for ln in lines:
                            # Expected [subnet_id, weight_bps, rao, [discount_bps?]]
                            if not (isinstance(ln, list) and len(ln) >= 3):
                                continue
                            sid = int(ln[0])
                            w_bps = int(ln[1])
                            rao = int(ln[2])
                            if len(ln) >= 4:
                                disc = int(ln[3])
                                compact_lines.append([sid, w_bps, rao, disc])
                            else:
                                compact_lines.append([sid, w_bps, rao])
                        if compact_lines:
                            i_list.append([int(uid), compact_lines])
                except Exception:
                    continue

        minimal_inline["i"] = i_list

        inline_str = _minidumps(minimal_inline)

        def blen(s: str) -> int:
            return len(s.encode("utf-8"))

        # If too large, trim *only* the inline winners list by removing lowest-value lines.
        if blen(inline_str) > RAW_BYTES_CEILING and i_list:
            from metahash.config import PLANCK  # local import to avoid cycles
            scored: list[tuple[int, int, float]] = []
            for j, (_uid, lines) in enumerate(i_list):
                for k, ln in enumerate(lines):
                    try:
                        sid = int(ln[0])
                        w_bps = int(ln[1])
                        rao = int(ln[2])
                        disc = int(ln[3]) if len(ln) >= 4 else 0
                    except Exception:
                        continue
                    # Heuristic value proxy used only for trimming:
                    value = (w_bps / 10_000.0) * (1.0 - disc / 10_000.0) * (rao / max(1, PLANCK))
                    scored.append((j, k, value))

            scored.sort(key=lambda x: x[2])  # drop smallest first

            trimmed = 0
            idx = 0
            # Work on a mutable copy
            mut_i = [[uid, list(lines)] for uid, lines in i_list]

            while blen(inline_str) > RAW_BYTES_CEILING and idx < len(scored):
                j, k, _ = scored[idx]
                idx += 1
                if 0 <= j < len(mut_i) and 0 <= k < len(mut_i[j][1]):
                    try:
                        del mut_i[j][1][k]
                        if not mut_i[j][1]:
                            del mut_i[j]
                        minimal_inline["i"] = mut_i
                        inline_str = _minidumps(minimal_inline)
                        trimmed += 1
                    except Exception:
                        break

            pretty.kv_panel(
                "Inline fallback trimmed to fit on-chain",
                [("lines_trimmed", trimmed), ("bytes_final", blen(inline_str)), ("limit", RAW_BYTES_CEILING)],
                style="bold yellow",
            )

        # Final guard: if still too large, keep pending and retry next epoch.
        if blen(inline_str) > RAW_BYTES_CEILING:
            pretty.log(
                f"[yellow]Inline payload still {blen(inline_str)}>{RAW_BYTES_CEILING} bytes; keeping pending and retrying next epoch.[/yellow]"
            )
            return

        # Publish the trimmed/minimal inline payload on-chain.
        try:
            ok = await write_plain_commitment_json(
                await self.parent._stxn(),
                wallet=self.parent.wallet,
                data=inline_str,
                netuid=self.parent.config.netuid,
            )
            if ok:
                pretty.kv_panel(
                    "Commitment Published (fallback v3 inline, size-capped)",
                    [
                        ("epoch_cleared", epoch_cleared),
                        ("payment_epoch (pe)", minimal_inline.get("pe")),
                        ("window", f"[{minimal_inline.get('as')}, {minimal_inline.get('de')}]"),
                        ("onchain_bytes", blen(inline_str)),
                    ],
                    style="bold green",
                )
                self.state.pending_commits.pop(key, None)
                self.state.save_pending_commits()
            else:
                pretty.log("[yellow]Commitment publish returned False (will retry next epoch).[/yellow]")
        except Exception as e:
            pretty.log(f"[yellow]Commitment publish failed (will retry next epoch): {e}[/yellow]")

    # ---------- utils ----------
    def _is_master_now(self) -> bool:
        tre = VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58)
        if not tre:
            return False
        uid = self._hotkey_to_uid().get(self.parent.hotkey_ss58)
        if uid is None:
            return False
        from metahash.config import S_MIN_MASTER_VALIDATOR
        try:
            return float(self.parent.metagraph.stake[uid]) >= S_MIN_MASTER_VALIDATOR
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
