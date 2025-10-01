# Task: Draft Gitbook-friendly MetaHash overview & auction flow explainer

> **Auditor Note (2024-05-23):** tightened source mapping for the role snapshots so miners/validators pull from their dedicated guides instead of overloading the README excerpt. Also called out the α transfer execution layer to keep “transfer → incentive assignment” traceable to both miner and validator code paths.

## Preparation
- Re-read the primary protocol references so we keep the “simplified” tone without drifting away from the actual implementation details:
  - Core messaging and subsystem hooks from `README.md`, `docs/miner.md`, and `docs/validator.md` to keep every persona grounded in the latest guidance.【F:README.md†L19-L67】【F:docs/miner.md†L35-L136】【F:docs/validator.md†L16-L168】
  - Validator execution pipeline and state transitions in the auction, clearing, settlement, and α-transfer modules so each auction milestone is backed by code.【F:metahash/validator/engines/auction.py†L1-L438】【F:metahash/validator/engines/clearing.py†L1-L349】【F:metahash/validator/engines/settlement.py†L1-L189】【F:metahash/validator/alpha_transfers.py†L1-L200】【F:neurons/validator.py†L1-L205】
  - Miner-side payment duties and wallet automation to explain how α transfers are initiated and reconciled.【F:neurons/miner.py†L169-L320】
- Skim the latest MetaHash subnet announcements (start with <https://x.com/MetaHashSn73/status/1972740673368727600>) so messaging stays consistent with public updates. If you cannot retrieve the full post, log the missing context in your task notes and highlight it in the document’s TODO section for future editors to incorporate once feedback arrives.

## Goal
Create a concise, Gitbook-ready explainer that introduces MetaHash (Subnet 73) to new readers and walks through the end-to-end auction lifecycle (auction start → bid submission → validator clearing → payment window → α transfer execution → settlement/incentive assignment). The document should be easy to skim, written in accessible language, and grounded in the actual code paths so technical readers can trust it while still feeling like a simplified guide for non-experts.

## Scope & Requirements
- **File location**: add a new markdown page under `docs/gitbook/overview.md` (create the folder if needed).
- **Structure**: use Gitbook-friendly formatting (`#` headings, bullet lists, callouts/quotes, and optional tables). Provide:
  - A short executive summary answering “What is MetaHash?” in simplified language before layering in protocol specifics.
  - Role-based snapshots (dTAO holders, miners, validators, subnet owners) explaining how each benefits, drawing from the overview plus the miner/validator field guides so every audience segment has code-backed context.【F:README.md†L19-L44】【F:docs/miner.md†L35-L118】【F:docs/validator.md†L16-L136】
  - A step-by-step auction flow section that mirrors the validator pipeline: auction start broadcast, bid validation (`AuctionEngine`), clearing (`ClearingEngine`), payment windows/invoices (`WinSynapse`), **α transfer execution** (miner payment scheduling + validator-side scanning), and settlement & weight updates (`SettlementEngine`). Highlight where commitments/IPFS fit in.【F:neurons/validator.py†L1-L205】【F:metahash/validator/engines/auction.py†L1-L438】【F:metahash/validator/engines/clearing.py†L1-L349】【F:metahash/validator/engines/settlement.py†L1-L189】【F:metahash/validator/alpha_transfers.py†L1-L200】【F:neurons/miner.py†L169-L320】
  - A diagram-friendly outline or numbered checklist that downstream editors can easily convert into visuals.
- **Clarity**: translate protocol terms (e.g., α planck, μTAO, reputation caps) into plain language while linking back to the specific modules/constants (`metahash/config.py`, `metahash/protocol.py`). Include short glossary bullets for recurring jargon.【F:metahash/config.py†L1-L111】【F:metahash/protocol.py†L1-L74】
- **Cross-links**: add relative links to existing guides (`docs/miner.md`, `docs/validator.md`) where readers can dive deeper.
- **Alignment**: explicitly confirm that the overview covers the four phases the team requested (auction start, bids, transfer/settlement, incentive assignment) and call out any assumptions that need future validation.
- **Feedback loop**: dedicate a short “How to iterate” subsection that tells future editors where to capture crowd feedback (e.g., Gitbook comments, Discord threads) so the simplified overview remains easy to update.

## Working Checklist (keep in the PR description)
1. [ ] Draft high-level summary (“What is MetaHash?”) in simplified language referencing concrete modules/constants for validation.
2. [ ] Complete persona snapshots for dTAO holders, miners, validators, and subnet owners with clear links back to their source files.
3. [ ] Walk through the entire auction lifecycle with subsections that align to auction start → bids → clearing → payment window → α transfer execution → settlement/incentive assignment.
4. [ ] Add inline TODOs for any messaging gaps discovered while reviewing the live announcements or community feedback threads so later editors can resolve them quickly.
5. [ ] Run `markdownlint` (or `uv run python -m markdown_it` preview) locally to ensure Gitbook-friendly formatting and no trailing whitespace.
6. [ ] Note any open questions or assumptions directly in the document for future editors.

## Deliverables
- `docs/gitbook/overview.md` containing the new explainer, written in an inviting but accurate tone.
- Any supporting assets (e.g., temporary diagrams) can be referenced but do **not** embed binary files yet; just leave TODO callouts.

## Acceptance Criteria
- Markdown renders cleanly in Gitbook (no raw HTML required).
- Every major protocol phase cites the source module(s) or docs so maintainers can verify facts quickly.
- Terminology is consistent with the repo (e.g., “Win invoice”, “α transfers”, “budget burn to UID 0”).
- Document clearly flags any open questions or assumptions so future editors can validate messaging before publishing.
