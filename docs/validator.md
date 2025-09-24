# 🧪 MetaHash Validator Setup Guide (Subnet 73)

Operate a MetaHash validator on **SN73** using the v3 pipeline: **Auction → Clearing → Commitments → Settlement**.

---

## 📦 Prerequisites
```text
1. **Subtensor lite node with `--pruning=2000`** configured
2. **Python 3.10+** installed
3. **pip/venv** for isolated environment
4. A funded coldkey/hotkey wallet with stake registered in Subnet 73
```
---

## 🔁 Epoch lifecycle (ground truth)
```text
Epoch e — Auction & Clearing
1. AuctionStart: broadcast start marker.
2. Collect bids: miners submit (subnet_id, alpha_amount, discount_bps).
3. Clearing: rank by TAO value with slippage; apply optional reputation caps; allow partial fills.
4. Early Win invoices: notify winners immediately; each line includes payment window [as, de] in e+1.
5. Stage commitment: persist the exact winners snapshot for e, including budget signals (bt_mu, bl_mu).

Epoch e+1 — Publish commitments
6. CID-only (v4) on-chain: publish a CID referencing the commitment for epoch e.
7. Full JSON to IPFS: push the full payload to IPFS; no catch-up publishing (strict publisher).

Epoch e+2 — Settlement & weights
8. Scan payments: merge windows, verify α transfers within [as, de] to known treasuries; respect STRICT_PER_SUBNET.
9. Burn underfill: deterministically burn unused budget to UID 0.
10. Set weights: call set_weights() unless TESTING=true (preview-only).
```
---

## 🧪 Setup Environment

```bash
# Clone the MetaHash repository
git clone https://github.com/fx-integral/metahash && cd metahash

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install MetaHash and dependencies
pip install -U pip wheel uv
uv pip install -e .
```

---

## ⚙️ Configuration

### 1. `.env` — Environment Variables

Set the environment values for your validator identity and network:

```dotenv
BT_WALLET_NAME=validator-wallet
BT_HOTKEY=validator-hotkey
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
pm2 start python --name metahash-validator -- neurons/validator.py --netuid 73 --subtensor.network archive --wallet.name validator-wallet --wallet.hotkey validator-hotkey --neuron.axon_off --logging.debug
```
Notes
``` text
- Map validator hotkey → treasury coldkey in metahash/treasuries.py (only these treasuries will be credited).
- Reputation controls & auction budget are configured in metahash/config.py
- Subnet weights are set in weights.yml
```

## 🔍 Observability & state
``` text
- Structured logs: Auction, Clearing, Commitments, Settlement sections.
- Local JSON state (atomic writes):
  - validated_epochs.json — published/settled epochs
  - pending_commits.json — staged commitments awaiting publish
  - reputation.json — per-coldkey reputation / caps
  - jailed_coldkeys.json — enforcement state

Optional reporter:
scripts/validator/report.sh
```
---

## 🧰 Configuration reference (env-driven)

See metahash/config.py for defaults. Common knobs:
``` text
- Network / lifecycle: BITTENSOR_NETWORK, START_V3_BLOCK, TESTING, EPOCH_LENGTH_OVERRIDE
- Auction / clearing: AUCTION_BUDGET_ALPHA, MAX_BIDS_PER_MINER
- Reputation: REPUTATION_ENABLED, REPUTATION_BASELINE_CAP_FRAC, REPUTATION_MAX_CAP_FRAC
- Slippage: K_SLIP, SLIP_TOLERANCE, SAMPLE_POINTS
- Settlement: STRICT_PER_SUBNET, JAIL_EPOCHS_*
- IPFS: IPFS_API_URL, IPFS_GATEWAYS
```
---

## ❓ FAQ (validator-specific)
``` text 
Do I need IPFS to validate?
Yes. Commitments are published as CID-only on-chain with full payloads stored in IPFS.

Why are miner discounts large numbers?
They’re basis points (bps) (1 bp = 0.01%). Example: 500 = 5%.

What if a miner pays late or to the wrong subnet?
The line is ignored in settlement; underfill is burned to UID 0.
```

## 📓 Logs & Debugging

Use PM2 to view logs:

```bash
pm2 logs metahash-validator
```

Or run in debug mode directly:

```bash
python neurons/validator.py --logging.debug
```

## 🔗 Resources

- 📁 **GitHub**: https://github.com/fx-integral/metahash/
- 📚 **Bittensor Docs**: https://docs.bittensor.com/
- 🔐 **Coldkey and Hotkey Workstation Security**: https://docs.learnbittensor.org/getting-started/coldkey-hotkey-security/
