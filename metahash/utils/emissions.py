# ====================================================================== #
# metahash/utils/emissions.py                            
# ====================================================================== #


from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional

from bittensor import AsyncSubtensor
from bittensor.utils.balance import Balance

from metahash.utils.subnet_utils import get_metagraph, _with_subtensor

# ╭──────────────────────── data models ─────────────────────────╮


@dataclass(slots=True, frozen=True)
class NeuronEmission:
    neuron_uid: int
    emission: Balance


@dataclass(slots=True)
class SubnetEmission:
    netuid: int
    neuron_emissions: List[NeuronEmission]

    @property
    def total(self) -> Balance:
        return sum((n.emission for n in self.neuron_emissions), Balance.tao(0))

    def __str__(self) -> str:                       # convenience
        return f"Subnet {self.netuid}: {self.total}"


@dataclass(slots=True)
class ColdkeyEmissionResult:
    coldkey: str
    subnet_emissions: Dict[int, SubnetEmission]

    @property
    def total(self) -> Balance:
        return sum((s.total for s in self.subnet_emissions.values()), Balance.tao(0))

    def totals_by_subnet(self) -> Dict[int, Balance]:
        return {uid: se.total for uid, se in self.subnet_emissions.items()}

    def __str__(self) -> str:
        per_subnet = ", ".join(f"{uid}:{bal}" for uid, bal in self.totals_by_subnet().items())
        return f"{self.coldkey} → total={self.total} ({per_subnet})"


@dataclass(slots=True)
class MultiColdkeyEmissionResult:
    results: Dict[str, ColdkeyEmissionResult]

    # aggregation helpers
    def total_for_coldkey(self, coldkey: str) -> Balance:
        return self.results[coldkey].total

    def totals_by_coldkey(self) -> Dict[str, Balance]:
        return {ck: res.total for ck, res in self.results.items()}

    def totals_by_subnet(self) -> Dict[int, Balance]:
        out: Dict[int, Balance] = {}
        for res in self.results.values():
            for uid, bal in res.totals_by_subnet().items():
                out[uid] = out.get(uid, Balance.tao(0)) + bal
        return out

    def grand_total(self) -> Balance:
        return sum(self.totals_by_coldkey().values(), Balance.tao(0))

    def to_nested_dict(self) -> Dict[str, Dict[int, str]]:
        return {
            ck: {uid: str(bal) for uid, bal in res.totals_by_subnet().items()}
            for ck, res in self.results.items()
        }

    def __str__(self) -> str:
        lines = [str(res) for res in self.results.values()]
        lines.append(f"GRAND TOTAL = {self.grand_total()}")
        return "\n".join(lines)


# ╭────────────────────── internal helpers ───────────────────────╮
async def _breakdown_single_subnet(
    coldkey: str,
    netuid: int,
    *,
    st: AsyncSubtensor,
    block: int | None = None,
) -> SubnetEmission:
    meta = await get_metagraph(netuid, st=st, lite=False, block=block)

    if not hasattr(meta, "emission"):
        raise RuntimeError(
            f"Metagraph for netuid={netuid} lacks 'emission' field."
        )

    neurons: List[NeuronEmission] = []
    for uid, ck, em in zip(meta.uids, meta.coldkeys, meta.emission, strict=True):
        if ck != coldkey:
            continue
        bal = em if isinstance(em, Balance) else Balance.from_rao(int(em))
        neurons.append(NeuronEmission(neuron_uid=int(uid), emission=bal))

    return SubnetEmission(netuid=netuid, neuron_emissions=neurons)


# ╭──────────────────────── public API ────────────────────────────╮
async def get_total_current_emissions_for_coldkey(
    coldkey: str,
    netuids: List[int],
    *,
    st: Optional[AsyncSubtensor] = None,
    block: int | None = None,
) -> ColdkeyEmissionResult:
    async with _with_subtensor(st) as sub:
        coros = [
            _breakdown_single_subnet(coldkey, uid, st=sub, block=block)
            for uid in netuids
        ]
        breakdowns = await asyncio.gather(*coros, return_exceptions=False)

    return ColdkeyEmissionResult(
        coldkey=coldkey,
        subnet_emissions={b.netuid: b for b in breakdowns},
    )


async def get_total_current_emissions_for_coldkeys(
    coldkeys: List[str],
    netuids: List[int],
    *,
    st: Optional[AsyncSubtensor] = None,
    block: int | None = None,
) -> MultiColdkeyEmissionResult:
    async with _with_subtensor(st) as sub:

        async def _one_ck(ck: str):
            return await get_total_current_emissions_for_coldkey(
                ck, netuids, st=sub, block=block
            )

        coldkey_results = await asyncio.gather(*[_one_ck(ck) for ck in coldkeys])

    return MultiColdkeyEmissionResult(
        results={res.coldkey: res for res in coldkey_results}
    )


# ╭───────────────────── doctest‑style demo ───────────────────────╮
if __name__ == "__main__":  # pragma: no cover
    async def _demo():
        netuids = [1, 10, 23]
        coldkeys = ["tk1_test_ckA", "tk1_test_ckB"]

        single = await get_total_current_emissions_for_coldkey(
            coldkey=coldkeys[0], netuids=netuids
        )
        print("--- single coldkey ---")
        print(single, end="\n\n")

        multi = await get_total_current_emissions_for_coldkeys(
            coldkeys=coldkeys, netuids=netuids
        )
        print("--- multi coldkey ---")
        print(multi)

    asyncio.run(_demo())
