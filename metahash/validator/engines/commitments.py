# neurons/commitments.py
from __future__ import annotations

import json
from typing import Dict

from metahash.utils.ipfs import aadd_json, minidumps as ipfs_minidumps, IPFSError
from metahash.utils.pretty_logs import pretty
from metahash.utils.commitments import write_plain_commitment_json
from treasuries import VALIDATOR_TREASURIES

from metahash.validator.flags import ALLOW_INLINE_FALLBACK, RAW_BYTES_CEILING
from metahash.validator.state import StateStore


def _minidumps(obj: dict) -> str:
    # Minimal JSON, deterministic key order is *not* required here.
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class CommitmentsEngine:
    """
    Handles commitment publication:
      - Primary path: v4 (CID-only on-chain, heavy JSON to IPFS)
      - Fallback: v3 inline (size-capped), with safe trimming
    """

    def __init__(self, parent, state: StateStore):
        self.parent = parent
        self.state = state

    async def publish_commitment_for(self, epoch_cleared: int):
        """
        Publish winners payload for epoch e-1 using stored window.
        Reads payload from state.pending_commits[str(epoch_cleared)].
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

        # Ensure metadata present in the heavy payload
        pending.setdefault("hk", self.parent.hotkey_ss58)
        pending.setdefault("t", VALIDATOR_TREASURIES.get(self.parent.hotkey_ss58, ""))

        payload = dict(pending)
        payload.setdefault("pe", epoch_cleared + 1)
        if "as" not in payload or "de" not in payload:
            epoch_len = int(self.parent.epoch_end_block - self.parent.epoch_start_block + 1)
            payload["as"] = int(self.parent.epoch_end_block) + 1
            payload["de"] = int(self.parent.epoch_end_block + epoch_len)

        st = await self.parent._stxn()
        try:
            # Upload full JSON to IPFS
            cid, sha_hex, byte_len = await aadd_json(payload, filename=f"commit_e{epoch_cleared}.json", pin=True, sort_keys=True)

            # Compose the tiny on-chain commitment (CID‑only)
            commit_v4 = {
                "v": 4,
                "e": int(payload.get("e", epoch_cleared)),
                "pe": int(payload.get("pe")),
                "c": str(cid),
            }
            commit_str = ipfs_minidumps(commit_v4, sort_keys=True)
            bytes_commit = len(commit_str.encode("utf-8"))
            if bytes_commit > RAW_BYTES_CEILING:
                # Should never happen with CID-only, but remain defensive.
                raise ValueError(f"v4 CID-only commit unexpectedly too large ({bytes_commit}>{RAW_BYTES_CEILING})")

            ok = await write_plain_commitment_json(st, wallet=self.parent.wallet, data=commit_str, netuid=self.parent.config.netuid)
            if ok:
                pretty.kv_panel(
                    "Commitment Published (v4 via IPFS, CID‑only on‑chain)",
                    [
                        ("epoch_cleared", epoch_cleared),
                        ("payment_epoch (pe)", payload.get("pe")),
                        ("window@ipfs", f"[{payload.get('as')}, {payload.get('de')}]"),
                        ("cid", str(cid)[:18] + "…"),
                        ("json_bytes@ipfs", byte_len),
                        ("onchain_bytes", bytes_commit),
                        ("sha256", sha_hex[:16] + "…"),
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
            pretty.log(f"[yellow]IPFS publish failed; attempting inline fallback: {ie}[/yellow]")
        except Exception as e:
            pretty.log(f"[yellow]Commit via IPFS failed; attempting inline fallback: {e}[/yellow]")

        # If configured, try inline fallback (v3). Otherwise keep pending for retry.
        if not ALLOW_INLINE_FALLBACK:
            pretty.log("[yellow]Inline fallback disabled by config; will retry publishing next epoch.[/yellow]")
            return

        payload_str = _minidumps(payload)

        def bytes_len(s: str) -> int:
            return len(s.encode("utf-8"))

        # Trim low-value lines first
        if bytes_len(payload_str) > RAW_BYTES_CEILING:
            i_list = list(payload.get("i", []))
            scored = []
            from metahash.config import PLANCK  # local import to avoid cycles
            for j, (uid, lines) in enumerate(i_list):
                for k, ln in enumerate(lines):
                    if not (isinstance(ln, list) and len(ln) >= 3):
                        continue
                    try:
                        sid = int(ln[0])
                        w_bps = int(ln[1])
                        rao = int(ln[2])
                        disc = int(ln[3]) if len(ln) >= 4 else 0
                    except Exception:
                        continue
                    score = (w_bps / 10_000.0) * (1.0 - disc / 10_000.0) * (rao / max(1, PLANCK))
                    scored.append((j, k, score))
            scored.sort(key=lambda x: x[2])
            trimmed = 0
            idx = 0
            while bytes_len(payload_str) > RAW_BYTES_CEILING and idx < len(scored):
                j, k, _ = scored[idx]
                idx += 1
                if 0 <= j < len(i_list) and 0 <= k < len(i_list[j][1]):
                    try:
                        del i_list[j][1][k]
                        if not i_list[j][1]:
                            del i_list[j]
                        payload = dict(payload, i=i_list)
                        payload_str = _minidumps(payload)
                        trimmed += 1
                    except Exception:
                        break
            pretty.kv_panel(
                "Commitment payload trimmed (fallback)",
                [("lines_trimmed", trimmed), ("bytes_final", bytes_len(payload_str)), ("limit", RAW_BYTES_CEILING)],
                style="bold yellow"
            )

        # Final guard: never submit if still too large
        if bytes_len(payload_str) > RAW_BYTES_CEILING:
            pretty.log(f"[yellow]Fallback inline payload still {bytes_len(payload_str)}>{RAW_BYTES_CEILING} bytes; keeping pending and retrying next epoch.[/yellow]")
            return

        try:
            ok = await write_plain_commitment_json(await self.parent._stxn(), wallet=self.parent.wallet, data=payload_str, netuid=self.parent.config.netuid)
            if ok:
                pretty.kv_panel(
                    "Commitment Published (fallback v3 inline)",
                    [
                        ("epoch_cleared", epoch_cleared),
                        ("payment_epoch (pe)", payload.get("pe")),
                        ("window", f"[{payload.get('as')}, {payload.get('de')}]"),
                        ("payload_bytes", len(payload_str.encode("utf-8"))),
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
