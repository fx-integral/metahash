#!/usr/bin/env bash
# miner_epoch_report.sh
# Summarize last N pay epochs from a Metahash Miner pm2 log.
# - Extracts "Win received" (accepted α) and "Payment OK" lines.
# - Maps wins to their pay_epoch (e+1) and compares to executed payments.
# - Prints per-epoch totals: expected (wins), paid, unpaid, failed, pending.
# Usage:
#   ./miner_epoch_report.sh -p <pm2_name> -n 6
#   ./miner_epoch_report.sh -f /path/to/pm2-out.log -n 6 [--include-error]
# Notes:
#   * Requires only awk/sed/grep; optionally uses jq to resolve pm2 log paths.
#   * Epochs here are **pay epochs** (when money is due / sent).
#   * "Failed" = invoices with a terminal "PAY exit" (window over / max attempts).
#     Anything else not yet paid is "Pending".

set -euo pipefail

N=5
LOGFILE=""
ERRFILE=""
PM2_NAME=""
INCLUDE_ERROR=0

usage() {
  cat <<'USAGE'
miner_epoch_report.sh - summarize Metahash miner activity by pay epoch

Options:
  -n, --epochs N        Number of last pay epochs to include (default: 5)
  -p, --pm2 NAME        pm2 process name to read logs from
  -f, --file PATH       Path to pm2 out log file (overrides -p)
      --include-error   Also include the pm2 error log, if available
  -h, --help            Show this help

Examples:
  ./miner_epoch_report.sh -p miner -n 8
  ./miner_epoch_report.sh -f ~/.pm2/logs/miner-out.log --include-error
USAGE
}

# --- Parse args ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--epochs) N="${2:-}"; shift 2;;
    -p|--pm2) PM2_NAME="${2:-}"; shift 2;;
    -f|--file) LOGFILE="${2:-}"; shift 2;;
    --include-error) INCLUDE_ERROR=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown argument: $1" >&2; usage; exit 1;;
  esac
done

# --- Resolve pm2 log paths if needed ---
if [[ -z "$LOGFILE" && -n "$PM2_NAME" ]]; then
  if command -v jq >/dev/null 2>&1; then
    # Use pm2 JSON list for robust parsing.
    if ! pm2 jlist >/dev/null 2>&1; then
      echo "pm2 jlist failed. Is pm2 running?" >&2
      exit 1
    fi
    LOGFILE="$(pm2 jlist | jq -r --arg name "$PM2_NAME" \
      '.[] | select(.name==$name) | .pm2_env.pm_out_log_path' | head -n1)"
    ERRFILE="$(pm2 jlist | jq -r --arg name "$PM2_NAME" \
      '.[] | select(.name==$name) | .pm2_env.pm_err_log_path' | head -n1)"
  else
    # Fallback: parse `pm2 info` text output.
    INFO="$(pm2 info "$PM2_NAME" 2>/dev/null || true)"
    if [[ -z "$INFO" ]]; then
      echo "pm2 info $PM2_NAME returned nothing. Is the process name correct?" >&2
      exit 1
    fi
    LOGFILE="$(echo "$INFO" | grep -i 'out log path' | sed -E 's/.*out log path[^/]*([/A-Za-z0-9._:\-]+).*/\1/' | head -n1 || true)"
    ERRFILE="$(echo "$INFO" | grep -i 'error log path' | sed -E 's/.*error log path[^/]*([/A-Za-z0-9._:\-]+).*/\1/' | head -n1 || true)"
  fi
fi

if [[ -z "$LOGFILE" || ! -f "$LOGFILE" ]]; then
  echo "Could not find pm2 out log. Pass -f <file> or -p <pm2_name>." >&2
  exit 1
fi

# Merge out + err if requested.
if [[ "$INCLUDE_ERROR" -eq 1 && -n "${ERRFILE:-}" && -f "$ERRFILE" ]]; then
  # Tag error lines so they’re distinguishable (not necessary for parsing).
  INPUT="$(cat "$LOGFILE"; sed 's/^/[err] /' "$ERRFILE")"
