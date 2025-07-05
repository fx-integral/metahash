# ⛏️ MetaHash Miner Guide (Subnet 73)

## 🤔 Should You Mine?

Before you spin up a miner in **Subnet 73 (SN73)**, ask yourself:

> 💰 **Do I want to sell OTC α-tokens from other subnets?**  
> Mining SN73 only makes sense if you hold surplus α-tokens from other subnets that you wish to liquidate at a discount instead of impacting their on-chain liquidity pools.

**✅ If YES** → Proceed with this guide!  
**❌ If NO** → Mining SN73 won't add value for you.

---

## ⚙️ How Subnet 73 Mining Works

Each epoch (~1 hour) an on-chain auction distributes **148 SN73 α-tokens** to miners, proportional to the total **τ-value** of α-tokens they supply from other subnets.

### 🔑 Key Points
- 🪙 You bid with α-tokens *from any subnet except 73*
- 📊 Your share of the 148 prize tokens is **proportional to your τ-value** at auction close
- 🏁 The effective discount you get depends entirely on competition; more bidders → smaller discount

```
💰 payout = (your τ-value / total τ-value) × 148 SN73 α
```

---

## 🚀 Quick Start

1. **🎯 Decide** which subnet's α you want to sell  
2. **📥 Install** the MetaHash tooling and dependencies  
3. **📝 Register** your miner (one-time per `coldkey`)  
4. **💰 Fund** the same `coldkey` with the α you intend to bid  
5. **🎲 Bid** manually or automate with the provided scripts

```bash
# 📂 Clone & install
git clone https://github.com/fx-integral/metahash.git && cd metahash
python3 -m venv .venv && source .venv/bin/activate
pip install uv && uv pip install -e .

#Install btcli followng https://docs.learnbittensor.org/getting-started/install-btcli
pip install bittensor-cli # Use latest or desired version

# 🔐 One-time miner registration
btcli s register \
    --netuid 73 \
    --wallet.name YOUR_WALLET \
    --wallet.hotkey YOUR_HOTKEY
```

---

## 🔧 Mining Tools

| 🛠️ Tool | 📋 Purpose | 💻 Example |
|---------|------------|------------|
| **📊 Leaderboard** | Monitor current and historical winners | `python scripts/miner/leaderboard.py --meta-netuid 73 --wallet.name YOUR_WALLET --wallet.hotkey YOUR_HOTKEY` |
| **🤖 Auto-Bidder** | Automatically watch auctions and place incremental bids while respecting a minimum discount | `python scripts/miner/auction_watch.py --netuid SOURCE_SUBNET_ID --validator-hotkey VALIDATOR_HOTKEY_ADDRESS --wallet.name YOUR_WALLET --wallet.hotkey YOUR_HOTKEY --max-alpha 100 --step-alpha 5 --max-discount 8` |

### 🤖 Auto-Bidder Workflow
- ▶️ Starts bidding when a new auction opens
- 📈 Increases bids in step-alpha increments until reaching max-alpha or max-discount
- 🛑 Stops automatically when the discount becomes unattractive

---

## 🎯 Auction Mechanics

- **⏰ Frequency**: Every 361 blocks (~1 hour)
- **🏆 Prize Pool**: 148 SN73 α-tokens
- **✅ Eligibility**: Any miner registered on SN73
- **⚖️ Weighting**: Payouts proportional to each miner's τ-contribution

### 📊 Example

| 📈 Metric | 💰 Value |
|-----------|----------|
| Total τ-value | 100 α |
| Your bid | 20 α |
| Your share | 20% × 148 = **29.6 SN73 α** |

---

## 📜 Rules & Restrictions

| ✅ **Allowed** | ❌ **Forbidden** |
|----------------|------------------|
| 🪙 α-tokens from any subnet except 73 | 🚫 Sending SN73 α back into the auction |
| 🔄 Multiple concurrent auctions | 🚫 More than one registration per coldkey |
| 🤖 Automation and custom scripts | — |

> 🎯 **Goal**: Maximise the τ-value you send while paying the lowest discount.

---

## 🏆 Winning Strategies

### ⚡ Be Fast
- 🚀 Bid early to lock higher discounts
- 🤖 Automate to stay ahead of manual competitors

### 🧠 Be Smart
- 🎯 Define a minimum acceptable discount and step-alpha to avoid over-bidding
- 👀 Monitor the leaderboard before each auction to gauge competition
- 💡 Only bid surplus α to avoid harming your main subnet

### 🛡️ Be Safe
- 🛑 Abort when an auction becomes over-subscribed—late bids can dilute everyone's discount
- 📊 Track your ROI across multiple epochs; refine parameters gradually
- 🐣 Start small; scale after several successful runs

---

## 📊 Typical Auction Scenarios

| 🎯 Scenario | 🔍 Indicators | 📈 Outcome |
|-------------|---------------|------------|
| 🟢 **Low Competition** | 👥 Few miners, thin τ-value | 💰 Deep discount, high returns |
| 🟡 **Moderate Competition** | 👥👥 Several miners, rising τ-value | 📊 Reduced but still positive discount |
| 🔴 **Over-Subscribed** | 👥👥👥 Many miners join late | 💸 Discount collapses; you may earn nothing |

---

## ✅ Getting Started Checklist

- [ ] 🪙 Acquire α-tokens on other subnets
- [ ] 👛 Prepare a wallet (wallet.name) and hotkey (wallet.hotkey)
- [ ] 📝 Register on SN73 once
- [ ] 👀 Observe several auctions via the leaderboard
- [ ] ⚙️ Configure and dry-run the auto-bidder
- [ ] 📈 Scale up bids as confidence grows

---

## 📋 Requirements

- 🐍 Python ≥ 3.10
- 👛 btcli wallet set up
- 🪙 α-tokens from subnets other than 73
- 🧠 Basic understanding of Dutch/weighted auctions

---

## 🔗 Resources

- 📁 **GitHub**: https://github.com/fx-integral/metahash/
- 📚 **Bittensor Docs**: https://docs.bittensor.com/
- 📋 **SN73 Technical Specs**: https://github.com/fx-integral/metahash/blob/main/docs/sn73-specs.md