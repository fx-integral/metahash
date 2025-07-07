# 🧪 MetaHash Validator Setup Guide (Subnet 73)

This guide outlines how to set up and operate a **MetaHash SN73 Validator**, which is responsible for:
- Tracking cross-subnet alpha deposits,
- Pricing and rewarding alpha contributions,
- Issuing SN73 token incentives to bonded miners.

---

## 📦 Prerequisites
1. **Subtensor lite node with `--pruning=2000`** configured
2. **Python 3.10+** installed
3. **pip/venv** for isolated environment
4. A funded coldkey/hotkey wallet with stake registered in Subnet 73

---

## 🧪 Setup Environment

```bash
# Clone the MetaHash repository
git clone https://github.com/fx-integral/metahash && cd metahash

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install MetaHash and dependencies
pip install uv
uv pip install -e .
```

---

## ⚙️ Configuration

### 1. `.env` — Environment Variables

Set the environment values for your validator identity and network:

```dotenv
BT_WALLET_NAME=validator-wallet
BT_HOTKEY=validator-hotkey
BT_NETWORK=finney
```

---

### 2. 🧊 Wallet Initialization

If needed, create and register your validator wallet:

```bash
btcli new-coldkey --wallet.name validator-wallet
btcli new-hotkey --wallet.name validator-wallet --wallet.hotkey validator-hotkey
```

Register your hotkey on Subnet 73:

```bash
btcli subnets register --wallet.name mywallet --wallet.hotkey myhotkey --netuid 73
```

> Make sure your wallet has sufficient stake to appear to operate as a validator.

---

## 🚀 Running the Validator

### ✅ Using PM2 (recommended)

```bash
pm2 start python --name metahash-validator -- neurons/validator.py --netuid 73 --subtensor.network finney --wallet.name validator-wallet --wallet.hotkey validator-hotkey --logging.debug
```

### 🔁 Development Mode

```bash
python neurons/validator.py --netuid 73 --subtensor.network finney --wallet.name validator-wallet --wallet.hotkey validator-hotkey --logging.debug
```

This validator will:
- Detect new `StakeTransferred` events (alpha deposits)
- Apply finality lag to avoid chain reorg risk
- Evaluate alpha using pricing oracle and slippage
- Emit `set_weights()` to reward coldkeys with SN73 incentives
- Record all participation via epoch-based ledger

---

## 🧠 How It Works

- Validators run `EpochValidatorNeuron`, executing once per chain tempo
- Events are scanned from the chain using `alpha_transfers.py`
- Prices are fetched via dynamic pricing interfaces
- Rewards are calculated using `rewards.py`, with planck-level precision
- Scores are normalized, and weights are set via `subtensor.set_weights(...)`

---

## ⚖️ Reward Mechanics

Validators apply:

| Mechanism        | Purpose                                             |
|------------------|-----------------------------------------------------|
| Slippage Curve   | Discounts alpha based on pool depth                |
| Finality Lag     | Ensures event stability before reward distribution |
| Price Oracle     | Retrieves TAO/alpha price for reward conversion    |

> All monetary math is computed using `decimal.Decimal` with 60-digit precision.

---

## 🛡️ Safeguards

- Wallet sync is enforced on failure to avoid dehydration
- Rewards only emitted if valid scores exist (`E-2` guard)
- `adaptive_wait_for_head()` ensures efficient epoch pacing
- Coldkey addresses are masked when dumping logs

---

## 📓 Logs & Debugging

Use PM2 to view logs:

```bash
pm2 logs metahash-validator
```

Or run in debug mode directly:

```bash
python neurons/validator.py --logging.debug
```

---

## 🧩 System Components

- `alpha_transfers.py`: monitors alpha inflow events
- `rewards.py`: calculates SN73 reward weights
- `bond_utils.py`: implements the bonding curve
- `validator.py`: main loop, score aggregation, weight emission
- `epoch_validator.py`: orchestrates the epoch-pacing cycle

---

## ✅ You're Validating!

Once running, your validator:
- Accepts OTC alpha deposits from bonded coldkeys
- Measures value and liquidity-adjusted worth of alpha
- Incentivizes contributors with appropriately priced SN73 rewards
- Builds the MetaHash alpha treasury to benefit all SN73 holders

## 🔗 Resources

- 📁 **GitHub**: https://github.com/fx-integral/metahash/
- 📚 **Bittensor Docs**: https://docs.bittensor.com/
- 🔐 **Coldkey and Hotkey Workstation Security**: https://docs.learnbittensor.org/getting-started/coldkey-hotkey-security/
