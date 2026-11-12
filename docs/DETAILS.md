# iridium_satalyz3r: details

The [README](../README.md) covers everyday use. This page has the rest: how the installer works, the SDR
configuration, why the HackRF needs a manual IF, measurements behind the defaults, file formats and the full
troubleshooting table.

## Contents

- [Installation in detail](#installation-in-detail)
- [Command line](#command-line)
- [Raw IQ recordings](#raw-iq-recordings)
- [Choosing the demodulator](#choosing-the-demodulator)
- [Configuration](#configuration)
- [Why the HackRF uses a manual IF](#why-the-hackrf-uses-a-manual-if)
- [The dashboard in detail](#the-dashboard-in-detail)
- [Files](#files)
- [Troubleshooting](#troubleshooting)
- [Project layout and tests](#project-layout-and-tests)

## Installation in detail

`install.sh` does the following. Nothing is installed system-wide except distro packages:

1. **System packages** (apt / dnf / pacman): GNU Radio + development headers, gr-osmosdr, VOLK, pybind11,
   libsndfile, libusb, cmake, git, compilers, and Python modules `numpy`, `crcmod` and `pyproj`. Also the `rtl-sdr` and
   `hackrf` packages, which provide the udev rules that let you use the devices without root.
2. **Looks for gr-iridium, iridium-sniffer and iridium-toolkit that are already installed**, e.g. on DragonOS, which ships them.
   A copy is reused only if it supports everything the scripts and the dashboard use: for gr-iridium, stdin input,
   `-f hackrf`, `-D` and `-d`; for iridium-sniffer, `--format`, SoapySDR, `--bias-tee` and `--soapy-gain`; for iridium-toolkit, `--harder`/`--uw-ec` and the msg/sbd/acars reassembler modes.
   iridium-toolkit is found through `IRIDIUM_TOOLKIT`, common locations (`/opt`, `/usr/src`, `/usr/local/src`, `~/SDR`, …) or a short search.
   What was reused is recorded in `vendor/paths.env`. Pass `--build-all` to ignore system copies and use the
   pinned versions below, for example if a system copy turns out to be incompatible.
3. **Fetches whatever is still needed, at pinned, tested versions** → `vendor/`
   - gr-iridium `a09b254`
   - iridium-toolkit `7938ae0`
   - rtl-sdr-blog `aed0ea1`
   - iridium-sniffer `830e34c`
4. **Builds gr-iridium** into `vendor/gr-iridium` and **iridium-sniffer** into `vendor/iridium-sniffer`, unless system copies are reused.
5. **Builds the RTL-SDR Blog driver** into `vendor/rtlsdr-blog`, always. The run scripts load it in place of the distro
   `librtlsdr`, which handles the V4 poorly at L-band. The stock driver also recognises the V4, so the two can't be
   told apart reliably, and the build only takes seconds. Your system driver is not touched.
6. **Checks** the installation, says whether each tool is the system or the local copy, and lists anything missing.

Options:

| Option | Meaning |
|---|---|
| `--no-deps` | skip system packages (install them yourself, see below) |
| `--no-rtl-blog` | skip the RTL-SDR Blog driver (HackRF-only setups) |
| `--no-sniffer` | skip iridium-sniffer; the scripts then use gr-iridium |
| `--build-all` | build local copies of gr-iridium, iridium-sniffer and iridium-toolkit even if they're already installed |
| `--blacklist-dvb` | write `/etc/modprobe.d/blacklist-rtlsdr.conf` so the kernel TV driver leaves RTL-SDRs alone (delete the file to undo) |
| `--check` | only run the checks |

To try newer upstream versions, override the pins, e.g. `GR_IRIDIUM_REF=master ./install.sh --no-deps`.

<details>
<summary><b>Manual package list (Debian/Ubuntu)</b></summary>

```bash
sudo apt install git cmake build-essential pkg-config gnuradio gnuradio-dev gr-osmosdr \
  libvolk-dev pybind11-dev libsndfile1-dev libusb-1.0-0-dev \
  python3-numpy python3-crcmod python3-pyproj rtl-sdr hackrf \
  libfftw3-dev libsoapysdr-dev soapysdr-module-rtlsdr libhackrf-dev
# Ubuntu 22.04: use libvolk2-dev instead of libvolk-dev
./install.sh --no-deps
```
If `gnuradio-dev` is "not found", run `sudo apt update` first. A stale package list was the cause on the test machine.
</details>

## Command line

| Command | What it does |
|---|---|
| `./run-ui.sh` | live decoding + dashboard, until Ctrl+C |
| `./run-ui.sh 1800` | receive for 30 minutes; the dashboard stays open until Ctrl+C |
| `SDR=hackrf ./run-ui.sh` | use `configs/hackrf.conf` (any `configs/<name>.conf` works: `SDR=<name>`) |
| `PORT=9000 ./run-ui.sh` | dashboard on another port |
| `DEMOD=gr ./run-ui.sh` | use gr-iridium instead of iridium-sniffer (`DEMOD=sniffer` forces iridium-sniffer) |
| `./run-ui.sh --browse` | dashboard with the radio off: start it, or open recordings, from the page |
| `./run-ui.sh --demo` | simulated reception, no radio needed |
| `./run-ui.sh --replay logs/frames-XXXX.parsed` | replay a recorded session, no SDR needed |
| `./run-live.sh [seconds]` | terminal-only decoding, prints frame counts at the end |
| `./record-iq.sh [seconds]` | record raw IQ to a WAV file (see [Raw IQ recordings](#raw-iq-recordings)) |
| `./install.sh --check` | re-check the installation |
| `IRIDIUM_LOGS=/data/iridium ./run-ui.sh` | keep recordings in another folder (e.g. a bigger disk for IQ files) |

Timed runs (`./run-ui.sh 1800`, `./record-iq.sh 600`) can be cut short with Ctrl+C; the radio stops and the antenna
power goes off. Closing the terminal or `kill` does the same.

Every run is saved in `logs/` (see [Files](#files)). Antenna power is switched off automatically when the receiver stops.

## Raw IQ recordings

`./record-iq.sh [seconds]` (or `SDR=hackrf ./record-iq.sh ...`, or *Record raw IQ* under the Receiver button) records the raw signal instead of decoding it, with
the same settings as the live scripts: frequency, sample rate, gain, antenna power, and for the HackRF the manual IF.
The file goes to `logs/iq/iq-<sdr>-<time>_<centre>Hz_<rate>sps.wav`: 8-bit stereo WAV (I = left, Q = right),
the format IridiScope, SDR# and SDR++ open as an IQ recording. IridiScope takes the sample rate from the WAV header and
the centre frequency from the `<number>Hz` part of the name, so keep it if you rename the file.

| SDR | centre | rate | size |
|---|---|---|---|
| RTL-SDR | 1625.75 MHz | 2.5 MSps | 300 MB/min |
| HackRF | 1622 MHz | 10 MSps | 1.2 GB/min |

The script refuses to start a timed recording that won't fit on the disk, and deletes a recording that received no
samples (radio missing or busy) instead of leaving an empty file. Without a duration it runs until Ctrl+C,
and the file is still finalised properly. Recordings over 4 GB are fine: the WAV size fields wrap, and IridiScope
handles that.

## Choosing the demodulator

By default (`DEMOD=auto`) the scripts use **iridium-sniffer** when it is installed and fall back to gr-iridium.
The run scripts and the dashboard header show which one is active.

| | gr-iridium | iridium-sniffer |
|---|---|---|
| Frames identified, RTL-SDR V4, same 10-min recording | 167 | **253** (+51 %) |
| Frames identified, HackRF, same 5-min recording | 128 | **164** (+28 %) |
| CPU time for a 5-min HackRF recording | 148 s | **26 s** |

iridium-sniffer found nearly every burst gr-iridium found (192 of 197 and 175 of 184) plus many weaker ones.

How each SDR is connected:
- **RTL-SDR**: iridium-sniffer opens it through SoapySDR (`-i soapy:driver=rtlsdr`), using the frequency, sample rate,
  gain and `bias=1` from `configs/rtl-sdr.conf`. The Blog driver is still used, through `LD_LIBRARY_PATH`.
- **HackRF**: `hackrf_transfer` with the manual IF, piped into iridium-sniffer, exactly as with gr-iridium.
  iridium-sniffer's built-in HackRF support uses the automatic IF and would bring back the Wi-Fi problem.

## Configuration

SDR settings live in `configs/*.conf` (gr-iridium's format):

| Setting | RTL-SDR (`rtl-sdr.conf`) | HackRF (`hackrf.conf`) |
|---|---|---|
| `sample_rate` | 2.5 MSps | 10 MSps |
| `center_freq` | 1625.75 MHz → 1624.5–1627.0 MHz | 1622 MHz → 1617–1627 MHz |
| antenna power | `bias=1` (≈4.5 V) | `bias=1` (≈3.3 V) |
| gain | `gain=50` (max) | `[hackrf-transfer]`: `amp=1`, `lna=40`, `vga=50` |
| receiver | gr-iridium's osmosdr source | `hackrf_transfer` with a **manual IF of 2.25 GHz** → gr-iridium (see below) |
| detection threshold | 18 dB (gr-iridium default) | `threshold=16` |

## Why the HackRF uses a manual IF

The HackRF doesn't sample 1.6 GHz directly. It first mixes the signal up to an intermediate frequency (IF),
which it picks automatically at about **2.42 GHz**, right inside 2.4 GHz Wi-Fi. Wi-Fi leaking into the
IF stage then shows up as thousands of short, narrow blips across the received band, especially the lower half.
gr-iridium's burst detector triggers on them constantly, and most "decoded" frames are junk.

Measured on a HackRF One clone in a home with Wi-Fi nearby:

| | Automatic IF (≈2.42 GHz) | Manual IF 2.6 GHz |
|---|---|---|
| False detections | ≈ 57,000 / min, unaffected by gain, threshold or decimation | ≈ 170 / min |
| High-confidence frames that are real (structured) Iridium frames | 9 of 44 | 28 of 29 |
| Pollution moves when retuning? | yes, it stays at a fixed offset below the centre, so it is generated inside the receiver | n/a |

GNU Radio's osmosdr source can't set the IF, so the scripts stream from `hackrf_transfer -i <IF> -o <LO> -m 1`
into gr-iridium. The live pipeline was checked to be lossless: decoding live and from a recording of the same stream gave
identical results. To go back to the osmosdr source: `HACKRF_OSMOSDR=1 SDR=hackrf ./run-ui.sh`.

The table above was measured at 2.6 GHz. A later sweep showed that lower IFs add less receiver noise: with the
antenna powered, the noise floor rises 1.4 dB at 2.17–2.35 GHz but only 0.9 dB at 2.6 GHz. That works out to about
1.7 dB more SNR per burst, so the default is now **2.25 GHz**, which sits between LTE band 1 downlink (≤ 2170 MHz) and LTE band 40
(2300–2400 MHz). The manual IF costs a few dB of sensitivity against the automatic one, which `amp=1` makes up.

To compare IFs on your own unit, measure the noise rise when antenna power is switched on. A bigger rise means the antenna,
not the HackRF, sets the noise floor. Frame counts from short runs mostly track satellite passes and are not reliable
for comparison.

`THRESHOLD=<dB>` overrides the detection threshold for any SDR.

Rules gr-iridium enforces:
- **`sample_rate` ÷ decimation must be a multiple of 250 kHz.** The scripts use decimation 1 for RTL-SDR and 4 for HackRF; override it with `DECIMATION=n`.
- **Gains must be whole numbers.**

The dashboard reads the config to label the header and size the channel chart.

## The dashboard in detail

| Panel | Shows |
|---|---|
| Header | *Receiver*, *Recordings*, *Export*; LIVE / STOPPED / IDLE / REPLAY / RECORDING / DEMO, time since the last frame, the message-content switch |
| Totals | frames, frames/min, satellites, ring alerts, pages, average SNR, burst decode success, messages |
| Satellite map | per-satellite tracks (positions reported in ring alerts) and spot-beam circles; *Auto-fit* / *Fit view* |
| Satellites | satellites identified from ring alerts and broadcasts, with direction in the sky; click one for its details |
| Sky view | every decoded satellite position as seen from the receiver (N up, horizon at the edge), shaded by how often each part of the sky was heard. *Location* sets your position |
| Frame types | counts per frame type with descriptions |
| Channel activity | *Totals*: frames per 20 kHz; duplex (two-way) and simplex bands, with the pager and ring-alert channels marked. *Waterfall*: every frame of the last 30 min by frequency and time, coloured by type (open `/#waterfall` to start in this view) |
| Signal quality | average SNR or noise floor per minute, plus frame counts |
| Receiver health | bursts/s, decode rate (now and whole run), queue, dropped bursts, rejected positions, and tips when something looks wrong |
| Voice traffic | calls (≥3 voice frames on one channel, gaps < 20 s): active now, durations, channel, Doppler drift, timeline. **Metadata only.** |
| Live frames | the newest 250 frames, filterable by type; click a row for the full frame, level, noise and exact frequency |
| Pages / Messages | ring-alert pages (masked TMSIs); pager / ACARS / SBD messages once all fragments arrive, with pager RIC (masked), part count, checksum and a *device data* label |

The map uses Leaflet and Esri tiles, so the browser needs internet access for the background map. Add `?poll` to
the URL to use polling instead of a live stream (useful behind some proxies).

Open it as `http://localhost:PORT/` or `http://127.0.0.1:PORT/`: other host names get "403 forbidden host".
Over SSH, forward the port (`ssh -L 8888:localhost:8888 box`) and open `http://localhost:8888/` locally.

### Receiver control

The **Receiver** button opens the receiver panel: the radios from `configs/` (marked *connected* when USB shows them),
an optional run time, the demodulator, and *Start receiving*, *Record raw IQ* and *Stop*. One job runs at a time.
Every run is saved and appears under Recordings. If the radio fails, a red bar under the header says why, and the
panel shows the receiver log. Opening the dashboard with the radio off (`--browse`) shows the panel straight away.

Under the hood the server starts `scripts/pipeline.sh` (radio → demodulator → parser) in its own process group and stops
it the way Ctrl+C would, so the radio shuts down cleanly and the antenna power goes off.

### Sky view and receiver location

To turn satellite positions into directions in the sky, the dashboard needs to know where you are. Without any input it
estimates that from the spot beams it hears (the beam centres cluster around the receiver), which is good to about
200 km. For exact angles click *Location* and type your coordinates or pick the spot on the map. The location is stored
in your browser only.

Read the sky view like this: dots are positions where a ring alert was decoded, lines are passes, the shading shows how
often each patch of sky was heard. Satellites cover the whole sky over a few hours, so a sector that stays empty is
blocked for your antenna (a wall, the roof, a building). The text under the plot names the direction you hear best.

### Satellite details and Doppler

Click a satellite in the table for its details. The **Doppler curve** shows how far its ring alerts were shifted from
their channel during the last pass: up to about ±36 kHz, positive while it approaches, negative while it moves away.
Where the curve crosses zero the satellite was closest. The steepness of that crossing tells how high the pass was:
around 300 Hz/s and more means nearly overhead, under ~140 Hz/s a low pass far to the side. When only one side of the
pass was heard but it ends close to zero, the closest approach is extrapolated (and marked so). A constant offset of a
few kHz is your radio's own frequency error. Below the curve: the SNR of each ring alert and, when the location is
known, the elevation over the same time.

**Which channel?** The Iridium documentation puts ring alerts on 1626.2708 MHz, but satellites also send them on other
simplex channels: in a 5-minute outdoor test most arrived on 1626.3958 MHz. The dashboard therefore works out the channel
of each pass: the 41.667 kHz-grid channel for which every ring alert stays within the physical Doppler limit
(±39.5 kHz, including some receiver error). A pass heard on both sides of its closest approach fits only one channel. A
short one-sided pass can fit two neighbouring channels; it then gets the channel the unambiguous passes of the same
session used, else the one nearest its median frequency. The channel is shown above the curve.

### Exports

*Export* downloads what the current view shows (the live session, or the recording opened in the UI):

| Export | Contents |
|---|---|
| Frames (CSV) | every frame of the session: time, type, frequency, SNR, level, noise, confidence, details. TMSIs are masked and voice payloads left out, as in the dashboard |
| Satellite tracks (GeoJSON) | one line per satellite pass and a point per spot beam; open it in QGIS, geojson.io or Google Earth |
| Summary (JSON) | totals, frame types, satellites, voice call statistics, SNR and noise per minute. No message text |

### Demo mode

`./run-ui.sh --demo` simulates the full constellation (66 satellites in six polar planes) over a receiver in northern
Italy with a south-facing window. Everything in the dashboard works, including the sky view, which shows the "window"
clearly. Positions are only roughly those of real satellites, and messages and voice frames are made up.

### Opening recordings

The **Recordings** button (top right) lists everything in `logs/`:

- **Decoded sessions** (`frames-*.parsed`, one per receiver run): date, duration, frames, **frames per minute** (with a
  bar, the fairest way to compare antenna positions, radios and settings), average SNR, satellites, decode rate, radio
  and demodulator. Click a column title to sort. Type a note into a row (saved when you leave the field), and *Delete*
  removes a session with its `.bits` and receiver log.
- **IQ recordings** (`logs/iq/*.wav`, from `record-iq.sh`): radio, centre frequency, sample rate, duration, size. The first
  *Decode* runs iridium-sniffer + iridium-toolkit on the file, with a progress bar; the result is saved next to the WAV
  (`<name>.wav.parsed`), so the next *Open* is instant. *Re-decode* runs it again, e.g. after updating iridium-sniffer.
  Other 2-channel IQ WAVs (8/16-bit or float, e.g. from SDR++) work too if they are copied into `logs/iq/` and have the
  centre frequency in the name (`..._1622000000Hz...`).

The opened recording gets its own view, with a bar showing its details: file, radio, tuning, duration, size, when it was
recorded and the decode status. All the panels show the recording, with times taken from the recording itself.
*Back to live* returns to the receiver, which keeps running the whole time, and *Close recording* closes it.
The dialog header shows how much space the recordings use and how much is free.

Links that open a part of the dashboard directly: `#rec` (the opened recording), `#receiver`, `#recordings`,
`#sat=59` (satellite 59), `#waterfall`.

**Weak signal?** Messages need every fragment, so they mostly appear with a good sky view. Indoors you will
still see ring alerts, broadcasts, timing frames and call activity.

## Files

Each receiver run writes to `logs/` (or `$IRIDIUM_LOGS`):

| File | Contents |
|---|---|
| `frames-<time>.bits` | demodulated bits (iridium-toolkit RAW format) |
| `frames-<time>.parsed` | decoded frames, one per line: replay them, or feed them to other iridium-toolkit tools |
| `extractor-<time>.log` | receiver statistics and error messages, with the radio and demodulator used |
| `iq/iq-<sdr>-<time>_<centre>Hz_<rate>sps.wav` | raw IQ recordings |
| `iq/<name>.wav.parsed` | the decoded result of an IQ recording, saved the first time it is opened |
| `notes.json` | your notes from the Recordings list |

A run that decoded nothing (e.g. the radio wasn't plugged in) leaves no files.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `usb_claim_interface error -6` / RTL-SDR busy | kernel TV driver: `./install.sh --no-deps --blacklist-dvb`, then replug |
| Errors from the extractor or parser on a machine with preinstalled tools (e.g. DragonOS) | the system copy may be too old or too new: `./install.sh --no-deps --build-all` |
| Very low signal on RTL-SDR V4 / antenna power doesn't switch | make sure the Blog driver was built (`./install.sh --check`) and the TV driver is blacklisted |
| Permission denied opening the SDR | install the `rtl-sdr` / `hackrf` packages (udev rules), then replug, or log out and in |
| `Sample rate divided by decimation must be a multiple of 250000` | fix `sample_rate` or `DECIMATION` |
| `invalid literal for int()` for `gain` | gains must be whole numbers |
| Frames decode but no satellites on the map | positions only come from ring alerts. Wait a few minutes, or improve the sky view. |
| No frames at all | check the antenna connection and `bias` setting, and move the antenna to a window. GNSS antennas with a GPS-only filter won't receive Iridium. |
| HackRF: detector flooded, `ok` rate ≈ 0–1 %, mostly low-confidence RAW frames | Wi-Fi leaking into the HackRF IF: use the default `[hackrf-transfer]` setup (manual IF), not `HACKRF_OSMOSDR=1`. See [Why the HackRF uses a manual IF](#why-the-hackrf-uses-a-manual-if). |
| HackRF: very low RMS / few frames | the 8-bit converter is under-driven: raise `vga` (aim for sample RMS ≈ 10–25 without clipping) |
| Frame counts vary hugely between runs | normal: traffic depends on which satellites pass overhead (17 vs 258 frames in consecutive 45 s windows in testing) |
| `gnuradio-dev` not found | `sudo apt update`, then re-run the installer |
| Dashboard port in use | another dashboard is still running: close it, or `PORT=8899 ./run-ui.sh` |
| Red bar "The receiver stopped" | the reason is in the bar; *Details* shows the receiver log. Usually the radio isn't plugged in, isn't passed through to the VM, or another program uses it |
| Sky view says "Waiting for a spot beam" | it needs a few ring alerts with beam positions to estimate where you are; or set your location with *Location* |
| Satellite shown greyed out with "?" | heard only once so far; it is confirmed by the second frame (single frames can have a corrupted satellite ID) |
| "Positions rejected" in Receiver health | positions that would need the satellite to jump thousands of km: bit errors in weak frames, left off the map |
| Map background blank | the browser needs internet access for map tiles |
| `403 forbidden host` | open the dashboard as `http://localhost:PORT/` (not by LAN IP or host name); use SSH port forwarding for remote access |
| "Message reassembly not running" under Messages | iridium-toolkit is missing or broken: `./install.sh --check` |

## Project layout and tests

```
install.sh            installer / checker
run-ui.sh             dashboard + receiver (or --browse / --demo / --replay)
run-live.sh           terminal-only decoding
record-iq.sh          raw IQ recording to WAV (for IridiScope or later decoding)
configs/              SDR configs (rtl-sdr.conf, hackrf.conf, add your own)
scripts/env.sh        shared environment (finds the built tools, SDR selection, antenna power off)
scripts/pipeline.sh   radio -> demodulator -> parser, started and stopped by the dashboard
scripts/iq2wav.py     8-bit IQ stream -> WAV writer used by record-iq.sh
ui/server.py          dashboard backend (Python stdlib only): state, recordings, HTTP API
ui/receiver.py        receiver control: start / stop / record, failure reasons
ui/frames.py          the iridium-parser line format, masking
ui/export.py          CSV / GeoJSON / JSON exports
ui/demo.py            constellation simulator for --demo
ui/static/            dashboard frontend (HTML/CSS/JS, Leaflet)
tests/                tests (no radio needed):  python3 -m unittest discover -s tests
vendor/               fetched + built by install.sh   (git-ignored)
logs/                 recordings                       (git-ignored)
```

Run the tests with `python3 -m unittest discover -s tests`. They need no radio. The receiver tests replay two real
frames (`tests/fixtures/itl.bits`) through `scripts/pipeline.sh` in place of a radio; they and the IQ decoding test
are skipped when iridium-toolkit / iridium-sniffer aren't installed (as on GitHub's test runners).