else
  INPUT="$(cat "$LOGFILE")"
fi

# Ensure UTF-8 so awk can happily see the α glyph when present.
export LC_ALL="${LC_ALL:-C.UTF-8}"
export LANG="${LANG:-C.UTF-8}"

# --- Parse & summarize with awk ---
# We look for blocks titled:
#   "Win received"  -> fields include: invoice_id, epoch_now (e), subnet, accepted α, pay_epoch (e+1)
#   "Payment OK"    -> fields include: inv, α, sid (or subnet), pay_e
#   "PAY exit"      -> with reason "window over" or "max attempts" and an inv line
#
# We aggregate by pay epoch (the epoch when the payment should/may occur).

awk -v N="$N" '
function trim(s){ sub(/^[[:space:]]+/,"",s); sub(/[[:space:]]+$/,"",s); return s }
function strip_ansi(s){ gsub(/\x1B\[[0-9;]*[A-Za-z]/,"",s); return s }
function parse_int(s,    m){ if (match(s, /-?[0-9]+/)) { m=substr(s,RSTART,RLENGTH); return m+0 } return 0 }
function parse_alpha(s,   m){
  # Prefer number that’s right before an alpha glyph; fallback to first float.
  if (match(s, /[0-9]+(\.[0-9]+)?[[:space:]]*α/)) {
    m=substr(s,RSTART,RLENGTH); gsub(/[^0-9.]/,"",m); return m+0
  } else if (match(s, /[0-9]+(\.[0-9]+)?/)) {
    m=substr(s,RSTART,RLENGTH); return m+0
  }
  return 0
}

BEGIN{
  FS="\n"; RS="\n"
  in_win=0; in_pay=0; in_exit=0
}

{
  line=$0
  s=strip_ansi(line)

  # Normalize for detections
  sl=tolower(s)

  # Any new panel-like header resets finer parsers:
  if (sl ~ /auctionstart|win received|payment ok|payment failed|pay exit|bids sent|miner started/) {
    in_win=0; in_pay=0; in_exit=0
  }

  # --- WIN RECEIVED PANEL ---
  if (sl ~ /win received/) {
    in_win=1
    w_inv=""; w_epoch=""; w_alpha=""; w_subnet=""; w_payepoch=""
    next
  }
  if (in_win) {
    if (index(sl, "invoice_id")>0 && w_inv=="") {
      w_inv=s; sub(/^.*invoice_id[^0-9A-Za-z_-]*/,"",w_inv); w_inv=trim(w_inv)
    }
    if (sl ~ /epoch_now.*\(e\)/) { w_epoch=parse_int(s) }
    if ((index(sl,"accepted")>0 && index(s,"α")>0) || index(sl,"amount_to_pay")>0) {
      # Prefer accepted α; fallback amount_to_pay
      val=parse_alpha(s)
      if (val>0) w_alpha=val
    }
    if (sl ~ /\bsubnet\b/) { w_subnet=parse_int(s) }
    if (sl ~ /pay_epoch/) { w_payepoch=parse_int(s) }

    # When we have the essentials, store & close the block.
    if (w_inv!="" && w_payepoch!="" && w_alpha!="") {
      wins[w_inv]=1
      win_alpha[w_inv]=w_alpha+0.0
      win_epoch[w_inv]=w_epoch+0
      win_subnet[w_inv]=w_subnet+0
      pay_epoch[w_inv]=w_payepoch+0
      in_win=0
    }
    next
  }

  # --- PAYMENT OK PANEL ---
  if (sl ~ /payment ok/) {
    in_pay=1
    p_inv=""; p_alpha=0; p_subnet=""; p_payepoch=""
    next
  }
  if (in_pay) {
    if (index(sl, "inv")>0 && p_inv=="") {
      p_inv=s; sub(/^.*inv[^0-9A-Za-z_-]*/,"",p_inv); p_inv=trim(p_inv)
    }
    if (index(s,"α")>0) {
      val=parse_alpha(s); if (val>0) p_alpha=val
    }
    if (sl ~ /\bsid\b/ || sl ~ /\bsubnet\b/) { p_subnet=parse_int(s) }
    if (sl ~ /pay_e/) { p_payepoch=parse_int(s) }

    if (p_inv!="" && p_alpha>0 && p_payepoch!="") {
      payments[p_inv]=1
      pay_amt[p_inv]=p_alpha+0.0
      pay_epoch_by_inv[p_inv]=p_payepoch+0
      pay_subnet_by_inv[p_inv]=p_subnet+0
      in_pay=0
    }
    next
  }

  # --- PAY EXIT (terminal fail) ---
  if (sl ~ /pay exit/ && (sl ~ /max attempts/ || sl ~ /window over/)) {
    in_exit=1
    ex_reason=(sl ~ /max attempts/ ? "max attempts" : "window over")
    ex_inv=""
    if (index(sl,"inv")>0) {
      ex_inv=s; sub(/^.*inv[^0-9A-Za-z_-]*/,"",ex_inv); ex_inv=trim(ex_inv)
      if (ex_inv!="") exit_status[ex_inv]=ex_reason
      in_exit=0
    }
    next
  }
  if (in_exit) {
    if (index(sl,"inv")>0) {
      ex_inv=s; sub(/^.*inv[^0-9A-Za-z_-]*/,"",ex_inv); ex_inv=trim(ex_inv)
      if (ex_inv!="") exit_status[ex_inv]=ex_reason
      in_exit=0
    }
  }
}

END{
  # Aggregate by pay epoch
  max_pay=0
  for (id in wins) {
    e = pay_epoch[id]+0
    wins_sum[e] += win_alpha[id]
    wins_cnt[e] += 1
    if (e > max_pay) max_pay=e
  }
  for (id in payments) {
    e = pay_epoch_by_inv[id]+0
    paid_sum[e] += pay_amt[id]
    paid_cnt[e] += 1
    if (e > max_pay) max_pay=e
  }
  # Failed / Pending classification based on exits and paid
  for (id in wins) {
    e = pay_epoch[id]+0
    if (id in payments) {
      # paid
    } else if (id in exit_status) {
      failed_sum[e] += win_alpha[id]
      failed_cnt[e] += 1
    } else {
      pending_sum[e] += win_alpha[id]
      pending_cnt[e] += 1
    }
  }

  if (N<=0) N=5
  start = max_pay - N + 1
  if (start < 0) start = 0

  # Header
  printf("\nMetahash Miner summary (last %d pay epochs: %d..%d)\n", N, start, max_pay)
  printf("Timeline reminder: bid now (e) → pay in (e+1) → weights from e in (e+2)\n\n")

  printf("%-7s | %15s | %10s | %15s | %10s | %15s | %12s | %12s\n",
         "Epoch", "Wins α (due)", "Wins cnt", "Paid α", "Paid cnt", "Unpaid α", "Failed α", "Pending α")
  printf("%-7s-+-%15s-+-%10s-+-%15s-+-%10s-+-%15s-+-%12s-+-%12s\n",
         "-------","---------------","----------","---------------","----------","---------------","------------","------------")

  TW=0; TWC=0; TP=0; TPC=0; TU=0; TF=0; TPD=0

  for (e=start; e<=max_pay; e++) {
    w = wins_sum[e]+0.0
    wc = wins_cnt[e]+0
    p = paid_sum[e]+0.0
    pc = paid_cnt[e]+0
    f = failed_sum[e]+0.0
    pd = pending_sum[e]+0.0
    u = w - p; if (u < 0) u = 0.0

    printf("%-7d | %15.4f | %10d | %15.4f | %10d | %15.4f | %12.4f | %12.4f\n",
           e, w, wc, p, pc, u, f, pd)

    TW += w; TWC += wc; TP += p; TPC += pc; TU += u; TF += f; TPD += pd
  }

  printf("\nTotals: Wins α=%.4f (cnt=%d) | Paid α=%.4f (cnt=%d) | Unpaid α=%.4f | Failed α=%.4f | Pending α=%.4f\n\n",
         TW, TWC, TP, TPC, TU, TF, TPD)

  # Subnet breakdown over the reported window
  # Build per-subnet aggregates only for epochs within [start..max_pay]
  delete subnet_wins; delete subnet_paid
  for (id in wins) {
    e = pay_epoch[id]+0
    if (e>=start && e<=max_pay) {
      sid = win_subnet[id]+0
      subnet_wins[sid] += win_alpha[id]
    }
  }
  for (id in payments) {
    e = pay_epoch_by_inv[id]+0
    if (e>=start && e<=max_pay) {
      sid = pay_subnet_by_inv[id]+0
      subnet_paid[sid] += pay_amt[id]
    }
  }
  # Print if any
  hasSubnet=0
  for (k in subnet_wins) { hasSubnet=1; break }
  for (k in subnet_paid) { hasSubnet=1; break }
  if (hasSubnet) {
    printf("Subnet breakdown (epochs %d..%d):\n", start, max_pay)
    printf("%-8s | %15s | %15s | %15s\n", "Subnet", "Wins α (due)", "Paid α", "Unpaid α")
    printf("%-8s-+-%15s-+-%15s-+-%15s\n", "--------","---------------","---------------","---------------")
    # Collect keys
    n=0
    for (sid in subnet_wins) { keys[++n]=sid }
    for (sid in subnet_paid) {
      found=0
      for (i=1;i<=n;i++) if (keys[i]==sid) { found=1; break }
      if (!found) keys[++n]=sid
    }
    # Sort keys numerically
    for (i=1;i<=n;i++) for (j=i+1;j<=n;j++) if (keys[j]<keys[i]) { tmp=keys[i]; keys[i]=keys[j]; keys[j]=tmp }
    for (i=1;i<=n;i++) {
      sid=keys[i]
      w = subnet_wins[sid]+0.0
      p = subnet_paid[sid]+0.0
      u = w - p; if (u < 0) u = 0.0
      printf("%-8d | %15.4f | %15.4f | %15.4f\n", sid, w, p, u)
    }
    printf("\n")
  }

  # List pending invoices (truncated to 10)
  pend_count=0
  for (id in wins) {
    e=pay_epoch[id]+0
    if (e>=start && e<=max_pay) {
      if (!(id in payments) && !(id in exit_status)) {
        pend_list[id]=1; pend_count++
      }
    }
  }
  if (pend_count>0) {
    printf("Pending invoices (up to 10 shown):\n")
    printf("%-10s | %-8s | %-6s | %-10s\n","Invoice","pay_e","sid","α due")
    printf("%-10s-+-%-8s-+-%-6s-+-%-10s\n","----------","--------","------","----------")
    shown=0
    for (id in pend_list) {
      if (shown>=10) break
      printf("%-10s | %-8d | %-6d | %-10.4f\n", id, pay_epoch[id]+0, win_subnet[id]+0, win_alpha[id]+0.0)
      shown++
    }
    printf("\n")
  }

  # List failures (truncated to 10)
  fail_count=0
  for (id in exit_status) {
    e=pay_epoch[id]+0
    if (e>=start && e<=max_pay) { fail_list[id]=1; fail_count++ }
  }
  if (fail_count>0) {
    printf("Failed invoices (up to 10 shown):\n")
    printf("%-10s | %-8s | %-12s | %-6s | %-10s\n","Invoice","pay_e","reason","sid","α")
    printf("%-10s-+-%-8s-+-%-12s-+-%-6s-+-%-10s\n","----------","--------","------------","------","----------")
    shown=0
    for (id in fail_list) {
      if (shown>=10) break
      printf("%-10s | %-8d | %-12s | %-6d | %-10.4f\n", id, pay_epoch[id]+0, exit_status[id], win_subnet[id]+0, win_alpha[id]+0.0)
      shown++
    }
    printf("\n")
  }
}
' <<< "$INPUT"
