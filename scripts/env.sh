# shellcheck shell=bash
# Common environment for all run scripts. Source it:  . "$ROOT/scripts/env.sh"
# Sets ROOT, VENDOR, LOGS, TOOLKIT, EXTRACTOR, SNIFFER, SDR, SDR_CONF, DECIMATION, LD_LIBRARY_PATH, PYTHONPATH.

ROOT="${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENDOR="$ROOT/vendor"
TOOLKIT="$VENDOR/iridium-toolkit"
EXTRACTOR="$VENDOR/gr-iridium/bin/iridium-extractor"
SNIFFER="$VENDOR/iridium-sniffer/bin/iridium-sniffer"
# install.sh writes vendor/paths.env when it reuses tools already installed on the system
# (e.g. DragonOS ships gr-iridium). It may override EXTRACTOR, SNIFFER and/or TOOLKIT.
# shellcheck disable=SC1091
[[ -f "$VENDOR/paths.env" ]] && . "$VENDOR/paths.env"
# an explicit IRIDIUM_TOOLKIT wins; exported so ui/server.py uses the same copy
TOOLKIT="${IRIDIUM_TOOLKIT:-$TOOLKIT}"
export IRIDIUM_TOOLKIT="$TOOLKIT"
# the dashboard decodes IQ recordings with the same iridium-sniffer
export IRIDIUM_SNIFFER="$SNIFFER"
export IRIDIUM_EXTRACTOR="$EXTRACTOR"

# where recordings go (IRIDIUM_LOGS=/some/disk/iridium to keep them elsewhere, e.g. for large IQ files)
LOGS="${IRIDIUM_LOGS:-$ROOT/logs}"
export IRIDIUM_LOGS="$LOGS"

# --- which SDR: SDR=rtl-sdr (default) or SDR=hackrf, or any configs/<name>.conf ---
SDR="${SDR:-rtl-sdr}"
SDR_CONF="${SDR_CONF:-$ROOT/configs/$SDR.conf}"
case "$SDR" in
  hackrf*) DECIMATION="${DECIMATION:-4}" ;;   # 10 MSps: split into 4 channels so the CPU keeps up
  *)       DECIMATION="${DECIMATION:-1}" ;;
esac

_prepend() { # _prepend VAR dir
  local var=$1 dir=$2
  [[ -d "$dir" ]] || return 0
  case ":${!var:-}:" in *":$dir:"*) return 0 ;; esac
  printf -v "$var" '%s' "$dir${!var:+:${!var}}"
  export "${var?}"
}

