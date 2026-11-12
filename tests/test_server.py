#!/usr/bin/env python3
"""Tests for ui/server.py (stdlib only, no SDR or iridium-toolkit needed).

    python3 -m unittest discover -s tests -v
"""
import base64, binascii, importlib.util, json, os, random, shutil, struct, tempfile, threading, time, unittest, urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("server", os.path.join(ROOT, "ui", "server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

T0 = 1762600000   # recording start (p-<unix time>)
IRA = (f"IRA: p-{T0}-e000 000001278.7213 1626270000  95% -68.40|-117.34|18.59 120 DL "
       "sat:016 beam:37 xyz=(+1396,+0711,+0862) pos=(+28.82/+026.99) alt=797 RAI:48 ?10 bc_sb:07 "
       "P01: PAGE(tmsi:113dbaf8 msc_id:02) {OK} FILL=10")
IRA_SPOT = (f"IRA: p-{T0} 000002000.0000 1626270000  90% -70.00|-117.00|16.00 120 DL "
            "sat:016 beam:12 xyz=(+1000,+0700,+0800) pos=(+30.00/+020.00) alt=002 RAI:48 ?10 bc_sb:07 {OK} FILL=12")
IBC = (f"IBC: p-{T0} 000003000.0000 1624500000  92% -71.00|-117.00|17.00 140 DL "
       "bc:0 sat:028 cell:24 0 slot:0 sv_blkn:0 aq_cl:1111111111111111 aq_sb:19 aq_ch:2 00 0000 time:2026-11-08T10:00:00.00Z [] []")


def voc(ms, freq=1625000000):
    return (f"VOC: p-{T0} {ms:014.4f} {freq} 88% -75.00|-117.00|15.00 179 DL "
            "LCW(0,T:maint,C:<silent>,0,0) [de.ad.be.ef.00.11]")


def device_data_text(payload=b"\x01\x02device-status-0042"):
    raw = payload + binascii.crc_hqx(payload, 0).to_bytes(2, "big")
    return base64.b64encode(raw).decode().translate(str.maketrans("/=", "*-"))


class Parsing(unittest.TestCase):
    def setUp(self):
        self.s = server.State("replay")

    def test_ring_alert_track_and_page(self):
        self.s.add_line(IRA)
        sat = self.s.sats[16]
        self.assertEqual(sat["track"][0][:2], [28.82, 26.99])
        self.assertEqual(sat["beams"], {37})
        self.assertEqual(self.s.page_total, 1)
        self.assertEqual(self.s.pages[0]["tmsi"], "113d····")        # masked
        self.assertAlmostEqual(self.s.last_frame, T0 + 1.2787213, places=5)

    def test_beam_spot(self):
        self.s.add_line(IRA_SPOT)
        self.assertEqual(self.s.sats[16]["spots"][0][:3], [30.0, 20.0, 12])
        self.assertEqual(self.s.sats[16]["track"], [])

    def test_broadcast(self):
        self.s.add_line(IBC)
        self.assertEqual(self.s.sats[28]["cells"], {24})

    def test_garbage_ignored(self):
        for line in ["", "RAW: junk", "Message 1 2 @x", "IRA: p-x 1 2"]:
            self.s.add_line(line)
        self.assertEqual(self.s.total, 0)

    def test_voice_metadata_only(self):
        for i in range(5):
            self.s.add_line(voc(1000 + i * 500))
        snap = self.s.snapshot()
        self.assertEqual(len(snap["calls"]["active"]), 1)
        self.assertEqual(snap["calls"]["active"][0]["frames"], 5)
        for f in snap["frames"]:
            self.assertNotIn("de.ad", f["text"])
            self.assertIn("[voice payload not shown]", f["text"])

    def test_call_ends_after_gap_and_short_bursts(self):
        for i in range(3):
            self.s.add_line(voc(1000 + i * 500))
        self.s.add_line(voc(1000 + 60000, freq=1620000000))        # 60 s later, other channel
        snap = self.s.snapshot()
        self.assertEqual(len(snap["calls"]["active"]), 0)
        self.assertEqual(len(snap["calls"]["recent"]), 1)
        self.s.ended = True
        self.assertEqual(self.s.snapshot()["calls"]["short_bursts"], 1)

    def test_incremental_snapshot(self):
        self.s.add_line(IRA)
        first = self.s.snapshot()
        self.s.add_line(IBC)
        nxt = self.s.snapshot(first["seq"])
        self.assertEqual([f["type"] for f in nxt["frames"]], ["IBC"])
        self.assertEqual([p[3] for p in nxt["points"]], ["IBC"])

    def test_rate_window_bounded_without_clients(self):
        self.s.recent_ts.extend([T0 - 3600] * 1000)                # frames from an hour before this one
        self.s.add_line(IRA)
        self.assertEqual(len(self.s.recent_ts), 1)
        live = server.State("live")
        live.add_line(IRA)                                         # an old frame arriving now (preloaded history)
        self.assertEqual(live.snapshot()["per_min"], 0)            # ... is not "the last minute"

    def test_snapshot_is_json(self):
        for line in (IRA, IRA_SPOT, IBC, voc(5000)):
            self.s.add_line(line)
        json.dumps(self.s.snapshot())


class Pager(unittest.TestCase):
    def test_crc_matches_xmodem(self):
        self.assertEqual(binascii.crc_hqx(b"123456789", 0), 0x31C3)   # CRC-16/XMODEM check value

    def test_device_data_detected(self):
        self.assertTrue(server.is_device_data(device_data_text()))

    def test_text_is_not_device_data(self):
        for body in ["HELLO WORLD CALL ME", "ABCDEFGHIJKLMNOP", "short", "", device_data_text()[:-4] + "AAAA"]:
            self.assertFalse(server.is_device_data(body), body)

    def test_parse_pager_line(self):
        line = f"Message 0123456 07 @2026-11-08T12:34:56 (len:3)  11   OK: {device_data_text()}"
        info = server.parse_pager(line)
        self.assertEqual(info["ric"], "012····")
        self.assertEqual(info["kind"], "device data")
        self.assertTrue(info["ok"])
        self.assertEqual(info["parts"], 4)   # toolkit prints the highest part index (0-based)
        self.assertIn("t", info)

    def test_parse_bcd_line(self):
        info = server.parse_pager("Message 0000042 01 @2026-11-08T12:00:00 (len:1) BCD fail: 0123")
        self.assertEqual(info["kind"], "numeric")
        self.assertFalse(info["ok"])

    def test_message_uses_frame_time(self):
        s = server.State("replay")
        s.add_line(IRA)
        s.add_message("sbd", "some sbd line")
        self.assertAlmostEqual(s.messages[0]["t"], s.last_frame)


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sess = server.Session("replay", "test")
        cls.state = cls.sess.state
        cls.state.add_line(IRA)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.sess))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path, host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_state_api(self):
        code, body = self.get("/api/state", f"localhost:{self.port}")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["total"], 1)

    def test_since_parameter(self):
        code, body = self.get("/api/state?since=1", f"localhost:{self.port}")
        self.assertEqual(json.loads(body)["frames"], [])

    def test_dns_rebinding_blocked(self):
        code, _ = self.get("/api/state", "evil.example:8888")
        self.assertEqual(code, 403)

    def test_static_and_traversal(self):
        self.assertEqual(self.get("/", f"127.0.0.1:{self.port}")[0], 200)
        self.assertEqual(self.get("/../server.py", f"127.0.0.1:{self.port}")[0], 404)
        self.assertEqual(self.get("/%2e%2e/server.py", f"127.0.0.1:{self.port}")[0], 404)

    def test_host_rules(self):
        for h in ["localhost", "localhost:8888", "127.0.0.1:1", "[::1]:8888", None]:
            self.assertTrue(server.host_allowed(h), h)
        for h in ["evil.com", "localhost.evil.com:8888", "127.0.0.1.nip.io", "192.168.1.5:8888"]:
            self.assertFalse(server.host_allowed(h), h)


