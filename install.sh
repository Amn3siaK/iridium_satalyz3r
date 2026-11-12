#!/usr/bin/env bash
# Installer for iridium_satalyz3r.
#   ./install.sh                  install packages (sudo), fetch + build what is missing into ./vendor
#   ./install.sh --no-deps        skip system packages (install them yourself, see README)
#   ./install.sh --build-all      build local copies even if gr-iridium / iridium-toolkit are already installed
#   ./install.sh --no-rtl-blog    skip the RTL-SDR Blog driver (e.g. HackRF-only setups)
#   ./install.sh --no-sniffer     skip iridium-sniffer (the scripts then use gr-iridium only)
#   ./install.sh --blacklist-dvb  stop the kernel DVB-T driver from grabbing RTL-SDR dongles (sudo)
#   ./install.sh --check          only check the installation
# Nothing is installed system-wide except distro packages (and the optional blacklist file).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENDOR="$ROOT/vendor"; SRC="$VENDOR/src"

# Pinned, tested versions (override with env vars to try newer ones)
GR_IRIDIUM_URL=${GR_IRIDIUM_URL:-https://github.com/muccc/gr-iridium.git}
GR_IRIDIUM_REF=${GR_IRIDIUM_REF:-a09b2546194a644bc756c6f25589ba8869c45433}
TOOLKIT_URL=${TOOLKIT_URL:-https://github.com/muccc/iridium-toolkit.git}
TOOLKIT_REF=${TOOLKIT_REF:-7938ae032e2a4e0da554a611ebc7f12a23101e0f}
RTLBLOG_URL=${RTLBLOG_URL:-https://github.com/rtlsdrblog/rtl-sdr-blog.git}
RTLBLOG_REF=${RTLBLOG_REF:-aed0ea19f3a273370a13c9009b96313c75d54c7b}
SNIFFER_URL=${SNIFFER_URL:-https://github.com/alphafox02/iridium-sniffer.git}
SNIFFER_REF=${SNIFFER_REF:-830e34c957f0a52dedbb11ad726b85d8eef3221a}

DEPS=1; RTLBLOG=1; SNIFF=1; BLACKLIST=0; CHECK_ONLY=0; BUILD_ALL=0
for a in "$@"; do
  case "$a" in
    --no-deps) DEPS=0 ;; --no-rtl-blog) RTLBLOG=0 ;; --no-sniffer) SNIFF=0 ;; --blacklist-dvb) BLACKLIST=1 ;; --check) CHECK_ONLY=1 ;;
    --build-all) BUILD_ALL=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_b=$'\e[1m'; c_0=$'\e[0m'
step() { echo; echo "${c_b}==> $*${c_0}"; }
ok()   { echo "  ${c_ok}✓${c_0} $*"; }
warn() { echo "  ${c_warn}!${c_0} $*"; }
fail() { echo "  ${c_err}✗${c_0} $*"; }
JOBS=$(nproc 2>/dev/null || echo 2)

SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"

install_packages() {
  step "Installing system packages"
  if command -v apt-get >/dev/null; then
    local volk=libvolk-dev
    apt-cache show libvolk-dev >/dev/null 2>&1 || volk=libvolk2-dev
    $SUDO apt-get update
    $SUDO apt-get install -y git cmake build-essential pkg-config \
      gnuradio gnuradio-dev gr-osmosdr "$volk" pybind11-dev libsndfile1-dev libusb-1.0-0-dev \
      python3-numpy python3-crcmod python3-pyproj rtl-sdr hackrf \
      libfftw3-dev libsoapysdr-dev soapysdr-module-rtlsdr libhackrf-dev
  elif command -v dnf >/dev/null; then
    warn "Fedora support is untested"
    $SUDO dnf install -y git cmake gcc-c++ make pkgconf-pkg-config gnuradio gnuradio-devel gr-osmosdr volk-devel \
      pybind11-devel libsndfile-devel libusb1-devel python3-numpy python3-crcmod python3-pyproj rtl-sdr hackrf \
      fftw-devel SoapySDR-devel soapy-rtlsdr hackrf-devel
  elif command -v pacman >/dev/null; then
    warn "Arch support is untested"
    $SUDO pacman -S --needed --noconfirm git cmake base-devel gnuradio gnuradio-osmosdr pybind11 libsndfile libusb \
      python-numpy python-crcmod python-pyproj rtl-sdr hackrf \
      fftw soapysdr soapyrtlsdr
  else
    fail "No supported package manager found. Install the packages listed in README.md, then run: ./install.sh --no-deps"
    exit 1
  fi
}

