#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
btv-health.sh  —  Summarize Bittensor validator logs by epoch (health + payments)

Usage:
  btv-health.sh [-n N] [--detailed] [-f LOGFILE]
  tail -n 5000 validator.log | btv-health.sh -n 5 --detailed

Options:
  -n, --epochs N     Number of last epochs to summarize (default: 5)
  -f, --file FILE    Read logs from FILE (default: STDIN)
  --detailed         Add a per-epoch detailed report after the summary
  -h, --help         Show this help

What it adds (payments):
  • From “Invoices — α to pay (ACCEPTED)”: α expected per miner and total VALUE (TAO)
  • From settlement scan (“Scanner result” / “Paid α pools” & “Budget Accounting — settlement”):
      - α actually paid (RAO→α)
      - credited VALUE (TAO)
  • Flags miners who did NOT pay (or only partially paid) during the payment window
USAGE
}

# --- args ---
N=5
DETAIL=0
INPUT="/dev/stdin"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--epochs) N="${2:?}"; shift 2 ;;
    -f|--file) INPUT="${2:?}"; shift 2 ;;
    --detailed) DETAIL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

# --- main ---
awk -v wantN="$N" -v detailed="$DETAIL" '
function trim(s) { gsub(/^[[:space:]]+|[[:space:]]+$/,"",s); return s }
function toAlpha(rao) { return rao/1000000000.0 }

BEGIN {
  PROCINFO["sorted_in"] = "cmp_num_asc"
  nOrder = 0
  set_weights_testing = 0

  current_e = ""
  auction_e = ""; budget_e = ""; winners_e = ""; settle_e = ""
  in_winners = 0
  in_budget_block = 0
  in_invoices = 0
}

function add_epoch(e){
  if (e == "" ) return
  if (!(e in seen_e)) { seen_e[e]=1; order[++nOrder]=e }
  current_e = e
}

