# MetaHash Miner Guide (Subnet 73)

## 🎯 What You're Doing

**You're competing with other miners in auctions to win SN73 rewards.**

- Trade your alpha tokens from other subnets (SN1, SN33, etc.)
- Bid against other miners every ~1 hour
- Win proportional rewards based on your bid value
- 148 SN73 tokens available per auction

## ⚡ Quick Start

**Step 1:** Get alpha tokens from other subnets first  
<br>**Step 2:** Register on Subnet 73 (one time only)  
**Step 3:** Use our scripts to compete in auctions

```bash
# Install
git clone https://github.com/fx-integral/metahash/ && cd metahash
python3 -m venv .venv && source .venv/bin/activate
pip install uv && uv pip install -e .

# Register (ONE TIME ONLY)
btcli s register --netuid 73 --wallet.name YOUR_WALLET --wallet.hotkey YOUR_HOTKEY
```

## 📊 How Auctions Work

### The Competition
- **Who:** All miners registered on SN73
- **When:** Every 361 blocks (~1 hour)
- **Prize:** 148 SN73 alpha tokens
- **How to win:** Send highest value alpha tokens

### Example Auction
```
Total auction value: 100 alpha tokens
Your bid: 20 alpha tokens  
Your share: 20% × 148 = 29.6 SN73 tokens
```

## 🛠️ Mining Tools

### Tool 1: Check Competition
See who's winning and track performance:

```bash
python scripts/miner/leaderboard.py \
    --meta-netuid 73 \
    --wallet.name YOUR_WALLET \
    --wallet.hotkey YOUR_HOTKEY
```

### Tool 2: Auto-Bid (Recommended)
Automatically compete in auctions:

```bash
python scripts/miner/auction_watch.py \
    --netuid SOURCE_SUBNET_ID \
    --validator-hotkey VALIDATOR_HOTKEY_ADDRESS \
    --wallet.name YOUR_WALLET \
    --wallet.hotkey YOUR_HOTKEY \
    --max-alpha MAX_ALPHA_PER_AUCTION \
    --step-alpha BIDDING_INCREMENT \
    --max-discount MINIMUM_DISCOUNT_THRESHOLD
```

**What it does:**
- Watches for new auctions
- Bids your alpha in small steps
- Stops if discount gets too low
- Prevents over-bidding

## ✅ Rules & Restrictions

### ✅ ALLOWED
- Send alpha from any subnet except 73
- Bid on multiple auctions
- Use automated scripts

### ⚠️ FORBIDDEN
- Cannot send SN73 alpha to auctions
- Only ONE registration per coldkey

### 🎯 GOAL
- Maximize value of alpha sent
- Beat other miners in auctions

## 💡 Winning Strategies

### Be Fast
- **Bid early** for better discounts
- **Use automation** to beat manual traders
- **Monitor constantly** for new auctions

### Be Smart
- **Set minimum discounts** (don't accept bad deals)
- **Watch competition levels** before bidding
- **Use surplus alpha only** (don't hurt your main subnet)

### Be Safe
- **⚠️ Stop if over-subscribed** (you get nothing if auction is too full)
- **⚠️ Track your performance** (learn what works)
- **⚠️ Start small** (test before going big)

## 🔥 Common Scenarios

### 🟢 Good Auction (Low Competition)
- Few miners bidding
- You get good discount
- High returns

### 🟡 Busy Auction (High Competition)  
- Many miners bidding
- Lower discount
- Still profitable if you're strategic

### 🔴 Bad Auction (Over-Subscribed)
- Too many miners bidding
- Late bidders destroy the discount for everyone
- Everyone gets worse deals

## 🚀 Getting Started

1. **Mine other subnets first** to get alpha tokens
2. **Register on SN73** (remember: only once per coldkey)
3. **Start with small bids** to learn the market
4. **Use the leaderboard** to study competition
5. **Scale up** as you get more confident

## 📋 Requirements

- Python 3.10+
- Alpha tokens from other subnets
- Configured btcli wallet
- Basic understanding of auctions

## 🔗 Resources

- [GitHub Repository](https://github.com/fx-integral/metahash/)
- [Bittensor Docs](https://docs.bittensor.com/)
- [Technical Specs](https://github.com/fx-integral/metahash/blob/main/docs/sn73-specs.md)

---

**💡 Pro Tip:** Start by running the leaderboard script to watch a few auctions before jumping in. Learn the patterns, then start bidding small amounts to get experience!