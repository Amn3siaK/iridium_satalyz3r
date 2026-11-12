"""Simulated Iridium reception, for trying the dashboard without a radio (./run-ui.sh --demo).

Sixty-six satellites fly the real constellation geometry (six polar planes, 780 km up, 100-minute orbits)
over a made-up receiver that sits behind a south-facing window: satellites in that part of the sky are
received well, the rest of the sky only now and then. Frames come out in iridium-parser's text format, so
the dashboard treats them exactly like real ones. Everything here is invented: positions are only roughly
those of real satellites, and message and voice contents are random bytes.
"""
import datetime, math, random

RX = (45.0, 10.0)               # receiver latitude/longitude (northern Italy), an arbitrary spot
WINDOW = (110.0, 250.0, 18.0)   # azimuth from/to and lowest elevation (degrees) with a clear view
MU, RE, ALT = 398600.4418, 6371.0, 780.0
A = RE + ALT
PERIOD = 2 * math.pi * math.sqrt(A ** 3 / MU)
INCL = math.radians(86.4)
EARTH_RATE = 7.2921159e-5
RA_FREQ = 1626270833
C = 299792.458


def ecef(lat, lon, r=RE):
    la, lo = math.radians(lat), math.radians(lon)
    return (r * math.cos(la) * math.cos(lo), r * math.cos(la) * math.sin(lo), r * math.sin(la))


def sat_ecef(k, t):
    """Earth-fixed position (km) of satellite k (0..65) at Unix time t."""
    plane, slot = divmod(k, 11)
    raan = math.radians(plane * 31.6)
    u = 2 * math.pi * (t / PERIOD + slot / 11 + plane / 22)
    x = A * (math.cos(raan) * math.cos(u) - math.sin(raan) * math.sin(u) * math.cos(INCL))
    y = A * (math.sin(raan) * math.cos(u) + math.cos(raan) * math.sin(u) * math.cos(INCL))
    z = A * math.sin(u) * math.sin(INCL)
    th = EARTH_RATE * t
    return (x * math.cos(th) + y * math.sin(th), -x * math.sin(th) + y * math.cos(th), z)


def look(rx, sat):
    """Azimuth, elevation (degrees) and range (km) of `sat` seen from `rx` (both Earth-fixed km)."""
    d = [s - r for s, r in zip(sat, rx)]
    rng = math.sqrt(sum(v * v for v in d))
    n = math.sqrt(sum(v * v for v in rx))
    up = [v / n for v in rx]
    east = (-up[1], up[0], 0.0)
    en = math.hypot(east[0], east[1]) or 1.0
    east = (east[0] / en, east[1] / en, 0.0)
    north = (up[1] * east[2] - up[2] * east[1], up[2] * east[0] - up[0] * east[2], up[0] * east[1] - up[1] * east[0])
    e, nn, uu = (sum(a * b for a, b in zip(d, v)) for v in (east, north, up))
    return math.degrees(math.atan2(e, nn)) % 360, math.degrees(math.asin(uu / rng)), rng


def latlon(p):
    return math.degrees(math.asin(p[2] / math.sqrt(sum(v * v for v in p)))), math.degrees(math.atan2(p[1], p[0]))


