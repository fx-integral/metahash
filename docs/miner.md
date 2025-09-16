# MetaHash Miner Guide (Subnet 73)

## Should You Mine?

Before you spin up a miner in **Subnet 73 (SN73)**, ask yourself:

> **Do I want to sell OTC α-tokens from other subnets possibly at a loss?**
> Mining SN73 only makes sense if you hold surplus α-tokens from other subnets that you wish to liquidate at a DISCOUNT instead of impacting their on-chain liquidity pools.

**If YES** → Proceed with this guide!  
**If NO** → Mining SN73 won't add value for you.

---

## How Subnet 73 Mining Works

Each epoch (~1 hour // 361 blocks x 12s) an on-chain auction distributes **148 SN73 α-tokens** to miners, proportional to the total **τ-value** of α-tokens they supply from other subnets.


### Key Points
- You bid with α-tokens *from any subnet except 73*
- Your share of the 148 prize tokens is **proportional to your τ-value** at auction close
- The effective discount you get depends entirely on competition; more bidders → smaller discount

```
payout = (your τ-value / total τ-value) × 148 SN73 α
```

---

## How to Participate in Auctions

**Send α (alpha) from any subnet _except 73_** to the **Treasury Address**:

`5GW6xj5wUpLBz7jCNp38FzkdS6DfeFdUTuvUjcn6uKH5krsn`


You may do this **manually**, or by using the **mining tools provided** – which automate stake transfers and require access to an **unlocked coldkey**.

> **Important:**  
> These tools are provided _as reference only_. Use them **at your own risk and responsibility**. Miners are **fully responsible** for the security of their wallets and funds. If you choose to use or adapt the tools, **ensure you follow best practices for key management and operational security**.

### Security Recommendations

- Review and understand the code before use  
- Dont have all the funds on hot coldkeys use for mining
- Use airgapped or hardware-enforced setups whenever possible
- When using automtion scripts for bittensor use firewalled systems and inyect PASSWORD via environment variables or more advance SECRET handling systems.

Your security is paramount – treat your coldkeys with the same caution as your private bank credent

---

## Quick Start

1. **Decide** which subnet's α you want to sell  
2. **Install** the MetaHash tooling and dependencies  
3. **Register** your miner (one-time per `coldkey`)  
4. **Fund** the miner `coldkey` you registered with the α you intend to bid  
5. **Bid** manually or automate with the provided scripts

## Prerequisites
1. **Subtensor lite node with `--pruning=2000`** configured (this is needed for leaderboard or use Archive node (--network archive))
2. **Python 3.10+** installed
3. **pip/venv** for isolated environment


```bash
# Clone & install
git clone https://github.com/fx-integral/metahash.git && cd metahash
python3 -m venv .venv && source .venv/bin/activate
pip install uv && uv pip install -e .

#Install btcli followng https://docs.learnbittensor.org/getting-started/install-btcli
uv pip install bittensor-cli # Use latest or desired version

# One-time miner registration
btcli subnets register \
    --netuid 73 \
    --wallet.name YOUR_WALLET \
    --wallet.hotkey YOUR_HOTKEY
```

---

## Mining Tools

- Leaderboard:
```bash
python scripts/leaderboard.py --meta-netuid 73 --wallet.name YOUR_WALLET --wallet.hotkey YOUR_HOTKEY --network archive
```
- Automatic Bidder
```bash
python scripts/wallet_access/auction_watch.py --netuid SOURCE_SUBNET_ID --source-hotkey SOURCE_HOTKEY_ADDRESS --wallet.name YOUR_WALLET --wallet.hotkey YOUR_HOTKEY --max-alpha 100 --step-alpha 5 --max-discount 8
```

```bash
NOTE: "WALLET_PASSWORD" is an environment variable that can be used to automate wallet operations.  
```

### Auto-Bidder Workflow
- Starts bidding when a new auction opens
- Increases bids in step-alpha increments until reaching max-alpha or max-discount
- Stops automatically when the discount becomes unattractive

---

## Auction Mechanics

- **Frequency**: Every 361 blocks (~1 hour)
- **Prize Pool**: 148 SN73 α-tokens
- **Eligibility**: Any miner registered on SN73
- **Weighting**: Payouts proportional to each miner's τ-contribution

### Example

| Metric | Value |
|-----------|----------|
| Total τ-value | 100 α |
| Your bid | 20 α |
| Your share | 20% × 148 = **29.6 SN73 α** |

---

## Rules & Restrictions

| **Allowed** | **Forbidden** |
|----------------|------------------|
| α-tokens from any subnet except 73 | Sending SN73 α back into the auction |
| Multiple hotkeys per coldkey | YOu can register them but they will not receive incentive |

> **Goal**: Maximise the τ-value you send while paying the lowest discount.

---

## Typical Auction Scenarios

| Scenario | Indicators | Outcome |
|-------------|---------------|------------|
| **Low Competition** | Few miners, thin τ-value | Profit, high returns |
| **Moderate Competition** | Several miners, rising τ-value | Reduced but still positive profits for miners |
| **Over-Subscribed** | Many miners join late | Profit collapses; Discount on alpha sent. Expected equilibrium |

---

## Resources

- **GitHub**: https://github.com/fx-integral/metahash/
- **Bittensor Docs**: https://docs.bittensor.com/
- **Coldkey and Hotkey Workstation Security**: https://docs.learnbittensor.org/getting-started/coldkey-hotkey-security/
