#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
btv-health.sh  —  Summarize Bittensor validator logs by epoch

Usage:
  btv-health.sh [-n N] [--detailed] [-f LOGFILE]
  tail -n 5000 validator.log | btv-health.sh -n 5 --detailed

Options:
  -n, --epochs N     Number of last epochs to summarize (default: 5)
  -f, --file FILE    Read logs from FILE (default: STDIN)
  --detailed         Add a per-epoch detailed report after the summary
  -h, --help         Show this help

What it does:
  • Detects epoch boundaries and key events
  • Aggregates health metrics (commitments, settlements, winners, spend, acks, reachability, errors)
  • Prints a concise summary for quick status, plus optional detailed sections per epoch
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
BEGIN {
  # Numeric locale
  PROCINFO["sorted_in"] = "cmp_num_asc"
  # Maps / state
  nOrder = 0
  set_weights_testing = 0

  # toggles/contexts
  current_e = ""
  budget_e = ""
  auction_e = ""
  winners_e = ""
  in_winners = 0
  settle_e = ""     # epoch currently being settled (e−2)
  in_budget_block = 0
}

function add_epoch(e,  already){
  if (e == "" ) return
  if (!(e in seen_e)) {
    seen_e[e] = 1
    order[++nOrder] = e
  }
  current_e = e
}

# --- epoch detection (two forms) ---
/\[epoch[[:space:]]+[0-9]+/ {
  if (match($0, /\[epoch[[:space:]]+([0-9]+)/, m)) add_epoch(m[1])
}
# e.g. "│ Epoch 324072 (label: e) │"
$0 ~ /Epoch[[:space:]]+[0-9]+[[:space:]]+\(label: e\)/ {
  if (match($0, /Epoch[[:space:]]+([0-9]+)/, m)) add_epoch(m[1])
}

# --- status head_block/start/end (informational) ---
$0 ~ /head_block=[0-9]+.*start=[0-9]+.*end=[0-9]+/ {
  if (current_e != "" && match($0, /head_block=([0-9]+).*start=([0-9]+).*end=([0-9]+)/, m)) {
    head_block[current_e] = m[1]
    start_block[current_e] = m[2]
    end_block[current_e]   = m[3]
  }
}

# --- Axon filter counts (auction-phase reachability) ---
$0 ~ /Axon filter/ { /* header only */ }
/^[[:space:]]*received:[[:space:]]*[0-9]+/ {
  if (current_e != "" && match($0, /received:[[:space:]]*([0-9]+)/, m)) ax_rcv[current_e] = m[1]
}
/^[[:space:]]*usable:[[:space:]]*[0-9]+/ {
  if (current_e != "" && match($0, /usable:[[:space:]]*([0-9]+)/, m)) ax_usable[current_e] = m[1]
}

# --- AuctionStart broadcast block (acks & explicit e) ---
$0 ~ /AuctionStart Broadcast/ { /* header only */ }
$0 ~ /e \(now\):[[:space:]]*[0-9]+/ {
  if (match($0, /e \(now\):[[:space:]]*([0-9]+)/, m)) { auction_e = m[1]; add_epoch(auction_e) }
}
$0 ~ /acks_received:[[:space:]]*[0-9]+\/[0-9]+/ {
  if (match($0, /acks_received:[[:space:]]*([0-9]+)\/([0-9]+)/, m)) {
    e = (auction_e != "" ? auction_e : current_e)
    acks_recv[e] += m[1]
    acks_total[e] += m[2]
  }
}

# --- Budget (VALUE) block (epoch + budget + price) ---
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
# end of budget block (next box line or blank is fine)
in_budget_block && ($0 ~ /^╰/ || $0 ~ /^$/ || $0 ~ /Connecting to Substrate/) { in_budget_block = 0 }

# --- Budget leftover (after allocation) ---
/Budget leftover .* after allocation:/ {
  if (match($0, /Budget leftover .*: ([0-9.]+)/, m)) {
    e = (budget_e != "" ? budget_e : current_e)
    budget_leftover[e] = m[1]
  }
}

# --- Winners — acceptances (VALUE) table ---
/Winners — acceptances .*VALUE/ {
  in_winners = 1
  winners_e = (budget_e != "" ? budget_e : (auction_e != "" ? auction_e : current_e))
  if (winners_e == "") winners_e = current_e
}
in_winners {
  # Table body rows contain "... | <VALUE> TAO(VALUE) | ..."
  if (match($0, /([0-9]+\.[0-9]+)[[:space:]]*TAO\(VALUE\)/, m)) {
    winners_value[winners_e] += m[1] + 0
    winners_count[winners_e] += 1
  }
  # Stop when we hit a box footer or a different section
  if ($0 ~ /^╰/ || $0 ~ /^$/ || $0 ~ /Staged commit payload/ || $0 ~ /Invoices —/) in_winners = 0
}

# --- Early Clear & Notify (records winners count + win_acks) ---
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
    winacks_recv[e] += m[1]
    winacks_total[e] += m[2]
  }
}

