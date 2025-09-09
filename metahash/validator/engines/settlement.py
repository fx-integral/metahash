# metahash/validator/engines/settlement.py — CID/IPFS smoke test only
from __future__ import annotations

import json
from typing import Dict, List, Optional, Set

from metahash.utils.pretty_logs import pretty
from metahash.utils.ipfs import aget_json
from metahash.utils.commitments import read_all_plain_commitments
from metahash.treasuries import VALIDATOR_TREASURIES
from metahash.validator.state import StateStore
from metahash.utils.helpers import safe_json_loads


def _j(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except Exception:
        try:
            return str(obj)
        except Exception:
            return "<unprintable>"


class SettlementEngine:
    """
    TEMP (debug build):
      • Only reads v4 commitments (CID) from chain,
      • Fetches full payload from IPFS,
      • Prints out the raw payload,
      • Pauses with input() so you can copy logs.

    Everything else is intentionally disabled/commented out for step-by-step verification.
    """

    def __init__(self, parent, state: StateStore):
        self.parent = parent
        self.state = state
        # Keep a reference to the validator's RPC lock for future use (not used here).
        self._rpc_lock = parent._rpc_lock  # noqa: F401

    async def settle_and_set_weights_all_masters(self, epoch_to_settle: int):
        """
        DEBUG STEP ONLY:
          1) Read commitments map from chain.
          2) For each master validator entry, pick v4 item for (e == epoch_to_settle, pe == e+1).
          3) Fetch payload by CID from IPFS.
          4) Print payload and pause.
        """
        pretty.rule("[bold cyan]SETTLEMENT (DEBUG) — CID/IPFS payload fetch[/bold cyan]")

        # 1) Load the on-chain commitments map
        st = await self.parent._stxn()
        try:
            commits = await read_all_plain_commitments(st, netuid=self.parent.config.netuid, block=None)
        except Exception as e:
            pretty.log(f"[red]Commitment read_all failed: {e}[/red]")
            return

        if not commits:
            pretty.log("[yellow]No commitments found on chain.[/yellow]")
            return

        masters_hotkeys: Set[str] = set(VALIDATOR_TREASURIES.keys())
        pretty.kv_panel("Commitments fetched", [("#hotkeys", len(commits or {}))], style="bold cyan")

        # Helper: decode one entry possibly wrapped in dict/list/inline
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

        found_any = False

        # 2) Iterate all hotkeys → pick v4 CID for requested epoch
        for hk_key, data_raw in (commits or {}).items():
            # Only care about known master validators
            if hk_key not in masters_hotkeys:
                continue

            data_decoded = _decode_commit_entry(data_raw)
            candidates: List[Dict] = []

            if isinstance(data_decoded, dict) and isinstance(data_decoded.get("sn"), list):
                # Some validators push an array of snapshots under 'sn'
                for s in data_decoded["sn"]:
                    s_dec = _decode_commit_entry(s)
                    if isinstance(s_dec, dict):
                        candidates.append(s_dec)
            elif isinstance(data_decoded, dict):
                # Single snapshot
                candidates = [data_decoded]
            else:
                continue

            # Prefer v4 entries with matching (e, pe)
            chosen_cid: Optional[str] = None
            chosen_meta: Optional[Dict] = None
            for s in candidates:
                if s.get("v") == 4 and s.get("e") == epoch_to_settle and s.get("pe") == (epoch_to_settle + 1):
                    cid = s.get("c", "")
                    if isinstance(cid, str) and cid:
                        chosen_cid = cid
                        chosen_meta = s
                        break

            if not chosen_cid:
                continue

            # 3) Fetch payload from IPFS
            pretty.kv_panel(
                "Fetching IPFS payload",
                [("hotkey", hk_key[:10] + "…"), ("cid", chosen_cid[:46] + ("…" if len(chosen_cid) > 46 else "")),
                 ("e", chosen_meta.get("e")), ("pe", chosen_meta.get("pe"))],
                style="bold cyan",
            )

            try:
                raw_json, _norm_bytes, _h = await aget_json(chosen_cid)
                payload = safe_json_loads(raw_json)
            except Exception as ipfs_exc:
                pretty.log(f"[yellow]Failed to fetch CID for hk={hk_key[:8]}…: {ipfs_exc}[/yellow]")
                continue

            if not isinstance(payload, dict):
                pretty.log(f"[yellow]CID fetched, but payload is not a dict (type={type(payload).__name__}).[/yellow]")
                continue

            found_any = True

            # 4) Print payload; pause for manual inspection
            pretty.rule("[bold magenta]RAW PAYLOAD (BEGIN)[/bold magenta]")
            pretty.log(_j(payload))
            pretty.rule("[bold magenta]RAW PAYLOAD (END)[/bold magenta]")

            try:
                input("\n[PAUSE] Payload printed. Press <enter> to continue to next (or Ctrl+C to stop)… ")
            except KeyboardInterrupt:
                pretty.log("[grey]Interrupted by user.[/grey]")
                return
            except Exception:
                # If running headless, just continue.
                pass

        if not found_any:
            pretty.log("[yellow]No matching v4 CIDs for the requested epoch were found among masters.[/yellow]")

        pretty.rule("[bold cyan]SETTLEMENT (DEBUG) — DONE[/bold cyan]")
    