def events(start, seed=1):
    """Endless (time, kind, text): kind 'line' is a decoded frame, 'pager' a reassembled message."""
    rnd = random.Random(seed)
    rx = ecef(*RX)
    epoch = int(start)
    step = 0.5
    t = float(epoch)
    visible, next_scan = [], 0.0
    calls = []                      # [end time, channel frequency, satellite]
    next_ra = {}                    # satellite -> time of its next ring alert
    msg_n = 0

    def view_quality(az, el):
        a0, a1, e0 = WINDOW
        return 1.0 if (a0 <= az <= a1 and el >= e0) else (0.12 if el > 35 else 0.03)

    def header(typ, ft, freq, snr, conf, ln, err="000"):
        noise = -117.0 + rnd.uniform(-0.6, 0.6)
        return (f"{typ}: p-{epoch}-e{err} {(ft - epoch) * 1000:014.4f} {int(freq):010d} {conf:3d}% "
                f"{noise + snr + 30.35:.2f}|{noise:07.2f}|{snr:05.2f} {ln} DL ")

    while True:
        if t >= next_scan:          # which satellites are above the horizon (changes slowly)
            visible = [k for k in range(66) if look(rx, sat_ecef(k, t))[1] > 2]
            next_scan = t + 20
        out = []
        for k in visible:
            pos = sat_ecef(k, t)
            az, el, rng = look(rx, pos)
            if el < 8:
                continue
            q = view_quality(az, el)
            rr = (look(rx, sat_ecef(k, t + 1))[2] - rng)             # km/s, + = moving away
            dop = lambda f: f * (1 - rr / C) + 900                   # +900 Hz: a typical receiver offset
            snr = lambda: max(8.0, 13 + 11 * math.sin(math.radians(el)) + rnd.gauss(0, 1.5) - (0 if q == 1 else 4))
            sid = k + 2
            # ring alert every 4.32 s, alternating satellite position and one of its spot beams
            due = next_ra.setdefault(k, t + rnd.uniform(0, 4.32))
            if t >= due:
                next_ra[k] = due + 4.32
                if rnd.random() < 0.75 * q:
                    ft = due + rnd.uniform(0, 0.05)
                    if rnd.random() < 0.5:
                        lat, lon = latlon(pos); alt = 780 + rnd.randint(0, 17)
                        xyz = tuple(int(v / 4) for v in pos); beam = rnd.randint(1, 48)
                    else:                                            # beam centre: between sub-point and receiver
                        slat, slon = latlon(pos); w = rnd.uniform(0.55, 0.95)
                        lat = slat + (RX[0] - slat) * w + rnd.uniform(-1.2, 1.2)
                        lon = slon + (RX[1] - slon) * w + rnd.uniform(-1.5, 1.5)
                        alt = rnd.randint(0, 25); xyz = tuple(int(v / 4) for v in ecef(lat, lon)); beam = 1 + (int(az) // 8) % 48
                    pages = ""
                    if rnd.random() < 0.18:
                        pages = "".join(f" PAGE(tmsi:{rnd.getrandbits(32):08x} msc_id:{rnd.randint(1, 20):02d})"
                                        for _ in range(rnd.choice((1, 1, 2))))
                    out.append((ft, "line", header("IRA", ft, dop(RA_FREQ), snr(), rnd.randint(70, 99), 120)
                                + f"sat:{sid:03d} beam:{beam:02d} xyz=({xyz[0]:+05d},{xyz[1]:+05d},{xyz[2]:+05d}) "
                                  f"pos=({lat:+06.2f}/{lon:+07.2f}) alt={alt:03d} RAI:48 ?00 bc_sb:{rnd.randint(1, 31):02d} "
                                  f"P{pages.count('PAGE'):02d}:{pages} {{OK}}"))
            p = q * step
            if rnd.random() < 0.35 * p:                              # broadcast channel
                ft = t + rnd.uniform(0, step)
                iso = datetime.datetime.fromtimestamp(ft, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.00Z")
                out.append((ft, "line", header("IBC", ft, dop(1624.5e6 + 41666.667 * rnd.choice((2, 9, 17, 25))), snr(), rnd.randint(65, 98), 131)
                            + f"bc:0 sat:{sid:03d} cell:{rnd.randint(1, 48):02d} 0 slot:0 sv_blkn:0 aq_cl:1111111111111111 "
                              f"aq_sb:{rnd.randint(1, 31):02d} aq_ch:2 00 0000 time:{iso} [] []"))
            if rnd.random() < 0.30 * p:                              # timing beacon
                ft = t + rnd.uniform(0, step)
                out.append((ft, "line", header("ITL", ft, dop(1626.4375e6), snr(), rnd.randint(70, 95), 432)
                            + f"V2 OK P{rnd.randint(1, 4)} S{rnd.randint(1, 12):02d} M{rnd.randint(1, 8):02d} 0000111 1010001 0010101"))
            if rnd.random() < 0.10 * p:                              # pager channel fragments
                ft = t + rnd.uniform(0, step)
                out.append((ft, "line", header("IMS", ft, dop(rnd.choice((1626.104e6, 1626.4375e6))), snr(), rnd.randint(60, 95), 117)
                            + f"1:A:{rnd.randint(1, 30):02d} len:03/T1/F00 1 0 " + " ".join(f"{rnd.getrandbits(21):021b}" for _ in range(4))))
            if rnd.random() < 0.22 * p:                              # frames too damaged to classify
                ft = t + rnd.uniform(0, step)
                out.append((ft, "line", header("RAW", ft, dop(1624.6e6 + rnd.random() * 2.2e6), max(8.0, snr() - 5), rnd.randint(40, 70), 179,
                                               f"{rnd.randint(1, 2)}{rnd.randint(0, 3)}{rnd.randint(0, 9)}")
                            + f"<{rnd.getrandbits(24):024b}> 0"))
            if q == 1 and rnd.random() < 0.004 * step:               # a call starts on a duplex channel
                calls.append([t + rnd.uniform(25, 200), 1624.6e6 + 41666.667 * rnd.randint(0, 30), k])
        for c in calls:                                              # voice frames of the running calls
            if c[2] in visible and rnd.random() < 0.55:
                pos = sat_ecef(c[2], t); az, el, rng = look(rx, pos)
                if el > 8 and rnd.random() < view_quality(az, el):
                    rr = look(rx, sat_ecef(c[2], t + 1))[2] - rng
                    ft = t + rnd.uniform(0, step)
                    payload = ".".join(f"{rnd.getrandbits(8):02x}" for _ in range(39))
                    out.append((ft, "line", header("VOC", ft, c[1] * (1 - rr / C) + 900,
                                                   max(8.0, 12 + 10 * math.sin(math.radians(el)) + rnd.gauss(0, 1.5)), rnd.randint(55, 95), 179)
                                + f"LCW(0,T:maint,C:sync[status:0,dtoa:0,dfoa:0],0|0) [{payload}]"))
        calls[:] = [c for c in calls if c[0] > t]
        if rnd.random() < 0.0012 * step and visible:                 # now and then a whole pager message
            msg_n += 1
            stamp = datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%S")
            text = rnd.choice(["DEMO: this pager message is simulated", "DEMO: test page, no real content",
                               "DEMO: simulated weather bulletin 12 kt NW"])
            out.append((t, "pager", f"Message {rnd.randint(1000000, 9999999):07d} {msg_n % 100:02d} @{stamp} (len:0)  {rnd.randint(10, 99)}   OK: {text}"))
        out.sort(key=lambda e: e[0])
        yield from out
        t += step
        if not out:
            yield (t, "tick", "")   # lets the caller pace itself through quiet stretches
