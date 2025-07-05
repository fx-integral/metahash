# 🛠 MetaHash Miner Setup Guide (Subnet 73)

## ⚠️ IMPORTANT: NO NEURON REQUIRED

**🚨 BIG WARNING: MetaHash miners DO NOT NEED TO RUN A MINER NEURON! 🚨**

**You only need to:**
1. **Register** your miner on Subnet 73
2. **Send alpha tokens** from your registered miner's coldkey to participate in auctions

**Miners can use our helper auction_watch.py script but they can also send alph manually**

---

This guide explains how to configure and operate a **MetaHash SN73 Miner**, which allows you to participate in the decentralized OTC (Over-The-Counter) alpha bonding auction system. You will trade alpha tokens from other subnets in exchange for discounted SN73 incentives.

---

## 📦 Prerequisites

Before starting, ensure you have:

1. **Python 3.10+** installed on your system
2. **pip/venv** for Python environment isolation
3. **Git** for repository cloning
4. **btcli** (Bittensor CLI) configured with your wallet
5. **Registered miner** on at least one subnet to obtain alpha tokens

---

## 🧪 Environment Setup

```bash
# Clone the MetaHash repository
git clone https://github.com/fx-integral/metahash/ && cd metahash

# Create and activate Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install uv
uv pip install -e .
```

---

## 🎯 How MetaHash Mining Works

Miners in SN73 participate in **alpha token auctions** where they:
- **Sell alpha tokens** from other subnets (e.g., SN33) in an OTC marketplace
- **Receive discounted SN73 alpha** in return
- **Contribute to the SN73 treasury** while avoiding impact on their home subnet's economy

### ⚠️ Important Restriction
**FORBIDDEN**: You **cannot send SN73 alpha tokens** to the auctions. Only alpha from other subnets (SN33, SN1, etc.) can be used for bidding.

### Auction Mechanics

- **Frequency**: Auctions occur every **361 blocks** (approximately every epoch)
- **Supply**: **148 SN73 alpha tokens** are auctioned each round
- **Discount Structure**: 
  - Starts at `D_START` (currently **10% discount**)
  - Discount **decreases** as more alpha is contributed to the auction
  - If total contributions exceed SN73 alpha value, excess contributions count as **0 value**
- **Undersubscription**: Remaining SN73 alpha tokens are **burned** if auction is not fully subscribed

---

## ⚙️ Miner Registration

### Step 1: Register Your Miner

```bash
# Register your miner on Subnet 73
btcli s register --netuid 73 --wallet.name YOUR_WALLET_NAME --wallet.hotkey YOUR_HOTKEY_NAME
```

### Step 2: Important Registration Rules

⚠️ **CRITICAL**: Only register **ONE miner per coldkey**
- Each coldkey can only have one active UID on SN73
- Multiple registered miners will result in only one receiving incentives
- All alpha sent from your coldkey will be attributed to a single miner
- **If you already have multiple miners registered**: That's okay, but only one will receive incentives from your alpha contributions

---

## 🚀 Mining Options

You have two approaches to participate in MetaHash mining:

### Option A: Manual/Custom Approach
- **Freestyle trading**: Send alpha to treasury manually using btcli
- **Custom scripts**: Build your own bidding automation
- **Full control**: Implement your own auction strategies

### Option B: Automated Auction Watcher (Recommended)

Use the provided auction monitoring script:

```bash
python scripts/miner/auction_watch.py \
    --netuid 33 \
    --validator-hotkey <VALIDATOR_HOTKEY> \
    --wallet.name <WALLET_NAME> \
    --wallet.hotkey <HOTKEY_NAME> \
    --max-alpha 30 \
    --step-alpha 5 \
    --max-discount 20
```

**Example with sample values:**
```bash
python scripts/miner/auction_watch.py \
    --netuid 33 \
    --validator-hotkey <YOUR_VALIDATOR_HOTKEY> \
    --wallet.name <YOUR_WALLET_NAME> \
    --wallet.hotkey <YOUR_HOTKEY_NAME> \
    --max-alpha 30 \
    --step-alpha 5 \
    --max-discount 20
```

#### Script Parameters Explained

| Parameter | Description | Example | Notes |
|-----------|-------------|---------|-------|
| `--netuid` | Source subnet for alpha tokens | `33` | Cannot be `73` (SN73 alpha forbidden) |
| `--validator-hotkey` | Validator hotkey from source subnet where your alpha is staked | `<YOUR_VALIDATOR_HOTKEY>` | This is where your alpha will be taken from |
| `--wallet.name` | Your coldkey/wallet name | `<YOUR_WALLET_NAME>` | Replace with your actual wallet name |
| `--wallet.hotkey` | Your hotkey name | `<YOUR_HOTKEY_NAME>` | Replace with your actual hotkey name |
| `--max-alpha` | Maximum alpha to bid per auction | `30` | Set based on your available alpha |
| `--step-alpha` | Incremental bidding steps | `5` | Amount to increase bids by |
| `--max-discount` | Maximum acceptable discount threshold | `20` | Stop bidding if discount falls below this |

#### How the Script Works

1. **Monitors** each auction in real-time
2. **Bids alpha** from subnet 33 in increments of 5 alpha
3. **Continues bidding** up to maximum of 30 alpha
4. **Stops bidding** when discount falls below 20%
5. **Automatically stops** if auction becomes over-subscribed

---

## 📊 Auction Strategy & Risk Management

### Discount Mechanics
- **Early bidding**: Higher discounts, better deals
- **Late bidding**: Lower discounts, worse deals
- **Over-subscription**: Zero value for excess contributions

### Best Practices
1. **Monitor discount levels** closely
2. **Set conservative max-discount** thresholds
3. **Stop bidding** when auctions become over-subscribed
4. **Diversify** across multiple auctions rather than going all-in

### Risk Warnings
⚠️ **Stop sending alpha if auction is over-subscribed** - your contributions will have zero value  
⚠️ **Monitor your max-discount threshold** - don't accept unfavorable deals  
⚠️ **Only use surplus alpha** - don't compromise your home subnet operations  
⚠️ **NEVER send SN73 alpha** - only alpha from other subnets is allowed

---


## 📚 Additional Resources

- [MetaHash GitHub Repository](https://github.com/fx-integral/metahash/)
- [Bittensor Documentation](https://docs.bittensor.com/)
- [SN73 Specifications](https://github.com/fx-integral/metahash/blob/main/docs/sn73-specs.md)

---

*Happy Mining! 🚀*