# gr-iridium (local build only): shared library dir(s) and python package dir, wherever the build put them
if [[ "$EXTRACTOR" == "$VENDOR/"* && -d "$VENDOR/gr-iridium" ]]; then
for d in "$VENDOR"/gr-iridium/lib "$VENDOR"/gr-iridium/lib64 "$VENDOR"/gr-iridium/lib/*-linux-gnu*; do
  compgen -G "$d/libgnuradio-iridium*" >/dev/null && _prepend LD_LIBRARY_PATH "$d"
done
# (|| true: callers run with set -e -o pipefail, and find fails on a partial build)
_pkg=$(find "$VENDOR/gr-iridium" -type d -name iridium -path '*-packages/iridium' 2>/dev/null | head -1 || true)
[[ -n "$_pkg" ]] && _prepend PYTHONPATH "$(dirname "$_pkg")"
fi

# RTL-SDR Blog driver (better RTL-SDR V4 support); prepended so GNU Radio's osmosdr picks it up
for d in "$VENDOR"/rtlsdr-blog/lib "$VENDOR"/rtlsdr-blog/lib64 "$VENDOR"/rtlsdr-blog/lib/*-linux-gnu*; do
  compgen -G "$d/librtlsdr.so*" >/dev/null && _prepend LD_LIBRARY_PATH "$d"
done
RTL_BIAST="$VENDOR/rtlsdr-blog/bin/rtl_biast"
[[ -x "$RTL_BIAST" ]] || RTL_BIAST="$(command -v rtl_biast || true)"

# --- receiver command ---------------------------------------------------------------
# Demodulator: DEMOD=sniffer (iridium-sniffer), DEMOD=gr (gr-iridium) or DEMOD=auto (default:
# iridium-sniffer if it is installed, else gr-iridium). iridium-sniffer found 28-51 % more frames
# than gr-iridium on identical recordings and needs far less CPU.
demod_choice() {
  case "${DEMOD:-auto}" in
    gr|gr-iridium) echo gr ;;
    sniffer|iridium-sniffer) echo sniffer ;;
    *) [[ -x "$SNIFFER" ]] && echo sniffer || echo gr ;;
  esac
}

# Sets RECV_BIN, RECV_ARGS, DEMOD_USED and SOURCE_CMD (array, may be empty):
#  - configs with a [hackrf-transfer] section: hackrf_transfer (explicit IF) | demodulator reading stdin
#  - RTL-SDR with iridium-sniffer: SoapySDR (uses the Blog driver through LD_LIBRARY_PATH)
#  - everything else: gr-iridium opens the SDR itself through osmosdr
build_receiver() {
  local demod; demod=$(demod_choice)
  if [[ $demod == sniffer && ! -x "$SNIFFER" ]]; then
    echo "iridium-sniffer is not installed (run ./install.sh), using gr-iridium" >&2; demod=gr
  fi
  SOURCE_CMD=(); RECV_BIN="$EXTRACTOR"; RECV_ARGS=(-D "$DECIMATION" "$SDR_CONF")
  local vars
  vars=$(python3 - "$SDR_CONF" <<'PYCONF'
import configparser, re, sys
cp = configparser.ConfigParser(inline_comment_prefixes=("#",)); cp.read(sys.argv[1])
o = cp["osmosdr-source"] if cp.has_section("osmosdr-source") else {}
fc = int(float(o.get("center_freq", 1622e6))); fs = int(float(o.get("sample_rate", 10e6)))
dev = o.get("device_args", "").strip("'\"")
bias = 1 if re.search(r"bias=1", dev) else 0
kind = "hackrf" if "hackrf" in dev else ("rtl" if "rtl" in dev else "other")
print(f"C_FC={fc} C_FS={fs} C_GAIN={int(float(o.get('gain', 30)))} C_BIAS={bias} C_KIND={kind}", end=" ")
if cp.has_section("hackrf-transfer"):
    h = cp["hackrf-transfer"]
    ifq = int(float(h.get("if_freq", 2250e6)))
    print(f"HT=1 HT_IF={ifq} HT_LO={ifq + fc} HT_BW={int(float(h.get('bandwidth', 9e6)))} "
          f"HT_AMP={int(h.get('amp', 1))} HT_LNA={int(h.get('lna', 40))} HT_VGA={int(h.get('vga', 50))} "
          f"HT_BIAS={int(h.get('bias', 1))} HT_TH={float(h.get('threshold', 16))}")
else:
    print("HT=0")
PYCONF
)
  local C_FC C_FS C_GAIN C_BIAS C_KIND HT=0 HT_IF HT_LO HT_BW HT_AMP HT_LNA HT_VGA HT_BIAS HT_TH
  eval "$vars"
  [[ "${HACKRF_OSMOSDR:-0}" == 1 ]] && { HT=0; demod=gr; }

  if [[ $HT == 1 ]]; then
    command -v hackrf_transfer >/dev/null || { echo "hackrf_transfer not found (install the hackrf package)" >&2; return 1; }
    # -m 1: low-pass image filter (RF below 2.15 GHz); the HackRF needs LO = IF + RF for that path
    SOURCE_CMD=(hackrf_transfer -r - -i "$HT_IF" -o "$HT_LO" -m 1 -s "$C_FS" -b "$HT_BW"
                -a "$HT_AMP" -l "$HT_LNA" -g "$HT_VGA" -p "$HT_BIAS")
    if [[ $demod == sniffer ]]; then
      RECV_BIN="$SNIFFER"; RECV_ARGS=(-f /dev/stdin --format=ci8 -c "$C_FC" -r "$C_FS" -d "${THRESHOLD:-$HT_TH}")
    else
      RECV_ARGS=(-c "$C_FC" -r "$C_FS" -f hackrf -D "$DECIMATION" -d "${THRESHOLD:-$HT_TH}")
    fi
  elif [[ $demod == sniffer && $C_KIND == rtl ]]; then
    RECV_BIN="$SNIFFER"
    RECV_ARGS=(-i soapy:driver=rtlsdr -c "$C_FC" -r "$C_FS" --soapy-gain="$C_GAIN")
    [[ $C_BIAS == 1 ]] && RECV_ARGS+=(-B)
    [[ -n "${THRESHOLD:-}" ]] && RECV_ARGS+=(-d "$THRESHOLD")
  else
    [[ $demod == sniffer ]] && echo "iridium-sniffer has no input path for this config, using gr-iridium" >&2
    [[ -n "${THRESHOLD:-}" ]] && RECV_ARGS=(-d "$THRESHOLD" "${RECV_ARGS[@]}")
  fi
  if [[ "$RECV_BIN" == "$SNIFFER" ]]; then DEMOD_USED=iridium-sniffer; else DEMOD_USED=gr-iridium; fi
}

# run_receiver [seconds] - raw demodulated bits on stdout, receiver log on stderr
# (timeout --foreground: without it timeout moves the radio into its own process group, where Ctrl+C
#  never reaches it and a timed run keeps receiving after the script is interrupted)
run_receiver() {
  local dur=${1:-}
  if (( ${#SOURCE_CMD[@]} )); then
    # the source's messages go to the receiver log too, without its once-a-second transfer statistics
    if [[ -n "$dur" ]]; then timeout --foreground --signal=INT "$dur" "${SOURCE_CMD[@]}"
    else "${SOURCE_CMD[@]}"; fi 2> >(grep --line-buffered -v 'MiB' >&2 || true) | "$RECV_BIN" "${RECV_ARGS[@]}"
  else
    if [[ -n "$dur" ]]; then timeout --foreground --signal=INT "$dur" "$RECV_BIN" "${RECV_ARGS[@]}"
    else "$RECV_BIN" "${RECV_ARGS[@]}"; fi
  fi
}

antenna_power_off() {
  case "$SDR" in
    rtl*) [[ -n "$RTL_BIAST" ]] && "$RTL_BIAST" -b 0 >/dev/null 2>&1 || true ;;
    *) : ;;   # HackRF switches antenna power off by itself when streaming stops
  esac
}

require_install() {
  local missing=0
  [[ -x "$EXTRACTOR" || -x "$SNIFFER" ]] || { echo "no demodulator (gr-iridium / iridium-sniffer) built: run ./install.sh" >&2; missing=1; }
  [[ -f "$TOOLKIT/iridium-parser.py" ]] || { echo "iridium-toolkit missing: run ./install.sh" >&2; missing=1; }
  [[ -f "$SDR_CONF" ]] || { echo "SDR config not found: $SDR_CONF (available: $(cd "$ROOT/configs" && ls *.conf | sed 's/\.conf//' | tr '\n' ' '))" >&2; missing=1; }
  return $missing
}
