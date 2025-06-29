# 🛠 MetaHash Miner Setup Guide (Subnet 73)

This guide explains how to configure and operate a **MetaHash SN73 Miner**, which allows you to participate in the decentralized OTC alpha bonding auction. 
You will trade alpha from other subnets in exchange for SN73 incentives.

---

## 📦 Prerequisites

1. **Python 3.10+** installed
2. **pip/venv** for environment isolation

---

## 🧪 Setup Environment

```bash
# clone repository
git clone https://github.com/xxyrad/metahash/ && cd metahash

# create python virtual environment and activate it 
python3 -m venv .venv
source .venv/bin/activate
pip install uv
uv pip install -e .
```

---

## ⚙️ Configuration

### 1. `miner.yml` — Auction Spending Profile

This file defines how your miner spends in the SN73 bonding auction.

Configure your coldkey(s) and respective subnet profiles in the project root: 

```yaml
coldkeys:
- address: 5H13H43jH1DLV3WG7tAL8vkmnMhjnqpkNaFiQcXmUyWBrT5C
  signature: f6afac65f2b2ddd1bf549f10a1b86ec0c9c7bf7d161febbe0615a63463dc6d5ce320e7bbc305f076d7c33c4906d632dcf20e951a98e36dfb6ad38e03468f9680
profiles:
- subnet: 1
  wallet: origin-1
  sell_percent: 0.5
  max_discount: 0.35
  step_tao: 1.0
- subnet: 2
  wallet: origin-2
  sell_percent: 0.3
  max_discount: 0.25
  step_tao: 0.5
- subnet: 3
  wallet: origin-3
  sell_tao: 200
  max_discount: 0.2
  step_tao: 2.0
```

---
### Configuration Details of `miner.yml`

### 🔐 `coldkeys`

Authorizes specific coldkeys to submit alpha to SN73 and receive SN73 incentives.

```yaml
coldkeys:
  - address: 5H13H43jH1DLV3WG7tAL8vkmnMhjnqpkNaFiQcXmUyWBrT5C
    signature: f6afac65f2b2ddd1bf549f10a1b86ec0c9c7bf7d161febbe...
```

| Field     | Description                                                                 |
|-----------|-----------------------------------------------------------------------------|
| `address` | SS58 address of the coldkey contributing alpha                              |
| `signature` | Cryptographic proof authorizing this address for SN73 OTC participation    |

---

### ⚙️ `profiles`

Each profile defines **how much TAO to bond**, **at what price**, from **which subnet and wallet**.

```yaml
profiles:
  - subnet: 1
    wallet: origin-1
    sell_percent: 0.5
    max_discount: 0.35
    step_tao: 1.0

  - subnet: 2
    wallet: origin-2
    sell_percent: 0.3
    max_discount: 0.25
    step_tao: 0.5

  - subnet: 3
    wallet: origin-3
    sell_tao: 200
    max_discount: 0.2
    step_tao: 2.0
```

| Field         | Description                                                                 |
|---------------|-----------------------------------------------------------------------------|
| `subnet`      | The UID of the source subnet providing alpha (e.g. 1, 2, 3)                 |
| `wallet`      | Bittensor wallet name (coldkey/hotkey pair)                                 |
| `sell_percent`| Optional: fraction of wallet TAO to bond this epoch (0.0–1.0)               |
| `sell_tao`    | Optional: fixed TAO amount to bond this epoch (overrides `sell_percent`)    |
| `max_discount`| Maximum acceptable discount (e.g. 0.35 = 35%)                               |
| `step_tao`    | How much TAO to bond per auction step (granularity)                         |

> You can define multiple profiles to run parallel strategies from different wallets or subnets.

---

### 🔄 Profile Evaluation Rules

- `sell_tao` takes precedence over `sell_percent` if both are defined.
- All auctions enforce:
  - Discount threshold (`max_discount`)
  - Step granularity (`step_tao`)
  - Wallet reserve and quota checks (configured internally or in future extensions)
- All coldkeys **must match an authorized entry with a valid signature**.

---

### ✅ Example Summary

```yaml
coldkeys:
  - address: 5H13H...
    signature: abc123...

profiles:
  - subnet: 1
    wallet: origin-1
    sell_percent: 0.5         # Use 50% of wallet TAO
    max_discount: 0.35        # Require ≤ 35% discount
    step_tao: 1.0             # Bond in 1.0 TAO increments

  - subnet: 3
    wallet: origin-3
    sell_tao: 200             # Bond 200 TAO this epoch
    max_discount: 0.2
    step_tao: 2.0
```

This configuration:
- Authorizes one coldkey for OTC bonding
- Defines auction strategies for 2 wallets:
  - `origin-1` will bond 50% of its TAO in subnet 1
  - `origin-3` will bond exactly 200 TAO from subnet 3



---

### 2. `.env` — Environment Variables

Create a `.env` file in the project root and define:
```dotenv
BT_WALLET_NAME=mywallet
BT_HOTKEY=myhotkey
BT_NETWORK=finney
```

---

### 3. 🧊 Wallet Initialization

Initialize coldkey/hotkey if not yet created:
```bash
btcli new-coldkey --wallet.name mywallet
btcli new-hotkey --wallet.name mywallet --wallet.hotkey myhotkey
btcli subnets register --wallet.name mywallet --wallet.hotkey myhotkey
```

Authorize SN73 subnet on-chain:
```bash
python scripts/add_coldkey.py --coldkey mywallet --hotkey myhotkey
```

---

## 🚀 Running the Miner

### ✅ Run with PM2 (recommended)

```bash
pm2 start python --name metahash-miner -- neurons/auction_loop.py
```

### 🔁 Script Mode (testing)

```bash
python neurons/auction_loop.py
```

The auction loop will:
- Load your YAML profile(s)
- Check on-chain alpha balances and wallet health
- Bond TAO using the SN73 bonding curve
- Ensure quota, cooldown, and reserve rules are respected
- Atomically log auctions using `filelock`-protected JSON

---

## 🧠 How It Works

- The miner monitors bonding opportunities based on the bonding curve (`bond_utils.py`)
- Each epoch, it participates in auctions and receives SN73 incentives
- Bids are evaluated for discount rate and quota compliance
- Auctions can be customized for aggressiveness and spending tolerance

---

## 🛡️ Safeguards

- **Atomic lockfiles** prevent concurrent bidding
- **Reserve protection** keeps wallet solvent
- **Cooldown timers** reduce chain spam
- **Discount filters** ensure you don’t overspend for SN73

---

## 📓 Logs & Debugging

Check PM2 logs:

```bash
pm2 logs metahash-miner
```

Or enable debug mode with:

```bash
python neurons/auction_loop.py --logging.debug
```

---

## 🧩 Related Components

- **SN73 Validators** track alpha inflows and emit weights
- **Bonding Curve** enforces dynamic SN73 pricing
- **Epoch Engine** determines when auctions are reset

---

## ✅ You're Mining!

If successful, you will:
- Contribute alpha to the SN73 treasury
- Receive discounted SN73 tokens
- Avoid impacting your home subnet's economy
