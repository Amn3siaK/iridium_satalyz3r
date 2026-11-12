#!/usr/bin/env python3
"""Tests for the receiver control, satellite data quality, sky/Doppler data, exports, notes and demo mode.

    python3 -m unittest discover -s tests -v

The receiver tests run the real scripts/pipeline.sh with IRIDIUM_FAKE_BITS (two real timing frames from
tests/fixtures/itl.bits) in place of a radio; they need iridium-toolkit and are skipped without it.
"""
import csv, importlib.util, io, json, os, shutil, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("server", os.path.join(ROOT, "ui", "server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
import demo, export, frames, receiver          # noqa: E402  (ui/ is on sys.path once server is loaded)

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "itl.bits")
HAVE_TOOLKIT = os.path.isfile(os.path.join(server.TOOLKIT, "iridium-parser.py"))
T0 = 1762600000


def ira(ms, lat, lon, sat=16, alt=797, beam=37, freq=server.RA_FREQ, snr=18.0):
    return (f"IRA: p-{T0}-e000 {ms:014.4f} {freq} 95% -68.40|-117.34|{snr:.2f} 120 DL "
            f"sat:{sat:03d} beam:{beam:02d} xyz=(+1396,+0711,+0862) pos=({lat:+06.2f}/{lon:+07.2f}) alt={alt:03d} "
            "RAI:48 ?10 bc_sb:07 P00: {OK}")


class Frames(unittest.TestCase):
    def test_parse_and_mask(self):
        f = frames.parse(ira(1000, 28.8, 27.0))
        self.assertEqual((f["type"], f["clock"], f["freq"], f["snr"]), ("IRA", True, server.RA_FREQ, 18.0))
        self.assertAlmostEqual(f["t"], T0 + 1.0)
        self.assertEqual(frames.mask("IRA", "PAGE(tmsi:113dbaf8 msc_id:02)"), "PAGE(tmsi:113d···· msc_id:02)")
        self.assertIn("voice payload not shown", frames.mask("VOC", "LCW(0) [de.ad.be.ef.00.11]"))
        self.assertIsNone(frames.parse("RAW: junk"))

    def test_frames_without_clock_use_the_base_time(self):
        line = "ITL: u-hrf_1622000000Hz-e001 000051711.3858 1626262382  84% -30.19|-083.02|22.51 432 DL V2 OK P2"
        f = frames.parse(line, base=1000.0)
        self.assertFalse(f["clock"])
        self.assertAlmostEqual(f["t"], 1051.7113858)
        st = server.State("replay")
        st.add_line(line)
        self.assertAlmostEqual(st.last_frame, st.started + 51.7113858, places=4)   # not "now" for every frame


class SatelliteQuality(unittest.TestCase):
    def setUp(self):
        self.s = server.State("replay")

    def test_satellite_id_zero_ignored(self):
        self.s.add_line(ira(1000, 28.8, 27.0, sat=0))
        self.assertEqual(self.s.sats, {})

    def test_single_frame_is_unconfirmed(self):
        self.s.add_line(ira(1000, 28.8, 27.0))
        self.assertFalse(self.s.snapshot()["sats"][0]["confirmed"])
        self.s.add_line(ira(5320, 28.6, 27.0))
        self.assertTrue(self.s.snapshot()["sats"][0]["confirmed"])

    def test_impossible_jump_rejected(self):
        self.s.add_line(ira(0, 30.0, 20.0))
        self.s.add_line(ira(4320, 29.75, 20.0))         # ~28 km in 4 s: fine
        self.s.add_line(ira(8640, -40.0, 120.0))         # thousands of km: a bit error
        self.s.add_line(ira(12960, 29.25, 20.0))         # back on the track
        track = [p[:2] for p in self.s.sats[16]["track"]]
        self.assertNotIn([-40.0, 120.0], track)
        self.assertEqual(track[-1], [29.25, 20.0])
        self.assertEqual(self.s.rejected, 0)            # still pending, counted once a second bad point arrives

    def test_bad_first_point_is_replaced(self):
        self.s.add_line(ira(0, -40.0, 120.0))            # the first position is the bad one
        self.s.add_line(ira(4320, 30.0, 20.0))
        self.s.add_line(ira(8640, 29.75, 20.0))          # confirms the jump
        track = [p[:2] for p in self.s.sats[16]["track"]]
        self.assertEqual(track, [[30.0, 20.0], [29.75, 20.0]])
        self.assertEqual(self.s.rejected, 1)

    def test_receiver_estimate_from_spot_beams(self):
        for i, (lat, lon) in enumerate([(47.0, 10.0), (49.0, 12.0), (48.0, 11.0)]):
            self.s.add_line(ira(i * 1000, lat, lon, alt=5, beam=10 + i))
        est = self.s.snapshot()["rx_est"]
        self.assertAlmostEqual(est["lat"], 48.0, delta=0.1)
        self.assertAlmostEqual(est["lon"], 11.0, delta=0.1)
        self.assertEqual(est["n"], 3)

    def test_doppler_and_detail(self):
        for i, shift in enumerate((20000, 5000, -10000, -25000)):            # one pass over the documented channel
            self.s.add_line(ira(i * 4320, 30.0 - i * 0.25, 20.0, freq=server.RA_FREQ + shift))
        self.s.add_line(ira(5 * 4320, 28.75, 20.0, freq=1625000000))        # not in the simplex band
        d = self.s.sat_detail(16)
        self.assertEqual([p[1] for p in d["doppler"]], [20000, 5000, -10000, -25000])
        self.assertEqual({p[3] for p in d["doppler"]}, {server.RA_FREQ})
        self.assertEqual(len(d["track"]), 5)
        self.assertIsNone(self.s.sat_detail(99))

    def test_doppler_on_another_simplex_channel(self):
        # as in a real outdoor recording: ring alerts on 1626.3958 MHz, heard just after closest approach
        for i, shift in enumerate((-900, -2600, -5200, -11200, -18200, -27600)):
            self.s.add_line(ira(i * 30000, 30.0 - i, 20.0, freq=1626395833 + shift))
        d = self.s.sat_detail(16)
        self.assertEqual({p[3] for p in d["doppler"]}, {1626395833})
        self.assertEqual(d["doppler"][0][1], -900)

    def test_one_sided_pass_uses_the_sessions_channel(self):
        # a full pass on 1626.3958 MHz makes that channel unambiguous for the session ...
        for i, shift in enumerate((30000, 15000, 0, -15000, -30000)):
            self.s.add_line(ira(i * 30000, 30.0 - i, 20.0, sat=49, freq=1626395833 + shift))
        # ... so a far, slow, one-sided pass that fits two channels is put on it too (+34 kHz, not -8 kHz)
        for i, shift in enumerate((34300, 33000, 31500)):
            self.s.add_line(ira(i * 30000, 50.0 - i, 20.0, sat=3, freq=1626395833 + shift))
        self.assertEqual([p[1] for p in self.s.sat_detail(3)["doppler"]], [34300, 33000, 31500])

    def test_channel_grid(self):
        self.assertEqual(round(server.simplex_channel(1626270000)), server.RA_FREQ)
        self.assertEqual(round(server.simplex_channel(1625000000)), 1626020833)    # clamped to the band
        self.assertEqual(round(server.simplex_channel(1626430126)), 1626437500)

    def test_noise_per_minute(self):
        self.s.add_line(ira(0, 30.0, 20.0))
        row = self.s.snapshot()["snr_series"][0]
        self.assertEqual(row[1:], [18.0, 1, -117.3])                # snr, frames, noise

    def test_calls_counted_beyond_the_kept_list(self):
        c = server.Calls()
        c.done = c.done.__class__(maxlen=2)
        for i in range(5):                                          # five calls, 100 s apart
            for k in range(3):
                c.add(i * 100 + k, 1625e6, 15, 90)
        snap = c.snapshot(1000)
        self.assertEqual(snap["total"], 5)
        self.assertEqual(len(snap["recent"]), 2)


class Exports(unittest.TestCase):
    def setUp(self):
        self.s = server.State("replay")
        for i in range(3):
            self.s.add_line(ira(i * 4320, 30.0 - i * 0.25, 20.0))
        self.s.add_line(ira(5000, 45.0, 10.0, alt=5, beam=12))

    def test_csv(self):
        lines = [ira(0, 30.0, 20.0).replace("P00: {OK}", "P01: PAGE(tmsi:113dbaf8 msc_id:02) {OK}"), "garbage"]
        rows = list(csv.reader(io.StringIO("".join(export.frames_csv(lines)))))
        self.assertEqual(rows[0], export.CSV_COLUMNS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "2025-11-08T11:06:40.000Z")
        self.assertIn("tmsi:113d····", rows[1][-1])                  # masked like in the dashboard

    def test_geojson(self):
        g = export.tracks_geojson([self.s.sat_detail(16)])
        kinds = [f["properties"]["kind"] for f in g["features"]]
        self.assertEqual(kinds, ["satellite track", "spot beam centre"])
        self.assertEqual(g["features"][0]["geometry"]["type"], "LineString")
        self.assertEqual(g["features"][0]["geometry"]["coordinates"][0], [20.0, 30.0])     # lon, lat

    def test_summary_has_no_contents(self):
        snap = self.s.snapshot()
        out = export.summary(snap, [self.s.sat_detail(16)])
        json.dumps(out)
        self.assertEqual(out["total"], 4)
        self.assertNotIn("frames", out)
        self.assertNotIn("messages_text", out)


class NotesAndDelete(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for ext in ("parsed", "bits"):
            with open(os.path.join(self.dir, f"frames-20261108-120000.{ext}"), "w") as f:
                f.write(ira(0, 30.0, 20.0) + "\n")
        open(os.path.join(self.dir, "extractor-20261108-120000.log"), "w").close()
        self.lib = server.Library(self.dir)
        self.name = "frames-20261108-120000.parsed"

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_note(self):
        self.assertEqual(self.lib.set_note("session", self.name, "  antenna\n at the   window "), "antenna at the window")
        self.assertEqual(self.lib.sessions()[0]["note"], "antenna at the window")
        self.lib.set_note("session", self.name, "")
        self.assertEqual(self.lib.notes(), {})
        with self.assertRaises(LookupError):
            self.lib.set_note("session", "../x.parsed", "x")

    def test_delete_removes_all_files(self):
        self.lib.set_note("session", self.name, "x")
        self.assertGreater(self.lib.delete("session", self.name), 0)
        self.assertEqual(sorted(os.listdir(self.dir)), ["notes.json"])
        self.assertEqual(self.lib.notes(), {})

    def test_delete_refused_while_recording(self):
        self.lib.busy = lambda: {self.name}
        with self.assertRaises(ValueError):
            self.lib.delete("session", self.name)
        self.assertTrue(self.lib.sessions()[0]["live"])

    def test_listing_metrics(self):
        s = self.lib.sessions()[0]
        self.assertEqual((s["frames"], s["avg_snr"], s["sats"]), (1, 18.0, 0))      # one frame: not confirmed


class ReceiverUnits(unittest.TestCase):
    def test_configs(self):
        rx = receiver.Receiver(ROOT, tempfile.gettempdir(), None)
        names = {c["name"]: c for c in rx.configs()}
        self.assertEqual(names["hackrf"]["radio"], "HackRF")
        self.assertTrue(names["hackrf"]["manual_if"])
        self.assertEqual(names["rtl-sdr"]["radio"], "RTL-SDR")
        for bad in ("../etc/passwd", "nope", "", None, "a/b"):
            with self.assertRaises(LookupError):
                rx.config(bad)

    def test_seconds(self):
        self.assertIsNone(receiver.Receiver._seconds(None))
        self.assertEqual(receiver.Receiver._seconds("90"), 90)
        for bad in ("x", -5, 10 ** 9):
            with self.assertRaises(ValueError):
                receiver.Receiver._seconds(bad)

    def test_reasons(self):
        r = receiver.Receiver._reason
        self.assertIn("No HackRF", r(["hackrf_open() failed: HackRF not found (-5)"]))
        self.assertIn("No RTL-SDR", r(["Unable to open SoapySDR device: No RTL-SDR devices found!"]))
        self.assertIn("busy", r(["usb_claim_interface error -6"]))
        self.assertEqual(r(["something odd"]), "something odd")


class Hooks:
    """What server.App gives the receiver, recording what happens."""
    def __init__(self, logs):
        self.logs, self.sessions = logs, []

    def new_session(self, conf, stamp):
        s = server.Session("live", conf["radio"])
        s.parsed_path = os.path.join(self.logs, f"frames-{stamp}.parsed")
        self.sessions.append(s)
        return s

    def label(self, sess, radio, demod):
        sess.state.sdr["name"] = f"{radio} · {demod}"

    def follow_log(self, sess, path, stop):
        server.tail_extractor(sess.state, path, stop)


def wait_for(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


@unittest.skipUnless(HAVE_TOOLKIT, "iridium-toolkit not installed")
class ReceiverPipeline(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = {k: os.environ.get(k) for k in ("IRIDIUM_FAKE_BITS", "IRIDIUM_FAKE_DELAY")}
        os.environ.update(IRIDIUM_FAKE_BITS=FIXTURE, IRIDIUM_FAKE_DELAY="0.01")
        self.hooks = Hooks(self.dir)
        self.rx = receiver.Receiver(ROOT, self.dir, self.hooks)

    def tearDown(self):
        self.rx.stop(wait=True)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir)

    def test_start_and_stop(self):
        self.rx.start("hackrf")
        with self.assertRaises(ValueError):
            self.rx.start("hackrf")                                  # one job at a time
        sess = self.hooks.sessions[0]
        self.assertTrue(wait_for(lambda: sess.state.total == 2), "frames from the fake radio")
        self.assertEqual(sess.state.sdr["name"], "HackRF · replayed bits")
        self.assertTrue(self.rx.stop())
        self.assertTrue(wait_for(lambda: self.rx.state == "idle"))
        last = self.rx.last
        self.assertEqual((last["failed"], last["frames"], last["stopped"]), (False, 2, True))
        self.assertEqual(sess.state.snapshot()["mode"], "stopped")
        parsed = [f for f in os.listdir(self.dir) if f.endswith(".parsed")]
        self.assertEqual(len(parsed), 1)
        with open(os.path.join(self.dir, parsed[0])) as f:
            self.assertEqual([l.split(":")[0] for l in f], ["ITL", "ITL"])

    def test_timed_run_stops_by_itself(self):
        self.rx.start("rtl-sdr", seconds=2)
        self.assertTrue(wait_for(lambda: self.rx.state == "idle", 15))
        self.assertFalse(self.rx.last["failed"])

    def test_missing_input_is_a_failure_and_leaves_no_files(self):
        os.environ["IRIDIUM_FAKE_BITS"] = os.path.join(self.dir, "missing.bits")
        self.rx.start("hackrf")
        self.assertTrue(wait_for(lambda: self.rx.state == "idle"))
        self.assertTrue(self.rx.last["failed"])
        self.assertIn("no such file", self.rx.last["error"])
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith("frames-")], [])


class Demo(unittest.TestCase):
    def test_ten_simulated_minutes(self):
        st = server.State("demo")
        kinds = set()
        for t, kind, text in demo.events(1794000000, seed=3):
            if t > 1794000000 + 600:
                break
            if kind == "line":
                self.assertIsNotNone(frames.parse(text), text)
                st.add_line(text)
                kinds.add(text[:3])
        self.assertTrue({"IRA", "IBC", "ITL"} <= kinds)
        snap = st.snapshot()
        self.assertGreater(len([s for s in snap["sats"] if s["confirmed"]]), 2)
        self.assertEqual(st.rejected, 0)                             # simulated tracks are physically possible
        est = snap["rx_est"]
        self.assertAlmostEqual(est["lat"], demo.RX[0], delta=4)      # spot beams cluster around the receiver


class AppHttp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "frames-20261108-120000.parsed"), "w") as f:
            f.write(ira(0, 30.0, 20.0) + "\n" + ira(4320, 29.75, 20.0) + "\n")
        self.app = server.App(server.Session("idle", "x"), server.Library(self.dir))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.app.close(); shutil.rmtree(self.dir)

    def req(self, path, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body)
        r.add_header("Host", f"localhost:{self.port}")
        if body is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), e.headers

    def test_receiver_status_and_guards(self):
        code, body, _ = self.req("/api/receiver")
        st = json.loads(body)
        self.assertEqual((code, st["state"], st["main_mode"]), (200, "idle", "idle"))
        self.assertIn("hackrf", [c["name"] for c in st["configs"]])
        self.assertEqual(self.req("/api/receiver/start", json.dumps({"sdr": "../x"}).encode())[0], 404)
        self.assertEqual(self.req("/api/receiver/start", json.dumps({"sdr": "hackrf", "seconds": "x"}).encode())[0], 400)
        code, body, _ = self.req("/api/receiver/stop", b"{}")
        self.assertEqual((code, json.loads(body)["stopped"]), (200, False))

    def test_open_then_satellite_and_exports(self):
        body = json.dumps({"kind": "session", "name": "frames-20261108-120000.parsed"}).encode()
        self.assertEqual(self.req("/api/open", body)[0], 200)
        self.assertTrue(wait_for(lambda: self.app.library.current.info.get("status") == "done", 5))
        code, body, _ = self.req("/api/sat?view=rec&id=16")
        self.assertEqual((code, len(json.loads(body)["track"])), (200, 2))
        self.assertEqual(self.req("/api/sat?view=rec&id=99")[0], 404)
        code, body, headers = self.req("/api/export?view=rec&what=frames")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(len(body.decode().strip().splitlines()), 3)          # header + 2 frames, from the file
        self.assertEqual(self.req("/api/export?view=rec&what=tracks")[0], 200)
        self.assertEqual(self.req("/api/export?view=rec&what=nope")[0], 404)

    def test_note_and_delete_over_http(self):
        name = "frames-20261108-120000.parsed"
        code, body, _ = self.req("/api/note", json.dumps({"kind": "session", "name": name, "note": "roof"}).encode())
        self.assertEqual((code, json.loads(body)["note"]), (200, "roof"))
        self.assertEqual(self.req("/api/delete", json.dumps({"kind": "session", "name": name}).encode())[0], 200)
        self.assertEqual(json.loads(self.req("/api/recordings")[1])["sessions"], [])


if __name__ == "__main__":
    unittest.main()
