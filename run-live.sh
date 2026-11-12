#!/usr/bin/env bash
# Live Iridium decoding in the terminal (no dashboard).
#   ./run-live.sh [seconds]            SDR=hackrf ./run-live.sh  to use the HackRF
# Output: logs/frames-<timestamp>.bits (demodulated) and .parsed (decoded)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
. "$ROOT/scripts/env.sh"
require_install
mkdir -p "$LOGS"
TS=$(date +%Y%m%d-%H%M%S)
BITS="$LOGS/frames-$TS.bits"; PARSED="$LOGS/frames-$TS.parsed"
trap antenna_power_off EXIT

build_receiver
echo "SDR: $SDR · demodulator: $DEMOD_USED · writing $PARSED"

run_receiver "${1:-}" 2> "$LOGS/extractor-$TS.log" | grep --line-buffered '^RAW' | tee "$BITS" \
  | python3 -u "$TOOLKIT/iridium-parser.py" --harder --uw-ec 2>/dev/null | tee "$PARSED" \
  | grep --line-buffered -E '^(IRA|IBC|IMS|ITL):' | cut -c1-200 || true

echo; echo "Frame types:"; awk '{print $1}' "$PARSED" | sort | uniq -c | sort -rn
