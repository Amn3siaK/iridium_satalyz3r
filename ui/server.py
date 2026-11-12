#!/usr/bin/env python3
"""iridium_satalyz3r dashboard server: a web UI + JSON/SSE API on 127.0.0.1.

It keeps the state of the frames decoded by iridium-parser, feeds the multi-frame reassemblers
(pager / ACARS / SBD), and runs the receiver itself:

  server.py --start [--sdr hackrf] [--seconds N]   start receiving now (what ./run-ui.sh does)
  server.py --browse                               start the receiver or open recordings from the dashboard
  server.py --replay logs/frames-X.parsed          replay a saved session
  server.py --demo                                 simulated reception, no radio needed
  ... | iridium-parser.py | server.py --stdin      frames from another pipeline
"""
import argparse, base64, binascii, collections, datetime, glob, itertools, json, math, os, queue, re, signal
import struct, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:                # sibling modules, also when this file is loaded by the tests
    sys.path.insert(0, HERE)
import demo, export, receiver           # noqa: E402
STATIC = os.path.join(HERE, "static")
ROOT = os.path.dirname(HERE)
TOOLKIT = os.environ.get("IRIDIUM_TOOLKIT") or os.path.join(ROOT, "vendor", "iridium-toolkit")
SNIFFER = os.environ.get("IRIDIUM_SNIFFER") or os.path.join(ROOT, "vendor", "iridium-sniffer", "bin", "iridium-sniffer")
EXTRACTOR = os.environ.get("IRIDIUM_EXTRACTOR") or os.path.join(ROOT, "vendor", "gr-iridium", "bin", "iridium-extractor")

from frames import HDR, NAME_TS, PAYLOAD_RE, TYPES, VOICE_TYPES, mask    # noqa: E402,F401

IRA_RE = re.compile(r'sat:(\d+) beam:(\d+) (?:xyz=\S+ )?pos=.([+-][0-9.]+)/([+-][0-9.]+). alt=(-?\d+)')
PAGE_RE = re.compile(r'PAGE\(tmsi:([0-9a-f]+) msc_id:(\d+)[^)]*\)')
IBC_RE = re.compile(r'sat:(\d+) cell:(\d+)')
# gr-iridium and iridium-sniffer status line. When reading a pipe/file, iridium-sniffer prints
# "srr: N%" instead of "i: N/s", so fall back to i_avg for the burst rate.
EXT_RE = re.compile(r'\|\s*(?:i:\s*(\d+)/s|srr:[^|]*)\s*\|\s*i_avg:\s*(\d+)/s.*?\|\s*i_ok:\s*(\d+)%'
                    r'.*?\|\s*ok_avg:\s*(\d+)%\s*\|\s*ok:\s*(\d+)')

EXT_QUEUE_RE = re.compile(r'q_max:\s*(\d+)')
EXT_DROP_RE = re.compile(r'\|\s*d:\s*(\d+)')
EXT_SRR_RE = re.compile(r'srr:\s*([\d.]+)%')

# end-of-run summary lines printed by the sbd/acars reassemblers (not messages)
STATS_LINE = re.compile(r'^(Kept \d+/|\d+ valid packets|\d+/\d+ \(.*\) broken|\d+ dupes removed|SBD: |ACARS: |\d+ messages)')

FMIN, FMAX, FBIN = 1624.5e6, 1627.0e6, 20e3
RA_FREQ = 1626270833            # the ring alert channel centre (Hz) in the Iridium documentation
SIMPLEX_LO, SIMPLEX_HI = 1626.0e6, 1626.5e6
CH_GRID = 41666.667             # Iridium channel spacing; simplex channel centres sit at 1626.0 MHz + (k + 0.5) x spacing


def simplex_channel(freq):
    """The simplex channel centre nearest to `freq` (Hz)."""
    k = min(11, max(0, round((freq - SIMPLEX_LO) / CH_GRID - 0.5)))
    return SIMPLEX_LO + (k + 0.5) * CH_GRID
EARTH_R = 6371.0                # km
SAT_GROUND_SPEED = 7.5          # km/s, a little above the real ~6.6 km/s of the sub-satellite point


DOPPLER_MAX = 39.5e3    # Hz: an Iridium satellite shifts 1626 MHz by at most ~36.5 kHz, plus a little receiver error


def _passes(points, gap=600):
    seg = []
    for p in points:
        if seg and p[0] - seg[-1][0] > gap:
            yield seg; seg = []
        seg.append(p)
    if seg:
        yield seg


def _fitting_channels(seg):
    """Simplex channels for which every ring alert of the pass is within the physical Doppler limit."""
    lo, hi = min(p[1] for p in seg), max(p[1] for p in seg)
    return [c for c in (SIMPLEX_LO + (k + 0.5) * CH_GRID for k in range(12)) if hi - c <= DOPPLER_MAX and c - lo <= DOPPLER_MAX]


def channel_prior(dop_lists):
    """How many ring alerts each channel carried in passes whose channel is unambiguous (only one fits)."""
    prior = collections.Counter()
    for pts in dop_lists:
        for seg in _passes(pts):
            fit = _fitting_channels(seg)
            if len(fit) == 1:
                prior[round(fit[0])] += len(seg)
    return prior


