# ====================================================================== #
# MetaHash helpers – shared by miner & auction_loop                      #
# Patched – 2025‑06‑26:                                                   #
#   * cold‑keys file now keyed by miner hot‑key                           #
#   * load_verified_coldkeys() returns *mapping* hot‑key → list[dict]     #
# ====================================================================== #
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import bittensor as bt
import yaml
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.utils.wallet_utils import check_coldkeys_and_signatures

# ─────────────────────────── NEW: atomic file‑locking ────────────────────
try:
    from filelock import FileLock  # pip install filelock
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "'filelock' is required for MetaHash miner v2025‑06‑24. "
        "Please run:  pip install filelock"
    ) from e
# ------------------------------------------------------------------------ #

# ------------------------------------------------------------------ #
#  Constants & paths                                                 #
# ------------------------------------------------------------------ #
TEMPO_DEFAULT = 360
ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(__file__).resolve().parents[2] / "auction_cfg"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------------ #
#  Dataclasses                                                       #
# ------------------------------------------------------------------ #


@dataclass(slots=True)
class Profile:
    subnet: int
    wallet: bt.wallet
    max_discount: float
    step_tao: float
    cooldown_blocks: int
    quota_percent: Optional[float] = None
    quota_tao: Optional[float] = None
    reserve_tao: float = 0.0
    min_wallet_tao: float = 0.0
    #  epoch‑local
    quota: Optional[float] = None
    sent: float = 0.0
    last_block: int = -10**9

# ------------------------------------------------------------------ #
#  YAML helpers                                                      #
# ------------------------------------------------------------------ #


def load_profiles(yml: str | Path = "miner.yml") -> List[Profile]:
    """Reads spending profiles used by the auction loop."""
    yml = Path(yml)
    if not yml.is_absolute():
        yml = ROOT.parent / yml
    if not yml.exists():
        clog.error(f"❌  {yml} not found", color="red")
        raise SystemExit(1)

    raw = yaml.safe_load(yml.read_text()) or {}
    blocks = raw.get("profiles", raw)
    if not isinstance(blocks, list) or not blocks:
        clog.error(f"❌  {yml} must contain a non‑empty 'profiles' list",
                   color="red")
        raise SystemExit(1)

    out: List[Profile] = []
    for idx, blk in enumerate(blocks, 1):
        try:
            out.append(
                Profile(
                    subnet=int(blk["subnet"]),
                    wallet=bt.wallet(name=str(blk["wallet"])),
                    max_discount=float(blk["max_discount"]),
                    step_tao=float(blk.get("step_tao", 1.0)),
                    cooldown_blocks=int(blk.get("cooldown_blocks", 100)),
                    quota_percent=float(blk.get("quota_percent", blk.get("percent")))
                    if blk.get("quota_percent", blk.get("percent")) is not None
                    else None,
                    quota_tao=float(blk.get("quota_tao"))
                    if blk.get("quota_tao") is not None
                    else None,
                    reserve_tao=float(blk.get("reserve_tao", 0.0)),
                    min_wallet_tao=float(blk.get("min_wallet_tao", 0.0)),
                )
            )
        except Exception as e:
            clog.error(f"Bad profile #{idx} in {yml}: {e}", color="red")
            raise SystemExit(1)
    return out


def load_verified_coldkeys(file: str | Path = "miner.yml") -> Dict[str, List[Dict]]:
    """
    Devuelve un mapeo  HOTKEY_SS58 → list[{address, signature}]
    y *verifica* cada firma contra **ese hot‑key**.
    """
    file = Path(file)
    if not file.is_absolute():
        file = ROOT.parent.parent / file
    if not file.exists():
        clog.error("❌  cold‑keys file missing", color="red")
        raise SystemExit(1)

    data = yaml.safe_load(file.read_text()) or {}
    data = data.get("coldkeys", data) if isinstance(data, dict) else data
    if not isinstance(data, dict):
        clog.error("❌  'coldkeys' must be a mapping keyed by miner hot‑key",
                   color="red")
        raise SystemExit(1)

    out: Dict[str, List[Dict]] = {}
    for hotkey, lst in data.items():
        if not isinstance(lst, list):
            clog.error(f"❌  coldkeys[{hotkey}] must be a YAML list", color="red")
            raise SystemExit(1)

        # ⬇️  **Nuevo**: verificar contra el *hot‑key* correspondiente
        out[hotkey] = check_coldkeys_and_signatures(lst, message=hotkey)

    return out

# ------------------------------------------------------------------ #
#  Auction‑config persistence utilities (unchanged)                  #
# ------------------------------------------------------------------ #


def compute_epoch(block: int, tempo: int = TEMPO_DEFAULT) -> int:
    return block // tempo


def write_validator_config(
    epoch: int,
    validator_id: str,
    payload: Dict,
    *,
    miner_hotkey: str = "default",
) -> None:
    fname = CONFIG_DIR / f"{miner_hotkey[:12]}_epoch_{epoch}.json"
    lock = FileLock(str(fname) + ".lock")

    with lock:
        obj: Dict[str, Dict] = {}
        if fname.exists():
            try:
                obj = json.loads(fname.read_text())
            except Exception:
                obj = {}

        obj[validator_id] = payload
        fname.write_text(json.dumps(obj, indent=2))