def write_wav(path, rate=2500000, seconds=0.2, streaming=False, bits=8):
    """Small IQ WAV of noise. streaming=True leaves the size fields at 0, like record-iq.sh while recording."""
    n = int(rate * seconds) * 2 * bits // 8
    data = bytes(random.getrandbits(8) for _ in range(n))
    size = 0 if streaming else n
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 0 if streaming else 36 + n) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 2 * bits // 8, 2 * bits // 8, bits))
        f.write(b"data" + struct.pack("<I", size) + data)
    return n


class Recordings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.dir, "iq"))
        with open(os.path.join(self.dir, "frames-20261108-120000.parsed"), "w") as f:
            f.write("\n".join([IRA, IRA_SPOT, IBC, "garbage line"]) + "\n")
        with open(os.path.join(self.dir, "extractor-20261108-120000.log"), "w") as f:
            f.write("iridium-sniffer: center_freq=1625750000 Hz, sample_rate=2500000 Hz, threshold=16.0 dB\n"
                    "Found Rafael Micro R828D tuner\nRTL-SDR Blog V4 Detected\n")
        self.wav = "iq-rtl-sdr-20261108-120500_1625750000Hz_2500000sps.wav"
        self.wav_bytes = write_wav(os.path.join(self.dir, "iq", self.wav), streaming=True)
        write_wav(os.path.join(self.dir, "iq", "nofreq.wav"))
        self.lib = server.Library(self.dir)

    def tearDown(self):
        self.lib.close()
        shutil.rmtree(self.dir)

    def test_listing(self):
        ls = self.lib.listing()
        s = ls["sessions"][0]
        self.assertEqual((s["name"], s["frames"], s["demod"], s["sdr"]),
                         ("frames-20261108-120000.parsed", 3, "iridium-sniffer", "RTL-SDR"))
        iq = {i["name"]: i for i in ls["iq"]}
        w = iq[self.wav]
        self.assertEqual((w["centre"], w["rate"], w["format"], w["sdr"]), (1625750000.0, 2500000, "cu8", "RTL-SDR"))
        self.assertEqual(w["bytes"], self.wav_bytes)          # size field 0: data runs to the end of the file
        self.assertAlmostEqual(w["duration"], 0.2, places=1)
        self.assertIn("centre frequency", iq["nofreq.wav"]["error"])

    def test_wrapped_size_field(self):
        p = os.path.join(self.dir, "iq", "big_1622000000Hz.wav")
        n = write_wav(p)
        with open(p, "r+b") as f:      # pretend the real size was n + 4 GB
            f.seek(40); f.write(struct.pack("<I", (n - 1000) & 0xFFFFFFFF))
        self.assertEqual(server.wav_info(p)["bytes"], n - 1000)   # an honest smaller size is kept
        with open(p, "r+b") as f:
            f.seek(40); f.write(struct.pack("<I", n + 10))
        self.assertEqual(server.wav_info(p)["bytes"], n)          # too large: data runs to the end

    def test_names_only_from_logs(self):
        for kind, name in [("session", "../frames-x.parsed"), ("session", "/etc/passwd"), ("iq", "../../x.wav"),
                           ("session", "frames-nope.parsed"), ("other", self.wav), ("session", None)]:
            with self.assertRaises(LookupError):
                self.lib.open(kind, name)

    def test_open_session_replays(self):
        sess = self.lib.open("session", "frames-20261108-120000.parsed")
        for _ in range(50):
            if sess.info.get("status") == "done":
                break
            time.sleep(0.1)
        snap = sess.state.snapshot()
        self.assertEqual(snap["total"], 3)
        self.assertEqual(snap["mode"], "replay")
        self.assertEqual(snap["session"]["name"], "frames-20261108-120000.parsed")
        self.assertEqual(snap["freq"]["min"], 1624.5e6)       # band from the receiver log (1625.75 +- 1.25 MHz)
        self.lib.close()
        self.assertIsNone(self.lib.current)

    @unittest.skipUnless(os.access(server.SNIFFER, os.X_OK) and
                         os.path.isfile(os.path.join(server.TOOLKIT, "iridium-parser.py")), "iridium-sniffer not built")
    def test_decode_wav_writes_cache(self):
        sess = self.lib.open("iq", self.wav)
        for _ in range(300):
            if sess.info.get("status") != "decoding":
                break
            time.sleep(0.1)
        self.assertEqual(sess.info["status"], "done", sess.info.get("error"))
        self.assertEqual(sess.info["progress"], 1.0)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "iq", self.wav + ".parsed")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "iq", self.wav + ".parsed.partial")))
        again = self.lib.open("iq", self.wav)                 # second open uses the cache
        self.assertTrue(again.info.get("cached"))