# --- reuse tools that are already installed (e.g. DragonOS ships gr-iridium) ----------------------
# Each check prints the path and succeeds only if the copy has everything the scripts and dashboard use.
system_gr_iridium() {
  local exe help
  exe=$(command -v iridium-extractor 2>/dev/null) || return 1
  [[ "$exe" == "$VENDOR/"* ]] && return 1
  python3 -c 'import iridium' 2>/dev/null || return 1
  help=$("$exe" --help 2>&1) || return 1
  # stdin input + ci8 from hackrf_transfer (-f hackrf), channelizer (-D), threshold (-d)
  grep -q -- 'hackrf' <<<"$help" && grep -q -- '--decimation' <<<"$help" && grep -q -- '--db' <<<"$help" \
    && grep -qi 'stdin' <<<"$help" || return 1
  echo "$exe"
}

system_sniffer() {
  local exe help
  exe=$(command -v iridium-sniffer 2>/dev/null) || return 1
  [[ "$exe" == "$VENDOR/"* ]] && return 1
  help=$("$exe" --help 2>&1) || true
  # file/pipe input with --format, and SoapySDR (for RTL-SDR) with bias tee and gain
  grep -q -- '--format' <<<"$help" && grep -q -- 'soapy' <<<"$help" && grep -q -- '--bias-tee' <<<"$help" \
    && grep -q -- '--soapy-gain' <<<"$help" || return 1
  echo "$exe"
}

system_toolkit() {
  local d cand=("${IRIDIUM_TOOLKIT:-}" /opt/iridium-toolkit /usr/src/iridium-toolkit /usr/local/src/iridium-toolkit
                /usr/share/iridium-toolkit /usr/local/share/iridium-toolkit "$HOME/iridium-toolkit"
                "$HOME/src/iridium-toolkit" "$HOME/SDR/iridium-toolkit" "$HOME/sdr/iridium-toolkit")
  while IFS= read -r d; do cand+=("$(dirname "$d")"); done < <(
    find /opt /usr/src /usr/local/src /usr/share "$HOME" -maxdepth 4 -name iridium-parser.py \
      -not -path "$VENDOR/*" 2>/dev/null | head -5)
  for d in "${cand[@]}"; do
    [[ -n "$d" && "$d" != "$VENDOR/"* && -f "$d/iridium-parser.py" && -f "$d/reassembler.py" ]] || continue
    # parser options and reassembler modes used by run-ui.sh / ui/server.py
    grep -q -- '--uw-ec' "$d/iridium-parser.py" && grep -q -- '--harder' "$d/iridium-parser.py" || continue
    [[ -f "$d/iridiumtk/reassembler/msg.py" && -f "$d/iridiumtk/reassembler/sbd.py" ]] || continue
    grep -q acars "$d/iridiumtk/reassembler/sbd.py" || continue
    echo "$d"; return 0
  done
  return 1
}

fetch() { # fetch URL REF DIR
  local url=$1 ref=$2 dir=$3
  if [[ -d "$dir/.git" ]]; then
    git -C "$dir" cat-file -e "$ref^{commit}" 2>/dev/null || git -C "$dir" fetch --quiet origin
  else
    git clone --quiet "$url" "$dir"
  fi
  git -C "$dir" -c advice.detachedHead=false checkout --quiet "$ref"
  ok "$(basename "$dir") @ ${ref:0:10}"
}

build_gr_iridium() {
  step "Building gr-iridium (a few minutes)"
  rm -rf "$SRC/gr-iridium/build"
  cmake -S "$SRC/gr-iridium" -B "$SRC/gr-iridium/build" -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$VENDOR/gr-iridium" >/dev/null
  cmake --build "$SRC/gr-iridium/build" -j"$JOBS"
  cmake --install "$SRC/gr-iridium/build" >/dev/null
  ok "installed into vendor/gr-iridium"
}

build_sniffer() {
  step "Building iridium-sniffer"
  rm -rf "$SRC/iridium-sniffer/build"
  cmake -S "$SRC/iridium-sniffer" -B "$SRC/iridium-sniffer/build" -DCMAKE_BUILD_TYPE=Release \
        -DUSE_OPENCL=OFF -DCMAKE_INSTALL_PREFIX="$VENDOR/iridium-sniffer" >/dev/null
  cmake --build "$SRC/iridium-sniffer/build" -j"$JOBS"
  cmake --install "$SRC/iridium-sniffer/build" >/dev/null
  if "$VENDOR/iridium-sniffer/bin/iridium-sniffer" --list 2>&1 | grep -qi soapy || \
     grep -q HAVE_SOAPYSDR "$SRC/iridium-sniffer/build/CMakeFiles/iridium-sniffer.dir/flags.make" 2>/dev/null; then
    ok "installed into vendor/iridium-sniffer (SoapySDR enabled)"
  else
    warn "installed into vendor/iridium-sniffer, but WITHOUT SoapySDR: install libsoapysdr-dev and re-run for RTL-SDR support"
  fi
}

