"""Receiver control for the dashboard: start and stop the SDR pipeline (scripts/pipeline.sh) and raw IQ
recordings (record-iq.sh), one job at a time, and keep the reason when a job fails.

The pipeline runs in its own process group. Stopping sends SIGINT to the group (what Ctrl+C does), so the
SDR and demodulator shut down cleanly and the scripts switch the antenna power off; SIGTERM and SIGKILL
follow only if that does not work.
"""
import collections, configparser, glob, os, re, signal, subprocess, threading, time

USB_IDS = {"rtl": ("0bda:2838", "0bda:2832"), "hackrf": ("1d50:6089",)}
KIND_NAMES = {"rtl": "RTL-SDR", "hackrf": "HackRF"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")
HEADER_RE = re.compile(r"^pipeline: sdr=(\S+) demod=(.+)$")
WAV_RE = re.compile(r"to (\S+\.wav)")
STAT_LINE = re.compile(r"\|\s*i_avg:")          # periodic statistics, not an error message
DEMODS = ("auto", "sniffer", "gr")


def read_config(path):
    """Name, radio type and tuning of a configs/<name>.conf."""
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",))
    cp.read(path)
    sect = next((s for s in cp.sections() if s.endswith("-source")), None)
    o = cp[sect] if sect else {}
    dev = o.get("device_args", "").strip("'\"")
    kind = "hackrf" if "hackrf" in dev else ("rtl" if "rtl" in dev else "other")
    try:
        centre, rate = float(o.get("center_freq", 0)), float(o.get("sample_rate", 0))
    except ValueError:
        centre = rate = 0.0
    return {"name": os.path.basename(path)[:-len(".conf")], "kind": kind, "radio": KIND_NAMES.get(kind, "SDR"),
            "centre": centre, "rate": rate, "antenna_power": "bias=1" in dev,
            "manual_if": cp.has_section("hackrf-transfer")}


class Receiver:
    """hooks.new_session(conf, stamp) returns the Session that takes the decoded frames;
    hooks.follow_log(session, path, stop_event) follows the receiver log for statistics."""

    def __init__(self, root, logs, hooks):
        self.root, self.logs, self.hooks = root, logs, hooks
        self.lock = threading.RLock()
        self.state = "idle"             # idle | running | recording | stopping
        self.proc = None
        self.job = None
        self.last = None                # how the previous job ended
        self._usb = (0.0, None)

    # --- what is available ---------------------------------------------------------
    def configs(self):
        out = []
        for p in sorted(glob.glob(os.path.join(self.root, "configs", "*.conf"))):
            try:
                c = read_config(p)
            except (configparser.Error, OSError):
                continue
            c["connected"] = self.connected(c["kind"])
            out.append(c)
        return out

    def config(self, name):
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise LookupError("unknown SDR config")
        p = os.path.join(self.root, "configs", name + ".conf")
        if not os.path.isfile(p):
            raise LookupError("unknown SDR config")
        return read_config(p)

    def connected(self, kind):
        """True/False if lsusb shows the radio, None when that can't be told."""
        ids = USB_IDS.get(kind)
        if not ids:
            return None
        t, text = self._usb
        if time.time() - t > 3:
            try:
                text = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=3).stdout.lower()
            except (OSError, subprocess.SubprocessError):
                text = None
            self._usb = (time.time(), text)
        return None if text is None else any(i in text for i in ids)

    # --- jobs ----------------------------------------------------------------------
    def _spawn(self, cmd, env, **kw):
        # A dashboard started in the background (./run-ui.sh &, nohup) has SIGINT ignored, and children
        # inherit that: the stop signal would do nothing. Give the job the default behaviour back.
        return subprocess.Popen(cmd, cwd=self.root, env=env, text=True, errors="replace",
                                start_new_session=True, stdin=subprocess.DEVNULL,
                                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL), **kw)

    def _env(self, sdr, demod=None):
        env = dict(os.environ, SDR=sdr, IRIDIUM_LOGS=self.logs)
        env.pop("SDR_CONF", None)           # the config follows the SDR chosen here
        if demod in DEMODS:
            env["DEMOD"] = demod
        return env

    def _stamp(self):
        while True:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            if not os.path.exists(os.path.join(self.logs, f"frames-{stamp}.parsed")):
                return stamp
            time.sleep(0.2)

    def start(self, sdr, seconds=None, demod=None):
        """Start live decoding. Raises LookupError (unknown config) or ValueError (busy, bad arguments)."""
        conf = self.config(sdr)
        seconds = self._seconds(seconds)
        with self.lock:
            if self.state != "idle":
                raise ValueError("the receiver is already busy: stop it first")
            stamp = self._stamp()
            os.makedirs(self.logs, exist_ok=True)
            proc = self._spawn(["bash", os.path.join(self.root, "scripts", "pipeline.sh"), stamp],
                               self._env(sdr, demod), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            session = self.hooks.new_session(conf, stamp)
            job = {"kind": "live", "sdr": sdr, "radio": conf["radio"], "started": time.time(), "seconds": seconds,
                   "stamp": stamp, "session": session, "stopping": False, "errors": collections.deque(maxlen=12),
                   "stop_log": threading.Event(), "demod": None}
            self.proc, self.job, self.state = proc, job, "running"
        threading.Thread(target=self._stderr, args=(proc, job), daemon=True).start()
        threading.Thread(target=self._run_live, args=(proc, job), daemon=True).start()
        threading.Thread(target=self.hooks.follow_log, daemon=True,
                         args=(session, os.path.join(self.logs, f"extractor-{stamp}.log"), job["stop_log"])).start()
        self._arm_timer(proc, seconds)
        return job

    def record(self, sdr, seconds=None):
        """Start a raw IQ recording (record-iq.sh)."""
        conf = self.config(sdr)
        if conf["kind"] not in ("rtl", "hackrf"):
            raise ValueError("IQ recording supports RTL-SDR and HackRF configs only")
        seconds = self._seconds(seconds)
        with self.lock:
            if self.state != "idle":
                raise ValueError("the receiver is already busy: stop it first")
            cmd = ["bash", os.path.join(self.root, "record-iq.sh")] + ([str(seconds)] if seconds else [])
            proc = self._spawn(cmd, self._env(sdr), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            job = {"kind": "record", "sdr": sdr, "radio": conf["radio"], "started": time.time(), "seconds": seconds,
                   "rate": conf["rate"], "file": None, "stopping": False, "errors": collections.deque(maxlen=12)}
            self.proc, self.job, self.state = proc, job, "recording"
        threading.Thread(target=self._run_record, args=(proc, job), daemon=True).start()
        return job

    @staticmethod
    def _seconds(seconds):
        if seconds in (None, "", 0):
            return None
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            raise ValueError("the duration must be a number of seconds")
        if not 1 <= seconds <= 7 * 86400:
            raise ValueError("the duration must be between 1 second and 7 days")
        return seconds

    def _arm_timer(self, proc, seconds):
        if seconds:
            t = threading.Timer(seconds, lambda: self.stop(only=proc))
            t.daemon = True
            t.start()

    def _stderr(self, proc, job):
        for line in proc.stderr:
            line = line.strip()
            m = HEADER_RE.match(line)
            if m:
                job["demod"] = m.group(2)
                self.hooks.label(job["session"], job["radio"], m.group(2))
            elif line:
                job["errors"].append(line)

    def _run_live(self, proc, job):
        session = job["session"]
        try:
            session.ingest(proc.stdout)
        finally:
            proc.wait()
            job["stop_log"].set()
            log = list(job["errors"]) + self._log_tail(os.path.join(self.logs, f"extractor-{job['stamp']}.log"))
            frames = session.state.total
            if frames == 0:                 # nothing to replay: don't leave an empty session in the recordings
                for name in (f"frames-{job['stamp']}.bits", f"frames-{job['stamp']}.parsed", f"extractor-{job['stamp']}.log"):
                    try:
                        os.remove(os.path.join(self.logs, name))
                    except OSError:
                        pass
            # the pipeline has no duration of its own: if nobody stopped it, the receiver failed
            failed = not job["stopping"]
            self._finish(proc, job, {"frames": frames, "failed": failed, "log": log[-12:], "demod": job["demod"],
                                     "error": (self._reason(log) if failed else None)})

    def _run_record(self, proc, job):
        for line in proc.stdout:
            line = line.strip()
            m = WAV_RE.search(line)
            if m and not job["file"]:
                job["file"] = m.group(1)
            if line:
                job["errors"].append(line)
        proc.wait()
        path, size = job["file"], 0
        if path and os.path.exists(path):
            size = os.path.getsize(path)
        log = list(job["errors"])
        failed = size <= 44 or (proc.returncode != 0 and not job["stopping"])
        self._finish(proc, job, {"file": os.path.basename(path) if size > 44 else None, "size": size,
                                 "failed": failed, "log": log[-12:], "error": self._reason(log) if failed else None})

    def _finish(self, proc, job, result):
        result.update(kind=job["kind"], sdr=job["sdr"], radio=job["radio"], ended=time.time(),
                      duration=round(time.time() - job["started"], 1), stopped=job["stopping"])
        with self.lock:
            self.last = result
            if self.proc is proc:
                self.proc, self.job, self.state = None, None, "idle"

    @staticmethod
    def _log_tail(path, limit=6):
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 6000))
                lines = f.read().decode(errors="replace").replace("\r", "\n").splitlines()
        except OSError:
            return []
        return [l.strip() for l in lines if l.strip() and not STAT_LINE.search(l)][-limit:]

    @staticmethod
    def _reason(log):
        """The most telling line of a failed job's log."""
        text = "\n".join(log).lower()
        hints = [
            ("usb_claim_interface", "The RTL-SDR is busy: another program (or the kernel TV driver) is using it."),
            ("resource busy", "The radio is busy: another program is using it."),
            ("hackrf_open() failed", "No HackRF found. Is it plugged in (and passed through to the VM)? Is another program using it?"),
            ("no rtl-sdr devices found", "No RTL-SDR found. Is it plugged in (and passed through to the VM)?"),
            ("no supported devices found", "No RTL-SDR found. Is it plugged in (and passed through to the VM)?"),
            ("wrong rtlsdr device index", "No RTL-SDR found. Is it plugged in (and passed through to the VM)?"),
            ("unable to open soapysdr device", "SoapySDR could not open the radio. Is it plugged in? Is soapysdr-module-rtlsdr installed?"),
            ("not enough disk space", "Not enough disk space for this recording."),
            ("hackrf_transfer not found", "hackrf_transfer is missing: install the hackrf package."),
            ("run ./install.sh", "The decoding tools are not installed: run ./install.sh"),
            ("no module named", "A Python module is missing: run ./install.sh --check"),
        ]
        for needle, msg in hints:
            if needle in text:
                return msg
        return log[-1] if log else "The receiver stopped without an error message."

    def stop(self, wait=False, only=None):
        """Stop the running job. `only` limits it to one process (timers must not stop a later job)."""
        with self.lock:
            proc, job = self.proc, self.job
            if proc is None or (only is not None and proc is not only):
                return False
            job["stopping"] = True
            self.state = "stopping"
        t = threading.Thread(target=self._terminate, args=(proc,), daemon=True)
        t.start()
        if wait:
            t.join(16)
        return True

    @staticmethod
    def _terminate(proc):
        for sig, patience in ((signal.SIGINT, 8), (signal.SIGTERM, 4), (signal.SIGKILL, 2)):
            if proc.poll() is not None:
                return
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(patience)
            except subprocess.TimeoutExpired:
                pass

    def status(self):
        with self.lock:
            job = self.job
            out = {"state": self.state, "last": self.last, "job": None}
            if job:
                j = {k: job[k] for k in ("kind", "sdr", "radio", "started", "seconds") }
                j["elapsed"] = round(time.time() - job["started"], 1)
                if job["kind"] == "live":
                    j["demod"] = job["demod"]
                    j["frames"] = job["session"].state.total
                else:
                    path = job["file"]
                    j["file"] = os.path.basename(path) if path else None
                    j["size"] = os.path.getsize(path) if path and os.path.exists(path) else 0
                out["job"] = j
        out["configs"] = self.configs()
        return out