class RecordingsHttp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "frames-20261108-120000.parsed"), "w") as f:
            f.write(IRA + "\n")
        self.lib = server.Library(self.dir)
        self.app = server.App(server.Session("idle", "x"), self.lib)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.lib.close(); shutil.rmtree(self.dir)

    def req(self, path, body=None, ctype="application/json", origin=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body)
        r.add_header("Host", f"localhost:{self.port}")
        if ctype:
            r.add_header("Content-Type", ctype)
        if origin:
            r.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_open_and_view(self):
        code, ls = self.req("/api/recordings")
        self.assertEqual((code, ls["main_mode"], len(ls["sessions"])), (200, "idle", 1))
        self.assertEqual(self.req("/api/state?view=rec")[0], 404)
        body = json.dumps({"kind": "session", "name": "frames-20261108-120000.parsed"}).encode()
        self.assertEqual(self.req("/api/open", body)[0], 200)
        time.sleep(0.3)
        code, snap = self.req("/api/state?view=rec")
        self.assertEqual((code, snap["total"]), (200, 1))
        self.assertEqual(self.req("/api/close", b"{}")[0], 200)
        self.assertEqual(self.req("/api/state?view=rec")[0], 404)

    def test_post_guards(self):
        body = json.dumps({"kind": "session", "name": "frames-20261108-120000.parsed"}).encode()
        self.assertEqual(self.req("/api/open", body, ctype="text/plain")[0], 415)         # form/simple POST
        self.assertEqual(self.req("/api/open", body, origin="http://evil.example")[0], 403)
        self.assertEqual(self.req("/api/open", json.dumps({"kind": "session", "name": "../x"}).encode())[0], 404)
        self.assertEqual(self.req("/api/open", body, origin=f"http://localhost:{self.port}")[0], 200)


if __name__ == "__main__":
    unittest.main()
