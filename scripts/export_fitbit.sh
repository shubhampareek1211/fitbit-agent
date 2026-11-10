#!/usr/bin/env bash
# Export Google Health API (Fitbit) data to JSON files using the official ghealth CLI.
#
# Usage: scripts/export_fitbit.sh [FROM] [TO] [OUT_DIR]
#   FROM     default 2026-08-01
#   TO       default today
#   OUT_DIR  default data/raw
#
# Layout:
#   OUT_DIR/daily/<type>__<from>__<to>.json      daily rollups (chunked: API caps rollups at 90 days)
#   OUT_DIR/records/<type>__<from>__<to>.json    sessions & daily summaries (sleep, exercise, resting HR, ...)
#   OUT_DIR/intraday/<type>/<date>.json          high-frequency samples, one file per day
#   OUT_DIR/manifest.tsv                         type, kind, range, points, status
#
# Re-running is safe: existing non-empty files are skipped (delete a file to refetch it).
set -uo pipefail

FROM="${1:-2026-08-01}"
TO="${2:-$(date +%Y-%m-%d)}"
OUT="${3:-data/raw}"
CHUNK_DAYS=30          # records & sessions
ROLLUP_CHUNK_DAYS=14   # some rollups (heart-rate, total-calories, active-minutes, ...) cap at 14 days

DAILY_ROLLUP=(steps distance floors active-minutes active-zone-minutes total-calories
  active-energy-burned time-in-heart-rate-zone calories-in-heart-rate-zone heart-rate
  sedentary-period hydration-log weight)
RECORDS=(sleep exercise daily-resting-heart-rate daily-heart-rate-variability
  daily-oxygen-saturation daily-respiratory-rate daily-sleep-temperature-derivations
  daily-vo2-max run-vo2-max vo2-max respiratory-rate-sleep-summary weight body-fat height
  irregular-rhythm-notification hydration-log nutrition-log)
INTRADAY=(heart-rate steps heart-rate-variability oxygen-saturation activity-level)

command -v ghealth >/dev/null || { echo "ghealth not on PATH" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq not installed" >&2; exit 1; }
ghealth auth status >/dev/null 2>&1 || { echo "ghealth not authenticated: run 'ghealth auth login'" >&2; exit 1; }

mkdir -p "$OUT/daily" "$OUT/records" "$OUT/intraday"
MANIFEST="$OUT/manifest.tsv"
[[ -f "$MANIFEST" ]] || printf "type\tkind\tfrom\tto\tpoints\tstatus\n" > "$MANIFEST"

add_days() { date -j -v+"$2"d -f %Y-%m-%d "$1" +%Y-%m-%d; }   # macOS date

# fetch <kind> <type> <from> <to> <file> <ghealth args...>
fetch() {
  local kind=$1 type=$2 from=$3 to=$4 file=$5; shift 5
  if [[ -s "$file" ]]; then return; fi
  local tmp="$file.tmp"
  if ghealth data "$type" "$@" --from "$from" --to "$to" > "$tmp" 2> "$tmp.err"; then
    local n; n=$(jq '.dataPoints | length' "$tmp" 2>/dev/null || echo "?")
    if [[ "$n" == "0" ]]; then
      rm -f "$tmp"; status=empty
    else
      mv "$tmp" "$file"; status=ok
    fi
  else
    n=0; status="error: $(head -c 200 "$tmp.err" "$tmp" 2>/dev/null | tr '\n\t' '  ')"
    rm -f "$tmp"
  fi
  rm -f "$tmp.err"
  printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$type" "$kind" "$from" "$to" "$n" "$status" >> "$MANIFEST"
  printf "  %-36s %s..%s  %6s  %s\n" "$kind/$type" "$from" "$to" "$n" "${status:0:60}"
}

echo "Exporting $FROM..$TO into $OUT"

echo "[1/3] daily rollups"
for type in "${DAILY_ROLLUP[@]}"; do
  s=$FROM
  while [[ ! "$s" > "$TO" ]]; do
    e=$(add_days "$s" $((ROLLUP_CHUNK_DAYS - 1))); [[ "$e" > "$TO" ]] && e=$TO
    fetch daily "$type" "$s" "$e" "$OUT/daily/${type}__${s}__${e}.json" daily-rollup
    s=$(add_days "$e" 1)
  done
done

echo "[2/3] records (sessions & daily summaries)"
for type in "${RECORDS[@]}"; do
  extra=(list --limit 1000000)
  [[ "$type" == "sleep" ]] && extra+=(--detail)
  s=$FROM
  while [[ ! "$s" > "$TO" ]]; do
    e=$(add_days "$s" $((CHUNK_DAYS - 1))); [[ "$e" > "$TO" ]] && e=$TO
    fetch records "$type" "$s" "$e" "$OUT/records/${type}__${s}__${e}.json" "${extra[@]}"
    s=$(add_days "$e" 1)
  done
done

echo "[3/3] intraday (one file per day)"
for type in "${INTRADAY[@]}"; do
  mkdir -p "$OUT/intraday/$type"
  d=$FROM
  while [[ ! "$d" > "$TO" ]]; do
    fetch intraday "$type" "$d" "$d" "$OUT/intraday/$type/$d.json" list --limit 1000000
    d=$(add_days "$d" 1)
  done
done

echo "Done. Summary of non-empty exports:"
awk -F'\t' 'NR>1 && $6=="ok" {pts[$2"/"$1]+=$5} END {for (k in pts) printf "  %-40s %8d points\n", k, pts[k]}' "$MANIFEST" | sort
grep -c "error" "$MANIFEST" | xargs -I{} echo "  errors logged: {} (see $MANIFEST)"