# --- epoch detection ---
/\[epoch[[:space:]]+[0-9]+/ {
  if (match($0, /\[epoch[[:space:]]+([0-9]+)/, m)) add_epoch(m[1])
}
$0 ~ /Epoch[[:space:]]+[0-9]+[[:space:]]+\(label: e\)/ {
  if (match($0, /Epoch[[:space:]]+([0-9]+)/, m)) add_epoch(m[1])
}

# --- status info (head/start/end blocks) ---
$0 ~ /head_block=[0-9]+.*start=[0-9]+.*end=[0-9]+/ {
  if (current_e != "" && match($0, /head_block=([0-9]+).*start=([0-9]+).*end=([0-9]+)/, m)) {
    head_block[current_e] = m[1]
    start_block[current_e] = m[2]
    end_block[current_e]   = m[3]
  }
}

# --- Axon filter (reachability) ---
$0 ~ /Axon filter/ { /* header only */ }
/^[[:space:]]*received:[[:space:]]*[0-9]+/ {
  if (current_e != "" && match($0, /received:[[:space:]]*([0-9]+)/, m)) ax_rcv[current_e] = m[1]
}
/^[[:space:]]*usable:[[:space:]]*[0-9]+/ {
  if (current_e != "" && match($0, /usable:[[:space:]]*([0-9]+)/, m)) ax_usable[current_e] = m[1]
}

# --- AuctionStart (acks) ---
$0 ~ /AuctionStart Broadcast/ { /* header only */ }
$0 ~ /e \(now\):[[:space:]]*[0-9]+/ {
  if (match($0, /e \(now\):[[:space:]]*([0-9]+)/, m)) { auction_e = m[1]; add_epoch(auction_e) }
}
$0 ~ /acks_received:[[:space:]]*[0-9]+\/[0-9]+/ {
  if (match($0, /acks_received:[[:space:]]*([0-9]+)\/([0-9]+)/, m)) {
    e = (auction_e != "" ? auction_e : current_e)
    acks_recv[e] += m[1]; acks_total[e] += m[2]
  }
}

# --- Budget block (price & my budget) ---
/Budget \(VALUE.*base subnet/ { in_budget_block = 1 }
in_budget_block && $0 ~ /epoch:[[:space:]]*[0-9]+/ {
  if (match($0, /epoch:[[:space:]]*([0-9]+)/, m)) { budget_e = m[1]; add_epoch(budget_e) }
}
in_budget_block && $0 ~ /base_price_tao\/α:[[:space:]]*[0-9.]+/ {
  if (budget_e != "" && match($0, /base_price_tao\/α:[[:space:]]*([0-9.]+)/, m)) base_price[budget_e] = m[1]
}
in_budget_block && $0 ~ /my_budget_tao \(VALUE\):[[:space:]]*[0-9.]+/ {
  if (budget_e != "" && match($0, /my_budget_tao \(VALUE\):[[:space:]]*([0-9.]+)/, m)) my_budget[budget_e] = m[1]
}
in_budget_block && ($0 ~ /^╰/ || $0 ~ /^$/ || $0 ~ /Connecting to Substrate/) { in_budget_block = 0 }

# --- Budget leftover after allocation ---
/Budget leftover .* after allocation:/ {
  if (match($0, /Budget leftover .*: ([0-9.]+)/, m)) {
    e = (budget_e != "" ? budget_e : current_e)
    budget_leftover[e] = m[1]
  }
}

# --- Winners (VALUE) table ---
/Winners — acceptances .*VALUE/ {
  in_winners = 1
  winners_e = (budget_e != "" ? budget_e : (auction_e != "" ? auction_e : current_e))
  if (winners_e == "") winners_e = current_e
}
in_winners {
  if (match($0, /([0-9]+\.[0-9]+)[[:space:]]*TAO\(VALUE\)/, m)) {
    winners_value[winners_e] += m[1] + 0
    winners_count[winners_e] += 1
  }
  if ($0 ~ /^╰/ || $0 ~ /^$/ || $0 ~ /Staged commit payload/ || $0 ~ /Invoices —/) in_winners = 0
}

# --- Early clear (winners + win acks) ---
/Early Clear & Notify .*epoch e/ { /* header only */ }
$0 ~ /winners:[[:space:]]*[0-9]+/ {
  if (match($0, /winners:[[:space:]]*([0-9]+)/, m)) {
    e = (budget_e != "" ? budget_e : (auction_e != "" ? auction_e : current_e))
    early_winners[e] = m[1]
  }
}
$0 ~ /win_acks:[[:space:]]*[0-9]+\/[0-9]+/ {
  if (match($0, /win_acks:[[:space:]]*([0-9]+)\/([0-9]+)/, m)) {
    e = (budget_e != "" ? budget_e : (auction_e != "" ? auction_e : current_e))
    winacks_recv[e] += m[1]; winacks_total[e] += m[2]
  }
}

# --- Invoices — α to pay (ACCEPTED) ---
/Invoices — α to pay \(ACCEPTED\)/ { in_invoices = 1; invoice_e = (budget_e != "" ? budget_e : current_e); add_epoch(invoice_e) }
in_invoices && /╰/ { in_invoices = 0 }
in_invoices && $0 ~ /[0-9][[:space:]]*│/ && $0 !~ /UID[[:space:]]*│/ {
  # Split by box pipes to get columns safely
  n = split($0, col, /│/)
  # Expected column layout:
  # col[2]= UID, col[3]= CK, col[4]= Subnet, col[5]= α to pay, col[6]= price TAO/α, col[7]= Total VALUE (TAO), ...
  ck = trim(col[3]); gsub(/…/,"", ck); gsub(/[[:space:]]/,"", ck)  # truncated prefix sans ellipsis
  alpha_str = trim(col[5]); sub(/[[:space:]]*α.*/,"", alpha_str)
  val_str   = trim(col[7]); sub(/[[:space:]]*TAO.*/,"", val_str)
  if (alpha_str != "" && ck != "") {
    a = alpha_str + 0
    invoices_alpha_rao[invoice_e] += int(a * 1000000000.0 + 0.5)
    inv_by_ck[invoice_e, ck] += int(a * 1000000000.0 + 0.5)
  }
  if (val_str != "") {
    invoices_value_tao[invoice_e] += (val_str + 0)
  }
}

# --- Staged commit payload / preview (has_inv) ---
/Staged commit payload/ { /* header only */ }
$0 ~ /^[[:space:]]*e:[[:space:]]*[0-9]+/ { if (match($0,/e:[[:space:]]*([0-9]+)/,m)) { staged_e=m[1]; add_epoch(staged_e) } }
$0 ~ /#miners:[[:space:]]*[0-9]+/  { if (staged_e!="" && match($0,/#miners:[[:space:]]*([0-9]+)/,m)) staged_miners[staged_e]=m[1] }
$0 ~ /#lines_total:[[:space:]]*[0-9]+/ { if (staged_e!="" && match($0,/#[^:]*:[[:space:]]*([0-9]+)/,m)) staged_lines[staged_e]=m[1] }

/Commit Payload \(preview\)/ { /* header only */ }
$0 ~ /epoch:[[:space:]]*[0-9]+/ && $0 ~ /has_inv:/ {
  if (match($0, /epoch:[[:space:]]*([0-9]+)/, a) && match($0, /has_inv:[[:space:]]*(true|false)/, b)) {
    e = a[1]; commit_has_inv[e] = b[1]
  }
}

# --- Commitment Published (cid) ---
/Commitment Published/ { /* header only */ }
$0 ~ /epoch_cleared:[[:space:]]*[0-9]+/ { if (match($0,/epoch_cleared:[[:space:]]*([0-9]+)/,m)) { e=m[1]; commit_published[e]=1 } }
$0 ~ /cid:[[:space:]]*bafk/ { if (e!="" && match($0,/cid:[[:space:]]*([a-z0-9]+)/,m)) commit_cid[e]=m[1] }

# --- Settlement begin/complete ---
/Settlement for epoch[[:space:]]*[0-9]+/ { if (match($0,/Settlement for epoch[[:space:]]*([0-9]+)/,m)) { settle_e=m[1]; add_epoch(settle_e) } }
$0 ~ /Settlement Complete/ && /epoch_settled .*:[[:space:]]*[0-9]+/ {
  if (match($0,/epoch_settled .*:[[:space:]]*([0-9]+)/,m)) {
    e=m[1]; settled[e]=1
    if (match($0,/miners_scored:[[:space:]]*([0-9]+)/,mm)) miners_scored[e]=mm[1]
  }
}

# --- Weights preview/summary (for settlement epoch) ---
/mode=[a-zA-Z-]+/ { if (settle_e!="" && match($0,/mode=([a-zA-Z-]+)/,m)) weights_mode[settle_e]=m[1] }
$0 ~ /nonzero:[[:space:]]*[0-9]+/ { if (settle_e!="" && match($0,/nonzero:[[:space:]]*([0-9]+)/,m)) weights_nonzero[settle_e]=m[1] }
$0 ~ /max:[[:space:]]*[0-9.]+/    { if (settle_e!="" && match($0,/max:[[:space:]]*([0-9.]+)/,m))     weights_max[settle_e]=m[1] }
$0 ~ /sum\(scores\):[[:space:]]*[0-9.]+/ { if (settle_e!="" && match($0,/sum\(scores\):[[:space:]]*([0-9.]+)/,m)) sum_scores[settle_e]=m[1] }

# --- Budget Accounting — settlement (VALUE paid/credited/burn) ---
/Budget Accounting — settlement/ { /* header only */ }
$0 ~ /spent_from_payload .*:[[:space:]]*[0-9.]+/      { if (settle_e!="" && match($0,/spent_from_payload .*:[[:space:]]*([0-9.]+)/,m)) spent_payload[settle_e]=m[1] }
$0 ~ /leftover_from_payload .*:[[:space:]]*[0-9.]+/   { if (settle_e!="" && match($0,/leftover_from_payload .*:[[:space:]]*([0-9.]+)/,m)) leftover_payload[settle_e]=m[1] }
$0 ~ /target_budget .*:[[:space:]]*[0-9.]+/           { if (settle_e!="" && match($0,/target_budget .*:[[:space:]]*([0-9.]+)/,m)) target_budget[settle_e]=m[1] }
$0 ~ /credited_value .*:[[:space:]]*[0-9.]+/          { if (settle_e!="" && match($0,/credited_value .*:[[:space:]]*([0-9.]+)/,m)) credited_value[settle_e]=m[1] }
$0 ~ /burn_deficit .*:[[:space:]]*[0-9.]+/            { if (settle_e!="" && match($0,/burn_deficit .*:[[:space:]]*([0-9.]+)/,m)) burn_deficit[settle_e]=m[1] }

# --- α transfer scan (events) — sum RAO by epoch and by src_ck prefix ---
/events\(sample\):/ {
  if (settle_e == "") next
  # Normalize into one event per token: replace "},{"
  copy=$0
  gsub(/\},[[:space:]]*\{/, "}\n{", copy)
  n = split(copy, parts, /\n/)
  for (i=1;i<=n;i++){
    ev=parts[i]
    ck=""
    if (match(ev, /"src_ck":"([A-Za-z0-9]+)/, mck)) ck=mck[1]
    if (match(ev, /amt\(rao\)":([0-9]+)/, ma)){
      amt = ma[1] + 0
      alpha_paid_rao[settle_e] += amt
      if (ck!="") alpha_paid_by_ck[settle_e, ck] += amt
    }
  }
}

# --- Paid α pools (JSON-ish sample) — also capture per-ck paid RAO ---
/paid_pools\(sample\):/ {
  if (settle_e == "") next
  s=$0
  # Break objects
  gsub(/\},[[:space:]]*\{/, "}\n{", s)
  n=split(s, parts, /\n/)
  for (i=1;i<=n;i++){
    p=parts[i]
    if (match(p, /"ck":"([A-Za-z0-9]+)/, mck)) ck=mck[1]; else ck=""
    if (match(p, /paid_rao":([0-9]+)/, mp)) pr=mp[1]+0; else pr=0
    if (pr>0){
      alpha_paid_rao[settle_e] += pr
      if (ck!="") alpha_paid_by_ck[settle_e, ck] += pr
    }
  }
}

# --- Miner credit table (optional hint: OK vs not) ---
/Miner credit .*Required .*VALUE sum/ { /* header only */ }
$0 ~ /^[[:space:]]*[0-9]+[[:space:]]*│/ && $0 ~ /Credit\?/ {
  # Try to parse: UID │ Coldkey │ Gate │ α_required(rao) │ α_alloc(rao?) │ Credit? │ VALUE_sum(TAO)
  n=split($0, col, /│/); ck=trim(col[2]); gsub(/…/,"",ck); gsub(/[[:space:]]/,"",ck)
  credit=trim(col[6])
  if (settle_e!="" && ck!="") credit_status[settle_e, ck]=credit
}

# --- Error counters ---
/Cannot connect to host/ {
  e = (auction_e!="" ? auction_e : (budget_e!="" ? budget_e : current_e))
  conn_err_total++; if (e!="") conn_err[e]++
}
/TimeoutError#/ {
  e = (auction_e!="" ? auction_e : (budget_e!="" ? budget_e : current_e))
  timeout_total++; if (e!="") timeouts[e]++
}

# --- END: compute & print ---
END {
  if (nOrder == 0) { print "No epochs found in input."; exit 1 }

  # Choose last N epochs encountered
  start = (nOrder - wantN + 1); if (start < 1) start = 1
  lastCnt = 0
  for (i = start; i <= nOrder; i++) { pick[ order[i] ] = 1; last[++lastCnt] = order[i] }

  # Aggregates
  agg_epochs = lastCnt
  agg_commits = agg_settled = agg_winner_epochs = agg_winners = 0
  agg_value = agg_budget = agg_leftover = 0
  agg_ack_recv = agg_ack_total = 0
  agg_winack_recv = agg_winack_total = 0
  agg_reach_sum = agg_reach_den = 0
  w_normal = w_burn = w_other = 0
  agg_conn = agg_timeo = 0
  # Payments (matured = settled epochs only)
  mat_epochs = 0
  mat_value_expected = mat_value_credited = 0.0
  mat_alpha_expected_rao = mat_alpha_paid_rao = 0
  mat_unpaid_invoices = 0
  epochs_with_unpaid = 0

  for (j = 1; j <= lastCnt; j++) {
    e = last[j]

    if (commit_published[e]) agg_commits++
    if (settled[e]) agg_settled++

    if (winners_count[e] + 0 > 0) agg_winner_epochs++
    agg_winners += (winners_count[e] + 0)
    agg_value   += (winners_value[e] + 0)

    agg_budget   += (my_budget[e] + 0)
    agg_leftover += (budget_leftover[e] + 0)

    agg_ack_recv  += (acks_recv[e]  + 0)
    agg_ack_total += (acks_total[e] + 0)

    agg_winack_recv  += (winacks_recv[e]  + 0)
    agg_winack_total += (winacks_total[e] + 0)

    if ((ax_rcv[e] + 0) > 0) { agg_reach_sum += (ax_usable[e] + 0); agg_reach_den += (ax_rcv[e] + 0) }

    if (weights_mode[e] == "normal") w_normal++
    else if (weights_mode[e] == "burn-all") w_burn++
    else if (weights_mode[e] != "") w_other++

    agg_conn  += (conn_err[e] + 0)
    agg_timeo += (timeouts[e] + 0)

    # Payments: use settlement epoch metrics (matured only)
    if (settled[e]) {
      mat_epochs++
      mat_value_expected += (spent_payload[e] + 0)
      mat_value_credited += (credited_value[e] + 0)
      mat_alpha_expected_rao += (invoices_alpha_rao[e] + 0)
      mat_alpha_paid_rao     += (alpha_paid_rao[e] + 0)

      # Non-payers per epoch (compare per-ck expected vs paid)
      unpaid_here = 0
      # Build a set of ck that had invoices at this epoch
      for (k in inv_by_ck) {
        split(k, K, SUBSEP); ek = K[1]; ck = K[2]
        if (ek != e) continue
        exp_rao = inv_by_ck[ek, ck] + 0
        paid_rao = alpha_paid_by_ck[ek, ck] + 0
        if (paid_rao + 0 < exp_rao) { unpaid_here++; unpaid_ck[e, ck] = exp_rao - paid_rao }
      }
      mat_unpaid_invoices += unpaid_here
      if (unpaid_here > 0) epochs_with_unpaid++
    }
  }

  ack_rate    = (agg_ack_total    > 0 ? 100.0 * agg_ack_recv    / agg_ack_total    : 0)
  winack_rate = (agg_winack_total > 0 ? 100.0 * agg_winack_recv / agg_winack_total : 0)
  reach_rate  = (agg_reach_den    > 0 ? 100.0 * agg_reach_sum   / agg_reach_den    : 0)
  budget_used = (agg_budget       > 0 ? 100.0 * (agg_budget - agg_leftover) / agg_budget : 0)

  value_shortfall = mat_value_expected - mat_value_credited
  if (value_shortfall < 0) value_shortfall = 0
  alpha_shortfall_rao = (mat_alpha_expected_rao - mat_alpha_paid_rao); if (alpha_shortfall_rao < 0) alpha_shortfall_rao = 0

  # --- SUMMARY ---
  fmt="%-34s %s\n"
  line="--------------------------------------------------------------------"
  printf("%s\n", line)
  printf("Validator Health (last %d epochs: %s..%s)\n", agg_epochs, last[1], last[lastCnt])
  printf("%s\n", line)

  printf(fmt, "Epochs analyzed:", agg_epochs)
  printf(fmt, "Commitments published:", sprintf("%d (%.1f%%)", agg_commits, 100.0*agg_commits/agg_epochs))
  printf(fmt, "Settlements completed:", sprintf("%d (%.1f%%)", agg_settled, 100.0*agg_settled/agg_epochs))

  printf(fmt, "Epochs with winners:", sprintf("%d / %d", agg_winner_epochs, agg_epochs))
  printf(fmt, "Accepted winners (rows):", agg_winners)
  printf(fmt, "TAO spent (winners VALUE):", sprintf("%.6f", agg_value))

  printf(fmt, "Budget (my total TAO):", sprintf("%.6f", agg_budget))
  printf(fmt, "Budget leftover (TAO):", sprintf("%.6f", agg_leftover))
  printf(fmt, "Budget usage (est.):", sprintf("%.2f%%", budget_used))

  printf(fmt, "Auction ACK rate:", sprintf("%.2f%% (%d/%d)", ack_rate, agg_ack_recv, agg_ack_total))
  if (agg_winack_total > 0)
    printf(fmt, "Win ACK rate:", sprintf("%.2f%% (%d/%d)", winack_rate, agg_winack_recv, agg_winack_total))
  if (agg_reach_den > 0)
    printf(fmt, "Axon reachability:", sprintf("%.2f%% (%d/%d usable)", reach_rate, agg_reach_sum, agg_reach_den))

  printf(fmt, "Weights preview modes:", sprintf("normal=%d burn-all=%d other=%d", w_normal, w_burn, w_other))
  if (set_weights_testing) printf(fmt, "On-chain set_weights():", "TESTING (suppressed)")

  # --- NEW: Payments summary (settled only) ---
  printf("%s\n", line)
  printf("Payments (settled epochs only)\n")
  printf(fmt, "Settled epochs considered:", mat_epochs)
  printf(fmt, "VALUE expected vs credited:", sprintf("%.6f vs %.6f TAO (shortfall=%.6f)", mat_value_expected, mat_value_credited, value_shortfall))
  printf(fmt, "α expected vs paid:", sprintf("%.4f vs %.4f α (shortfall=%.4f)", toAlpha(mat_alpha_expected_rao), toAlpha(mat_alpha_paid_rao), toAlpha(alpha_shortfall_rao)))
  printf(fmt, "Unpaid/partial invoices:", sprintf("%d epochs with unpaid: %d", mat_unpaid_invoices, epochs_with_unpaid))
  printf("%s\n", line)

  if (!detailed) exit 0

  # --- DETAILED PER‑EPOCH ---
  print "DETAILED EPOCH REPORT"
  print line

  for (jj = lastCnt; jj >= 1; jj--) {
    e = last[jj]
    printf("Epoch %s\n", e)

    print "  Auction"
    if (acks_total[e] + 0 > 0)
      printf("    acks:              %d/%d (%.2f%%)\n", acks_recv[e]+0, acks_total[e]+0, 100.0*(acks_recv[e]+0)/(acks_total[e]+0))
    if (ax_rcv[e] + 0 > 0)
      printf("    axons usable:      %d/%d (%.2f%%)\n", ax_usable[e]+0, ax_rcv[e]+0, 100.0*(ax_usable[e]+0)/(ax_rcv[e]+0))
    if (my_budget[e] != "")
      printf("    budget (my):       %.6f TAO  @price≈%s TAO/α\n", my_budget[e]+0, (base_price[e] != "" ? base_price[e] : "?"))
    if (budget_leftover[e] != "")
      printf("    leftover:          %.6f TAO\n", budget_leftover[e]+0)
    if (winners_count[e] + 0 > 0)
      printf("    winners:           %d rows, total VALUE=%.6f TAO\n", winners_count[e]+0, winners_value[e]+0)
    else
      printf("    winners:           none\n")
    if (early_winners[e] != "")
      printf("    cleared winners:   %d\n", early_winners[e]+0)
    if (winacks_total[e] + 0 > 0)
      printf("    win acks:          %d/%d (%.2f%%)\n", winacks_recv[e]+0, winacks_total[e]+0, 100.0*(winacks_recv[e]+0)/(winacks_total[e]+0))

    print "  Commitment"
    if (staged_miners[e] != "" || staged_lines[e] != "")
      printf("    staged:            miners=%s lines=%s\n", (staged_miners[e] != "" ? staged_miners[e] : "?"), (staged_lines[e] != "" ? staged_lines[e] : "?"))
    if (commit_has_inv[e] != "")
      printf("    payload:           has_inv=%s\n", commit_has_inv[e])
    printf("    published:         %s", (commit_published[e] ? "yes" : "no"))
    if (commit_published[e] && commit_cid[e] != "")
      printf(" (cid=%s)", substr(commit_cid[e],1,20) "…")
    print ""

    print "  Settlement"
    if (settled[e]) {
      printf("    status:            complete (miners_scored=%d)\n", (miners_scored[e] != "" ? miners_scored[e] : 0))
      if (weights_mode[e] != "")
        printf("    weights:           mode=%s nonzero=%s max=%.6f sum(scores)=%.6f\n",
          weights_mode[e],
          (weights_nonzero[e] != "" ? weights_nonzero[e] : "?"),
          (weights_max[e] != "" ? weights_max[e]+0 : 0),
          (sum_scores[e] != "" ? sum_scores[e]+0 : 0)
        )
      if (spent_payload[e] != "" || credited_value[e] != "" || burn_deficit[e] != "")
        printf("    accounting:        spent=%.6f credited=%.6f burn_deficit=%.6f (target=%.6f leftover=%.6f)\n",
          (spent_payload[e] != "" ? spent_payload[e]+0 : 0),
          (credited_value[e] != "" ? credited_value[e]+0 : 0),
          (burn_deficit[e] != "" ? burn_deficit[e]+0 : 0),
          (target_budget[e] != "" ? target_budget[e]+0 : 0),
          (leftover_payload[e] != "" ? leftover_payload[e]+0 : 0)
        )

      # Payments per-epoch
      exp_rao = (invoices_alpha_rao[e] + 0)
      paid_rao = (alpha_paid_rao[e] + 0)
      printf("  Payments\n")
      printf("    invoices:          %s VALUE=%.6f TAO, α=%.4f\n",
             (invoices_value_tao[e] != "" ? "present" : "missing"),
             (invoices_value_tao[e] + 0),
             toAlpha(exp_rao))
      printf("    paid α:            %.4f α (shortfall=%.4f α)\n", toAlpha(paid_rao), toAlpha(exp_rao - paid_rao))

      # List only the unpaid/partial miners (compact)
      unpaid_listed = 0
      for (k in inv_by_ck) {
        split(k, K, SUBSEP); ek=K[1]; ck=K[2]
        if (ek != e) continue
        er = inv_by_ck[ek, ck] + 0
        pr = alpha_paid_by_ck[ek, ck] + 0
        if (pr < er) {
          if (unpaid_listed == 0) print "    unpaid miners:"
          printf("      - %s  need=%.4f α  paid=%.4f α  short=%.4f α\n", ck, toAlpha(er), toAlpha(pr), toAlpha(er-pr))
          unpaid_listed++
        }
      }
      if (unpaid_listed == 0) print "    unpaid miners:     none"
    } else {
      print "    status:            pending (payments will be checked at settlement)"
    }

    if ((conn_err[e] + 0) > 0 || (timeouts[e] + 0) > 0) {
      printf("  Errors               connect=%d timeouts=%d\n", conn_err[e]+0, timeouts[e]+0)
    }
    print line
  }
}
' "$INPUT"