# --- Staged commit payload & Commit Payload (preview) (has_inv) ---
/Staged commit payload/ { /* header only */ }
$0 ~ /^[[:space:]]*e:[[:space:]]*[0-9]+/ {
  if (match($0, /e:[[:space:]]*([0-9]+)/, m)) {
    staged_e = m[1]
    add_epoch(staged_e)
  }
}
$0 ~ /#miners:[[:space:]]*[0-9]+/  { if (staged_e != "" && match($0, /#miners:[[:space:]]*([0-9]+)/, m)) staged_miners[staged_e] = m[1] }
$0 ~ /#lines_total:[[:space:]]*[0-9]+/ { if (staged_e != "" && match($0, /#lines_total:[[:space:]]*([0-9]+)/, m)) staged_lines[staged_e] = m[1] }

/Commit Payload \(preview\)/ { /* header only */ }
$0 ~ /epoch:[[:space:]]*[0-9]+/ && $0 ~ /has_inv:/ {
  if (match($0, /epoch:[[:space:]]*([0-9]+)/, a) && match($0, /has_inv:[[:space:]]*(true|false)/, b)) {
    e = a[1]; commit_has_inv[e] = b[1]
  }
}

# --- Commitment Published & settlement tracking ---
/Commitment Published/ { /* header only */ }
$0 ~ /epoch_cleared:[[:space:]]*[0-9]+/ {
  if (match($0, /epoch_cleared:[[:space:]]*([0-9]+)/, m)) {
    e = m[1]; commit_published[e] = 1
  }
}
$0 ~ /cid:[[:space:]]*bafk/ {
  if (e != "" && match($0, /cid:[[:space:]]*([a-z0-9]+)/, m)) commit_cid[e] = m[1]
}

# Start settlement window context (so we can attach WEIGHTS PREVIEW, accounting, etc. to the right e)
/Settlement for epoch[[:space:]]*[0-9]+/ {
  if (match($0, /Settlement for epoch[[:space:]]*([0-9]+)/, m)) { settle_e = m[1]; add_epoch(settle_e) }
}
/Settlement Complete/ && /epoch_settled .*:[[:space:]]*[0-9]+/ {
  if (match($0, /epoch_settled .*:[[:space:]]*([0-9]+)/, m)) {
    e = m[1]; settled[e] = 1
    # miners_scored is on the same or next line
    if (match($0, /miners_scored:[[:space:]]*([0-9]+)/, mm)) miners_scored[e] = mm[1]
  }
}

# Weights preview & summary (mapped to settle_e)
/mode=[a-zA-Z-]+/ {
  if (settle_e != "" && match($0, /mode=([a-zA-Z-]+)/, m)) weights_mode[settle_e] = m[1]
}
/Weigh[[:space:]]*Summary/ { /* header only */ }
$0 ~ /nonzero:[[:space:]]*[0-9]+/ {
  if (settle_e != "" && match($0, /nonzero:[[:space:]]*([0-9]+)/, m)) weights_nonzero[settle_e] = m[1]
}
$0 ~ /max:[[:space:]]*[0-9.]+/ {
  if (settle_e != "" && match($0, /max:[[:space:]]*([0-9.]+)/, m)) weights_max[settle_e] = m[1]
}
$0 ~ /sum\(scores\):[[:space:]]*[0-9.]+/ {
  if (settle_e != "" && match($0, /sum\(scores\):[[:space:]]*([0-9.]+)/, m)) sum_scores[settle_e] = m[1]
}

# Budget Accounting — settlement (spent/leftover/burn) mapped to settle_e
/Budget Accounting — settlement/ { /* header only */ }
$0 ~ /spent_from_payload .*:[[:space:]]*[0-9.]+/ {
  if (settle_e != "" && match($0, /spent_from_payload .*:[[:space:]]*([0-9.]+)/, m)) spent_payload[settle_e] = m[1]
}
$0 ~ /leftover_from_payload .*:[[:space:]]*[0-9.]+/ {
  if (settle_e != "" && match($0, /leftover_from_payload .*:[[:space:]]*([0-9.]+)/, m)) leftover_payload[settle_e] = m[1]
}
$0 ~ /target_budget .*:[[:space:]]*[0-9.]+/ {
  if (settle_e != "" && match($0, /target_budget .*:[[:space:]]*([0-9.]+)/, m)) target_budget[settle_e] = m[1]
}
$0 ~ /credited_value .*:[[:space:]]*[0-9.]+/ {
  if (settle_e != "" && match($0, /credited_value .*:[[:space:]]*([0-9.]+)/, m)) credited_value[settle_e] = m[1]
}
$0 ~ /burn_deficit .*:[[:space:]]*[0-9.]+/ {
  if (settle_e != "" && match($0, /burn_deficit .*:[[:space:]]*([0-9.]+)/, m)) burn_deficit[settle_e] = m[1]
}

