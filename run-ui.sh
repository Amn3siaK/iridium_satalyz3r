#!/usr/bin/env bash
# iridium_satalyz3r: live decoding with the web dashboard.
#   ./run-ui.sh                        receive until Ctrl+C
#   ./run-ui.sh 1800                   receive for 30 min (the dashboard stays up until Ctrl+C)
#   SDR=hackrf ./run-ui.sh             use the HackRF (configs/hackrf.conf)
#   ./run-ui.sh --browse               don't start the radio: start it, or open recordings, from the dashboard
#   ./run-ui.sh --demo                 simulated reception, to try the dashboard without a radio
#   ./run-ui.sh --replay FILE.parsed   replay a saved session
# Dashboard: http://localhost:8888/   (PORT=xxxx to change)
# The receiver can also be stopped, restarted and switched to the other radio from the dashboard.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
. "$ROOT/scripts/env.sh"
PORT=${PORT:-8888}
server=(python3 "$ROOT/ui/server.py" --port "$PORT")

case "${1:-}" in
  -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
  --replay)
    [[ -f "${2:-}" ]] || { echo "usage: $0 --replay FILE.parsed" >&2; exit 1; }
    exec "${server[@]}" --replay "$2" --replay-delay "${REPLAY_DELAY:-0.05}" ;;
  --browse) exec "${server[@]}" --browse ;;
  --demo)   exec "${server[@]}" --demo ;;
  ""|*[!0-9]*)
    [[ -z "${1:-}" ]] || { echo "unknown option: $1 (see $0 --help)" >&2; exit 1; }
    require_install
    exec "${server[@]}" --start --sdr "$SDR" ;;
  *)
    require_install
    exec "${server[@]}" --start --sdr "$SDR" --seconds "$1" ;;
esac
