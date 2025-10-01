#!/usr/bin/env bash
# btv-health.sh — Validator health + bids/winners/reputation report
# Works on mawk (Debian /usr/bin/awk) and gawk.
# Usage examples:
#   ./scripts/validator/report.sh --pm2-index 4 --lines 8000 -n 5 --detailed
#   ./scripts/validator/report.sh --pm2-name validator-v3 --lines 12000 -n 10
#   tail -n 5000 validator.log | ./scripts/validator/report.sh -n 5 --detailed
set -euo pipefail

usage() {
  cat <<'USAGE'
btv-health.sh — Summarize Bittensor validator logs by epoch (health + payments + bids + reputation)

Usage:
  btv-health.sh [-n N] [--detailed] [-f FILE]
  btv-health.sh --pm2-index N [--lines K] [-n N] [--detailed]
  btv-health.sh --pm2-name NAME [--lines K] [-n N] [--detailed]

Options:
  -n, --epochs N       Number of last epochs to summarize (default: 5)
  -f, --file FILE      Read logs from FILE (default: STDIN)
  --pm2-index N        Use PM2 process by index to discover logs
  --pm2-name  NAME     Use PM2 process by name to discover logs
  --lines K            Tail K lines from each PM2 log file (default: 5000)
  --detailed           Print per-epoch detailed sections
  -h, --help           Show this help
USAGE
}

# Defaults
N=5
DETAIL=0
INPUT="/dev/stdin"
PM2_IDX=""
PM2_NAME=""
LINES=5000
TMP_INPUT=""

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--epochs) N="${2:?}"; shift 2 ;;
    -f|--file)   INPUT="${2:?}"; shift 2 ;;
    --pm2-index) PM2_IDX="${2:?}"; shift 2 ;;
    --pm2-name)  PM2_NAME="${2:?}"; shift 2 ;;
    --lines)     LINES="${2:?}"; shift 2 ;;
    --detailed)  DETAIL=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

cleanup() { [[ -n "$TMP_INPUT" && -f "$TMP_INPUT" ]] && rm -f "$TMP_INPUT"; }
trap cleanup EXIT

resolve_pm2_logs() {
  local idx="$1" name="$2" lines="$3"
  local out="" err=""
  if ! command -v pm2 >/dev/null 2>&1; then
    echo "pm2 not found in PATH." >&2; exit 1
  fi
  if command -v jq >/dev/null 2>&1; then
    if [[ -n "$idx" ]]; then
      out=$(pm2 jlist | jq -r --argjson i "$idx" '.[] | select(.pm_id == $i) | .pm2_env.pm_out_log_path' | head -n1)
      err=$(pm2 jlist | jq -r --argjson i "$idx" '.[] | select(.pm_id == $i) | .pm2_env.pm_err_log_path' | head -n1)
    elif [[ -n "$name" ]]; then
      out=$(pm2 jlist | jq -r --arg n "$name" '.[] | select(.name == $n) | .pm2_env.pm_out_log_path' | head -n1)
      err=$(pm2 jlist | jq -r --arg n "$name" '.[] | select(.name == $n) | .pm2_env.pm_err_log_path' | head -n1)
    fi
  else
    local base="${HOME}/.pm2/logs"
    if [[ -n "$idx" && -n "$name" ]]; then
      out="${base}/${name}-out-${idx}.log"
      err="${base}/${name}-error-${idx}.log"
    elif [[ -n "$idx" ]]; then
      echo "jq not found; with --pm2-index also pass --pm2-name for fallback paths." >&2; exit 1
    elif [[ -n "$name" ]]; then
      out="${base}/${name}-out-0.log"
      err="${base}/${name}-error-0.log"
    fi
  fi
  if [[ -z "${out}" && -z "${err}" ]]; then
    echo "Failed to resolve PM2 log paths." >&2; exit 1
  fi
  if [[ ! -f "$out" && ! -f "$err" ]]; then
    echo "PM2 logs not found:
  out: $out
  err: $err" >&2; exit 1
  fi
  TMP_INPUT=$(mktemp)
  { [[ -f "$out" ]] && tail -n "$lines" -- "$out"; [[ -f "$err" ]] && tail -n "$lines" -- "$err"; } > "$TMP_INPUT"
  INPUT="$TMP_INPUT"
}