build_rtlblog() {
  step "Building the RTL-SDR Blog driver (local copy, system driver untouched)"
  rm -rf "$SRC/rtl-sdr-blog/build"
  cmake -S "$SRC/rtl-sdr-blog" -B "$SRC/rtl-sdr-blog/build" -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$VENDOR/rtlsdr-blog" -DINSTALL_UDEV_RULES=OFF -DDETACH_KERNEL_DRIVER=ON >/dev/null
  cmake --build "$SRC/rtl-sdr-blog/build" -j"$JOBS"
  cmake --install "$SRC/rtl-sdr-blog/build" >/dev/null
  ok "installed into vendor/rtlsdr-blog"
}

blacklist_dvb() {
  step "Blacklisting the kernel DVB-T driver for RTL-SDR dongles"
  echo 'blacklist dvb_usb_rtl28xxu' | $SUDO tee /etc/modprobe.d/blacklist-rtlsdr.conf >/dev/null
  $SUDO modprobe -r dvb_usb_rtl28xxu 2>/dev/null || warn "driver in use: unplug and replug the dongle"
  ok "/etc/modprobe.d/blacklist-rtlsdr.conf written (delete it to undo)"
}

check() {
  step "Checking installation"
  local bad=0
  # shellcheck source=scripts/env.sh
  . "$ROOT/scripts/env.sh"
  python3 -c 'import sys; assert sys.version_info >= (3, 8)' && ok "python $(python3 -V 2>&1 | cut -d' ' -f2)" || { fail "python 3.8+ required"; bad=1; }
  for m in numpy crcmod pyproj; do
    python3 -c "import $m" 2>/dev/null && ok "python module $m" || { fail "python module $m missing"; bad=1; }
  done
  python3 -c 'from gnuradio import gr; print(gr.version())' >/dev/null 2>&1 && ok "GNU Radio $(python3 -c 'from gnuradio import gr; print(gr.version())')" || { fail "GNU Radio python module missing"; bad=1; }
  python3 -c 'from gnuradio import gr; import osmosdr' 2>/dev/null && ok "gr-osmosdr" || { fail "gr-osmosdr missing (needed for live SDR input)"; bad=1; }
  local where
  where="local build"; [[ "$EXTRACTOR" == "$VENDOR/"* ]] || where="system: $EXTRACTOR"
  [[ -x "$EXTRACTOR" ]] && python3 -c 'import iridium' 2>/dev/null && ok "gr-iridium ($where)" || { fail "gr-iridium not built"; bad=1; }
  where="local build"; [[ "$SNIFFER" == "$VENDOR/"* ]] || where="system: $SNIFFER"
  [[ -x "$SNIFFER" ]] && ok "iridium-sniffer ($where) - default demodulator" \
    || warn "iridium-sniffer not installed: the scripts use gr-iridium (finds fewer frames)"
  # iridium-sniffer opens the RTL-SDR through SoapySDR: without the rtlsdr module it fails at start-up
  if [[ -x "$SNIFFER" ]] && command -v SoapySDRUtil >/dev/null; then
    # capture first: with pipefail, grep -q exiting early would SIGPIPE SoapySDRUtil and fail the check
    local soapy; soapy=$(SoapySDRUtil --check=rtlsdr 2>&1 || true)
    grep -q PRESENT <<<"$soapy" && ok "SoapySDR RTL-SDR module" \
      || warn "SoapySDR RTL-SDR module missing: install soapysdr-module-rtlsdr, or use DEMOD=gr with the RTL-SDR"
  fi
  where="local copy"; [[ "$TOOLKIT" == "$VENDOR/"* ]] || where="system: $TOOLKIT"
  [[ -f "$TOOLKIT/iridium-parser.py" ]] && ok "iridium-toolkit ($where)" || { fail "iridium-toolkit missing"; bad=1; }
  compgen -G "$VENDOR/rtlsdr-blog/lib*/librtlsdr.so*" >/dev/null || compgen -G "$VENDOR/rtlsdr-blog/lib/*/librtlsdr.so*" >/dev/null \
    && ok "RTL-SDR Blog driver" || warn "RTL-SDR Blog driver not built (fine for HackRF; recommended for RTL-SDR V4)"

  if command -v lsusb >/dev/null; then
    lsusb | grep -qi "0bda:2838" && ok "RTL-SDR dongle connected" || true
    lsusb | grep -qi "1d50:6089" && ok "HackRF connected" || true
    lsusb | grep -qiE "0bda:2838|1d50:6089" || warn "no RTL-SDR or HackRF detected on USB (fine for --replay)"
  fi
  if lsmod 2>/dev/null | grep -q '^dvb_usb_rtl28xxu'; then
    warn "kernel DVB-T driver is loaded and may block the RTL-SDR: run ./install.sh --blacklist-dvb --no-deps, then replug"
  fi
  echo
  if [[ $bad -eq 0 ]]; then echo "${c_ok}${c_b}Ready.${c_0} Start with: ./run-ui.sh   then open http://localhost:8888/"
  else echo "${c_err}${c_b}Some checks failed${c_0} (see above)."; fi
  return $bad
}

