#!/usr/bin/env python3
"""Single shared tuner arbiter for both DAB+ and FM native streaming.

Why this exists: dab_bridge2.py and fm_bridge2.py each used to own an
independent ffmpeg-based StreamManager and listen for /live requests on
their own port. But there is exactly one physical Sundtek tuner behind
both of them - it can only ever be on one (mode, delsys, freq[, sid]) at
a time, and switching between DAB and FM (or between two DAB services,
even ones on the very same already-tuned frequency - live-tested, see
below) requires rtspd to fully release and retune. Two uncoordinated
processes each assuming they alone own the tuner is exactly what made
TVHeadend unable to tune reliably once it tried to touch both networks -
neither bridge knew the other had a stream open.

This service is the only thing that spawns ffmpeg / talks to rtspd's
/stream/ endpoint. dab_bridge2.py and fm_bridge2.py keep everything else
(scanning, station lists, MOT/DLS, RDS, TVHeadend channel mapping) but
point their generated M3U files at this service's /live URL instead of
serving streams themselves.

One important, live-tested correction from an earlier assumption: a
single DAB ensemble carries several services on one physical frequency,
so it seemed reasonable that rtspd could serve two of them concurrently
without a retune. Directly tested (two concurrent HTTP clients, same
delsys/freq, different sid) and disproved - the second concurrent
connection reliably receives 0 bytes while the first keeps working, even
right after a clean mediasrv/rtspd restart. So the resource model here is
deliberately the simple, strict one: at most one active stream, full
stop, for every switch - same frequency/different sid, different
frequency, or DAB<->FM - exactly mirroring how it already behaved through
rtspd's own web player.
"""
import json
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE_BASE = "/storage/dab-tvh-bridge"
SUNDTEK_BASE = "http://127.0.0.1:22000"
FM_DELSYS = 255

FFMPEG_BIN = "/storage/.kodi/addons/tools.ffmpeg-tools/bin/ffmpeg"
AUDIO_BITRATE = "192k"
LISTEN_PORT = 8090
DEBUG_LOG = f"{BRIDGE_BASE}/tuner_debug.log"


def _debug_log(msg):
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


class _StreamManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._key = None          # (mode, delsys, freq, sid) of the one active stream
        self._proc = None
        self._reader_thread = None
        self._clients = []        # list of file-like wfile objects currently attached
        self._generation = 0

    def _stream_url(self, mode, delsys, freq, sid):
        if mode == "fm":
            return f"{SUNDTEK_BASE}/stream/?delsys={FM_DELSYS}&freq={freq}&devid=0"
        if sid:
            return f"{SUNDTEK_BASE}/stream/?delsys={delsys}&freq={freq}&sid={sid}"
        return f"{SUNDTEK_BASE}/stream/?delsys={delsys}&freq={freq}&devid=0"

    def attach(self, mode, delsys, freq, sid, wfile):
        key = (mode, delsys, freq, sid)
        with self._lock:
            if key != self._key:
                self._stop_locked()
                try:
                    self._start_locked(key)
                except Exception as e:
                    _debug_log(f"[stream] FAILED to start ffmpeg for key={key}: {e!r}")
                    raise
            self._clients.append(wfile)
            _debug_log(f"[stream] client attached key={key} total_clients={len(self._clients)}")

    def detach(self, wfile):
        with self._lock:
            if wfile in self._clients:
                self._clients.remove(wfile)
                _debug_log(f"[stream] client detached, remaining={len(self._clients)}")
                if not self._clients:
                    self._stop_locked()

    def is_attached(self, wfile):
        with self._lock:
            return wfile in self._clients

    def status(self):
        with self._lock:
            if self._key is None:
                return {"active": None, "clients": 0}
            mode, delsys, freq, sid = self._key
            return {
                "active": {"mode": mode, "delsys": delsys, "freq": freq, "sid": sid},
                "clients": len(self._clients),
            }

    def _start_locked(self, key):
        mode, delsys, freq, sid = key
        url = self._stream_url(mode, delsys, freq, sid)
        cmd = [
            FFMPEG_BIN, "-loglevel", "error", "-i", url,
            "-vn", "-c:a", "mp2", "-b:a", AUDIO_BITRATE, "-f", "mpegts", "pipe:1",
        ]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self._key = key
        self._generation += 1
        my_generation = self._generation
        _debug_log(f"[stream] starting ffmpeg pid={self._proc.pid} key={key}")
        self._reader_thread = threading.Thread(target=self._reader_loop, args=(self._proc, my_generation), daemon=True)
        self._reader_thread.start()

    def _stop_locked(self):
        if self._proc is not None:
            _debug_log(f"[stream] stopping ffmpeg pid={self._proc.pid} key={self._key}")
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        self._key = None
        self._clients = []

    def _reader_loop(self, proc, generation):
        # A client's disconnect is only ever discovered here (the next
        # wfile.write() after they leave raises) - the request-handler
        # thread's own loop (do_GET's "while True: sleep(1); wfile.flush()")
        # does NOT reliably notice a half-closed connection (flush() on a
        # broken pipe live-tested as not raising), so it can never be
        # trusted to call detach() on its own. Stopping ffmpeg here, the
        # moment the client list empties out, is what actually matters -
        # without this, a single dropped client leaves ffmpeg (and the
        # tuner) running forever, starving every subsequent request.
        total = 0
        chunks = 0
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    _debug_log(f"[stream] reader gen={generation} got EOF from ffmpeg after {chunks} chunks / {total} bytes")
                    break
                chunks += 1
                total += len(chunk)
                with self._lock:
                    if generation != self._generation:
                        return
                    dead = []
                    for wfile in self._clients:
                        try:
                            wfile.write(chunk)
                        except Exception:
                            dead.append(wfile)
                    for wfile in dead:
                        if wfile in self._clients:
                            self._clients.remove(wfile)
                    if not self._clients:
                        _debug_log(f"[stream] reader gen={generation} last client gone after {chunks} chunks - stopping ffmpeg")
                        self._stop_locked()
                        return
        except Exception as e:
            _debug_log(f"[stream] reader loop error: {e!r}")


_stream_manager = _StreamManager()


def _send_json(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        if path == "/live":
            mode = (query.get("mode") or ["dab"])[0]
            freq = (query.get("freq") or [None])[0]
            if mode == "fm":
                delsys, sid = None, None
            else:
                delsys = (query.get("delsys") or [None])[0]
                sid = (query.get("sid") or [None])[0]
            if not freq or (mode != "fm" and not delsys):
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.end_headers()
            self.close_connection = True
            try:
                _stream_manager.attach(mode, delsys, freq, sid, self.wfile)
                # blocks for the connection's lifetime - see _reader_loop for
                # why detection of a disconnect has to live there, not here
                while _stream_manager.is_attached(self.wfile):
                    time.sleep(1)
            except Exception as e:
                _debug_log(f"[live] connection ended: {e!r}")
            finally:
                _stream_manager.detach(self.wfile)
            return

        if path == "/tuner/status":
            _send_json(self, 200, _stream_manager.status())
            return
        if path == "/tuner/debug":
            try:
                with open(DEBUG_LOG, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-200:]
            except FileNotFoundError:
                tail = []
            _send_json(self, 200, {"log_tail": "".join(tail)})
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