if [[ -n "$PM2_IDX" || -n "$PM2_NAME" ]]; then
  resolve_pm2_logs "$PM2_IDX" "$PM2_NAME" "$LINES"
fi

export LC_ALL=C

awk -v wantN="$N" -v detailed="$DETAIL" '
function trim(s){ gsub(/^[[:space:]]+|[[:space:]]+$/,"",s); return s }
function toAlpha(rao){ return rao/1000000000.0 }
function nz(v, d){ return (v == "" ? d : v) + 0 }
function nzs(v, d){ return (v == "" ? d : v) }
function key(e,i){ return e SUBSEP i }
function kck(e,ck){ return e SUBSEP ck }

BEGIN{
  # No PROCINFO["sorted_in"] — works on mawk/gawk
  nOrder=0
  current_e=""; auction_e=""; budget_e=""; winners_e=""; settle_e=""
  in_budget_block=0; in_winners=0; in_invoices=0
  in_bids=0; in_rep=0
}

function add_epoch(e){
  if(e=="") return
  if(!(e in seen_e)){ seen_e[e]=1; order[++nOrder]=e }
  current_e=e
}

# ---- Epoch detection ----
/\[epoch[[:space:]]+[0-9]+/ { if(match($0,/\[epoch[[:space:]]+([0-9]+)/,m)) add_epoch(m[1]) }
$0 ~ /Epoch[[:space:]]+[0-9]+[[:space:]]+\(label: e\)/ { if(match($0,/Epoch[[:space:]]+([0-9]+)/,m)) add_epoch(m[1]) }

# ---- Status (head/start/end) ----
$0 ~ /head_block=[0-9]+.*start=[0-9]+.*end=[0-9]+/ {
  if(current_e!="" && match($0,/head_block=([0-9]+).*start=([0-9]+).*end=([0-9]+)/,m)){
    head_block[current_e]=m[1]; start_block[current_e]=m[2]; end_block[current_e]=m[3]
  }
}

# ---- Axon filter ----
/Axon filter/ { }
/^[[:space:]]*received:[[:space:]]*[0-9]+/ { if(current_e!="" && match($0,/received:[[:space:]]*([0-9]+)/,m)) ax_rcv[current_e]=m[1] }
/^[[:space:]]*usable:[[:space:]]*[0-9]+/   { if(current_e!="" && match($0,/usable:[[:space:]]*([0-9]+)/,m))  ax_usable[current_e]=m[1] }

# ---- AuctionStart ACKs ----
/AuctionStart Broadcast/ { }
$0 ~ /e \(now\):[[:space:]]*[0-9]+/ { if(match($0,/e \(now\):[[:space:]]*([0-9]+)/,m)){ auction_e=m[1]; add_epoch(auction_e) } }
$0 ~ /acks_received:[[:space:]]*[0-9]+\/[0-9]+/ {
  if(match($0,/acks_received:[[:space:]]*([0-9]+)\/([0-9]+)/,m)){
    e = (auction_e!="" ? auction_e : current_e); acks_recv[e]+=m[1]; acks_total[e]+=m[2]
  }
}

# ---- Budget (VALUE) block ----
/Budget \(VALUE.*base subnet/ { in_budget_block=1 }
in_budget_block && $0 ~ /epoch:[[:space:]]*[0-9]+/ { if(match($0,/epoch:[[:space:]]*([0-9]+)/,m)){ budget_e=m[1]; add_epoch(budget_e) } }
in_budget_block && match($0,/base_price_tao[[:space:]]*\/[[:space:]]*α:[[:space:]]*([0-9.]+)/,m) { if(budget_e!="") base_price[budget_e]=m[1] }
in_budget_block && match($0,/my_budget_tao[[:space:]]*\(VALUE\):[[:space:]]*([0-9.]+)/,m) { if(budget_e!="") my_budget[budget_e]=m[1] }
in_budget_block && ($0 ~ /^╰/ || $0 ~ /^$/ || $0 ~ /Connecting to Substrate/) { in_budget_block=0 }

/Budget leftover .* after allocation:/ { if(match($0,/Budget leftover .*: ([0-9.]+)/,m)){ e=(budget_e!=""?budget_e:current_e); budget_leftover[e]=m[1] } }

# ---- Bids — ordered by VALUE (TAO) ----
/^.*Bids[[:space:]]*[—-].*VALUE.*TAO/ { in_bids=1; bids_e=(budget_e!=""?budget_e:(auction_e!=""?auction_e:current_e)); add_epoch(bids_e); next }
in_bids {
  # Only capture proper bid rows: must include VALUE ... TAO and start with numeric UID
  if(index($0,"TAO")>0 && $0 ~ /^[[:space:]]*[0-9]+[[:space:]]*│/ && $0 !~ /UID[[:space:]]*│/){
    n=split($0,col,/│/)
    uid=trim(col[1]); ck=trim(col[2]); sid=trim(col[3]); wbps=trim(col[4]); disc=trim(col[5])
    bida=trim(col[6]); price=trim(col[7]); depth=trim(col[8]); val=trim(col[9])
    gsub(/[^0-9.]/,"",bida); gsub(/[^0-9.]/,"",price); gsub(/[^0-9.]/,"",val)
    bcount[bids_e]++; i=bcount[bids_e]
    bids_uid[key(bids_e,i)]=uid
    bids_ck[key(bids_e,i)]=ck
    bids_sid[key(bids_e,i)]=sid
    bids_wbps[key(bids_e,i)]=wbps
    bids_disc[key(bids_e,i)]=disc
    bids_alpha[key(bids_e,i)]=bida+0
    bids_price[key(bids_e,i)]=price+0
    bids_value[key(bids_e,i)]=val+0
  }
  if($0 ~ /^╵|^╰|^$/){ in_bids=0 }
}

# ---- Reputation caps (TAO, per coldkey) ----
/^.*Reputation caps.*\(TAO.*per coldkey\)/ { in_rep=1; rep_e=(budget_e!=""?budget_e:current_e); add_epoch(rep_e); next }
in_rep {
  # Caps lines include the TAO unit
  if(index($0,"TAO")>0 && $0 ~ /│/ && $0 !~ /Coldkey/){
    n=split($0,col,/│/)
    ck=trim(col[1]); q=trim(col[2]); cap=trim(col[3])
    gsub(/[^0-9.]/,"",q); gsub(/[^0-9.]/,"",cap)
    rep_q[kck(rep_e,ck)]=q+0
    rep_cap[kck(rep_e,ck)]=cap+0
    rep_seen[rep_e]=1
  }
  if($0 ~ /^╵|^╰|^$/){ in_rep=0 }
}

# ---- Winners — acceptances ----
/^.*Winners.*acceptances/ { in_winners=1; winners_e=(budget_e!=""?budget_e:(auction_e!=""?auction_e:current_e)); add_epoch(winners_e); next }
in_winners {
  # Winner rows also show VALUE ... TAO
  if(index($0,"TAO")>0 && $0 ~ /^[[:space:]]*[0-9]+[[:space:]]*│/ && $0 !~ /UID[[:space:]]*│/){
    n=split($0,col,/│/)
    uid=trim(col[1]); ck=trim(col[2]); sid=trim(col[3])
    reqa=trim(col[4]); acca=trim(col[5]); disc=trim(col[6])
    wbps=trim(col[7]); val=trim(col[8]); fill=trim(col[9])
    gsub(/[^0-9.]/,"",reqa); gsub(/[^0-9.]/,"",acca); gsub(/[^0-9.]/,"",val)
    wcount[winners_e]++; i=wcount[winners_e]
    win_uid[key(winners_e,i)]=uid
    win_ck[key(winners_e,i)]=ck
    win_sid[key(winners_e,i)]=sid
    win_req_alpha[key(winners_e,i)]=reqa+0
    win_acc_alpha[key(winners_e,i)]=acca+0
    win_disc[key(winners_e,i)]=disc
    win_wbps[key(winners_e,i)]=wbps
    win_value[key(winners_e,i)]=val+0
    win_fill[key(winners_e,i)]=fill
    winners_value[winners_e]+=val+0
    winners_count[winners_e]+=1
  }
  if($0 ~ /^╵|^╰|^$/ || $0 ~ /Staged commit payload/ || $0 ~ /Invoices .*/){ in_winners=0 }
}

# ---- Early clear ----
/Early Clear .*epoch e/ { }
$0 ~ /winners:[[:space:]]*[0-9]+/ { if(match($0,/winners:[[:space:]]*([0-9]+)/,m)){ e=(budget_e!=""?budget_e:(auction_e!=""?auction_e:current_e)); early_winners[e]=m[1] } }
$0 ~ /win_acks:[[:space:]]*([0-9]+)\/([0-9]+)/ {
  if(match($0,/win_acks:[[:space:]]*([0-9]+)\/([0-9]+)/,m)){ e=(budget_e!=""?budget_e:(auction_e!=""?auction_e:current_e)); winacks_recv[e]+=m[1]; winacks_total[e]+=m[2] }
}

# ---- Invoices (expected α) ----
/Invoices .*α to pay/ { in_invoices=1; invoice_e=(budget_e!=""?budget_e:current_e); add_epoch(invoice_e) }
in_invoices && /╰/ { in_invoices=0 }
in_invoices && $0 ~ /[0-9][[:space:]]*│/ && $0 !~ /UID[[:space:]]*│/ {
  n=split($0,col,/│/)
  ck=trim(col[2]); gsub(/…/,"",ck); gsub(/[[:space:]]/,"",ck)
  alpha_str=trim(col[4]); sub(/[[:space:]]*α.*/,"",alpha_str)
  val_str=trim(col[6]); sub(/[[:space:]]*TAO.*/,"",val_str)
  if(alpha_str!="" && ck!=""){
    a = alpha_str + 0
    invoices_alpha_rao[invoice_e] += int(a*1000000000.0+0.5)
    inv_by_ck[invoice_e,ck] += int(a*1000000000.0+0.5)
  }
  if(val_str!=""){ invoices_value_tao[invoice_e] += (val_str+0) }
}

# ---- Commit preview/publish ----
/Staged commit payload/ { }
$0 ~ /^[[:space:]]*e:[[:space:]]*[0-9]+/ { if(match($0,/e:[[:space:]]*([0-9]+)/,m)){ staged_e=m[1]; add_epoch(staged_e) } }
$0 ~ /#miners:[[:space:]]*[0-9]+/ { if(staged_e!="" && match($0,/#miners:[[:space:]]*([0-9]+)/,m)) staged_miners[staged_e]=m[1] }
$0 ~ /#lines_total:[[:space:]]*[0-9]+/ { if(staged_e!="" && match($0,/#[^:]*:[[:space:]]*([0-9]+)/,m)) staged_lines[staged_e]=m[1] }

/Commit Payload \(preview\)/ { }
$0 ~ /epoch:[[:space:]]*[0-9]+/ && $0 ~ /has_inv:/ {
  if(match($0,/epoch:[[:space:]]*([0-9]+)/,a) && match($0,/has_inv:[[:space:]]*(true|false)/,b)){ e=a[1]; commit_has_inv[e]=b[1] }
}

/Commitment Published/ { }
$0 ~ /epoch_cleared:[[:space:]]*[0-9]+/ { if(match($0,/epoch_cleared:[[:space:]]*([0-9]+)/,m)){ e=m[1]; commit_published[e]=1 } }
$0 ~ /cid:[[:space:]]*bafk/ { if(e!="" && match($0,/cid:[[:space:]]*([a-z0-9]+)/,m)) commit_cid[e]=m[1] }

# ---- Settlement ----
/Settlement for epoch[[:space:]]*[0-9]+/ { if(match($0,/Settlement for epoch[[:space:]]*([0-9]+)/,m)){ settle_e=m[1]; add_epoch(settle_e) } }
$0 ~ /Settlement Complete/ && /epoch_settled .*:[[:space:]]*([0-9]+)/ {
  if(match($0,/epoch_settled .*:[[:space:]]*([0-9]+)/,m)){ e=m[1]; settled[e]=1; if(match($0,/miners_scored:[[:space:]]*([0-9]+)/,mm)) miners_scored[e]=mm[1] }
}

/mode=[a-zA-Z-]+/ { if(settle_e!="" && match($0,/mode=([a-zA-Z-]+)/,m)) weights_mode[settle_e]=m[1] }
$0 ~ /nonzero:[[:space:]]*[0-9]+/ { if(settle_e!="" && match($0,/nonzero:[[:space:]]*([0-9]+)/,m)) weights_nonzero[settle_e]=m[1] }
$0 ~ /max:[[:space:]]*[0-9.]+/    { if(settle_e!="" && match($0,/max:[[:space:]]*([0-9.]+)/,m)) weights_max[settle_e]=m[1] }
$0 ~ /sum\(scores\):[[:space:]]*[0-9.]+/ { if(settle_e!="" && match($0,/sum\(scores\):[[:space:]]*([0-9.]+)/,m)) sum_scores[settle_e]=m[1] }

/Budget Accounting .* settlement/ { }
$0 ~ /spent_from_payload .*:[[:space:]]*([0-9.]+)/    { if(settle_e!="" && match($0,/spent_from_payload .*:[[:space:]]*([0-9.]+)/,m)) spent_payload[settle_e]=m[1] }
$0 ~ /leftover_from_payload .*:[[:space:]]*([0-9.]+)/ { if(settle_e!="" && match($0,/leftover_from_payload .*:[[:space:]]*([0-9.]+)/,m)) leftover_payload[settle_e]=m[1] }
$0 ~ /target_budget .*:[[:space:]]*([0-9.]+)/         { if(settle_e!="" && match($0,/target_budget .*:[[:space:]]*([0-9.]+)/,m)) target_budget[settle_e]=m[1] }
$0 ~ /credited_value .*:[[:space:]]*([0-9.]+)/        { if(settle_e!="" && match($0,/credited_value .*:[[:space:]]*([0-9.]+)/,m)) credited_value[settle_e]=m[1] }
$0 ~ /burn_deficit .*:[[:space:]]*([0-9.]+)/          { if(settle_e!="" && match($0,/burn_deficit .*:[[:space:]]*([0-9.]+)/,m)) burn_deficit[settle_e]=m[1] }

# ---- α scan & paid pools ----
/events\(sample\):/ {
  if(settle_e=="") next
  s=$0; gsub(/\},[[:space:]]*\{/,"}\n{",s)
  n=split(s, parts, /\n/)
  for(i=1;i<=n;i++){
    p=parts[i]; ck=""; amt=0
    if(match(p,/"src_ck":"([A-Za-z0-9]+)/,mck)) ck=mck[1]
    if(match(p,/amt\(rao\)":([0-9]+)/,mp)) amt=mp[1]+0
    if(amt>0){ alpha_paid_rao[settle_e]+=amt; if(ck!="") alpha_paid_by_ck[settle_e,ck]+=amt }
  }
}
/paid_pools\(sample\):/ {
  if(settle_e=="") next
  s=$0; gsub(/\},[[:space:]]*\{/,"}\n{",s)
  n=split(s, parts, /\n/)
  for(i=1;i<=n;i++){
    p=parts[i]; ck=""; pr=0
    if(match(p,/"ck":"([A-Za-z0-9]+)/,mck)) ck=mck[1]
    if(match(p,/paid_rao":([0-9]+)/,mp)) pr=mp[1]+0
    if(pr>0){ alpha_paid_rao[settle_e]+=pr; if(ck!="") alpha_paid_by_ck[settle_e,ck]+=pr }
  }
}

# ---- Errors ----
/Cannot connect to host/ { e=(auction_e!=""?auction_e:(budget_e!=""?budget_e:current_e)); conn_err[e]++ }
/TimeoutError#/          { e=(auction_e!=""?auction_e:(budget_e!=""?budget_e:current_e)); timeouts[e]++ }

END{
  if(nOrder==0){ print "No epochs found in input."; exit 1 }
  start = nOrder - wantN + 1; if(start<1) start=1
  lastCnt=0; for(i=start;i<=nOrder;i++){ pick[order[i]]=1; last[++lastCnt]=order[i] }

  agg_epochs=lastCnt
  agg_commits=agg_settled=agg_winner_epochs=agg_winners=0
  agg_value=agg_budget=agg_leftover=0
  agg_ack_recv=agg_ack_total=0
  agg_winack_recv=agg_winack_total=0
  agg_reach_sum=agg_reach_den=0
  w_normal=w_burn=w_other=0
  agg_conn=agg_timeo=0

  mat_epochs=0
  mat_value_expected=mat_value_credited=0.0
  mat_alpha_expected_rao=mat_alpha_paid_rao=0
  mat_unpaid_invoices=0
  epochs_with_unpaid=0

  for(j=1;j<=lastCnt;j++){
    e=last[j]
    if(commit_published[e]) agg_commits++
    if(settled[e]) agg_settled++

    if(nz(winners_count[e],0)>0) agg_winner_epochs++
    agg_winners += nz(winners_count[e],0)
    agg_value   += nz(winners_value[e],0)

    agg_budget   += nz(my_budget[e],0)
    agg_leftover += nz(budget_leftover[e],0)

    agg_ack_recv  += nz(acks_recv[e],0)
    agg_ack_total += nz(acks_total[e],0)

    agg_winack_recv  += nz(winacks_recv[e],0)
    agg_winack_total += nz(winacks_total[e],0)

    if(nz(ax_rcv[e],0) > 0){ agg_reach_sum += nz(ax_usable[e],0); agg_reach_den += nz(ax_rcv[e],0) }

    if(weights_mode[e]=="normal") w_normal++
    else if(weights_mode[e]=="burn-all") w_burn++
    else if(weights_mode[e]!="") w_other++

    agg_conn  += nz(conn_err[e],0)
    agg_timeo += nz(timeouts[e],0)

    if(settled[e]){
      mat_epochs++
      mat_value_expected     += nz(spent_payload[e],0)
      mat_value_credited     += nz(credited_value[e],0)
      mat_alpha_expected_rao += nz(invoices_alpha_rao[e],0)
      mat_alpha_paid_rao     += nz(alpha_paid_rao[e],0)
      unpaid_here=0
      for(k in inv_by_ck){
        split(k,K,SUBSEP); ek=K[1]; ck=K[2]
        if(ek!=e) continue
        er = nz(inv_by_ck[ek,ck],0)
        pr = nz(alpha_paid_by_ck[ek,ck],0)
        if(pr < er){ unpaid_here++; unpaid_ck[e,ck]=er-pr }
      }
      mat_unpaid_invoices += unpaid_here
      if(unpaid_here>0) epochs_with_unpaid++
    }
  }

  ack_rate    = (agg_ack_total>0    ? 100.0*agg_ack_recv/agg_ack_total : 0)
  winack_rate = (agg_winack_total>0 ? 100.0*agg_winack_recv/agg_winack_total : 0)
  reach_rate  = (agg_reach_den>0    ? 100.0*agg_reach_sum/agg_reach_den : 0)
  budget_used = (agg_budget>0       ? 100.0*(agg_budget-agg_leftover)/agg_budget : 0)

  value_shortfall = mat_value_expected - mat_value_credited; if(value_shortfall<0) value_shortfall=0
  alpha_shortfall_rao = mat_alpha_expected_rao - mat_alpha_paid_rao; if(alpha_shortfall_rao<0) alpha_shortfall_rao=0

  fmt="%-34s %s\n"
  line="--------------------------------------------------------------------"
  printf("%s\n", line)
  printf("Validator Health (last %d epochs: %s..%s)\n", agg_epochs, last[1], last[lastCnt])
  printf("%s\n", line)

  printf(fmt, "Epochs analyzed:", agg_epochs)
  printf(fmt, "Commitments published:", sprintf("%d (%.1f%%)", agg_commits, (agg_epochs>0?100.0*agg_commits/agg_epochs:0)))
  printf(fmt, "Settlements completed:", sprintf("%d (%.1f%%)", agg_settled, (agg_epochs>0?100.0*agg_settled/agg_epochs:0)))

  printf(fmt, "Epochs with winners:", sprintf("%d / %d", agg_winner_epochs, agg_epochs))
  printf(fmt, "TAO spent (winners VALUE):", sprintf("%.6f", agg_value))
  printf(fmt, "Budget (my total TAO):", sprintf("%.6f", agg_budget))
  printf(fmt, "Budget leftover (TAO):", sprintf("%.6f", agg_leftover))
  printf(fmt, "Budget usage (est.):", sprintf("%.2f%%", budget_used))

  printf(fmt, "Auction ACK rate:", sprintf("%.2f%% (%d/%d)", ack_rate, agg_ack_recv, agg_ack_total))
  if(agg_winack_total>0) printf(fmt, "Win ACK rate:", sprintf("%.2f%% (%d/%d)", winack_rate, agg_winack_recv, agg_winack_total))
  if(agg_reach_den>0)    printf(fmt, "Axon reachability:", sprintf("%.2f%% (%d/%d usable)", reach_rate, agg_reach_sum, agg_reach_den))

  printf(fmt, "Weights preview modes:", sprintf("normal=%d burn-all=%d other=%d", w_normal, w_burn, w_other))
  printf("%s\n", line)

  printf("Payments (settled epochs only)\n")
  printf(fmt, "Settled epochs considered:", mat_epochs)
  printf(fmt, "VALUE expected vs credited:", sprintf("%.6f vs %.6f TAO (shortfall=%.6f)", mat_value_expected, mat_value_credited, value_shortfall))
  printf(fmt, "α expected vs paid:", sprintf("%.4f vs %.4f α (shortfall=%.4f)", toAlpha(mat_alpha_expected_rao), toAlpha(mat_alpha_paid_rao), toAlpha(alpha_shortfall_rao)))
  printf(fmt, "Unpaid/partial invoices:", sprintf("%d epochs with unpaid: %d", mat_unpaid_invoices, epochs_with_unpaid))
  printf("%s\n", line)

  if(agg_reach_den>0 && reach_rate < 20.0) print "WARN: Low axon reachability (<20%). Supply constrained."
  if(agg_ack_total>0 && ack_rate < 20.0)   print "WARN: Low auction ACK rate (<20%). Miners unresponsive."
  tburn=0; ttarget=0; for(j=1;j<=lastCnt;j++){ e=last[j]; tburn+=nz(burn_deficit[e],0); ttarget+=nz(target_budget[e],0) }
  if(tburn>0 && ttarget>0 && (100.0*tburn/ttarget)>50.0) print "WARN: Large burn deficit (>50% of target). Expect UID0 skew."
  printf("%s\n", line)

  if(!detailed) exit 0

  print "DETAILED EPOCH REPORT"
  print line

  for(jj=lastCnt; jj>=1; jj--){
    e=last[jj]
    printf("Epoch %s\n", e)

    print "  Auction"
    if(nz(acks_total[e],0)>0){
      ar = 100.0*nz(acks_recv[e],0)/nz(acks_total[e],1)
      printf("    acks:              %d/%d (%.2f%%)\n", nz(acks_recv[e],0), nz(acks_total[e],0), ar)
    }
    if(nz(ax_rcv[e],0)>0){
      rr = 100.0*nz(ax_usable[e],0)/nz(ax_rcv[e],1)
      printf("    axons usable:      %d/%d (%.2f%%)\n", nz(ax_usable[e],0), nz(ax_rcv[e],0), rr)
    }
    if(my_budget[e]!="")       printf("    budget (my):       %.6f TAO  @price≈%s TAO/α\n", nz(my_budget[e],0), nzs(base_price[e],"?"))
    if(budget_leftover[e]!="") printf("    leftover:          %.6f TAO\n", nz(budget_leftover[e],0))
    if(nz(winners_count[e],0)>0) printf("    winners:           %d rows, total VALUE=%.6f TAO\n", nz(winners_count[e],0), nz(winners_value[e],0))
    else                         printf("    winners:           none\n")
    if(early_winners[e]!="")   printf("    cleared winners:   %d\n", nz(early_winners[e],0))
    if(nz(winacks_total[e],0)>0){
      wr = 100.0*nz(winacks_recv[e],0)/nz(winacks_total[e],1)
      printf("    win acks:          %d/%d (%.2f%%)\n", nz(winacks_recv[e],0), nz(winacks_total[e],0), wr)
    }

    # Reputation caps only if captured
    if(rep_seen[e]){
      print "  Reputation caps (per coldkey)"
      printf("      %-14s  %-10s  %-10s\n", "Coldkey", "Quota", "Cap(TAO)")
      for(k in rep_cap){
        split(k,K,SUBSEP); ek=K[1]; ck=K[2]
        if(ek!=e) continue
        printf("      %-14s  %-10.3f  %-10.6f\n", ck, nz(rep_q[k],0), nz(rep_cap[k],0))
      }
    }

    if(nz(bcount[e],0)>0){
      print "  Bids — ordered by VALUE (TAO)"
      printf("      %5s  %-14s  %6s  %6s  %6s  %9s  %11s  %9s\n", "UID","CK","Subnet","W_bps","Disc","Bid α","Price TAO/α","VALUE")
      for(i=1;i<=bcount[e];i++){
        uu=bids_uid[key(e,i)]
        ck=bids_ck[key(e,i)]
        ss=bids_sid[key(e,i)]
        wb=bids_wbps[key(e,i)]
        dc=bids_disc[key(e,i)]
        ba=bids_alpha[key(e,i)]
        pr=bids_price[key(e,i)]
        va=bids_value[key(e,i)]
        printf("      %5s  %-14s  %6s  %6s  %6s  %9.4f  %11.8f  %9.6f\n", uu, ck, ss, wb, dc, ba, pr, va)
      }
    }

    if(nz(wcount[e],0)>0){
      print "  Winning bids (acceptances)"
      printf("      %5s  %-14s  %6s  %9s  %9s  %6s  %6s  %9s  %5s\n", "UID","CK","Subnet","Req α","Acc α","Disc","W_bps","VALUE","Fill")
      for(i=1;i<=wcount[e];i++){
        uu=win_uid[key(e,i)]
        ck=win_ck[key(e,i)]
        ss=win_sid[key(e,i)]
        rq=win_req_alpha[key(e,i)]
        ac=win_acc_alpha[key(e,i)]
        dc=win_disc[key(e,i)]
        wb=win_wbps[key(e,i)]
        va=win_value[key(e,i)]
        fl=win_fill[key(e,i)]
        printf("      %5s  %-14s  %6s  %9.4f  %9.4f  %6s  %6s  %9.6f  %5s\n", uu, ck, ss, rq, ac, dc, wb, va, fl)
      }
    }

    print "  Commitment"
    if(staged_miners[e]!="" || staged_lines[e]!="")
      printf("    staged:            miners=%s lines=%s\n", nzs(staged_miners[e],"?"), nzs(staged_lines[e],"?"))
    if(commit_has_inv[e]!="")  printf("    payload:           has_inv=%s\n", commit_has_inv[e])
    was_pub = (commit_published[e] ? "yes" : "no")
    if(commit_published[e] && commit_cid[e]!="")
      printf("    published:         %s (cid=%s…)\n", was_pub, substr(commit_cid[e],1,20))
    else
      printf("    published:         %s\n", was_pub)

    print "  Settlement"
    if(settled[e]){
      ms = nz(miners_scored[e],0)
      printf("    status:            complete (miners_scored=%d)\n", ms)
      if(weights_mode[e]!=""){
        wn = nzs(weights_nonzero[e],"?")
        wmax = nz(weights_max[e],0)
        ssum = nz(sum_scores[e],0)
        printf("    weights:           mode=%s nonzero=%s max=%.6f sum(scores)=%.6f\n", weights_mode[e], wn, wmax, ssum)
      }
      if(spent_payload[e]!="" || credited_value[e]!="" || burn_deficit[e]!=""){
        sp = nz(spent_payload[e],0)
        cv = nz(credited_value[e],0)
        bd = nz(burn_deficit[e],0)
        tb = nz(target_budget[e],0)
        lo = nz(leftover_payload[e],0)
        printf("    accounting:        spent=%.6f credited=%.6f burn_deficit=%.6f (target=%.6f leftover=%.6f)\n", sp, cv, bd, tb, lo)
      }
      exp_rao = nz(invoices_alpha_rao[e],0)
      paid_rao = nz(alpha_paid_rao[e],0)
      printf("  Payments\n")
      printf("    invoices:          %s VALUE=%.6f TAO, α=%.4f\n", (invoices_value_tao[e]!=""?"present":"missing"), nz(invoices_value_tao[e],0), toAlpha(exp_rao))
      printf("    paid α:            %.4f α (shortfall=%.4f α)\n", toAlpha(paid_rao), toAlpha(exp_rao-paid_rao))

      unpaid_listed=0
      for(k in inv_by_ck){
        split(k,K,SUBSEP); ek=K[1]; ck=K[2]
        if(ek!=e) continue
        er = nz(inv_by_ck[ek,ck],0)
        pr = nz(alpha_paid_by_ck[ek,ck],0)
        if(pr < er){
          if(unpaid_listed==0) print "    unpaid miners:"
          printf("      - %s  need=%.4f α  paid=%.4f α  short=%.4f α\n", ck, toAlpha(er), toAlpha(pr), toAlpha(er-pr))
          unpaid_listed++
        }
      }
      if(unpaid_listed==0) print "    unpaid miners:     none"
    } else {
      print "    status:            pending (payments will be checked at settlement)"
    }

    if(nz(conn_err[e],0)>0 || nz(timeouts[e],0)>0)
      printf("  Errors               connect=%d timeouts=%d\n", nz(conn_err[e],0), nz(timeouts[e],0))

    print line
  }
}
' "$INPUT"