if [[ $CHECK_ONLY -eq 1 ]]; then check; exit $?; fi

[[ $DEPS -eq 1 ]] && install_packages
mkdir -p "$SRC"
SYS_GR=""; SYS_TK=""; SYS_SN=""
if [[ $BUILD_ALL -eq 0 ]]; then
  step "Looking for tools that are already installed"
  if SYS_GR=$(system_gr_iridium); then ok "gr-iridium: using $SYS_GR"
  else SYS_GR=""; warn "gr-iridium: no usable system copy, will build a local one"; fi
  if SYS_TK=$(system_toolkit); then ok "iridium-toolkit: using $SYS_TK"
  else SYS_TK=""; warn "iridium-toolkit: no usable system copy, will fetch a local one"; fi
  if [[ $SNIFF -eq 1 ]]; then
    if SYS_SN=$(system_sniffer); then ok "iridium-sniffer: using $SYS_SN"
    else SYS_SN=""; warn "iridium-sniffer: no usable system copy, will build a local one"; fi
  fi
  # The RTL-SDR Blog driver is always built locally: stock librtlsdr 2.x also recognises the V4 but was
  # measured much weaker at L-band, and the two can't be told apart reliably. It builds in seconds.
fi
{ echo "# written by install.sh: tools reused from the system (delete this file or run --build-all to undo)"
  [[ -n "$SYS_GR" ]] && printf 'EXTRACTOR=%q\n' "$SYS_GR"
  [[ -n "$SYS_TK" ]] && printf 'TOOLKIT=%q\n' "$SYS_TK"
  [[ -n "$SYS_SN" ]] && printf 'SNIFFER=%q\n' "$SYS_SN"
  true; } > "$VENDOR/paths.env"

if [[ -z "$SYS_GR" || -z "$SYS_TK" || $RTLBLOG -eq 1 || ( $SNIFF -eq 1 && -z "$SYS_SN" ) ]]; then
  command -v git >/dev/null && command -v cmake >/dev/null || { fail "git and cmake are required"; exit 1; }
fi

step "Fetching sources (pinned versions)"
[[ -z "$SYS_GR" ]] && fetch "$GR_IRIDIUM_URL" "$GR_IRIDIUM_REF" "$SRC/gr-iridium"
[[ -z "$SYS_TK" ]] && fetch "$TOOLKIT_URL" "$TOOLKIT_REF" "$VENDOR/iridium-toolkit"
[[ $RTLBLOG -eq 1 ]] && fetch "$RTLBLOG_URL" "$RTLBLOG_REF" "$SRC/rtl-sdr-blog"
[[ $SNIFF -eq 1 && -z "$SYS_SN" ]] && fetch "$SNIFFER_URL" "$SNIFFER_REF" "$SRC/iridium-sniffer"
[[ -n "$SYS_GR" && -n "$SYS_TK" && $RTLBLOG -eq 0 && ( $SNIFF -eq 0 || -n "$SYS_SN" ) ]] && ok "nothing to fetch"

[[ -z "$SYS_GR" ]] && build_gr_iridium
[[ $RTLBLOG -eq 1 ]] && build_rtlblog
[[ $SNIFF -eq 1 && -z "$SYS_SN" ]] && build_sniffer
[[ $BLACKLIST -eq 1 ]] && blacklist_dvb
check