# α transfer scan / paid pools (mapped to settle_e)
/Scanner: frm=/ { /* range header */ }
$0 ~ /#events .*:[[:space:]]*[0-9]+/ {
  if (settle_e != "" && match($0, /#events.*:[[:space:]]*([0-9]+)/, m)) alpha_events[settle_e] = m[1]
}
/Paid α pools/ { /* header only */ }
$0 ~ /#pools:[[:space:]]*[0-9]+/ {
  if (settle_e != "" && match($0, /#pools:[[:space:]]*([0-9]+)/, m)) paid_pools[settle_e] = m[1]
}

# set_weights testing flags
/TESTING: skipping on-chain set_weights/ { set_weights_testing = 1 }
/TESTING — on-chain set_weights\(\) suppressed/ { set_weights_testing = 1 }

# Errors
/Cannot connect to host/ {
  e = (auction_e != "" ? auction_e : (budget_e != "" ? budget_e : current_e))
  conn_err_total++
  if (e != "") conn_err[e]++
}
/TimeoutError#/ {
  e = (auction_e != "" ? auction_e : (budget_e != "" ? budget_e : current_e))
  timeout_total++
  if (e != "") timeouts[e]++
}

# --- END: compute & print ---
END {
  if (nOrder == 0) { print "No epochs found in input."; exit 1 }

  # pick last wantN epochs
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
  agg_alpha_events = agg_paid_pools = 0
  w_normal = w_burn = w_other = 0
  agg_conn = agg_timeo = 0

  for (j = 1; j <= lastCnt; j++) {
    e = last[j]

    if (commit_published[e]) agg_commits++

    if (settled[e]) agg_settled++

    if (winners_count[e] + 0 > 0) agg_winner_epochs++
    agg_winners += (winners_count[e] + 0)
    agg_value += (winners_value[e] + 0)

    agg_budget += (my_budget[e] + 0)
    agg_leftover += (budget_leftover[e] + 0)

    agg_ack_recv += (acks_recv[e] + 0)
    agg_ack_total += (acks_total[e] + 0)

    agg_winack_recv += (winacks_recv[e] + 0)
    agg_winack_total += (winacks_total[e] + 0)

    if ((ax_rcv[e] + 0) > 0) {
      agg_reach_sum += (ax_usable[e] + 0)
      agg_reach_den += (ax_rcv[e] + 0)
    }

    agg_alpha_events += (alpha_events[e] + 0)
    agg_paid_pools += (paid_pools[e] + 0)

    # weights modes apply to settlement of that e
    if (weights_mode[e] == "normal") w_normal++
    else if (weights_mode[e] == "burn-all") w_burn++
    else if (weights_mode[e] != "") w_other++

    agg_conn += (conn_err[e] + 0)
    agg_timeo += (timeouts[e] + 0)
  }

  ack_rate    = (agg_ack_total    > 0 ? 100.0 * agg_ack_recv    / agg_ack_total    : 0)
  winack_rate = (agg_winack_total > 0 ? 100.0 * agg_winack_recv / agg_winack_total : 0)
  reach_rate  = (agg_reach_den    > 0 ? 100.0 * agg_reach_sum   / agg_reach_den    : 0)
  budget_used = (agg_budget       > 0 ? 100.0 * (agg_budget - agg_leftover) / agg_budget : 0)

  # --- SUMMARY ---
  fmt="%-32s %s\n"
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

  printf(fmt, "α scan events:", agg_alpha_events)
  printf(fmt, "Paid α pools:", agg_paid_pools)

  printf(fmt, "Network errors:", sprintf("connect=%d timeouts=%d", agg_conn, agg_timeo))
  printf("%s\n", line)

  if (!detailed) exit 0

  # --- DETAILED PER-EPOCH ---
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
      if (alpha_events[e] != "" || paid_pools[e] != "")
        printf("    α payments:        events=%d pools=%d\n", (alpha_events[e] != "" ? alpha_events[e]+0 : 0), (paid_pools[e] != "" ? paid_pools[e]+0 : 0))
    } else {
      print "    status:            pending"
    }

    if ((conn_err[e] + 0) > 0 || (timeouts[e] + 0) > 0) {
      printf("  Errors               connect=%d timeouts=%d\n", conn_err[e]+0, timeouts[e]+0)
    }
    print line
  }
}
' "$INPUT"
