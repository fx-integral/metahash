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
   Ensure your coldkey has enough TAO to register and process transactions to operate.

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
--subtensor.network "finney" \
--miner.bids.netuids 71 72 73 \
--miner.bids.amounts 1.0 0.5 0.25 \
--miner.bids.discounts 500 700 900 \
--logging.debug
```

> ⚠️ `--miner.bids.discounts` are **basis points (bps)** — not percent.  
> `500 = 5%`, `700 = 7%`, `900 = 9%`.

### Discount modes
- **Default (effective-discount)** – discount is scaled by subnet weight.  
- **Raw mode** – add `--miner.bids.raw_discount` to send your bps unchanged.

---

## 🧾 State & payments

- Miner state is stored in `miner_state.json`.  
- Transfers are attempted automatically **within** `[as, de]` and **retried** with a cooldown (`PAYMENT_RETRY_COOLDOWN_BLOCKS`).  
- You can also pay **manually** using `btcli`; ensure the **correct window and treasury**.

---

## 🔒 Security best practices

- Do **not** pass wallet passphrases on the command line.  
- Keep minimal balances in hotkeys used for mining.  
- Prefer hardware/air-gapped setups for coldkeys.  
- Review logs to confirm invoices, windows, and payments.

---

## 📝 Tips

- Set `BITTENSOR_NETWORK` in `.env` or pass `--subtensor.network` explicitly.  
- Watch logs for `AuctionStart`, **Win** (shows `[as, de]`), and payment attempts.  
- Late/invalid payments are ignored; underfill is **burned** to UID 0.  
- With `STRICT_PER_SUBNET=true`, **don’t cross-subsidize** — each subnet’s bid must be paid individually.
