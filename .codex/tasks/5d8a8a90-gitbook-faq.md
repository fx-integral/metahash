# Task: Build living MetaHash FAQ page for Gitbook

> **Auditor Note (2024-05-23):** added explicit α-transfer source coverage so the FAQ can quote both miner payment behavior and validator-side enforcement when answering the “transfer” phase questions.

## Preparation
- Skim the same core sources as the overview task so terminology, constants, and tone stay aligned between the two Gitbook pages:
  - Baseline messaging and participant responsibilities in `README.md`, `docs/miner.md`, and `docs/validator.md`. Keep notes on any discrepancies or stale references for future follow-up tasks.【F:README.md†L13-L67】【F:docs/miner.md†L1-L136】【F:docs/validator.md†L1-L168】
  - Auction lifecycle implementation (auction → bids → clearing → payment windows → α transfers → settlement/incentive assignment) in the validator engines and shared neuron helpers.【F:neurons/validator.py†L1-L205】【F:metahash/validator/engines/auction.py†L1-L438】【F:metahash/validator/engines/clearing.py†L1-L349】【F:metahash/validator/engines/settlement.py†L1-L189】【F:metahash/validator/alpha_transfers.py†L1-L200】
  - Miner-side payment automation to anchor FAQs about invoices, α transfer proofs, and error handling.【F:neurons/miner.py†L1-L320】
  - Config toggles that readers frequently ask about (strict settlement mode, subnet limits, testing utilities).【F:metahash/config.py†L1-L147】
- Cross-check the latest subnet announcements (begin with <https://x.com/MetaHashSn73/status/1972740673368727600>) so FAQ answers stay aligned with public messaging. If you cannot retrieve everything from the live thread, flag the missing details in a TODO block so future contributors can integrate feedback once community members supply it.

## Goal
Produce a modular FAQ document that we can keep updating as community feedback arrives. The FAQ should answer core onboarding questions, clarify edge cases from the auction/settlement code, and surface operational guardrails for miners and validators.

## Scope & Requirements
- **File location**: create `docs/gitbook/faq.md` beside the new overview page.
- **Audience**: newcomers (dTAO holders, miners, validators) plus technically curious readers. Keep answers concise with expandable sections (use Gitbook-friendly headings like `### Question` + bullet answers).
- **Content sources**:
  - Role/value summaries in `README.md` and `docs/miner.md` / `docs/validator.md` for baseline messaging.【F:README.md†L13-L57】【F:docs/miner.md†L1-L89】【F:docs/validator.md†L1-L126】
  - Auction + settlement behaviors from validator engines to explain timing, payment windows, burns, incentive assignment, and reputation caps.【F:neurons/validator.py†L1-L205】【F:metahash/validator/engines/auction.py†L1-L438】【F:metahash/validator/engines/clearing.py†L1-L349】【F:metahash/validator/engines/settlement.py†L1-L189】
  - Miner payment orchestration and validator α-transfer scanning so “transfer” answers cite the exact enforcement hooks.【F:neurons/miner.py†L169-L215】【F:metahash/validator/alpha_transfers.py†L1-L200】
  - Configuration switches that power FAQs about TESTING mode, strict per-subnet enforcement, budget sizing, etc.【F:metahash/config.py†L1-L147】
  - Wallet/payment handling from the miner implementation for user-facing troubleshooting.【F:neurons/miner.py†L1-L320】
- **Sections to cover (add others if useful):**
  1. “Quick facts” — What MetaHash does, supported assets, where data is published.
  2. “Auction & Bidding” — Who can bid, how discounts work, why bids may be rejected.
  3. “Payments & Settlement” — When invoices arrive, paying per-subnet, burns to UID 0, impact of late/missing payments, and how validator scanners detect α transfers.
  4. “Validator Operations” — Master validator requirements, commitment publishing rules, IPFS expectations.
  5. “Safety & Troubleshooting” — Treasury whitelists, jail conditions, and guidance on where to seek clarified messaging before referencing external announcements.
- **Linking & maintainability**: cross-reference the overview page for deep dives and leave clearly marked TODOs where additional community Q&A should be added.
- **Style**: use conversational questions (“How do I know my payment counted?”) with answers that point to specific constants or modules when relevant.

## Deliverables
- `docs/gitbook/faq.md` with organized sections, ready for Gitbook import.
- Inline TODO comments or callouts identifying where future feedback should be merged (e.g., `> TODO: add validator rewards example once data available`).

## Acceptance Criteria
- All claims cite or link to a verifiable source within the repo.
- FAQ addresses both high-level understanding and practical operational questions (payments, reputation caps, strict settlement policy).
- FAQ explicitly anchors answers around the four requested phases (auction start, bids, transfer/settlement, incentive assignment) so contributors can verify coverage quickly.
- Formatting avoids raw HTML; rely on Gitbook-friendly Markdown only.

## Working Checklist (keep in the PR description)
1. [ ] Capture at least three “Quick facts” entries that summarize MetaHash in simplified language while linking to underlying modules/constants.
2. [ ] Write FAQ entries for each phase: auction start, bids/clearing, α transfers/payments, settlement/incentive assignment, and ensure each cites the appropriate validator/miner modules.
3. [ ] Include maintenance guidance (e.g., `> TODO` blocks) wherever feedback from the community or live announcements is still pending so the FAQ stays easy to update collectively.
4. [ ] Validate Markdown structure with `markdownlint` or a Gitbook preview to confirm collapsible sections/headings render as expected.
5. [ ] Document any unresolved questions or items needing upstream clarification directly in the FAQ so subsequent editors can follow up quickly.
