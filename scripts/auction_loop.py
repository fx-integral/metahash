# neurons/auction_loop.py
# ======================================================================
"""
Incremental α‑TAO auction loop for subnet‑73 miners.

* Reads miner strategy from ~/.config/metahash/miner.yml
* Tracks live `m_eaten` by scanning only yet‑unseen blocks.
* Computes exact discount using the validator’s bond‑curve params.
* Throttles RPC & keeps heavy state in memory‑only caches.
"""
from __future__ import annotations

import asyncio
import time
import yaml
import json
from dataclasses import dataclass
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import bittensor as bt
from bittensor.utils.balance import Balance
from metahash.base.utils.logging import ColoredLogger as clog
from metahash.utils.bond_utils import discount_for_deposit
from metahash.utils.wallet_utils import transfer_alpha
from metahash.validator.alpha_transfers import AlphaTransfersScanner
from metahash.bittensor_config import config

# ----------------------- numeric precision ------------------------
getcontext().prec = 60
PLANCK_DEC = Decimal(10) ** 18    # 1 α  = 10¹⁸ planck

# ----------------------- configuration paths ----------------------
CFG_PATH = Path(__file__).resolve().parents[1] / "miner.yml"
CONFIG_DIR = Path(__file__).resolve().parents[2] / "auction_cfg"

# ----------------------------- caches -----------------------------
PRICE_TTL = 300.0    # seconds
EMISS_TTL = 120.0
CURVE_TTL = 60.0

_price_cache   : Dict[int, Tuple[float, float]] = {}
_emis_cache    : Dict[int, Tuple[float, float]] = {}
_curve_cache   : Dict[int, Tuple[dict, float]] = {}
_cache_lock = asyncio.Lock()

# ══════════════════════════ data classes ════════════════════════════════════


@dataclass(slots=True)
class Profile:
    subnet        : int
    wallet        : bt.wallet
    step_tao      : float
    max_discount  : float
    cooldown      : int
    quota         : Optional[float] = None   # epoch cap (TAO)
    sent          : float = 0.0              # running total
    last_block    : int = -10**9           # last transfer

    # strategy params (mutually exclusive)
    sell_percent  : Optional[float] = None
    sell_tao      : Optional[float] = None

# ══════════════════════════ util helpers ════════════════════════════════════


def _load_miner_cfg():
    if not CFG_PATH.exists():
        raise FileNotFoundError(f"miner.yml not found at {CFG_PATH}")
    return yaml.safe_load(CFG_PATH.read_text())


def _wallet_from_alias(alias: str) -> bt.wallet:
    ws_path = Path(bt.__path__[0]).parent / "wallets"
    return bt.wallet(path=str(ws_path / alias))


async def _get_pool_price(st: bt.AsyncSubtensor, subnet: int) -> float:
    """Cached α/TAO spot price."""
    now = time.time()
    async with _cache_lock:
        price, ts = _price_cache.get(subnet, (None, 0.0))
        if price is not None and now - ts < PRICE_TTL:
            return price
    bal = await st.alpha_tao_avg_price(subnet_id=subnet, start=0, end=0)
    price = float(bal.tao)
    async with _cache_lock:
        _price_cache[subnet] = (price, now)
    return price


async def _get_emissions(
    st: bt.AsyncSubtensor, subnet: int, coldkeys: List[str]
) -> float:
    """Live α emissions for a group of cold‑keys (cached)."""
    now = time.time()
    async with _cache_lock:
        emis, ts = _emis_cache.get(subnet, (None, 0.0))
        if emis is not None and now - ts < EMISS_TTL:
            return emis
    from metahash.utils.emissions import get_total_current_emissions_for_coldkeys
    res = await get_total_current_emissions_for_coldkeys(
        coldkeys=coldkeys, netuids=[subnet], st=st
    )
    emis = res.grand_total().tao
    async with _cache_lock:
        _emis_cache[subnet] = (emis, now)
    return emis


