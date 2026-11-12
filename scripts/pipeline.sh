#!/usr/bin/env bash
# Receiver pipeline for the dashboard: SDR -> demodulator -> iridium-parser, decoded frames on stdout.
#   SDR=hackrf scripts/pipeline.sh 20261108-120000
# writes logs/frames-<stamp>.bits (demodulated), logs/frames-<stamp>.parsed (decoded) and
# logs/extractor-<stamp>.log (receiver statistics and errors).
#
# ui/server.py starts this in its own process group and stops it with SIGINT to the group: the SDR and
# the demodulator stop, while the stages after them ignore SIGINT, so they drain what is left in the
# pipe and the last frames are not lost. Antenna power is switched off on the way out.
#
# IRIDIUM_FAKE_BITS=<file.bits> replaces the radio with a file of demodulated bits (tests, demos).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/scripts/env.sh"
TS="${1:?usage: pipeline.sh <stamp>}"
mkdir -p "$LOGS"
BITS="$LOGS/frames-$TS.bits"; PARSED="$LOGS/frames-$TS.parsed"; EXTLOG="$LOGS/extractor-$TS.log"

[[ -f "$TOOLKIT/iridium-parser.py" ]] || { echo "iridium-toolkit missing: run ./install.sh" >&2; exit 3; }
if [[ -n "${IRIDIUM_FAKE_BITS:-}" ]]; then
  [[ -f "$IRIDIUM_FAKE_BITS" ]] || { echo "IRIDIUM_FAKE_BITS: no such file: $IRIDIUM_FAKE_BITS" >&2; exit 3; }
  DEMOD_USED="replayed bits"
  run_receiver() {   # one line every few ms, then wait for the stop signal like a radio would
    python3 -u -c 'import sys, time
try:
    for line in open(sys.argv[1], errors="replace"):
        sys.stdout.write(line); time.sleep(float(sys.argv[2]))
    time.sleep(1e9)
except (KeyboardInterrupt, BrokenPipeError):
    pass' "$IRIDIUM_FAKE_BITS" "${IRIDIUM_FAKE_DELAY:-0.01}"
  }
else
  require_install || exit 3
  build_receiver || exit 4
fi

trap : INT TERM                 # survive the stop signal: the cleanup below still has to run
trap antenna_power_off EXIT
echo "pipeline: sdr=$SDR demod=$DEMOD_USED" | tee -a "$EXTLOG" >&2    # the dashboard reads both copies

# a stage that ignores SIGINT (ignored signals stay ignored across exec), so it ends on end-of-input
drain() { ( trap '' INT; exec "$@" ); }

run_receiver "" 2>> "$EXTLOG" \
  | drain grep --line-buffered '^RAW' | drain tee "$BITS" \
  | drain python3 -u "$TOOLKIT/iridium-parser.py" --harder --uw-ec 2>> "$EXTLOG" \
  | drain tee "$PARSED"
exit 0