def doppler(points, prior=None):
    """Ring alerts [t, Hz, snr] -> [t, shift from the channel centre (Hz), snr, channel centre (Hz)].

    A satellite may send its ring alerts on any simplex channel (not only the documented 1626.27 MHz one).
    The channel of a pass is the one for which all its ring alerts stay within the physical Doppler limit.
    A pass heard on both sides of its closest approach fits only one channel. A pass heard only on one side
    can fit two neighbouring channels; then the channel the unambiguous passes of the session used wins
    (`prior`, from channel_prior()), else the one nearest to the pass's median frequency.
    A constant receiver frequency error of a few kHz stays in the curve as an offset."""
    prior = prior or {}
    out = []
    for seg in _passes(points):
        fs = sorted(p[1] for p in seg)
        med = fs[len(fs) // 2]
        fit = _fitting_channels(seg) or [simplex_channel(med)]
        ch = max(fit, key=lambda c: (prior.get(round(c), 0), -abs(med - c)))
        out.extend([p[0], round(p[1] - ch), p[2], round(ch)] for p in seg)
    return out


def ground_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points on the ground."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))

# pager message line printed by toolkit reassembler.py -m msg:
#   Message 0123456 07 @2026-11-08T12:34:56 (len:3)  11   OK: text...
#   Message 0123456 07 @2026-11-08T12:34:56 (len:2) BCD fail: 0123...
# (len:N) is toolkit's msg_ctr_max, the highest 0-based part index, so the message has N+1 parts
PAGER_RE = re.compile(r'^Message (\d+) (\d+) @(\S+) \(len:(\d+)\)\s+(\d+|BCD)\s+(fail|OK): ?(.*)$')
BURST_CHARS = re.compile(r'^[A-Za-z0-9+/*=-]{8,}$')
BURST_TRANS = str.maketrans("*-", "/=")


def is_device_data(body):
    """Pager 'messages' that are base64 (with * for / and - for =) whose decoded bytes carry a valid
    CRC-16/XMODEM are machine-to-machine data, not text. Same test as toolkit's msg 'noburst' option."""
    body = body.strip()
    if not BURST_CHARS.match(body):
        return False
    try:
        raw = base64.b64decode(body.translate(BURST_TRANS))
    except (binascii.Error, ValueError):
        return False
    return len(raw) >= 3 and binascii.crc_hqx(raw, 0) == 0


def parse_pager(text):
    """Header fields of a reassembled pager message (the content itself is left to the UI toggle)."""
    m = PAGER_RE.match(text)
    if not m:
        return None
    ric, seq, date, parts, csum, ok, body = m.groups()
    info = {"ric": ric[:3] + "····", "seq": int(seq), "parts": int(parts) + 1, "ok": ok == "OK",
            "kind": "numeric" if csum == "BCD" else ("device data" if is_device_data(body) else "text")}
    try:
        info["t"] = datetime.datetime.fromisoformat(date).timestamp()
    except ValueError:
        pass
    return info


ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def host_allowed(host):
    """Block DNS rebinding: a web page on another site could otherwise read this localhost API."""
    if not host:
        return True                     # HTTP/1.0 clients without a Host header (curl -0, scripts)
    host = host.strip().lower()
    name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    return name in ALLOWED_HOSTS or host in ALLOWED_HOSTS

# --- voice-channel activity (metadata only; voice payload is never decoded or kept) -
CALL_FREQ_TOL = 15e3     # Hz: frames within this of the call's last frequency (Doppler drifts slowly)
CALL_GAP = 20.0          # s: a gap longer than this ends the call
CALL_MIN_FRAMES = 3      # sessions with fewer frames are counted as short bursts, not calls
CH_SPACING = 41666.667   # Iridium channel spacing


def channel_of(freq):
    """Approximate Iridium frequency-access channel number (Doppler can shift it by +-1)."""
    return int(round((freq - 1616e6) / CH_SPACING)) + 1


class Calls:
    def __init__(self):
        self.open = []
        self.done = collections.deque(maxlen=500)
        self.next_id = 1
        self.short = 0
        self.chan_use = collections.Counter()
        self.max_concurrent = 0
        self.latest = 0
        self.finished = 0               # all calls ever closed (self.done keeps only the newest 500)

    def add(self, t, freq, snr, conf):
        self.latest = max(self.latest, t)
        self.expire(t)
        best = None
        for c in self.open:
            if abs(freq - c["last_freq"]) < CALL_FREQ_TOL and t - c["end"] < CALL_GAP:
                if best is None or abs(freq - c["last_freq"]) < abs(freq - best["last_freq"]):
                    best = c
        if best is None:
            best = {"id": self.next_id, "start": t, "end": t, "frames": 0, "snr_sum": 0.0,
                    "first_freq": freq, "last_freq": freq, "channel": channel_of(freq)}
            self.next_id += 1
            self.open.append(best)
        best["end"] = max(best["end"], t)
        best["frames"] += 1
        best["snr_sum"] += snr
        best["last_freq"] = freq
        active = sum(1 for c in self.open if c["frames"] >= CALL_MIN_FRAMES)
        self.max_concurrent = max(self.max_concurrent, active)

    def expire(self, now):
        keep = []
        for c in self.open:
            if now - c["end"] > CALL_GAP:
                if c["frames"] >= CALL_MIN_FRAMES:
                    self.done.appendleft(c)
                    self.finished += 1
                    self.chan_use[c["channel"]] += 1
                else:
                    self.short += 1
            else:
                keep.append(c)
        self.open = keep

    @staticmethod
    def view(c, active):
        return {"id": c["id"], "start": c["start"], "end": c["end"], "duration": round(c["end"] - c["start"], 1),
                "frames": c["frames"], "snr": round(c["snr_sum"] / c["frames"], 1),
                "freq": c["last_freq"], "channel": c["channel"],
                "drift": round(c["last_freq"] - c["first_freq"]), "active": active}

    def snapshot(self, now_frame_time):
        # expire against frame time, so replays and quiet periods behave the same
        if now_frame_time:
            self.expire(now_frame_time)
        active = [self.view(c, True) for c in self.open if c["frames"] >= CALL_MIN_FRAMES]
        done = [self.view(c, False) for c in self.done]
        allc = active + done
        durs = [c["duration"] for c in allc]
        # calls started per 5-minute bucket
        buckets = collections.Counter(int(c["start"] // 300) * 300 for c in allc)
        return {
            "active": active, "recent": done[:100],
            "total": self.finished + len(active), "short_bursts": self.short,
            "avg_duration": round(sum(durs) / len(durs), 1) if durs else None,
            "longest": max(durs) if durs else None,
            "max_concurrent": self.max_concurrent,
            "timeline": sorted(buckets.items()),
            "channels": sorted(self.chan_use.items()),
        }


class State:
    def __init__(self, mode, fmin=None, fmax=None):
        self.lock = threading.Lock()
        self.mode = mode
        self.fmin = FMIN if fmin is None else fmin
        self.fmax = FMAX if fmax is None else fmax
        self.session_info = {}                        # details of an opened recording (shown in the UI)
        self.started = time.time()
        self.last_frame = None
        self.first_frame = None
        self.total = 0
        self.types = collections.Counter()
        self.recent_ts = collections.deque()          # for frames/min
        self.frames = collections.deque(maxlen=400)   # recent frame feed
        self.points = collections.deque(maxlen=8000)  # [seq, t, freq, type, snr] for the waterfall
        self.seq = 0
        self.sats = {}
        self.pages = collections.deque(maxlen=200)
        self.page_total = 0
        self.messages = collections.deque(maxlen=300)
        self.msg_seq = 0
        self.freq = [0] * max(1, int(round((self.fmax - self.fmin) / FBIN)))
        self.snr_min = collections.OrderedDict()      # minute -> [snr sum, n, noise sum]
        self.rx_vec = [0.0, 0.0, 0.0]                 # sum of spot-beam centres (unit vectors): where the receiver is
        self.rx_n = 0
        self.rejected = 0                             # satellite positions dropped as implausible
        self.snr_sum = 0.0
        self.extractor = {}
        self.calls = Calls()
        self.ended = False
        self.reassembler_status = dict          # replaced by main() once the reassemblers exist

    def sat(self, sid):
        s = self.sats.get(sid)
        if s is None:
            s = self.sats[sid] = {"id": sid, "frames": 0, "first": None, "last": None,
                                  "beams": set(), "cells": set(), "track": [], "spots": [],
                                  "snr": None, "types": collections.Counter(),
                                  "dop": collections.deque(maxlen=900), "pending": None}
        return s

    def add_line(self, line):
        m = HDR.match(line)
        if not m:
            return
        typ, name, ms, freq, conf, lvl, noise, snr, ln, direction, rest = m.groups()
        tm = NAME_TS.match(name)
        # recordings decoded without a clock in their name count from when this session was opened
        t = (int(tm.group(1)) if tm else self.started) + float(ms) / 1000.0
        freq, conf, snr, noise, lvl = int(freq), int(conf), float(snr), float(noise), float(lvl)
        with self.lock:
            self.seq += 1
            self.total += 1
            self.types[typ] += 1
            if self.last_frame is None or t > self.last_frame:      # frames can arrive slightly out of order
                self.last_frame = t
            if self.first_frame is None or t < self.first_frame:
                self.first_frame = t
            self.recent_ts.append(t)                  # frame times, so preloaded history doesn't count as "now"
            while len(self.recent_ts) > 1 and self.recent_ts[0] < t - 120:    # bounded even with no browser open
                self.recent_ts.popleft()
            self.snr_sum += snr
            mi = int(t // 60) * 60
            b = self.snr_min.setdefault(mi, [0.0, 0, 0.0])
            b[0] += snr; b[1] += 1; b[2] += noise
            while len(self.snr_min) > 180:
                self.snr_min.popitem(last=False)
            fi = int((freq - self.fmin) // FBIN)
            if 0 <= fi < len(self.freq):
                self.freq[fi] += 1
            info = {}
            sid = None
            if typ == "IRA":
                r = IRA_RE.search(rest)
                if r:
                    sid, beam = int(r.group(1)), int(r.group(2))
                    lat, lon, alt = float(r.group(3)), float(r.group(4)), int(r.group(5))
                    if sid == 0 or (lat == 0 and lon == 0) or abs(lat) > 90 or abs(lon) > 180:
                        sid = None                      # bit errors: no satellite has ID 0 or sits at 0/0
                    else:
                        s = self.sat(sid)
                        s["beams"].add(beam)
                        info = {"sat": sid, "beam": beam, "lat": lat, "lon": lon, "alt": alt}
                        # ring alerts are not only on the documented 1626.27 MHz channel: a satellite may use
                        # any simplex channel, so keep them all and find the channel in sat_detail()
                        if SIMPLEX_LO - 40e3 < freq < SIMPLEX_HI + 40e3:
                            s["dop"].append([round(t, 2), freq, snr])
                        if 700 < alt < 850:
                            self._add_position(s, [lat, lon, round(t, 2), snr])
                        elif -100 < alt < 100:
                            s["spots"].append([lat, lon, beam, t])
                            s["spots"] = s["spots"][-200:]
                            la, lo = math.radians(lat), math.radians(lon)
                            self.rx_vec[0] += math.cos(la) * math.cos(lo)
                            self.rx_vec[1] += math.cos(la) * math.sin(lo)
                            self.rx_vec[2] += math.sin(la)
                            self.rx_n += 1
                        for tmsi, msc in PAGE_RE.findall(rest):
                            self.page_total += 1
                            self.pages.appendleft({"t": t, "sat": sid, "beam": beam,
                                                   "tmsi": tmsi[:4] + "····", "msc": int(msc)})
                        info["pages"] = len(PAGE_RE.findall(rest))
            elif typ == "IBC":
                r = IBC_RE.search(rest)
                if r and int(r.group(1)) != 0:
                    sid = int(r.group(1))
                    self.sat(sid)["cells"].add(int(r.group(2)))
                    info = {"sat": sid, "cell": int(r.group(2))}
            if typ in VOICE_TYPES:
                self.calls.add(t, freq, snr, conf)
            if sid is not None:
                s = self.sat(sid)
                s["frames"] += 1
                s["types"][typ] += 1
                s["first"] = s["first"] or t
                s["last"] = t
                s["snr"] = snr
            self.points.append([self.seq, round(t, 2), freq, typ, round(snr, 1)])
            self.frames.append({"seq": self.seq, "t": t, "type": typ, "freq": freq, "conf": conf,
                                "snr": snr, "noise": noise, "level": lvl, "len": int(ln), "dir": direction,
                                "info": info, "text": mask(typ, rest)[:600]})

    def _add_position(self, s, pt):
        """Append a satellite position to its track unless it is physically impossible.

        A satellite's ground point moves about 6.6 km/s, so a position far outside that range of the last
        one is a decoding error (bit errors in the position, or in the satellite ID). A jump is accepted
        only when a second position confirms it, which also recovers from a bad first point."""
        track = s["track"]
        if track and track[-1][:2] == pt[:2]:
            return
        def reachable(a, b):
            return ground_km(a[0], a[1], b[0], b[1]) <= SAT_GROUND_SPEED * abs(b[2] - a[2]) + 250
        if not track or reachable(track[-1], pt):
            track.append(pt)
            s["pending"] = None
        elif s["pending"] and reachable(s["pending"], pt):
            if len(track) == 1:                         # the lone earlier point was the bad one
                track.clear(); self.rejected += 1
            track += [s["pending"], pt]
            s["pending"] = None
        else:
            if s["pending"]:
                self.rejected += 1
            s["pending"] = pt
        del track[:-600]

    def rx_estimate(self):
        """Rough receiver position: the mean of the spot-beam centres it has heard (good to ~200 km)."""
        if self.rx_n < 3:
            return None
        x, y, z = (v / self.rx_n for v in self.rx_vec)
        if math.hypot(x, y, z) < 0.3:
            return None
        return {"lat": round(math.degrees(math.atan2(z, math.hypot(x, y))), 2),
                "lon": round(math.degrees(math.atan2(y, x)), 2), "n": self.rx_n}

    def sat_detail(self, sid):
        """Everything known about one satellite (for the detail view)."""
        with self.lock:
            s = self.sats.get(sid)
            if s is None:
                return None
            return {"id": sid, "frames": s["frames"], "first": s["first"], "last": s["last"], "snr": s["snr"],
                    "beams": sorted(s["beams"]), "cells": sorted(s["cells"]), "types": dict(s["types"]),
                    "track": list(s["track"]), "spots": list(s["spots"]), "doppler": doppler(s["dop"], channel_prior(x["dop"] for x in self.sats.values())),
                    "confirmed": s["frames"] >= 2}

    def add_message(self, mode, text):
        with self.lock:
            self.msg_seq += 1
            # frame time, not wall-clock time, so replays show when the message was received
            item = {"seq": self.msg_seq, "t": self.last_frame or time.time(), "mode": mode, "text": text}
            if mode == "pager":
                info = parse_pager(text)
                if info:
                    item["t"] = info.pop("t", item["t"])
                    item["info"] = info
            else:
                try:
                    item["json"] = json.loads(text)
                except ValueError:
                    pass
            self.messages.appendleft(item)

    def _per_min(self):
        """Live: frames in the last minute. Recordings: average over the recording."""
        if self.mode in ("replay", "decoding") and self.total and self.last_frame is not None:
            return round(self.total / max(1.0, (self.last_frame - self.first_frame) / 60))
        horizon = time.time() - 60
        return sum(1 for x in self.recent_ts if x > horizon)

    def snapshot(self, since_frame=0, since_msg=0):
        now = time.time()
        with self.lock:
            horizon = (self.last_frame or now) - 3600
            sats = []
            for s in self.sats.values():
                sats.append({"id": s["id"], "frames": s["frames"], "first": s["first"], "last": s["last"],
                             "beams": sorted(s["beams"]), "cells": sorted(s["cells"]), "snr": s["snr"],
                             # one frame alone may be a bit error in the ID: wait for a second one
                             "confirmed": s["frames"] >= 2,
                             "types": dict(s["types"]),
                             "track": [p for p in s["track"] if p[2] > horizon],
                             "spots": [p for p in s["spots"] if p[3] > horizon]})
            sats.sort(key=lambda x: -(x["last"] or 0))
            return {
                "mode": self.mode, "now": now, "sdr": getattr(self, "sdr", {}), "started": self.started,
                "first_frame": self.first_frame, "last_frame": self.last_frame,
                "total": self.total, "per_min": self._per_min(),
                "avg_snr": round(self.snr_sum / self.total, 1) if self.total else None,
                "types": [{"type": k, "count": v, "desc": TYPES.get(k, "")} for k, v in self.types.most_common()],
                "sats": sats,
                "pages": list(self.pages)[:100], "page_total": self.page_total,
                "freq": {"min": self.fmin, "bin": FBIN, "counts": self.freq},
                "snr_series": [[k, round(v[0] / v[1], 1), v[1], round(v[2] / v[1], 1)] for k, v in self.snr_min.items()],
                "rx_est": self.rx_estimate(), "rejected": self.rejected,
                "extractor": self.extractor,
                # once input has ended, close every open call
                "calls": self.calls.snapshot((self.last_frame or 0) + (CALL_GAP + 1 if self.ended else 0)),
                "frames": [f for f in self.frames if f["seq"] > since_frame][-200:],
                "points": [p for p in self.points if p[0] > since_frame],
                "reassemblers": self.reassembler_status(),
                "messages": [m for m in self.messages if m["seq"] > since_msg][:100],
                "seq": self.seq, "msg_seq": self.msg_seq,
                "session": dict(self.session_info),
            }


# --- reassemblers (multi-frame decoders from iridium-toolkit) ----------------------
class Reassembler:
    def __init__(self, state, label, args):
        self.state, self.label = state, label
        self.q = queue.Queue(maxsize=10000)
        self.p = None
        if not os.path.isfile(os.path.join(TOOLKIT, "reassembler.py")):
            print(f"{label} reassembler unavailable: iridium-toolkit not found at {TOOLKIT} (run ./install.sh)",
                  file=sys.stderr)
            return
        try:
            self.p = subprocess.Popen([sys.executable, "-u", "reassembler.py"] + args, cwd=TOOLKIT,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            print(f"{label} reassembler failed to start: {e}", file=sys.stderr)
            return
        threading.Thread(target=self._writer, daemon=True).start()
        threading.Thread(target=self._reader, daemon=True).start()

    @property
    def status(self):
        if self.p is None:
            return "unavailable"
        return "running" if self.p.poll() is None else ("finished" if self.p.returncode == 0 else "failed")

    def feed(self, line, block=False):
        if self.p is None:
            return
        if not block:                   # live: never hold up the pipeline, drop if the reassembler lags
            try:
                self.q.put_nowait(line)
            except queue.Full:
                pass
            return
        while self.p.poll() is None:    # recordings: wait, so full-speed replays don't lose messages
            try:
                self.q.put(line, timeout=1); return
            except queue.Full:
                pass

    def kill(self):
        if self.p is not None and self.p.poll() is None:
            try:
                self.p.kill()
            except OSError:
                pass

    def close(self):
        if self.p is not None:
            self.q.put(None)

    def _writer(self):
        while True:
            line = self.q.get()
            try:
                if line is None:
                    self.p.stdin.close(); return
                self.p.stdin.write(line + "\n"); self.p.stdin.flush()
            except (BrokenPipeError, ValueError):
                return

    def _reader(self):
        for out in self.p.stdout:
            out = out.rstrip("\n")
            if out.strip() and not STATS_LINE.match(out):
                self.state.add_message(self.label, out)

def ext_stats(line):
    """Burst/decoder statistics from a gr-iridium / iridium-sniffer status line, or None."""
    m = EXT_RE.search(line)
    if not m:
        return None
    st = {"bursts_per_s": int(m.group(1) or m.group(2)), "ok_now": int(m.group(3)),
          "ok_avg": int(m.group(4)), "ok_total": int(m.group(5)), "t": time.time()}
    # queue length and dropped bursts: non-zero means the computer can't keep up with the radio
    for key, rx in (("queue", EXT_QUEUE_RE), ("dropped", EXT_DROP_RE)):
        x = rx.search(line)
        if x:
            st[key] = int(x.group(1))
    x = EXT_SRR_RE.search(line)
    if x:
        st["srr"] = float(x.group(1))       # samples received vs expected (pipe/file input), 100 % = no loss
    return st


def tail_extractor(state, path, stop=None):
    """Follow the receiver's status log for burst/decoder statistics, until `stop` is set."""
    while not os.path.exists(path):
        if stop is not None and stop.is_set():
            return
        time.sleep(0.5)
    with open(path, errors="replace") as f:
        while True:
            line = f.readline()
            if not line:
                if stop is not None and stop.is_set():
                    return
                time.sleep(0.5); continue
            st = ext_stats(line)
            if st:
                with state.lock:
                    state.extractor = st


# --- sessions: the live/replay input, or a recording opened from the dashboard ------
class Session:
    """One data source with its own state and reassemblers."""
    _ids = itertools.count(1)

    def __init__(self, mode, sdr_name, fmin=None, fmax=None, info=None):
        self.id = next(Session._ids)
        self.state = State(mode, fmin, fmax)
        self.state.sdr = {"name": sdr_name, "fmin": self.state.fmin, "fmax": self.state.fmax}
        self.info = self.state.session_info = info if info is not None else {}
        self.cancelled = threading.Event()
        self.procs = []
        self.reass = []
        self.parsed_path = None             # the decoded frames on disk, when there is such a file (exports)

    def start_reassemblers(self):
        self.reass = [Reassembler(self.state, "pager", ["-m", "msg"]),
                      Reassembler(self.state, "acars", ["-m", "acars", "-a", "json"]),
                      Reassembler(self.state, "sbd", ["-m", "sbd"])]
        self.state.reassembler_status = lambda: {r.label: r.status for r in self.reass}

    def ingest(self, src, delay=0.0, block=False, sink=None):
        try:
            for line in src:
                if self.cancelled.is_set():
                    break
                line = line.rstrip("\n")
                if sink:
                    sink.write(line + "\n")
                self.state.add_line(line)
                for r in self.reass:
                    r.feed(line, block)
                if delay:
                    time.sleep(delay)
        finally:
            for r in self.reass:
                r.close()
            with self.state.lock:
                self.state.ended = True
                if self.state.mode == "live":
                    self.state.mode = "stopped"
                elif self.state.mode == "decoding":
                    self.state.mode = "replay"

    def cancel(self):
        self.cancelled.set()
        for p in self.procs:
            try:
                p.kill()
            except OSError:
                pass
        for r in self.reass:
            r.kill()


# --- recordings library (logs/) ----------------------------------------------------
SAT_RE = re.compile(r'sat:(\d+)')
WAV_FREQ_RE = re.compile(r'(\d{6,12}(?:\.\d+)?)\s*[Hh][Zz]')    # same rule as IridiScope
NAME_TIME_RE = re.compile(r'(\d{8})-(\d{6})')
RAW_ID_RE = re.compile(r'^RAW: i-\d+-')
TAGGED_RE = re.compile(r'tagged (\d+) bursts total')
FLIP = bytes((b ^ 0x80) for b in range(256))
WAV_FORMATS = {(1, 8): "cu8", (1, 16): "ci16", (3, 32): "cf32"}   # (WAVE format tag, bits) -> IQ format


def name_time(name):
    """Local time stamp YYYYmmdd-HHMMSS in a file name, as Unix time (None if absent)."""
    m = NAME_TIME_RE.search(name)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"))
    except ValueError:
        return None


def guess_sdr(text):
    t = text.lower()
    if "rtl-sdr" in t or "rtlsdr" in t or "r828" in t:
        return "RTL-SDR"
    if "hackrf" in t:
        return "HackRF"
    return None


def wav_info(path):
    """Header details of an IQ WAV recording. Raises ValueError for files it can't decode."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        hdr = f.read(12)
        if len(hdr) < 12 or hdr[:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            raise ValueError("not a WAV file")
        fmt = None; off = dsz = None
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                break
            cid, sz = ch[:4], struct.unpack("<I", ch[4:])[0]
            if cid == b"fmt ":
                body = f.read(sz)
                if len(body) < 16:
                    raise ValueError("broken WAV header")
                tag, chans, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
                if tag == 0xFFFE and len(body) >= 26:
                    tag = struct.unpack("<H", body[24:26])[0]
                fmt = (tag, chans, rate, bits)
                if sz & 1:
                    f.read(1)
            elif cid == b"data":
                off, dsz = f.tell(), sz
                break
            else:
                f.seek(sz + (sz & 1), 1)
    if not fmt or off is None:
        raise ValueError("broken WAV header")
    tag, chans, rate, bits = fmt
    if chans != 2 or (tag, bits) not in WAV_FORMATS:
        raise ValueError(f"not 2-channel IQ in a supported format ({chans} ch, {bits}-bit)")
    remaining = max(0, size - off)
    # streaming writers leave the size at 0; files over 4 GB wrap it modulo 2^32
    if dsz == 0 or off + dsz > size or (remaining - dsz) % (1 << 32) == 0:
        dsz = remaining
    base = os.path.basename(path)
    freqs = WAV_FREQ_RE.findall(base)
    centre = float(freqs[-1]) if freqs else None
    stride = 2 * bits // 8
    duration = dsz / stride / rate if rate else 0
    start = name_time(base) or (os.path.getmtime(path) - duration)
    return {"format": WAV_FORMATS[(tag, bits)], "rate": rate, "centre": centre, "offset": off, "bytes": dsz,
            "duration": round(duration, 1), "start": start, "size": size, "sdr": guess_sdr(base)}


def parsed_info(path):
    """Frame count, time span and band of a decoded (.parsed) session."""
    frames = 0; first = last = None; fmin = fmax = None
    snr_sum = 0.0; ira = 0; clock = True
    sats = collections.Counter()
    with open(path, errors="replace") as f:
        for line in f:
            m = HDR.match(line)
            if not m:
                continue
            tm = NAME_TS.match(m.group(2))
            if not tm:
                clock = False               # no clock in the frame names: times are relative to the start
            t = (int(tm.group(1)) if tm else 0) + float(m.group(3)) / 1000.0
            fq = int(m.group(4))
            frames += 1
            snr_sum += float(m.group(8))
            first = t if first is None else min(first, t)
            last = t if last is None else max(last, t)
            fmin = fq if fmin is None else min(fmin, fq)
            fmax = fq if fmax is None else max(fmax, fq)
            if m.group(1) in ("IRA", "IBC"):
                ira += m.group(1) == "IRA"
                x = SAT_RE.search(m.group(11))
                if x and int(x.group(1)):
                    sats[int(x.group(1))] += 1
    duration = round(last - first) if frames else 0
    return {"frames": frames, "first": first if clock else None, "last": last if clock else None,
            "fmin": fmin, "fmax": fmax, "duration": duration,
            "avg_snr": round(snr_sum / frames, 1) if frames else None, "ring_alerts": ira,
            "sats": sum(1 for n in sats.values() if n >= 2),        # heard at least twice (see State.snapshot)
            "per_min": round(frames / max(1.0, duration / 60), 1) if frames else 0}


def receiver_log_info(path):
    """Demodulator, SDR and tuning from a run's receiver log (logs/extractor-<time>.log)."""
    out = {}
    try:
        with open(path, errors="replace") as f:
            head = f.read(4000)
    except OSError:
        return out
    try:                                   # the last status line holds the run's decode totals
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 8000))
            tail = f.read().decode(errors="replace").replace("\r", "\n")
        stats = [st for st in map(ext_stats, tail.splitlines()) if st]
        if stats:
            out["stats"] = stats[-1]
    except OSError:
        pass
    m = re.search(r"^pipeline: sdr=(\S+) demod=(.+)$", head, re.M)     # written by scripts/pipeline.sh
    if m:
        out["demod"] = m.group(2).strip()
        out["sdr"] = guess_sdr(m.group(1)) or m.group(1)
    if "iridium-sniffer" in head:
        out.setdefault("demod", "iridium-sniffer")
        m = re.search(r"center_freq=(\d+) Hz, sample_rate=(\d+) Hz", head)
        if m:
            out["centre"], out["rate"] = int(m.group(1)), int(m.group(2))
    else:
        out.setdefault("demod", "gr-iridium")
    sdr = out.get("sdr") or guess_sdr(head)
    if not sdr and out.get("demod") == "iridium-sniffer" and "Found " not in head:
        sdr = "HackRF"                 # older logs, piped from hackrf_transfer: no tuner lines
    if sdr:
        out["sdr"] = sdr
    return out


def band_for(centre, rate):
    lo, hi = centre - rate / 2, centre + rate / 2
    return max(1616e6, FBIN * int(lo // FBIN)), min(1627e6, hi)


def band_from_frames(fmin, fmax):
    if fmin is None:
        return FMIN, FMAX
    lo = max(1616e6, min(fmin - 100e3, 1626e6)); hi = min(1627e6, max(fmax + 100e3, lo + 1e6))
    return FBIN * int(lo // FBIN), hi


def sniffer_threshold(sdr):
    """The burst threshold the live scripts use for this SDR (configs/<sdr>.conf), or None for the default."""
    if sdr != "HackRF":
        return None
    import configparser
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
    cp.read(os.path.join(ROOT, "configs", "hackrf.conf"))
    try:
        return float(cp["hackrf-transfer"].get("threshold", 16))
    except (KeyError, ValueError):
        return 16.0


class Library:
    """Recordings in logs/ (decoded sessions) and logs/iq/ (raw IQ WAVs), and the one opened in the UI."""

    def __init__(self, logs):
        self.logs = os.path.realpath(logs)
        self.lock = threading.Lock()
        self.current = None
        self._cache = {}
        self.busy = lambda: set()           # file names a running receiver is writing (set by main)

    # --- notes: one line per recording, e.g. where the antenna was (logs/notes.json) ----
    def _notes_path(self):
        return os.path.join(self.logs, "notes.json")

    def notes(self):
        try:
            with open(self._notes_path()) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_note(self, kind, name, text):
        self._path(kind, name)
        if not isinstance(text, str):
            raise ValueError("the note must be text")
        text = " ".join(text.split())[:200]
        os.makedirs(self.logs, exist_ok=True)
        with self.lock:
            d = self.notes()
            if text:
                d[name] = text
            else:
                d.pop(name, None)
            tmp = self._notes_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f, indent=1, sort_keys=True)
            os.replace(tmp, self._notes_path())
        return text

    def delete(self, kind, name):
        """Delete a recording and the files that belong to it. Returns the number of bytes freed."""
        path = self._path(kind, name)
        if name in self.busy():
            raise ValueError("this recording is still being written: stop the receiver first")
        cur = self.current
        if cur and cur.info.get("kind") == kind and cur.info.get("name") == name:
            self.close()
        if kind == "session":
            stamp = name[len("frames-"):-len(".parsed")]
            files = [path, path[:-len(".parsed")] + ".bits", os.path.join(self.logs, f"extractor-{stamp}.log")]
        else:
            files = [path, path + ".parsed", path + ".parsed.partial", path + ".log"]
        freed = 0
        for f in files:
            try:
                freed += os.path.getsize(f)
                os.remove(f)
            except OSError:
                pass
        with self.lock:
            d = self.notes()
            if d.pop(name, None) is not None:
                with open(self._notes_path(), "w") as f:
                    json.dump(d, f, indent=1, sort_keys=True)
        return freed

    def _cached(self, path, fn):
        st = os.stat(path)
        key = (path, st.st_mtime, st.st_size)
        if key not in self._cache:
            self._cache[key] = fn(path)
        return self._cache[key]

    def sessions(self):
        out = []; notes = self.notes(); busy = self.busy()
        for p in sorted(glob.glob(os.path.join(self.logs, "frames-*.parsed")), reverse=True):
            name = os.path.basename(p)
            try:
                info = dict(parsed_info(p) if name in busy else self._cached(p, parsed_info))
                size = os.path.getsize(p)
            except OSError:
                continue                    # deleted while listing
            ts = name[len("frames-"):-len(".parsed")]
            info.update(receiver_log_info(os.path.join(self.logs, f"extractor-{ts}.log")))
            stats = info.pop("stats", None)
            info.update({"name": name, "size": size, "start": name_time(name) or info["first"],
                         "ok_avg": stats.get("ok_avg") if stats else None,
                         "note": notes.get(name, ""), "live": name in busy})
            out.append(info)
        return out

    def iq(self):
        out = []; notes = self.notes(); busy = self.busy()

        def mtime(p):
            try:
                return os.path.getmtime(p)
            except OSError:
                return 0
        for p in sorted(glob.glob(os.path.join(self.logs, "iq", "*.wav")) + glob.glob(os.path.join(self.logs, "iq", "*.WAV")),
                        key=mtime, reverse=True):
            name = os.path.basename(p)
            try:
                item = {"name": name, "size": os.path.getsize(p), "note": notes.get(name, ""), "live": name in busy}
            except OSError:
                continue                    # deleted while listing
            try:
                item.update(wav_info(p) if name in busy else self._cached(p, wav_info))
            except (OSError, ValueError, struct.error) as e:
                item["error"] = str(e)
            if not item.get("error") and not item.get("centre"):
                item["error"] = "no centre frequency in the file name (add e.g. _1622000000Hz)"
            cache = p + ".parsed"
            if os.path.exists(cache):
                try:
                    pi = self._cached(cache, parsed_info)
                    item.update(decoded=pi["frames"], per_min=pi["per_min"], avg_snr=pi["avg_snr"], sats=pi["sats"])
                except OSError:
                    pass
            out.append(item)
        return out

    def disk(self):
        """Free space where the recordings live, and how much they use."""
        used = 0
        for base, _, files in os.walk(self.logs):
            for f in files:
                try:
                    used += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
        try:
            st = os.statvfs(self.logs)
            free = st.f_bavail * st.f_frsize
        except OSError:
            free = None
        return {"used": used, "free": free}

    def listing(self):
        cur = self.current
        return {"sessions": self.sessions(), "iq": self.iq(), "logs": self.logs, "disk": self.disk(),
                "sniffer": os.access(SNIFFER, os.X_OK), "toolkit": os.path.isfile(os.path.join(TOOLKIT, "iridium-parser.py")),
                "current": dict(cur.info) if cur else None}

    def _path(self, kind, name):
        """Resolve a name from the listing only, never a path from the request."""
        if not isinstance(name, str) or "/" in name or "\\" in name or name.startswith("."):
            raise LookupError("unknown recording")
        if kind == "session":
            ok = name.startswith("frames-") and name.endswith(".parsed")
            p = os.path.join(self.logs, name)
        elif kind == "iq":
            ok = name.lower().endswith(".wav")
            p = os.path.join(self.logs, "iq", name)
        else:
            raise LookupError("unknown recording type")
        if not ok or not os.path.isfile(p) or not os.path.realpath(p).startswith(self.logs + os.sep):
            raise LookupError("unknown recording")
        return p

    def open(self, kind, name, redecode=False):
        path = self._path(kind, name)
        if kind == "session":
            info = dict(self._cached(path, parsed_info))
            ts = name[len("frames-"):-len(".parsed")]
            info.update(receiver_log_info(os.path.join(self.logs, f"extractor-{ts}.log")))
            if info.get("centre") and info.get("rate"):
                lo, hi = band_for(info["centre"], info["rate"])
            else:
                lo, hi = band_from_frames(info["fmin"], info["fmax"])
            info.update({"kind": "session", "name": name, "size": os.path.getsize(path), "status": "loading"})
            label = " · ".join(x for x in ("recording", info.get("sdr"), info.get("demod")) if x)
            stats = info.pop("stats", None)
            sess = Session("replay", label, lo, hi, info)
            sess.parsed_path = path
            if stats:
                sess.state.extractor = stats
            target = lambda: self._replay(sess, path)
        else:
            wi = wav_info(path)
            if not wi["centre"]:
                raise ValueError("no centre frequency in the file name (add e.g. _1622000000Hz)")
            cache = path + ".parsed"
            lo, hi = band_for(wi["centre"], wi["rate"])
            info = dict(wi, kind="iq", name=name)
            label = " · ".join(x for x in ("IQ recording", wi["sdr"], "iridium-sniffer") if x)
            if os.path.exists(cache) and not redecode:
                info.update(status="loading", cached=True)
                sess = Session("replay", label, lo, hi, info)
                sess.parsed_path = cache
                target = lambda: self._replay(sess, cache)
            else:
                if not os.access(SNIFFER, os.X_OK):
                    raise ValueError("iridium-sniffer is not installed (run ./install.sh)")
                if not os.path.isfile(os.path.join(TOOLKIT, "iridium-parser.py")):
                    raise ValueError("iridium-toolkit is not installed (run ./install.sh)")
                info.update(status="decoding", progress=0.0, decode_started=time.time())
                sess = Session("decoding", label, lo, hi, info)
                target = lambda: self._decode(sess, path, wi, cache)
        sess.start_reassemblers()
        with self.lock:
            old, self.current = self.current, sess
        if old:
            old.cancel()
        threading.Thread(target=target, daemon=True).start()
        return sess

    def close(self):
        with self.lock:
            old, self.current = self.current, None
        if old:
            old.cancel()

    @staticmethod
    def _replay(sess, path):
        try:
            with open(path, errors="replace") as f:
                sess.ingest(f, block=True)
        except OSError as e:                # deleted in the meantime
            sess.ingest([])
            sess.info.update(status="error", error=f"could not read the recording: {e.strerror}")
            return
        sess.info["status"] = "cancelled" if sess.cancelled.is_set() else "done"

    @staticmethod
    def _decode(sess, path, wi, cache):
        """WAV -> iridium-sniffer -> iridium-parser -> session (and a .parsed cache next to the WAV)."""
        info = sess.info
        fmt = "ci8" if wi["format"] == "cu8" else wi["format"]
        args = [SNIFFER, "-f", "/dev/stdin", f"--format={fmt}", "-c", str(int(wi["centre"])), "-r", str(wi["rate"])]
        th = sniffer_threshold(wi["sdr"])
        if th:
            args += ["-d", str(th)]
        nice = lambda: os.nice(10)          # stay behind a live receiver running at the same time
        errors = collections.deque(maxlen=4)
        partial = cache + ".partial"
        try:
            p1 = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  preexec_fn=nice)
            p2 = subprocess.Popen([sys.executable, "-u", os.path.join(TOOLKIT, "iridium-parser.py"), "--harder", "--uw-ec"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, preexec_fn=nice, cwd=TOOLKIT)
        except OSError as e:
            info.update(status="error", error=f"could not start the decoder: {e}")
            sess.ingest([])
            return
        sess.procs += [p1, p2]

        def feeder():                       # WAV samples -> sniffer (8-bit WAV is unsigned: flip to signed)
            try:
                with open(path, "rb") as f:
                    f.seek(wi["offset"]); left = wi["bytes"]; done = 0
                    while left > 0 and not sess.cancelled.is_set():
                        buf = f.read(min(1 << 20, left))
                        if not buf:
                            break
                        p1.stdin.write(buf.translate(FLIP) if wi["format"] == "cu8" else buf)
                        done += len(buf); left -= len(buf)
                        info["progress"] = round(done / wi["bytes"], 4) if wi["bytes"] else 1.0
            except (OSError, ValueError):
                pass
            finally:
                try:
                    p1.stdin.close()
                except OSError:
                    pass

        counts = {"raw": 0, "tagged": None}

        def pump():                         # sniffer RAW lines -> parser, timestamped at the recording time
            stamp = f"RAW: i-{int(wi['start'])}-"
            try:
                for b in p1.stdout:
                    line = b.decode(errors="replace")
                    if line.startswith("RAW:"):
                        counts["raw"] += 1
                        p2.stdin.write(RAW_ID_RE.sub(stamp, line, count=1))
            except (OSError, ValueError):
                pass
            finally:
                try:
                    p2.stdin.close()
                except OSError:
                    pass

        def stats():                        # sniffer status lines -> burst statistics
            for b in p1.stderr:
                line = b.decode(errors="replace").strip()
                st = ext_stats(line)
                m = TAGGED_RE.search(line)
                if st:
                    with sess.state.lock:
                        sess.state.extractor = st
                elif m:                     # faster than real time, the sniffer prints only this total
                    counts["tagged"] = int(m.group(1))
                elif line and "error" in line.lower():
                    errors.append(line)

        for fn in (feeder, pump, stats):
            threading.Thread(target=fn, daemon=True).start()
        try:
            with open(partial, "w") as sink:
                sess.ingest(p2.stdout, block=True, sink=sink)
        except OSError as e:                # logs/iq not writable: decode without a cache
            errors.append(f"cache not written: {e}")
            sess.ingest(p2.stdout, block=True)
        p1.wait(); p2.wait()
        if sess.cancelled.is_set():
            info["status"] = "cancelled"
        elif p1.returncode != 0:
            info.update(status="error", error="; ".join(errors) or f"iridium-sniffer exited with code {p1.returncode}")
        else:
            info.update(status="done", progress=1.0, decode_seconds=round(time.time() - info["decode_started"], 1))
            if counts["tagged"]:
                with sess.state.lock:
                    sess.state.extractor = {"bursts_total": counts["tagged"], "ok_total": counts["raw"],
                                            "ok_avg": round(100 * counts["raw"] / counts["tagged"]), "t": time.time()}
            if os.path.exists(partial):
                os.replace(partial, cache)
                sess.parsed_path = cache
        if os.path.exists(partial):
            os.remove(partial)


# --- the application: main view, opened recording, receiver ---------------------------
class App:
    """What the HTTP handler serves: the main session (live receiver, replay or demo), the recordings
    library with the recording opened in the UI, and the receiver control."""

    def __init__(self, main, library=None, logs=None):
        self.main = main
        self.library = library
        self.receiver = None
        self.demo_rx = None
        if library is not None:
            self.receiver = receiver.Receiver(ROOT, library.logs, self)
            library.busy = self.busy_files

    def session(self, view):
        if view == "rec":
            return self.library.current if self.library else None
        return self.main

    def busy_files(self):
        """Recordings a running job is still writing."""
        r = self.receiver
        job = r.job if r else None
        if not job:
            return set()
        if job["kind"] == "live":
            return {f"frames-{job['stamp']}.parsed"}
        return {os.path.basename(job["file"])} if job.get("file") else set()

    # --- hooks called by receiver.Receiver ---
    def new_session(self, conf, stamp):
        lo, hi = band_for(conf["centre"], conf["rate"]) if conf["centre"] and conf["rate"] else (FMIN, FMAX)
        sess = Session("live", conf["radio"], lo, hi)
        sess.parsed_path = os.path.join(self.library.logs, f"frames-{stamp}.parsed")
        sess.start_reassemblers()
        self.main = sess
        return sess

    @staticmethod
    def label(sess, radio, demod):
        sess.state.sdr["name"] = f"{radio} · {demod}"

    @staticmethod
    def follow_log(sess, path, stop):
        tail_extractor(sess.state, path, stop)

    # --- demo ---
    def start_demo(self, preroll=1200):
        """Simulated reception (ui/demo.py): the last `preroll` seconds at once, then in real time."""
        sess = Session("demo", "simulated receiver (demo)")
        sess.state.reassembler_status = lambda: {"pager": "running", "acars": "running", "sbd": "running"}
        self.main, self.demo_rx = sess, {"lat": demo.RX[0], "lon": demo.RX[1]}

        def run():
            for t, kind, text in demo.events(time.time() - preroll, seed=int(time.time())):
                wait = t - time.time()
                if wait > 0:
                    time.sleep(min(wait, 5))
                if kind == "line":
                    sess.state.add_line(text)
                elif kind == "pager":
                    sess.state.add_message("pager", text)
        threading.Thread(target=run, daemon=True).start()
        return sess

    def close(self):
        if self.receiver:
            self.receiver.stop(wait=True)
        if self.library:
            self.library.close()


# --- HTTP ---------------------------------------------------------------------------
def origin_allowed(origin):
    try:
        return urlsplit(origin).hostname in {"localhost", "127.0.0.1", "::1"}
    except ValueError:
        return False


def make_handler(app):
    if not isinstance(app, App):            # a bare Session: main view only
        app = App(app)
    library = app.library

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _snapshot(self, sess, since_f, since_m):
            snap = sess.state.snapshot(since_f, since_m)
            snap["main_mode"] = app.main.state.mode     # the UI hides "Back to live" when there is no live view
            if app.demo_rx and sess is app.main:
                snap["demo_rx"] = app.demo_rx
            if app.receiver:
                snap["receiver"] = app.receiver.state
            return snap

        def _download(self, name, body, ctype):
            self._send(200, body, ctype, [("Content-Disposition", f'attachment; filename="{name}"')])

        def _export(self, sess, what):
            st = sess.state
            snap = st.snapshot()
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(snap["first_frame"] or snap["now"]))
            sats = [d for d in (st.sat_detail(s["id"]) for s in snap["sats"]) if d]
            if what == "summary":
                body = json.dumps(export.summary(snap, sats), indent=1).encode()
                return self._download(f"iridium-{stamp}-summary.json", body, "application/json")
            if what == "tracks":
                body = json.dumps(export.tracks_geojson([s for s in sats if s["confirmed"]])).encode()
                return self._download(f"iridium-{stamp}-tracks.geojson", body, "application/geo+json")
            if what == "frames":
                path = sess.parsed_path
                if path and os.path.isfile(path):       # the whole session from disk
                    with open(path, errors="replace") as f:
                        body = "".join(export.frames_csv(f, st.started)).encode()
                else:                                   # no file (demo, piped input): what is still in memory
                    with st.lock:
                        recent = list(st.frames)
                    rows = [f"{x['type']}: p-{int(x['t'])} {(x['t'] % 1) * 1000:.4f} {x['freq']} {x['conf']}% "
                            f"{x['level']}|{x['noise']}|{x['snr']} {x['len']} {x['dir']} {x['text']}" for x in recent]
                    body = "".join(export.frames_csv(rows)).encode()
                return self._download(f"iridium-{stamp}-frames.csv", body, "text/csv; charset=utf-8")
            self._json(404, {"error": "unknown export"})

        def do_GET(self):
            if not host_allowed(self.headers.get("Host")):
                return self._send(403, b"forbidden host (open the dashboard as http://localhost:PORT/)", "text/plain")
            url = urlsplit(self.path)
            path, qs = url.path, parse_qs(url.query)
            num = lambda k: int(qs[k][0]) if qs.get(k, [""])[0].isdigit() else 0
            view = qs.get("view", ["live"])[0]
            if path == "/api/state":
                sess = app.session(view)
                if sess is None:
                    return self._json(404, {"closed": True})
                self._json(200, self._snapshot(sess, num("since"), num("since_msg")))
            elif path == "/api/sat":
                sess = app.session(view)
                detail = sess.state.sat_detail(num("id")) if sess else None
                if detail is None:
                    return self._json(404, {"error": "unknown satellite"})
                self._json(200, detail)
            elif path == "/api/export":
                sess = app.session(view)
                if sess is None:
                    return self._json(404, {"error": "nothing to export"})
                self._export(sess, qs.get("what", [""])[0])
            elif path == "/api/recordings":
                if library is None:
                    return self._json(404, {"error": "recordings are not available"})
                listing = library.listing()
                listing["main_mode"] = app.main.state.mode
                self._json(200, listing)
            elif path == "/api/receiver":
                if app.receiver is None:
                    return self._json(404, {"error": "receiver control is not available"})
                st = app.receiver.status()
                st["demods"] = {"sniffer": os.access(SNIFFER, os.X_OK), "gr": os.access(EXTRACTOR, os.X_OK)}
                st["main_mode"] = app.main.state.mode
                self._json(200, st)
            elif path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                sess = None; last_f = last_m = 0
                try:
                    while True:
                        cur = app.session(view)
                        if cur is None:
                            snap = {"closed": True}
                        else:
                            if cur is not sess:     # another recording or a new receiver run: start from the beginning
                                sess, last_f, last_m = cur, 0, 0
                            snap = self._snapshot(cur, last_f, last_m)
                            last_f, last_m = snap["seq"], snap["msg_seq"]
                        self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n")
                        self.wfile.flush()
                        time.sleep(1.5)
                except OSError:            # browser tab closed / connection dropped
                    return
            else:
                name = "index.html" if path == "/" else path.lstrip("/")
                full = os.path.realpath(os.path.join(STATIC, name))
                if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
                    return self._send(404, b"not found", "text/plain")
                ctype = {"html": "text/html", "js": "application/javascript", "css": "text/css",
                         "svg": "image/svg+xml", "ico": "image/x-icon"}.get(full.rsplit(".", 1)[-1], "application/octet-stream")
                with open(full, "rb") as f:
                    self._send(200, f.read(), ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype.endswith("javascript") else ""))

        # --- actions: POST with a JSON body --------------------------------------------
        def _post_open(self, b):
            sess = library.open(b.get("kind"), b.get("name"), bool(b.get("redecode")))
            return {"info": dict(sess.info)}

        def _post_close(self, b):
            library.close()

        def _post_note(self, b):
            return {"note": library.set_note(b.get("kind"), b.get("name"), b.get("note", ""))}

        def _post_delete(self, b):
            return {"freed": library.delete(b.get("kind"), b.get("name"))}

        def _post_start(self, b):
            app.receiver.start(b.get("sdr"), b.get("seconds"), b.get("demod"))

        def _post_record(self, b):
            app.receiver.record(b.get("sdr"), b.get("seconds"))

        def _post_stop(self, b):
            return {"stopped": app.receiver.stop()}

        POSTS = {"/api/open": _post_open, "/api/close": _post_close, "/api/note": _post_note,
                 "/api/delete": _post_delete, "/api/receiver/start": _post_start,
                 "/api/receiver/record": _post_record, "/api/receiver/stop": _post_stop}

        def do_POST(self):
            # only this dashboard may start things: right host name, same-origin, JSON body
            # (a cross-site page can't send a JSON POST without a CORS preflight, which this server never answers)
            if not host_allowed(self.headers.get("Host")):
                return self._json(403, {"error": "forbidden host"})
            origin = self.headers.get("Origin")
            if origin and not origin_allowed(origin):
                return self._json(403, {"error": "forbidden origin"})
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return self._json(415, {"error": "JSON body required"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= 4096:
                return self._json(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "invalid request"})
            action = self.POSTS.get(urlsplit(self.path).path)
            if action is None or library is None:
                return self._json(404, {"error": "not found"})
            try:
                out = action(self, body) or {}
            except LookupError as e:
                return self._json(404, {"error": str(e)})
            except (ValueError, OSError, struct.error) as e:
                return self._json(400, {"error": str(e)})
            self._json(200, dict(out, ok=True))
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8888)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--start", action="store_true", help="start the receiver now (SDR from --sdr, else the SDR env var)")
    src.add_argument("--replay", help="replay a saved .parsed file")
    src.add_argument("--browse", action="store_true", help="no input: start the receiver or open recordings from the dashboard")
    src.add_argument("--demo", action="store_true", help="simulated reception, to try the dashboard without a radio")
    src.add_argument("--stdin", action="store_true", help="read iridium-parser output from standard input")
    ap.add_argument("--sdr", default=os.environ.get("SDR") or "rtl-sdr", help="configs/<name>.conf for --start")
    ap.add_argument("--seconds", type=int, help="with --start: stop receiving after this many seconds")
    ap.add_argument("--replay-delay", type=float, default=0.0, help="seconds between replayed lines")
    ap.add_argument("--logs", default=os.environ.get("IRIDIUM_LOGS") or os.path.join(ROOT, "logs"),
                    help="recordings folder (default: $IRIDIUM_LOGS, else logs/)")
    ap.add_argument("--extractor-log", help="with --stdin: receiver log to follow for statistics")
    ap.add_argument("--sdr-config", help="with --stdin: SDR .conf, used to size the channel chart")
    ap.add_argument("--sdr-name", default="", help="with --stdin: SDR name shown in the header")
    a = ap.parse_args()

    global FMIN, FMAX
    if a.sdr_config and os.path.exists(a.sdr_config):
        conf = receiver.read_config(a.sdr_config)
        if conf["centre"] and conf["rate"]:
            FMIN, FMAX = band_for(conf["centre"], conf["rate"])

    library = Library(a.logs)
    app = App(Session("idle", "no receiver running"), library)
    if a.demo:
        app.start_demo()
    elif a.replay or a.stdin:
        sess = app.main = Session("replay" if a.replay else "live", a.sdr_name or ("replay" if a.replay else "SDR"))
        sess.start_reassemblers()
        if a.replay:
            sess.parsed_path = a.replay
        src_f = open(a.replay, errors="replace") if a.replay else sys.stdin
        threading.Thread(target=sess.ingest, args=(src_f, a.replay_delay), daemon=True).start()
        if a.extractor_log:
            threading.Thread(target=tail_extractor, args=(sess.state, a.extractor_log), daemon=True).start()

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(app))
    except OSError as e:
        sys.exit(f"Can't use port {a.port} ({e.strerror}). Is another dashboard still open? "
                 f"Close it with Ctrl+C, or choose another port: PORT={a.port + 1} ./run-ui.sh")
    srv.daemon_threads = True

    if a.start:
        try:
            app.receiver.start(a.sdr, a.seconds)
        except (LookupError, ValueError, OSError) as e:
            print(f"Receiver not started: {e}", file=sys.stderr)
    print(f"Dashboard: http://localhost:{a.port}/", file=sys.stderr, flush=True)

    # Ctrl+C or a kill: stop the radio (and switch the antenna power off) before leaving
    def leave(*_):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, signal.SIGHUP):     # also when the terminal window is closed
        signal.signal(sig, leave)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping…", file=sys.stderr, flush=True)
        app.close()


if __name__ == "__main__":
    main()
