"""The iridium-parser output format: one decoded frame per line. Shared by the server and the exports.

  IRA: p-1762600000-e000 000001278.7213 1626270000  95% -68.40|-117.34|18.59 120 DL sat:016 beam:37 ...
  type  name (p-<unix start>)  ms since start  Hz  confidence  level|noise|SNR  symbols  direction  details
"""
import re

# --- frame-type descriptions (shown in the UI) --------------------------------------
TYPES = {
    "IRA": "Ring alert: satellite & beam position, paging list",
    "IBC": "Broadcast: satellite/cell system info, time",
    "ITL": "Timing/location beacon",
    "IMS": "Pager (messaging) channel fragment",
    "IME": "Pager channel, extended",
    "MSG": "Pager message",
    "IRI": "Two-way channel control / data",
    "IDA": "Data link (SBD / ACARS carrier)",
    "IIP": "IP data",
    "IIQ": "Data (IIQ)",
    "IIR": "Data (IIR)",
    "IIU": "Data (IIU)",
    "IU3": "Unknown type 3",
    "I36": "Data (type 36)",
    "I38": "Data (type 38)",
    "VOC": "Voice frame (AMBE, encoded)",
    "VOD": "Voice/data frame",
    "VDA": "Voice data",
    "VO6": "Voice frame (6-byte variant)",
    "ISY": "Sync",
    "IAQ": "Acquisition",
    "RAW": "Iridium frame, unclassified",
}
VOICE_TYPES = {"VOC", "VOD", "VDA", "VO6"}

HDR = re.compile(r'^(\w+): (\S+) (\d+(?:\.\d+)?) (\d+) +(\d+)% (-?[\d.]+)\|(-?[\d.]+)\|(-?[\d.]+) +(\d+) (UL|DL)\s?(.*)$')
NAME_TS = re.compile(r'^[pu]-(\d{9,11})(?:-|$)')
PAYLOAD_RE = re.compile(r'\[[0-9a-f]{2}(?:\.[0-9a-f]{2})+\.?\]?')
TMSI_RE = re.compile(r'tmsi:([0-9a-f]{4})[0-9a-f]+')


def mask(typ, text):
    """Frame details as the dashboard shows them: no voice payload, subscriber IDs (TMSI) cut to 4 digits."""
    if typ in VOICE_TYPES:
        text = PAYLOAD_RE.sub("[voice payload not shown]", text)
    return TMSI_RE.sub("tmsi:\\1····", text)


def parse(line, base=0.0):
    """A frame line as a dict, or None. `base` is the start time for frames without a clock in their name."""
    m = HDR.match(line)
    if not m:
        return None
    typ, name, ms, freq, conf, lvl, noise, snr, ln, direction, rest = m.groups()
    tm = NAME_TS.match(name)
    return {"type": typ, "t": (int(tm.group(1)) if tm else base) + float(ms) / 1000.0, "clock": bool(tm),
            "freq": int(freq), "conf": int(conf), "level": float(lvl), "noise": float(noise), "snr": float(snr),
            "len": int(ln), "dir": direction, "text": rest}
