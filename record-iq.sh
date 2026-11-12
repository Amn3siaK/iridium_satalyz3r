#!/usr/bin/env bash
# Record raw IQ to a WAV file for IridiScope (File source) or later re-decoding.
#   ./record-iq.sh 600                 RTL-SDR, 10 minutes
#   SDR=hackrf ./record-iq.sh 600      HackRF, 10 minutes (manual IF, as in configs/hackrf.conf)
#   ./record-iq.sh                     until Ctrl+C
# Output: logs/iq/iq-<sdr>-<time>_<centre>Hz_<rate>sps.wav
# Uses the same frequency, rate, gain, IF and antenna power as the live scripts (configs/<sdr>.conf).
# IridiScope reads the sample rate from the WAV header and the centre frequency from the "<number>Hz"
# in the file name, so keep that part of the name if you rename the file.
# Size: RTL-SDR 2.5 MSps = 300 MB/min, HackRF 10 MSps = 1.2 GB/min.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
. "$ROOT/scripts/env.sh"
DUR="${1:-}"
[[ -z "$DUR" || "$DUR" =~ ^[0-9]+$ ]] || { echo "usage: $0 [seconds]" >&2; exit 1; }
[[ -f "$SDR_CONF" ]] || { echo "SDR config not found: $SDR_CONF" >&2; exit 1; }

vars=$(python3 - "$SDR_CONF" <<'PYCONF'
import configparser, re, sys
cp = configparser.ConfigParser(inline_comment_prefixes=("#",)); cp.read(sys.argv[1])
o = cp["osmosdr-source"] if cp.has_section("osmosdr-source") else {}
dev = o.get("device_args", "").strip("'\"")
print(f"FC={int(float(o.get('center_freq', 1622e6)))} FS={int(float(o.get('sample_rate', 10e6)))} "
      f"GAIN={int(float(o.get('gain', 30)))} BIAS={1 if re.search(r'bias=1', dev) else 0} "
      f"KIND={'hackrf' if 'hackrf' in dev else ('rtl' if 'rtl' in dev else 'other')}", end=" ")
h = cp["hackrf-transfer"] if cp.has_section("hackrf-transfer") else {}
print(f"IF={int(float(h.get('if_freq', 2250e6)))} BW={int(float(h.get('bandwidth', 9e6)))} "
      f"AMP={int(h.get('amp', 1))} LNA={int(h.get('lna', 40))} VGA={int(h.get('vga', 50))} "
      f"HBIAS={int(h.get('bias', 1))}")
PYCONF
)
eval "$vars"

case "$KIND" in
  hackrf)
    command -v hackrf_transfer >/dev/null || { echo "hackrf_transfer not found (install the hackrf package)" >&2; exit 1; }
    # manual IF; -m 1 = low-pass image filter, which needs LO = IF + RF (see configs/hackrf.conf)
    SRC=(hackrf_transfer -r - -i "$IF" -o "$((IF + FC))" -m 1 -s "$FS" -b "$BW"
         -a "$AMP" -l "$LNA" -g "$VGA" -p "$HBIAS")
    SIGNED=(--signed); ANT=$HBIAS ;;
  rtl)
    RTL_SDR="$VENDOR/rtlsdr-blog/bin/rtl_sdr"          # the Blog driver build knows the V4
    [[ -x "$RTL_SDR" ]] || RTL_SDR=$(command -v rtl_sdr) || { echo "rtl_sdr not found (run ./install.sh)" >&2; exit 1; }
    SRC=("$RTL_SDR" -f "$FC" -s "$FS" -g "$GAIN" -)
    SIGNED=(); ANT=$BIAS ;;
  *) echo "record-iq.sh supports RTL-SDR and HackRF configs only" >&2; exit 1 ;;
esac

OUTDIR="$LOGS/iq"; mkdir -p "$OUTDIR"
OUT="$OUTDIR/iq-$SDR-$(date +%Y%m%d-%H%M%S)_${FC}Hz_${FS}sps.wav"
RATE_MB=$(( FS * 2 / 1000000 ))
FREE_MB=$(( $(df -Pk "$OUTDIR" | awk 'NR==2 {print $4}') / 1024 ))
if [[ -n "$DUR" ]]; then
  NEED_MB=$(( RATE_MB * DUR ))
  (( NEED_MB < FREE_MB - 1024 )) || { echo "not enough disk space: need ~${NEED_MB} MB, ${FREE_MB} MB free" >&2; exit 1; }
fi

trap antenna_power_off EXIT
if [[ $KIND == rtl && $ANT == 1 ]]; then
  [[ -x "$RTL_BIAST" ]] && "$RTL_BIAST" -b 1 >/dev/null 2>&1 || echo "warning: could not switch on RTL-SDR antenna power (rtl_biast)" >&2
fi

echo "SDR: $SDR · centre $(awk "BEGIN{printf \"%.3f\", $FC/1e6}") MHz · $(awk "BEGIN{printf \"%.2f\", $FS/1e6}") MSps · antenna power $([[ $ANT == 1 ]] && echo on || echo off)"
[[ $KIND == hackrf ]] && echo "HackRF IF $((IF / 1000000)) MHz · amp $AMP · LNA $LNA · VGA $VGA"
echo "recording ${DUR:+$DUR s }to $OUT (${RATE_MB} MB/s, ${FREE_MB} MB free)$([[ -z "$DUR" ]] && echo ", Ctrl+C to stop")"

if [[ -n "$DUR" ]]; then
  timeout --foreground --signal=INT "$DUR" "${SRC[@]}" 2> "$OUT.log" || true
else
  "${SRC[@]}" 2> "$OUT.log" || true
fi | python3 "$ROOT/scripts/iq2wav.py" "${SIGNED[@]}" --rate "$FS" "$OUT" || {
  echo "Recording failed. The radio said:" >&2
  grep -v '^$' "$OUT.log" | tail -4 >&2 || true
  rm -f "$OUT.log"
  exit 1
}

echo "open it in IridiScope: File source -> $OUT"