async def _load_curve_params(subnet: int) -> dict | None:
    """
    Load the latest validator broadcast for *subnet* (bag_sn73, beta, …).
    Cached 60 s – file I/O is cheap but we touch it a lot.
    """
    now = time.time()
    async with _cache_lock:
        cfg, ts = _curve_cache.get(subnet, (None, 0.0))
        if cfg and now - ts < CURVE_TTL:
            return cfg

    pattern = f"*subnet_{subnet}_epoch_*.json"
    files = sorted(CONFIG_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    cfg = json.loads(files[-1].read_text())
    async with _cache_lock:
        _curve_cache[subnet] = (cfg, now)
    return cfg

# ════════════════════════ live m_eaten tracker ══════════════════════════════


class MEatenTracker:
    """
    Keeps an **up‑to‑date** `m_eaten` for one subnet by scanning only the
    blocks we haven’t processed yet.
    """

    def __init__(self, subnet: int, st: bt.AsyncSubtensor):
        self.subnet = subnet
        self.st = st
        self.params  : dict = {}
        self.m_eaten : Decimal = Decimal(0)
        self.last_blk: int | None = None
        self.scanner : AlphaTransfersScanner | None = None
        self.treasury: str | None = None

    async def refresh_params(self):
        cfg = await _load_curve_params(self.subnet)
        if not cfg or cfg == self.params:
            return
        self.params = cfg
        self.m_eaten = Decimal(str(cfg["m_eaten_prev"]))
        self.treasury = cfg["treasury_coldkey"]
        self.scanner = AlphaTransfersScanner(self.st, dest_coldkey=self.treasury)
        clog.info(f"[s={self.subnet}] bond‑curve params refreshed", color="gray")

    async def sync(self, head: int):
        """Scan new blocks up to *head* and update `m_eaten`."""
        if not self.params:
            await self.refresh_params()
            if not self.params:
                return

        start_blk = (self.last_blk or self.params["auction_start_block"]) + 1
        if start_blk > head:
            return

        evts = await self.scanner.scan(start_blk, head)   # inclusive
        if not evts:
            self.last_blk = head
            return

        # One RPC per sync – cheapest way to estimate slip
        depth = await self.st.alpha_pool_depth(subnet_id=self.subnet)
        price = await _get_pool_price(self.st, self.subnet)

        beta = Decimal(str(self.params["beta"]))
        c0 = Decimal(str(self.params["bag_sn73"])) / (
            Decimal(1) + beta * self.m_eaten
        )
        r_min = Decimal(str(self.params.get("r_min", 0.0)))
        bag = Decimal(str(self.params["bag_sn73"]))

        for ev in evts:
            # ---- slippage‑adjusted TAO value --------------------------
            tao_val = Decimal(ev.amount_rao) * Decimal(str(price)) / PLANCK_DEC
            slip = Decimal(ev.amount_rao) / Decimal(depth) if depth else Decimal(1)
            tao_post = tao_val * max(Decimal(0), (Decimal(1) - slip))

            # ---- sequential bond‑curve award -------------------------
            rate = max(r_min, c0 / (Decimal(1) + beta * self.m_eaten))
            want = tao_post * rate
            room = bag - self.m_eaten
            awarded = max(Decimal(0), min(want, room))
            self.m_eaten += awarded
            if room <= 0:
                break  # bag exhausted

        self.last_blk = head
        clog.debug(
            f"[s={self.subnet}] m_eaten={float(self.m_eaten):.2f}/{bag} "
            f"after {len(evts)} new deposits",
            color="gray",
        )

# ══════════════════════ main auction loop coroutine ════════════════════════


async def _run():
    # ───────────────── subtensor connection ─────────────────────────── #
    bittensor_config = config()

    st = bt.AsyncSubtensor(network=bittensor_config.subtensor.network)
    await st.initialize()
    clog.success("✓ Connected to subtensor", color="green")

    # ───────────────── config / profiles ────────────────────────────── #
    cfg = _load_miner_cfg()
    cold_cfg = cfg.get("coldkeys", [])

    # Acept list or dict
    if isinstance(cold_cfg, dict):
        flat = []
        for value in cold_cfg.values():    # value puede ser lista o dict
            if isinstance(value, list):
                flat.extend(value)
            elif isinstance(value, dict):
                flat.append(value)
        cold_cfg = flat

    coldkeys = [c["address"] for c in cold_cfg]

    profiles: List[Profile] = []
    for entry in cfg.get("profiles", []):
        wallet = _wallet_from_alias(entry["wallet"])
        profiles.append(Profile(
            subnet=entry["subnet"],
            wallet=wallet,
            step_tao=float(entry["step_tao"]),
            max_discount=float(entry["max_discount"]),
            cooldown=int(entry.get("cooldown", 12)),
            sell_percent=entry.get("sell_percent"),
            sell_tao=entry.get("sell_tao"),
        ))

    trackers: Dict[int, MEatenTracker] = {
        p.subnet: MEatenTracker(p.subnet, st) for p in profiles
    }

    clog.success("✓ Auction loop online", color="green")

    # ───────────────── continuous epoch loop ────────────────────────── #
    while True:
        try:
            head = await st.get_current_block()

            # --- 1. refresh bond‑curve params + live m_eaten -----------
            await asyncio.gather(*(t.refresh_params() for t in trackers.values()))
            await asyncio.gather(*(t.sync(head) for t in trackers.values()))

            # --- 2. update quotas once per epoch -----------------------
            for p in profiles:
                tempo = await st.tempo(p.subnet)
                epoch = head // tempo
                if getattr(p, "_epoch", None) == epoch:
                    continue  # already done for this epoch
                emis = await _get_emissions(st, p.subnet, coldkeys)
                p.quota = (
                    p.sell_tao
                    if p.sell_tao is not None
                    else emis * p.sell_percent
                    if p.sell_percent is not None
                    else None
                )
                p.sent = 0.0
                p._epoch = epoch
                clog.info(
                    f"[PROFILE s={p.subnet}] emissions={emis:.2f} α‑TAO, "
                    f"quota={'∞' if p.quota is None else f'{p.quota:.2f}'}",
                    color="cyan",
                )

            # --- 3. per‑profile bidding attempt ------------------------
            for p in profiles:
                head = await st.get_current_block()

                # cool‑down & quota
                if head - p.last_block < p.cooldown:
                    continue
                if p.quota is not None and p.sent + 1e-9 >= p.quota:
                    continue

                tracker = trackers[p.subnet]
                if not tracker.params:
                    continue  # no validator yet

                pool_price = await _get_pool_price(st, p.subnet)

                disc = discount_for_deposit(
                    tao_value=p.step_tao,
                    bag_sn73=tracker.params["bag_sn73"],
                    beta=tracker.params["beta"],
                    m_eaten_prev=float(tracker.m_eaten),   # ← live!
                    pool_price_tao=pool_price,
                )

                if disc > p.max_discount:
                    continue  # too expensive

                # wallet balance
                bal: Balance = await st.get_balance(
                    p.wallet.coldkey.ss58_address, netuid=p.subnet
                )
                spendable = max(0.0, bal.tao - 1.0)  # reserve 1 TAO
                if spendable <= 0:
                    continue

                amt = min(p.step_tao, spendable)
                if p.quota is not None:
                    amt = min(amt, p.quota - p.sent)
                if amt <= 0:
                    continue

                # --- transfer
                ok = False
                try:
                    ok = await transfer_alpha(
                        subtensor=st,
                        wallet=p.wallet,
                        hotkey_ss58=p.wallet.hotkey.ss58_address,
                        origin_netuid=p.subnet,
                        dest_coldkey_ss58=tracker.treasury,
                        amount=amt,
                        wait_for_inclusion=True,
                        wait_for_finalization=False,
                    )
                except Exception as e:
                    clog.error(f"Transfer failed: {e}", color="red")

                if ok:
                    p.sent += amt
                    p.last_block = head
                    clog.success(
                        f"[s={p.subnet}] α {amt:.2f} TAO sold "
                        f"(disc {disc*100:.1f} %, step={p.step_tao})",
                        color="green",
                    )

            await asyncio.sleep(3)

        # -------------- top‑level error handling ----------------------
        except Exception as e:
            clog.error(f"Top‑level loop error: {e}", color="red")
            await asyncio.sleep(10)

# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    asyncio.run(_run())
