#!/usr/bin/env sh
###############################################################################
# epoch_report.sh  –  Pretty summary of a Bittensor‑validator epoch
#
# USAGE:  epoch_report.sh <epoch_number> [log_file]
#
#   • Looks inside the (rotating) PM2 log produced by your validator
#   • Prints: start/end blocks scanned, deposits, rewards, weights, etc.
#   • Colours and sections for quick eyeballing.  Works with plain sh + awk.
###############################################################################

# --- configuration -----------------------------------------------------------
LOGFILE="${2:-/root/.pm2/logs/v73-out.log}"   # override with 2nd arg
EPOCH="$1"

# --- sanity ------------------------------------------------------------------
if [ -z "$EPOCH" ]; then
  echo "Usage: $0 <epoch> [logfile]" >&2
  exit 1
fi
[ -r "$LOGFILE" ] || { echo "Log file not readable: $LOGFILE" >&2; exit 1; }

# --- minimal ANSI helpers ----------------------------------------------------
bold()   { printf '\033[1m%s\033[0m' "$*"; }   # bold text
rule()   { printf '%s\n' "-------------------------------------------"; }

# --- awk does the heavy lifting ---------------------------------------------
awk -v epoch="$EPOCH" '
  # helper: strip everything that is not a digit (keeps NBSP‑tainted matches safe)
  function digits(s){ gsub(/[^0-9]/,"",s); return s }

  BEGIN{
      in_epoch=0
      start_block=end_block=""
      scan_line=""
      deposits_line=""
      total_val=""
      miner_val=""
      weight_uids=""
      weight_vals=""
      weight_ok="NO"
      epoch_head=""
  }

  {
      # detect any [epoch …] marker
      if (match($0, /\[epoch[^\]]*\]/)){
          epnum = digits(substr($0, RSTART, RLENGTH))
          if (epnum == epoch){
              in_epoch=1
              epoch_head=$0
              next
          }
          # we reached another epoch ‑‑ done collecting
          if (in_epoch) { exit }
      }

      if (!in_epoch) next

      # start / end blocks scanned
      if (match($0, /Scanning transfers on.?chain \(([0-9]+)-([0-9]+)\)/, m)){
          start_block=m[1]; end_block=m[2]; next
      }

      # scan summary
      if ($0 ~ /scan finished:/){ scan_line=$0; next }

      # deposits list
      if ($0 ~ /deposits\(after cast\):/){
          sub(/.*deposits\(after cast\):[[:space:]]*/, "", $0)
          deposits_line=$0; next
      }

      # rewards fields
      if (match($0, /total_value:[[:space:]]*([0-9.]+)/, t)){ total_val=t[1]; next }
      if ($0 ~ /value_per_miner_dict:/){
          sub(/.*value_per_miner_dict:[[:space:]]*/, "", $0)
          miner_val=$0; next
      }

      # weights
      if ($0 ~ /non_zero_weight_uids:/){
          sub(/.*non_zero_weight_uids:[[:space:]]*/, "", $0)
          weight_uids=$0; next
      }
      if ($0 ~ /non_zero_weights:/){
          sub(/.*non_zero_weights:[[:space:]]*/, "", $0)
          weight_vals=$0; next
      }
      if ($0 ~ /Successfully set weights/){ weight_ok="YES"; next }
  }

  END{
      # pretty print
      printf "%s\n", bold("Epoch " epoch " report")
      rule()

      if (epoch_head!="") printf "%s\n", epoch_head
      if (start_block!="") printf "Scan blocks : %s – %s\n", start_block, end_block
      if (scan_line!="")   printf "Scan result : %s\n", scan_line
      if (total_val!="")   printf "Total value : %s TAO\n", total_val

      if (miner_val!=""){
          printf "\n%s\n%s\n", bold("Value per miner"), miner_val
      }
      if (deposits_line!=""){
          printf "\n%s\n%s\n", bold("Deposits"), deposits_line
      }
      if (weight_uids!="" || weight_vals!=""){
          printf "\n%s\n", bold("Weights")
          if (weight_uids!="") printf "UIDs    : %s\n", weight_uids
          if (weight_vals!="")printf "Weights : %s\n", weight_vals
          printf "On‑chain set success: %s\n", weight_ok
      }
      print ""
  }

  function bold(s){ return sprintf("\033[1m%s\033[0m", s) }
  function rule(){ printf "-------------------------------------------\n" }
' "$LOGFILE"
