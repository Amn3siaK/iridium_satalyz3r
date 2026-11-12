"""Exports of a session: frames as CSV, satellite tracks as GeoJSON, and a JSON summary."""
import csv, datetime, io

import frames

CSV_COLUMNS = ["time_utc", "unix_time", "type", "direction", "freq_hz", "snr_db", "level_db", "noise_db",
               "confidence_pct", "symbols", "details"]


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def frames_csv(lines, base=0.0):
    """CSV text chunks for an iterable of iridium-parser lines. Details are masked like in the dashboard."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    n = 0
    for line in lines:
        f = frames.parse(line.rstrip("\n"), base)
        if not f:
            continue
        w.writerow([iso(f["t"]) if f["clock"] or base else "", f"{f['t']:.4f}", f["type"], f["dir"], f["freq"],
                    f["snr"], f["level"], f["noise"], f["conf"], f["len"],
                    " ".join(frames.mask(f["type"], f["text"]).split())])
        n += 1
        if n % 500 == 0:
            yield buf.getvalue(); buf.seek(0); buf.truncate()
    yield buf.getvalue()


def tracks_geojson(sats):
    """Satellite ground tracks (LineString per pass) and spot-beam centres (Points). `sats`: State.sat_detail() dicts."""
    feats = []
    for s in sats:
        seg = []
        segments = [seg]
        for p in s["track"]:
            if seg and p[2] - seg[-1][2] > 600:          # a new pass
                seg = []; segments.append(seg)
            seg.append(p)
        for seg in segments:
            if not seg:
                continue
            geom = ({"type": "LineString", "coordinates": [[p[1], p[0]] for p in seg]} if len(seg) > 1
                    else {"type": "Point", "coordinates": [seg[0][1], seg[0][0]]})
            feats.append({"type": "Feature", "geometry": geom,
                          "properties": {"kind": "satellite track", "sat": s["id"], "start": iso(seg[0][2]),
                                         "end": iso(seg[-1][2]), "points": len(seg)}})
        seen = {}
        for lat, lon, beam, t in s["spots"]:
            seen[beam] = (lat, lon, t)
        for beam, (lat, lon, t) in sorted(seen.items()):
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                          "properties": {"kind": "spot beam centre", "sat": s["id"], "beam": beam, "time": iso(t)}})
    return {"type": "FeatureCollection", "features": feats}


def summary(snap, sats):
    """A compact, self-describing summary of a State.snapshot(). No frame contents or message text."""
    keep = ("mode", "sdr", "total", "per_min", "avg_snr", "types", "page_total", "extractor", "rx_est", "session")
    out = {k: snap.get(k) for k in keep}
    out["generated"] = iso(snap["now"])
    out["first_frame"] = iso(snap["first_frame"]) if snap.get("first_frame") else None
    out["last_frame"] = iso(snap["last_frame"]) if snap.get("last_frame") else None
    out["satellites"] = [{"id": s["id"], "frames": s["frames"], "confirmed": s["confirmed"],
                          "first": iso(s["first"]) if s["first"] else None, "last": iso(s["last"]) if s["last"] else None,
                          "beams": s["beams"], "cells": s["cells"], "positions": len(s["track"]), "types": s["types"]}
                         for s in sats]
    calls = snap.get("calls") or {}
    out["voice"] = {k: calls.get(k) for k in ("total", "short_bursts", "avg_duration", "longest", "max_concurrent")}
    out["messages"] = snap.get("msg_seq", 0)
    out["snr_per_minute"] = [{"minute": iso(r[0]), "snr_db": r[1], "frames": r[2], "noise_db": r[3]} for r in snap.get("snr_series", [])]
    return out
