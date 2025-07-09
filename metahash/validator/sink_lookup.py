"""Runtime helper – map alpha_sink address → validator uid."""
import bittensor as bt

_CACHE = {}


def uid_from_sink(addr: str) -> int | None:
    if addr in _CACHE:
        return _CACHE[addr]
    try:
        st = bt.Subtensor()          # synchronous, one‑shot
        uid = st.get_uid_for_sink(addr)   # requires runtime RPC you’ll add
        _CACHE[addr] = uid
        return uid
    except Exception:
        return None
