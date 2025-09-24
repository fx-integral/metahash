# ⛏️ MetaHash Miner Guide (Subnet 73)

This guide walks you through the **basic setup** (environment, wallet + hotkey), the **miner ↔ validator flow**, and how to run the miner so your α payments are correctly counted during settlement.

---

## 🛠️ Basic setup

1. **Install**
   ```bash
   git clone https://github.com/fx-integral/metahash.git
   cd metahash
   python -m venv .venv && source .venv/bin/activate
   pip install -U pip wheel uv
   uv pip install -e .
   cp .env.template .env
   # edit .env with WALLET_PASSWORD and optionally BITTENSOR_NETWORK
   # or unlock wallet interractively by starting the miner
   ```
---

2. **Create wallet & hotkey; register on SN73**
   ```bash
   btcli wallet new_coldkey --wallet.name mywallet
   btcli wallet new_hotkey --wallet.name mywallet --wallet.hotkey miner1
   btcli register --netuid 73 --wallet.name mywallet --wallet.hotkey miner1
   ```

3. **Fund the wallet**  
```text
   Ensure your coldkey has enough TAO to register and process transactions to operate.
```
---

## 🧭 High-level overview (Miner ↔ Validator)

1. **AuctionStart (epoch e)** – Validator broadcasts auction start.  
2. **Bids from miner** – You submit lines:  
   ```
   (subnet_id, alpha_amount, discount_bps)
   ```
   - `subnet_id` – target subnet to support with α  
   - `alpha_amount` – how much α you offer  
   - `discount_bps` – **basis points (bps)** (1 bp = 0.01%)  
   - Examples: `500 = 5%`, `700 = 7%`, `900 = 9%` 
3. **Validator clearing** – Bids ranked by **TAO value** with slippage & optional reputation caps; partial fills allowed.  
4. **Win invoice (still epoch e)** – If accepted, you receive `Win` with **payment window** `[as, de]` (block numbers) occurring in **e+1**.  
5. **Miner sends α (epoch e+1)** – Pay within `[as, de]` to a **known treasury** (`metahash/treasuries.py`).  
   - With `STRICT_PER_SUBNET=true`, each accepted bid line must be **paid on its own subnet**.  
6. **Settlement & weights (epoch e+2)** – Validator verifies payments, **burns underfill** to UID 0, and sets weights on-chain.

---

## 🚀 Run the miner

```bash
python neurons/miner.py \
--netuid 73 \
--wallet.name mywallet \
--wallet.hotkey miner1 \
--subtensor.network "ws://x.x.x.x:9944" \
--miner.bids.netuids 71 72 73 \
--miner.bids.amounts 1.0 0.5 0.25 \
--miner.bids.discounts 500 700 900 \
--axon.port 8091 \
--axon.external_port 8091 \
--logging.debug \
--payment.validators $source-stake-hotkeyA $source-stake-hotkeyB $source-stake-hotkeyC

--
```

> ⚠️ `--payment.validators ` are **hotkeys where stake will be transferred from**.

> ⚠️ `--miner.bids.discounts` are **basis points (bps)** — not percent.  
> `500 = 5%`, `700 = 7%`, `900 = 9%`.

### Discount modes
- **Default (effective-discount)** – discount is scaled by subnet weight.  
- **Raw mode** – add `--miner.bids.raw_discount` to send your bps unchanged.

---
### Understanding `discount_bps`

Each bid includes a `discount_bps` field, which sets the **maximum discount you are willing to accept** on the value of your α, after adjusting for slippage and validator-specific subnet weights.

- **Basis points (bps):** 1 bp = 0.01%
  - `0` bps = no discount (you expect full value)
  - `500` bps = 5% max discount
  - `2500` bps = 25% max discount

- **How it works:**
  - Subnet weights apply an implicit haircut. For example, a subnet weight of `0.8` means you already face a 20% baseline discount.
  - Your `discount_bps` sets the maximum haircut you are willing to accept, including this baseline.
  - The validator ensures your **effective discount never exceeds your configured max**.
  - If the subnet’s baseline discount is greater than your max, the bid is not sent.

- **Example 1 (accepted):**
  - Subnet weight = `0.8` (20% haircut)
  - Your discount = 25% max (`2500` bps)
  - Effective discount applied = 5% (difference between 25% and 20%)
  - You receive 75% of your α’s value.

- **Example 2 (tight fill):**
  - Subnet weight = `0.95` (5% haircut)
  - Your discount = 25% max (`2500` bps)
  - Effective discount applied = 25% – 5% = 20%
  - You receive 75% of your α’s value.

- **Example 3 (rejected):**
  - Subnet weight = `0.7` (30% haircut)
  - Your discount = 25% max (`2500` bps)
  - Since 30% > 25%, the bid is **not sent**.

- **Why this matters:**
  - You can “set and forget” by specifying the maximum discount you’re comfortable with.
  - The code guarantees you will never be forced into a worse deal.
  - This provides certainty and safety, similar to OTC markets where miners typically offer ~10–20% discounts.
