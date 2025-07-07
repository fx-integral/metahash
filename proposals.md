## 📈 Proposals for the Future

---
### 🧾 Proposal 1: Off-Chain Buy-Offer Sync (Miner ↔ Validator)

**Pain point**  
On-chain bidding is noisy, unpredictable, and vulnerable to frontrunning.

**Proposed fix**  
Introduce a lightweight buy-offer protocol over HTTP:

1. **Validators broadcast** signed SN73 buy quotes (price, size, expiry).  
2. **Miners respond** off-chain with signed $ALPHA sell offers.  
3. **Validator matches & seals** the first compatible offer, records the match on-chain, and lock the deal for **N** blocks.  
4. **Auto-expire:** If settlement isn’t confirmed with the alpha transaction within **N** blocks, the lock dissolves and both parties may re-bid.

**Advantages**

- **Predictable pricing** — eliminates gas wars and slippage.  
- **Front-running resistance** — signatures are revealed only after the lock.  
- **Better UX** — simple HTTP endpoints integrate easily with bots and dashboards.

---

### 💼 Proposal 2: Validators as Hedge Funds

**Problem:** No current strategic asset selection exists.

**Solution:** Empower validators to manage 'adquisition' logic.

#### Architecture:
- Validators > S_MIN stake get **treasury coldkeys**
- Treasury is **not controlled**, only managed via on-chain logic
- Validators configure:
  - Subnet whitelist/blacklist
  - Discount curves
  - Lock periods or advanced incentives

#### Validator Mini-Auctions

Each validator has a budget:

```math
Validator Budget = (361 × 0.41) × (Validator Stake / Total Stake)
```

- Miners **route alpha** based on validator performance
- Validators compete for **high-quality alpha streams**
- Alpha stake flows to the most **effective validators**
- Validators validate auctions for every other validator


### 📋 What Validators Can Control

- **Subnet Selection:** Prefer or exclude specific subnets
- **Dynamic Pricing:** Customize bonding curve per subnet
- **Deal Terms:** Lock periods, discount tiers, bonus incentives

### 💰 Why Validators Care

Validators are rewarded by:

- **Performance-Based Staking:** More efficient = more stake
- **Management Fees:** Take a cut of treasury profits
- **Capital Control:** Choose how to use earned alpha


## 🫂 Aligning Holders with Performance

**Problem:** No incentive for holders to choose top validators

**Solution:** Profit-sharing model with SN73 holders

```math
Shared with Holders = Treasury Profits × (1 - Validator Fee) × Holder Share
```

> Remaining profits are used for SN73 buybacks.

- Aligns validator effort with network value
- Encourages long-term performance-focused behavior
