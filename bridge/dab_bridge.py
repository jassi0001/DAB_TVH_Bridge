#!/usr/bin/env python3
import base64
import ctypes
import glob
import json
import os
import pty
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Central install paths - change these two if the bridge or the Sundtek SDK
# ever move to a different location on the box; every other path below is
# derived from them rather than hardcoded separately.
BRIDGE_BASE = "/storage/dab-tvh-bridge"
DABPLUS_BASE = "/storage/dabplus"

SUNDTEK_BASE = "http://127.0.0.1:22000"
DAB_DEVICE_ID = 0
DAB_DELSYS = 15  # Sundtek's delivery-system enum value for DAB/DAB+ on this stick

# Direct tuner access via Sundtek's own SDK (libmcsimple.so, ships with the
# driver under DABPLUS_BASE/opt/) instead of the HTTP servercmd.xhx API -
# used only for read-only signal/ensemble info, which the HTTP API doesn't
# expose at all. Constants/structs from DABPLUS_BASE/opt/include/mediacmds.h.
# See TVHEADEND_QUIRKS.md for why this is deliberately NOT used for anything
# beyond read-only queries (scan control etc. stay on the proven HTTP API).
MCSIMPLE_LIB = f"{DABPLUS_BASE}/opt/lib/libmcsimple.so"
DAB_GET_TUNER = 68
DAB_HAS_LOCK = 0x01
DAB_GET_ENSEMBLE_INFO = 128
DAB_GET_SERVICE = 111

# Cover art / station logo (DAB+ "MOT Slideshow") capture. `mediaclient
# --getdigitaldata` decodes this and any Dynamic Label (song title) text
# broadcast on the currently selected service, writing decoded images to
# /tmp/<n>.JPG - but as of the SDK build on this box (2026-06-25) it only
# ever emits Slideshow images, never DLS text, even for stations confirmed
# to broadcast both (see SUNDTEK_INSTALL.md Abschnitt 4 for the
# investigation). Sundtek has an open forum inquiry about this; once a
# future mediaclient build starts emitting DLS text too, _digitaldata_reader
# below is where to add parsing for it - see the TODO in that function.
MEDIACLIENT_BIN = f"{DABPLUS_BASE}/opt/bin/mediaclient"
# Live observed (2026-09-23): not every station's slideshow is a single
# rotating cover image - some (e.g. NDR 2, NDR Info) interleave several
# distinct content types under one service (news graphics, weather,
# teasers, programme tips, the actual cover) at a fast pace, each with its
# own content-name pattern like "<date>_<time>Z_<TYPE>_<id>.jpg". Keeping
# only ever the single latest successful capture meant most of these types
# never got shown even when they DID arrive intact, since a later type's
# (possibly still-incomplete) slide could "currently be in progress"
# without ever superseding the last complete one in a meaningful way, and
# there was no way to tell them apart anyway. ARTWORK_DIR instead holds one
# file per content type (see _artwork_type_from_filename), so e.g. a
# completed "wetter" capture keeps showing even while "sendung" is mid
# transfer.
ARTWORK_DIR = f"{BRIDGE_BASE}/artwork"
DEFAULT_ARTWORK_TYPE = "cover"
# Matches mediaclient's own long-form content names, e.g.
# "20260923_051518Z_PROGRAMMTIPP_47698.jpg" -> type "programmtipp". Plain
# incrementing counters like "37.JPG" (most stations tested so far) don't
# match and fall back to DEFAULT_ARTWORK_TYPE - not a real distinction
# there since those stations only ever send one type of slide anyway.
_ARTWORK_TYPE_RE = re.compile(r"^\d{8}_\d{6}Z_([A-Za-z0-9]+)_\d+\.\w+$")
DIGITALDATA_POLL_SECONDS = 5
FFMPEG_BIN = "ffmpeg"
AUDIO_BITRATE = "192k"
SERVICE_PROVIDER = "Radio channels"
LISTEN_PORT = 8081
EXCLUDE_FILE = f"{BRIDGE_BASE}/exclude.txt"
RENAME_FILE = f"{BRIDGE_BASE}/renames.txt"
RESCAN_LOG = f"{BRIDGE_BASE}/dab.log"

# Per-location saved station lists (mirrors the DVB-T bridge's per-location
# networks) - lets a previously visited location's list be reactivated
# instantly, without a fresh scan. Purely additive: the GPS-triggered
# /dab/rescan flow never names a location and always clears any active one,
# so it keeps working exactly as before and always wins with fresh data.
DAB_LOCATIONS_DIR = f"{BRIDGE_BASE}/locations"
DAB_ACTIVE_LOCATION_FILE = f"{BRIDGE_BASE}/active_location.txt"

TVH_HOST = "127.0.0.1"
TVH_PORT = 9981
TVH_USER = "claude"
TVH_PASS = "claude"
TVH_NETWORK_NAME = "DAB+"
MAPPER_POLL_SECONDS = 30
MAPPER_SETTINGS = {
    "check_availability": False,
    "encrypted": False,
    "merge_same_name": True,
    "merge_same_name_fuzzy": True,
    "tidy_channel_name": True,
    "type_tags": False,
    "provider_tags": False,
    "network_tags": False,
}
# TVHeadend channel tag this box already has set up; matched by name so a
# manual retag/recreate in the UI doesn't break this.
RADIO_CHANNELS_TAG_NAME = "Radio channels"


def _tvh_req(path, data=None, timeout=15):
    auth = base64.b64encode(f"{TVH_USER}:{TVH_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        r = urllib.request.Request(f"http://{TVH_HOST}:{TVH_PORT}{path}", data=body, headers=headers, method="POST")
    else:
        r = urllib.request.Request(f"http://{TVH_HOST}:{TVH_PORT}{path}", headers=headers)
    return urllib.request.urlopen(r, timeout=timeout)


def map_unmapped_dab_services():
    """Finds DAB+ services without a mapped channel and maps them, merging
    same-named services (from SFN simulcasts) into a single channel."""
    with _tvh_req("/api/mpegts/service/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    unmapped = [
        e["uuid"] for e in grid["entries"]
        if e.get("network") == TVH_NETWORK_NAME and not e.get("channel")
    ]
    if not unmapped:
        return 0
    node = {"services": unmapped, **MAPPER_SETTINGS}
    with _tvh_req("/api/service/mapper/save", data={"node": json.dumps(node)}):
        pass

    # DAB+ streams carry no real EPG data (synthetic ffmpeg-piped audio, no
    # EIT table) - TVHeadend's "auto EPG source" just wastes cycles searching
    # for a name match, so disable it on the channels this pass just created.
    with _tvh_req("/api/mpegts/service/grid?start=0&limit=500") as resp:
        grid_after = json.loads(resp.read())
    new_channel_uuids = sorted({
        c for e in grid_after["entries"]
        if e["uuid"] in unmapped
        for c in e.get("channel", [])
    })
    if new_channel_uuids:
        epgauto_node = json.dumps([{"uuid": u, "epgauto": False} for u in new_channel_uuids])
        with _tvh_req("/api/idnode/save", data={"node": epgauto_node}):
            pass

    return len(unmapped)


def disable_dab_mux_epg():
    """Muxes default to EPG-Abfrage "Aktivieren (auto)" - separate from and
    in addition to the per-channel epgauto flag above. Same reasoning: DAB+
    muxes never carry real EPG data, so leave this off for any mux (existing
    or newly (re)scanned) where it's still on."""
    with _tvh_req("/api/mpegts/mux/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    stale = [e["uuid"] for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME and e.get("epg") != 0]
    if not stale:
        return 0
    node = json.dumps([{"uuid": u, "epg": 0} for u in stale])
    with _tvh_req("/api/idnode/save", data={"node": node}):
        pass
    return len(stale)


def _get_tag_uuid(name):
    with _tvh_req("/api/channeltag/grid?start=0&limit=200") as resp:
        grid = json.loads(resp.read())
    match = next((t for t in grid["entries"] if t.get("name") == name), None)
    return match["uuid"] if match else None


def tag_dab_channels_as_radio():
    """Every DAB+ channel is radio by definition - apply the "Radio
    channels" tag to any that's missing it (covers ones mapped before this
    existed, not just newly mapped ones)."""
    tag_uuid = _get_tag_uuid(RADIO_CHANNELS_TAG_NAME)
    if not tag_uuid:
        return 0
    with _tvh_req("/api/mpegts/service/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    channel_uuids = sorted({c for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME for c in e.get("channel", [])})
    with _tvh_req("/api/channel/grid?start=0&limit=2000") as resp:
        chgrid = json.loads(resp.read())
    by_uuid = {c["uuid"]: c for c in chgrid["entries"]}
    missing = [u for u in channel_uuids if u in by_uuid and tag_uuid not in (by_uuid[u].get("tags") or [])]
    if not missing:
        return 0
    node = json.dumps([{"uuid": u, "tags": sorted(set((by_uuid[u].get("tags") or []) + [tag_uuid]))} for u in missing])
    with _tvh_req("/api/idnode/save", data={"node": node}):
        pass
    return len(missing)


def mapper_loop():
    while True:
        try:
            n = map_unmapped_dab_services()
            if n:
                print(f"[mapper] mapped {n} newly scanned DAB+ service(s) to channels")
        except Exception as e:
            print(f"[mapper] error: {e}")
        try:
            n = disable_dab_mux_epg()
            if n:
                print(f"[mapper] disabled EPG scan on {n} DAB+ mux(es)")
        except Exception as e:
            print(f"[mapper] error: {e}")
        try:
            n = tag_dab_channels_as_radio()
            if n:
                print(f"[mapper] tagged {n} DAB+ channel(s) as Radio channels")
        except Exception as e:
            print(f"[mapper] error: {e}")
        time.sleep(MAPPER_POLL_SECONDS)


def _sundtek_post(cmd_body, timeout=15):
    req = urllib.request.Request(
        f"{SUNDTEK_BASE}/servercmd.xhx",
        data=cmd_body.encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(errors="replace")


def _device_serial():
    with urllib.request.urlopen(f"{SUNDTEK_BASE}/devices.json", timeout=10) as resp:
        devices = json.loads(resp.read())
    return devices[0][3]


class _DabTuner(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("signalstrength", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("rssi", ctypes.c_uint8),
        ("snr", ctypes.c_uint8),
        ("fic_quality", ctypes.c_uint8),
        ("cnr", ctypes.c_uint8),
    ]


class _DabEnsembleInfo(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("global_id", ctypes.c_uint8),
        ("lang", ctypes.c_uint8),
        ("charset_id", ctypes.c_uint8),
        ("label", ctypes.c_char * 17),
    ]


class _DabService(ctypes.Structure):
    # matches struct dab_service in mediacmds.h - the currently selected
    # service's own SID, straight from the tuner. There's no equivalent
    # "get current frequency" ioctl in this SDK (DAB_SET_FREQUENCY is
    # write-only); the frequency shown alongside this is looked up from the
    # station's own stream URL instead (see get_dab_service_info()).
    _pack_ = 1
    _fields_ = [
        ("id", ctypes.c_uint8),
        ("service_name", ctypes.c_char * 17),
        ("sid", ctypes.c_uint32),
        ("comp", ctypes.c_uint16 * 10),
        ("n_comp", ctypes.c_int8),
    ]


_mcsimple = None


def _get_mcsimple():
    global _mcsimple
    if _mcsimple is None:
        lib = ctypes.CDLL(MCSIMPLE_LIB)
        lib.net_open.restype = ctypes.c_int
        lib.net_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.net_ioctl.restype = ctypes.c_int
        lib.net_ioctl.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
        lib.net_close.restype = ctypes.c_int
        lib.net_close.argtypes = [ctypes.c_int]
        _mcsimple = lib
    return _mcsimple


def _find_active_dab_device():
    """/dev/dabN device nodes accumulate across scans/tunes (stale ones fail
    to open) - the most recently touched one is the live one."""
    lib = _get_mcsimple()
    candidates = sorted(glob.glob("/dev/dab*"), key=lambda p: os.stat(p).st_mtime, reverse=True)
    for device in candidates:
        fd = lib.net_open(device.encode(), 0)
        if fd >= 0:
            lib.net_close(fd)
            return device
    return None


def _current_station_frequency(channel_name):
    """Best-effort frequency lookup for the given (TVHeadend display) channel
    name, from the station's own stream URL - the SDK has no "get current
    frequency" ioctl (DAB_SET_FREQUENCY is write-only), but mediasrv's own
    chlist URLs always carry a "freq=" query parameter, and we already fetch
    that list for /dab/stations. Returns None on any lookup failure (no
    active location, name mismatch, network hiccup) - this is purely
    cosmetic info for the dashboard, never worth failing the whole
    /dab/signal response over."""
    if not channel_name:
        return None
    try:
        renames = load_renames()
        for raw_name, url in fetch_stations():
            display_name = renames.get(raw_name, raw_name)
            if display_name == channel_name or raw_name == channel_name:
                parsed = urllib.parse.urlsplit(url)
                query = urllib.parse.parse_qs(parsed.query)
                freq = (query.get("freq") or [None])[0]
                return int(freq) if freq else None
    except Exception:
        return None
    return None


def get_dab_signal_info():
    """Ensemble name + tuner lock/signal quality, read directly via the SDK
    (libmcsimple.so) rather than the HTTP API, which doesn't expose this at
    all. Also includes the current service's SID (DAB_GET_SERVICE, straight
    from the tuner) and, best-effort, the tuned frequency (looked up from
    the current station's own stream URL - see _current_station_frequency,
    there's no direct "get frequency" ioctl in this SDK)."""
    lib = _get_mcsimple()
    candidates = sorted(glob.glob("/dev/dab*"), key=lambda p: os.stat(p).st_mtime, reverse=True)
    for device in candidates:
        fd = lib.net_open(device.encode(), 0)
        if fd < 0:
            continue
        try:
            tuner = _DabTuner()
            lib.net_ioctl(fd, DAB_GET_TUNER, ctypes.byref(tuner))
            ens = _DabEnsembleInfo()
            lib.net_ioctl(fd, DAB_GET_ENSEMBLE_INFO, ctypes.byref(ens))
            svc = _DabService()
            lib.net_ioctl(fd, DAB_GET_SERVICE, ctypes.byref(svc))
            service_name = svc.service_name.decode("ascii", "replace").strip()
            channel = get_dab_now_playing()["playing"]
            channel_name = channel[0] if channel else None
            return {
                "device": device,
                "locked": bool(tuner.status & DAB_HAS_LOCK),
                "rssi": tuner.rssi,
                "snr": tuner.snr,
                "fic_quality": tuner.fic_quality,
                "cnr": tuner.cnr,
                "ensemble": ens.label.decode("ascii", "replace").strip(),
                "sid": f"0x{svc.sid:X}" if svc.sid else None,
                "service_name": service_name or None,
                "frequency": _current_station_frequency(channel_name),
            }
        finally:
            lib.net_close(fd)
    return {
        "device": None, "locked": False, "rssi": None, "snr": None,
        "fic_quality": None, "cnr": None, "ensemble": None,
        "sid": None, "service_name": None, "frequency": None,
    }


def get_dab_now_playing():
    """Which DAB+ channel(s) are currently being streamed, straight from
    TVHeadend's own subscription table - far more reliable than trying to
    read the current service back off the tuner itself (see
    SUNDTEK_INSTALL.md Abschnitt 4). TVHeadend already knows exactly which
    channel each active client subscribed to, since it's the one that set up
    the subscription - no separate tuner-state guessing needed."""
    with _tvh_req("/api/status/subscriptions") as resp:
        data = json.loads(resp.read())
    channels = sorted({
        e["channel"] for e in data.get("entries", [])
        if f"/{TVH_NETWORK_NAME}/" in (e.get("service") or "") and e.get("channel")
    })
    return {"playing": channels}


_digitaldata_lock = threading.Lock()
_digitaldata_proc = None
_digitaldata_channel = None
# Maps artwork "type" (see _artwork_type_from_filename) -> {"updated_at":
# <ts>, "channel": <name>} for every type successfully captured for the
# CURRENT channel. Replaces the old single artwork_updated_at/artwork_
# channel pair now that a station can carry several concurrent slide types
# (see ARTWORK_DIR above). Cleared (along with ARTWORK_DIR's contents)
# every time the playing channel changes, same as the old single-file
# state was.
_artwork_buffer = {}


def _artwork_type_from_filename(src):
    m = _ARTWORK_TYPE_RE.match(os.path.basename(src))
    return m.group(1).lower() if m else DEFAULT_ARTWORK_TYPE


def _artwork_path_for_type(artwork_type):
    return f"{ARTWORK_DIR}/{artwork_type}.jpg"


# Bumped every time digital_data_loop() starts or stops a mediaclient
# process (i.e. on every channel-state transition). A _copy_artwork_when_
# stable() call captures the generation that was current when it was
# spawned and checks it again right before writing - live confirmed bug:
# a slide's stability-check can take up to its ~3 min budget, so a watcher
# spawned for the previous channel can still be mid-poll when the channel
# changes (e.g. RTL RADIO -> Absolut BELLA); without this check, that late
# watcher finishes validating its (old-channel) image well after the
# channel-change reset already ran and overwrites the fresh "no cover yet"
# state with a stale image tagged with the WRONG (old) channel name and a
# deceptively recent updated_at, making HA show "current cover" for a
# station that has actually moved on.
_digitaldata_generation = 0

# Persistent debug log for the digitaldata/artwork pipeline specifically -
# print() output from this thread is unreliable (systemd's journal has been
# observed to rotate away everything within ~15 min on this box, and
# print() through a pty is line-buffered but still easy to miss live), so
# anything worth being able to reconstruct after the fact goes here
# instead. Deliberately separate from RESCAN_LOG (different concern, would
# otherwise be hard to follow interleaved). Kept small by design: every
# OPENED line, every capture attempt's outcome, every channel transition -
# not full mediaclient output.
DIGITALDATA_DEBUG_LOG = f"{BRIDGE_BASE}/digitaldata_debug.log"


def _debug_log(msg):
    try:
        with open(DIGITALDATA_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _copy_artwork_when_stable(src, channel, generation, checks=4, interval=2.0, max_attempts=90):
    """Waits for `src` to stop growing before copying it - mediaclient
    writes the file incrementally over several seconds as MOT body segments
    arrive, so copying too early grabs a truncated/corrupt image. Live
    tested: copying on "OPENED:" itself produced a ~500-byte fragment;
    copying on the NEXT "OPENED:" line (implying the previous write must be
    done) still produced a corrupt JPEG missing its header - only a genuine
    size-stability check turned out to be reliable.

    max_attempts is deliberately generous (~3 min budget) - image size (and
    so transfer time) varies a lot by station. Live observed: some stations'
    slideshow images are 5-10x larger than others (confirmed: one station's
    image was still growing linearly past 24KB at the 36s mark, nowhere
    near done) - the original 30s budget was simply too short and silently
    discarded every single slide for that station, discovered only by
    watching file sizes directly rather than trusting the "it times out
    sometimes" assumption."""
    _debug_log(f"[watch] start src={src!r} channel={channel!r} generation={generation}")
    last_size = -1
    stable_count = 0
    for _ in range(max_attempts):
        time.sleep(interval)
        try:
            size = os.path.getsize(src)
        except OSError as e:
            _debug_log(f"[watch] src={src!r} vanished/unreadable during stability wait: {e}")
            return
        if size == last_size and size > 0:
            stable_count += 1
            if stable_count >= checks:
                break
        else:
            stable_count = 0
        last_size = size
    else:
        _debug_log(f"[watch] src={src!r} never stabilized within {max_attempts * interval:.0f}s budget, last size={last_size}")
        return  # never stabilized within the time budget - skip this one

    # Even a size-stable file can be a genuinely corrupt/truncated JPEG -
    # live observed both ends of this: reception issues occasionally
    # drop/garble the first MOT body segment (missing SOI marker), and even
    # with that fixed, a file can still be missing its trailing EOI marker
    # (truncated at the end - the stability check can be fooled by a pause
    # between segment bursts that isn't actually completion). Checking both
    # markers is what actually caught a truncated-but-"stable" file in
    # testing. One short retry covers the "stability check ended just
    # barely too early" case; if it's still bad after that, skip and wait
    # for the next slide rather than serving a broken image to HA.
    for attempt in range(2):
        try:
            with open(src, "rb") as f:
                data = f.read()
        except OSError as e:
            _debug_log(f"[watch] src={src!r} could not be read for SOI/EOI check: {e}")
            return
        if data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
            break
        if attempt == 0:
            time.sleep(3)
    else:
        _debug_log(f"[watch] src={src!r} size={len(data)} failed SOI/EOI check after retry (SOI={data[:2].hex()}, EOI={data[-2:].hex()}) - discarded")
        return

    artwork_type = _artwork_type_from_filename(src)
    artwork_path = _artwork_path_for_type(artwork_type)
    with _digitaldata_lock:
        if generation != _digitaldata_generation:
            _debug_log(f"[watch] src={src!r} channel={channel!r} type={artwork_type!r} passed validation but generation stale ({generation} != {_digitaldata_generation}) - discarded")
            return  # channel changed while this slide was being validated - discard, it's stale
    try:
        os.makedirs(ARTWORK_DIR, exist_ok=True)
        # Write to a temp file and rename into place rather than writing
        # artwork_path directly - a new slide typically arrives while
        # the previous one is still being served to an HTTP client (GET
        # /dab/artwork), and a plain open()+write() on the same path lets a
        # concurrent reader see a torn mix of the old and new file's bytes
        # (live confirmed: a downloaded "artwork.jpg" had neither a valid
        # SOI nor EOI marker, even though this function validates both
        # before ever writing). os.replace() is atomic on the same
        # filesystem (POSIX rename) - a concurrent reader always sees
        # either the complete old file or the complete new one, never both.
        #
        # The temp filename must be unique per call, not shared - a first
        # attempt using a single fixed "<path>.tmp" for every call still
        # produced corrupt downloads, because _digitaldata_reader() spawns
        # one of these calls per "OPENED:" line with no limit on how many
        # can be in flight together; two overlapping calls (e.g. mediaclient
        # opening a new slide while the previous one is still being
        # stability-checked) both truncating and writing the same shared
        # tmp path race each other, so even the tmp file itself can end up
        # torn before the rename ever happens. Suffixing with the source
        # filename (mediaclient's own /tmp/<n>.JPG counter, unique per
        # slide) gives each call its own tmp file - and since two different
        # types can never collide on the same artwork_path anyway (that's
        # the whole point of the per-type buffer), this only still matters
        # for two overlapping slides of the SAME type.
        tmp_path = f"{artwork_path}.{os.path.basename(src)}.tmp"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, artwork_path)
        with _digitaldata_lock:
            _artwork_buffer[artwork_type] = {"updated_at": time.time(), "channel": channel}
        _debug_log(f"[watch] src={src!r} channel={channel!r} type={artwork_type!r} SUCCESS size={len(data)}")
    except OSError as e:
        _debug_log(f"[watch] src={src!r} channel={channel!r} type={artwork_type!r} write failed: {e}")


def _digitaldata_reader(master_fd, channel, generation):
    """Runs in its own thread for the lifetime of one `mediaclient
    --getdigitaldata` process, reading its output line by line as it
    arrives via a pty (see digital_data_loop() for why a plain pipe doesn't
    work). Currently only handles the MOT Slideshow (cover art) lines it
    actually emits - see the TODO below for where Dynamic Label (song
    title) text would be parsed once a future mediaclient build supports it
    (Sundtek has an open forum inquiry about this). Each "OPENED: <path>"
    spawns its own stability-poll watcher (see _copy_artwork_when_stable)
    rather than trying to time the copy off log lines alone.

    Deliberately does not assume anything about the OPENED path's filename
    (extension, numeric vs. named, length) - mediaclient's own naming
    varies by station (plain incrementing counters like /tmp/19.JPG on most
    stations tested so far, but live confirmed at least one station, NDR 2,
    uses long timestamp+label names like
    /tmp/20260922_052520Z_SENDUNG_59613.jpg instead) and there is no reason
    to assume those are the only two forms. Whatever text follows "OPENED: "
    up to the newline is used as-is; validation is by file content (SOI/EOI
    bytes in _copy_artwork_when_stable), never by name pattern."""
    buf = b""
    opened_count = 0
    _debug_log(f"[reader] started channel={channel!r} generation={generation}")
    try:
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as e:
                _debug_log(f"[reader] channel={channel!r} generation={generation} os.read failed: {e}")
                break  # pty closed - process exited/was terminated
            if not chunk:
                _debug_log(f"[reader] channel={channel!r} generation={generation} got EOF on pty")
                break
            buf += chunk
            while b"\n" in buf:
                raw_line, buf = buf.split(b"\n", 1)
                line = raw_line.decode(errors="replace").strip()
                if line.startswith("OPENED: "):
                    src = line[len("OPENED: "):].strip()
                    opened_count += 1
                    _debug_log(f"[reader] channel={channel!r} generation={generation} OPENED #{opened_count}: {src!r}")
                    threading.Thread(target=_copy_artwork_when_stable, args=(src, channel, generation), daemon=True).start()
                # TODO: once mediaclient starts emitting Dynamic Label (song
                # title/artist) text again - currently it only ever emits MOT
                # Slideshow data, never DLS, see SUNDTEK_INSTALL.md
                # Abschnitt 4 - parse those lines here the same way and
                # expose them via a new get_dab_now_playing_metadata()-style
                # function/endpoint.
    finally:
        _debug_log(f"[reader] exiting channel={channel!r} generation={generation} total OPENED lines seen={opened_count}")
        try:
            os.close(master_fd)
        except OSError:
            pass


def digital_data_loop():
    """Keeps exactly one `mediaclient --getdigitaldata` process attached to
    the currently active DAB+ channel, for as long as something is actually
    playing (get_dab_now_playing()) - restarted whenever the playing channel
    changes so it re-syncs to the new station's slideshow, and stopped
    entirely while idle so it doesn't hold the device open for nothing.

    Requires two consecutive polls to agree on a "changed" channel before
    acting on it - live testing showed a single spurious/transient read of
    get_dab_now_playing() (TVHeadend subscription reporting blip) was enough
    to trigger a restart mid-capture, throwing away an already-valid,
    fully-downloaded slideshow image for no real reason."""
    global _digitaldata_proc, _digitaldata_channel, _digitaldata_generation
    pending_channel = None
    while True:
        try:
            playing = get_dab_now_playing()["playing"]
            channel = playing[0] if playing else None

            if channel != _digitaldata_channel:
                if pending_channel != channel:
                    pending_channel = channel
                    channel = _digitaldata_channel  # not confirmed yet - keep current state this round
            else:
                pending_channel = channel

            if _digitaldata_proc is not None and channel != _digitaldata_channel:
                _debug_log(f"[loop] stopping channel={_digitaldata_channel!r} (switching to {channel!r})")
                _digitaldata_proc.terminate()
                try:
                    _digitaldata_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _digitaldata_proc.kill()
                _digitaldata_proc = None
                _digitaldata_channel = None
                with _digitaldata_lock:
                    _artwork_buffer.clear()
                    _digitaldata_generation += 1
                for f in glob.glob(f"{ARTWORK_DIR}/*.jpg"):
                    try:
                        os.remove(f)
                    except OSError:
                        pass

            if channel and _digitaldata_proc is None:
                device = _find_active_dab_device()
                if device:
                    # mediaclient buffers its own stdout in big (~4KB+)
                    # blocks when it isn't a TTY (standard glibc stdio
                    # behaviour) - live tested: through a plain pipe, it ran
                    # for its whole lifetime without ever flushing a single
                    # "OPENED:" line to us, and a killed process never gets
                    # to flush its buffer, so the output was lost entirely.
                    # Giving it a pty instead makes it detect an
                    # interactive terminal and switch to line-buffering.
                    master_fd, slave_fd = pty.openpty()
                    proc = subprocess.Popen(
                        [MEDIACLIENT_BIN, "--getdigitaldata", "-d", device],
                        stdout=slave_fd, stderr=slave_fd, stdin=subprocess.DEVNULL,
                    )
                    os.close(slave_fd)
                    _digitaldata_proc = proc
                    _digitaldata_channel = channel
                    with _digitaldata_lock:
                        _digitaldata_generation += 1
                        my_generation = _digitaldata_generation
                    _debug_log(f"[loop] starting channel={channel!r} device={device!r} pid={proc.pid} generation={my_generation}")
                    threading.Thread(target=_digitaldata_reader, args=(master_fd, channel, my_generation), daemon=True).start()
        except Exception as e:
            _debug_log(f"[loop] error: {e!r}")
            print(f"[digitaldata] error: {e}")
        time.sleep(DIGITALDATA_POLL_SECONDS)


def get_dab_artwork_info():
    """Status of the whole per-type artwork buffer. "types" lists every
    currently-held slide type for the active channel (e.g. "cover",
    "wetter", "sendung" - see _artwork_type_from_filename), each with its
    own updated_at/channel and fetchable individually via
    /dab/artwork/<type>. "available"/"updated_at"/"channel" mirror the
    single most-recently-updated type, for backward compatibility with
    /dab/artwork (no type suffix) and existing dashboards built against the
    pre-buffer single-image API."""
    with _digitaldata_lock:
        buffer_snapshot = dict(_artwork_buffer)
    types = [
        {"type": t, "updated_at": info["updated_at"], "channel": info["channel"]}
        for t, info in sorted(buffer_snapshot.items(), key=lambda kv: kv[1]["updated_at"], reverse=True)
    ]
    if types:
        latest = types[0]
        return {
            "available": True,
            "updated_at": latest["updated_at"],
            "channel": latest["channel"],
            "types": types,
        }
    return {"available": False, "updated_at": None, "channel": None, "types": []}


def _latest_scan_prid():
    # cmd=providers returns [[name, serial, 0, [prid, delsys, label], [prid, delsys, label], ...]]
    # scan history isn't append-only - prid indices get renumbered/pruned by
    # mediasrv, so this must be re-resolved on every use rather than cached.
    # Scan history is shared across ALL delivery systems on this device (DAB+,
    # FM, ...), so this MUST filter by delsys - picking the plain max prid
    # picked up the FM bridge's own scans once FM scanning was added,
    # serving FM stations under the DAB+ channel list.
    data = json.loads(_sundtek_post("cmd=providers"))
    if not data:
        return None
    scans = [e for e in data[0] if isinstance(e, list) and len(e) == 3 and e[1] == DAB_DELSYS]
    if not scans:
        return None
    return max(scans, key=lambda e: e[0])[0]


_rescan_lock = threading.Lock()
_rescan_state = {"running": False, "returncode": None}

# Separate from _rescan_state: _rescan_state/"running" covers the whole
# multi-step sequence (Sundtek scan -> save -> TVHeadend restart), which
# doesn't tell a caller which part is actually active right now - and
# TVHeadend keeps scanning muxes on its own well after that whole sequence
# reports done (see rescan_status()'s tvh_scanning). This tracks the
# Sundtek/DAB+ scan step specifically (dab_scanning True only while
# _perform_dab_scan() itself is polling cmd=scanstat), plus live progress
# (current frequency/station count as the scan sweeps) and the last
# completed scan's result - so the HA dashboard can show "DAB+ scans right
# now, on frequency X" separately from "TVHeadend is still working through
# its mux queue".
_dab_scan_progress = {
    "dab_scanning": False,
    "current_freq": None,
    "current_stations": None,
    "last_freq": None,
    "last_stations": None,
    "last_finished_at": None,
}


def _rescan_log(line):
    try:
        with open(RESCAN_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _perform_dab_scan():
    _rescan_log("restarting sundtek-mediasrv + sundtek-rtspd (required before a scan will persist) ...")
    subprocess.run(["systemctl", "restart", "sundtek-mediasrv.service"], check=True)
    time.sleep(3)
    subprocess.run(["systemctl", "restart", "sundtek-rtspd.service"], check=True)
    time.sleep(3)

    _rescan_log("triggering DAB+ scan and waiting for it to finish (up to 4 min) ...")
    _sundtek_post(f"scan=start&deviceid={DAB_DEVICE_ID}&mode={DAB_DELSYS}")

    with _rescan_lock:
        _dab_scan_progress["dab_scanning"] = True
        _dab_scan_progress["current_freq"] = None
        _dab_scan_progress["current_stations"] = None

    last_freq = None
    stable = 0
    station_count = None
    try:
        for _ in range(48):
            time.sleep(5)
            try:
                arr = json.loads(_sundtek_post(f"cmd=scanstat&deviceid={DAB_DEVICE_ID}"))
            except Exception:
                continue
            freq = arr[6] if len(arr) > 6 else None
            station_count = arr[12] if len(arr) > 12 else station_count
            with _rescan_lock:
                _dab_scan_progress["current_freq"] = freq
                _dab_scan_progress["current_stations"] = station_count
            stable = stable + 1 if freq == last_freq else 0
            last_freq = freq
            if stable >= 3:
                break
        _rescan_log(f"scan settled at freq={last_freq}, {station_count} station(s) found")
    finally:
        with _rescan_lock:
            _dab_scan_progress["dab_scanning"] = False
            _dab_scan_progress["current_freq"] = None
            _dab_scan_progress["current_stations"] = None
            _dab_scan_progress["last_freq"] = last_freq
            _dab_scan_progress["last_stations"] = station_count
            _dab_scan_progress["last_finished_at"] = time.time()


def _restart_tvheadend():
    _rescan_log("restarting TVHeadend so it re-scans the DAB+ IPTV network ...")
    subprocess.run(["systemctl", "restart", "service.tvheadend43.service"], check=True)
    time.sleep(8)


def _run_rescan():
    try:
        _set_active_location(None)
        _rescan_log("[1/3] " + "restarting sundtek-mediasrv + sundtek-rtspd (required before a scan will persist) ...")
        _perform_dab_scan()
        _rescan_log("[2/3] scan done")
        _rescan_log("[3/3] " + "restarting TVHeadend so it re-scans the DAB+ IPTV network ...")
        _restart_tvheadend()

        n = len(prepare_stations(fetch_stations()))
        _rescan_log(f"done. station count now: {n}")
        _rescan_state["returncode"] = 0
    except Exception as e:
        _rescan_log(f"ERROR: {e}")
        _rescan_state["returncode"] = 1
    finally:
        _rescan_state["running"] = False


def _run_scan_new_location(name):
    try:
        _rescan_log(f"[1/3] scanning for DAB+ location '{name}' ...")
        _perform_dab_scan()

        stations = fetch_live_stations()
        save_location(name, stations)
        _set_active_location(name)
        _rescan_log(f"[2/3] saved {len(stations)} station(s) as location '{name}'")

        _rescan_log("[3/3] restarting TVHeadend so it re-scans the DAB+ IPTV network ...")
        _restart_tvheadend()

        n = len(prepare_stations(fetch_stations()))
        _rescan_log(f"done. station count now: {n}")
        _rescan_state["returncode"] = 0
    except Exception as e:
        _rescan_log(f"ERROR: {e}")
        _rescan_state["returncode"] = 1
    finally:
        _rescan_state["running"] = False


def _run_activate_location(name):
    try:
        data = load_location(name)  # raises if unknown - surfaces as an error below
        _set_active_location(name)
        _rescan_log(f"[1/2] activated saved location '{name}' ({len(data['stations'])} station(s), no rescan needed)")

        _rescan_log("[2/2] restarting TVHeadend so it re-scans the DAB+ IPTV network ...")
        _restart_tvheadend()

        n = len(prepare_stations(fetch_stations()))
        _rescan_log(f"done. station count now: {n}")
        _rescan_state["returncode"] = 0
    except Exception as e:
        _rescan_log(f"ERROR: {e}")
        _rescan_state["returncode"] = 1
    finally:
        _rescan_state["running"] = False


def _start_background(target, args):
    with _rescan_lock:
        if _rescan_state["running"]:
            return False
        _rescan_state["running"] = True
        _rescan_state["returncode"] = None
        try:
            open(RESCAN_LOG, "w").close()
        except OSError:
            pass
        threading.Thread(target=target, args=args, daemon=True).start()
        return True


def start_rescan():
    return _start_background(_run_rescan, ())


def start_scan_new_location(name):
    return _start_background(_run_scan_new_location, (name,))


def start_activate_location(name):
    return _start_background(_run_activate_location, (name,))


def rescan_status():
    with _rescan_lock:
        running = _rescan_state["running"]
        returncode = _rescan_state["returncode"]
        dab_scan = dict(_dab_scan_progress)

    tail = ""
    try:
        with open(RESCAN_LOG, "r", encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-25:])
    except FileNotFoundError:
        pass

    station_count = None
    try:
        station_count = len(prepare_stations(fetch_stations()))
    except Exception:
        pass

    tvheadend_muxes = None
    # TVHeadend's own mux scan_state: 0=INAKTIV (idle/done), 1=AUSSTEHEND
    # (queued), 2=IDLE PEND, 3=AKTIV (scanning right now). After our own
    # scan-trigger script finishes (returncode set, running=False), TVHeadend
    # itself keeps working through its mux scan queue in the background for a
    # while - each mux is scanned one at a time. Our own "running" flag only
    # covers the scan-runner subprocess we launched, not this follow-up
    # queue, so a caller who only checks "running" can wrongly conclude the
    # tuner is free while TVHeadend is still actively retuning muxes. Live
    # observed (2026-09-21): after a bridge-triggered scan already reported
    # running=False/returncode=0, TVHeadend's own mux grid still showed
    # dozens of DAB+ muxes at scan_state=1 (pending) and one at scan_state=3
    # (actively retuning) - the tuner was still busy for a good while after
    # our own script considered the job done.
    tvh_mux_scan_pending = None
    tvh_mux_scan_active = None
    try:
        with _tvh_req("/api/mpegts/mux/grid?limit=500", timeout=5) as resp:
            grid = json.loads(resp.read())
        dab_muxes = [e for e in grid["entries"] if e.get("network") == TVH_NETWORK_NAME]
        tvheadend_muxes = len(dab_muxes)
        tvh_mux_scan_pending = sum(1 for e in dab_muxes if e.get("scan_state") in (1, 2))
        tvh_mux_scan_active = sum(1 for e in dab_muxes if e.get("scan_state") == 3)
    except Exception:
        pass

    return {
        "running": running,
        "returncode": returncode,
        "station_count": station_count,
        "tvheadend_muxes": tvheadend_muxes,
        "dab_scanning": dab_scan["dab_scanning"],
        "dab_scan_current_freq": dab_scan["current_freq"],
        "dab_scan_current_stations": dab_scan["current_stations"],
        "dab_last_scan_freq": dab_scan["last_freq"],
        "dab_last_scan_stations": dab_scan["last_stations"],
        "dab_last_scan_finished_at": dab_scan["last_finished_at"],
        "tvh_mux_scan_pending": tvh_mux_scan_pending,
        "tvh_mux_scan_active": tvh_mux_scan_active,
        "tvh_scanning": bool((tvh_mux_scan_pending or 0) + (tvh_mux_scan_active or 0)),
        "active_location": _get_active_location(),
        "locations": list_locations(),
        "log_tail": tail,
    }


def get_local_ip():
    # doesn't actually send traffic - just asks the kernel which local
    # interface/IP would be used to reach an external address
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _localize_url(url, local_ip):
    # mediasrv embeds whatever host it thinks it is reachable at, which may
    # not be the interface TVHeadend actually reaches it through
    parts = urllib.parse.urlsplit(url)
    host, _, port = parts.netloc.partition(":")
    new_netloc = f"{local_ip}:{port}" if port else local_ip
    return urllib.parse.urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))


def load_excluded():
    try:
        with open(EXCLUDE_FILE, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip() and not line.startswith("#")}
    except FileNotFoundError:
        return set()


def save_excluded(names):
    os.makedirs(os.path.dirname(EXCLUDE_FILE), exist_ok=True)
    with open(EXCLUDE_FILE, "w", encoding="utf-8") as f:
        f.write("# DAB+ station names to leave out of the channel list entirely.\n")
        f.write("# Managed via /dab/exclude - editing by hand is also fine.\n")
        for name in sorted(names):
            f.write(name + "\n")


def set_dab_excluded(name, excluded):
    names = load_excluded()
    if excluded:
        names.add(name)
    else:
        names.discard(name)
    save_excluded(names)


def load_renames():
    # renames.txt: one "raw name = display name" per line.
    # Use this to canonicalize names that TVHeadend/DAB report slightly
    # differently for what is really the same station (e.g. "NDR 2 NDS" ->
    # "NDR 2"), so duplicate simulcast entries collapse into one station
    # before they ever reach TVHeadend.
    renames = {}
    try:
        with open(RENAME_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                raw, _, display = line.partition("=")
                renames[raw.strip()] = display.strip()
    except FileNotFoundError:
        pass
    return renames


def save_renames(renames):
    os.makedirs(os.path.dirname(RENAME_FILE), exist_ok=True)
    with open(RENAME_FILE, "w", encoding="utf-8") as f:
        f.write("# raw DAB name = display name\n")
        f.write("# Managed via /dab/rename - editing by hand is also fine.\n")
        for raw, display in sorted(renames.items()):
            f.write(f"{raw}={display}\n")


def set_dab_rename(raw_name, display_name):
    renames = load_renames()
    if display_name and display_name != raw_name:
        renames[raw_name] = display_name
    else:
        renames.pop(raw_name, None)
    save_renames(renames)


def list_dab_stations():
    """Raw (unfiltered) current station list with exclude/rename status, for
    building a management UI (e.g. in Home Assistant) on top of."""
    excluded = load_excluded()
    renames = load_renames()
    result = []
    for raw_name, _url in fetch_stations():
        display_name = renames.get(raw_name, raw_name)
        is_excluded = display_name in excluded or raw_name in excluded
        result.append({
            "raw_name": raw_name,
            "display_name": display_name,
            "excluded": is_excluded,
        })
    return result


def esc(value):
    return value.replace("\\", "\\\\").replace(" ", "\\ ")


def fetch_live_stations():
    prid = _latest_scan_prid()
    if prid is None:
        return []
    serial = _device_serial()
    data = json.loads(_sundtek_post(
        f"cmd=chlist&mode=scanlist&devserial={serial}&prid={prid}&filter=radio-sd-hd-pradio-psd-phd"
    ))
    if not data:
        return []
    local_ip = get_local_ip()
    stations = []
    for row in data[0][1:]:
        name = row[0].strip()
        # empty names are the ensemble/multiplex label itself (not a
        # station); "... EPG" entries are DAB program-guide data services,
        # not audio - neither belongs in the channel list
        if not name or "EPG" in name.upper():
            continue
        stations.append((name, _localize_url(row[1], local_ip)))
    return stations


def _location_path(name):
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()
    return os.path.join(DAB_LOCATIONS_DIR, f"{safe}.json")


def _get_active_location():
    try:
        with open(DAB_ACTIVE_LOCATION_FILE, "r", encoding="utf-8") as f:
            name = f.read().strip()
        return name or None
    except FileNotFoundError:
        return None


def _set_active_location(name):
    if name is None:
        try:
            os.remove(DAB_ACTIVE_LOCATION_FILE)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(DAB_ACTIVE_LOCATION_FILE), exist_ok=True)
    with open(DAB_ACTIVE_LOCATION_FILE, "w", encoding="utf-8") as f:
        f.write(name)


def save_location(name, stations):
    os.makedirs(DAB_LOCATIONS_DIR, exist_ok=True)
    with open(_location_path(name), "w", encoding="utf-8") as f:
        json.dump({"name": name, "saved_at": time.time(), "stations": stations}, f)


def load_location(name):
    with open(_location_path(name), "r", encoding="utf-8") as f:
        return json.load(f)


def delete_location(name):
    """Removes a saved location's file. If it was the active one, falls
    back to "live" (same as the GPS-triggered rescan does) rather than
    leaving the active-location pointer dangling."""
    path = _location_path(name)
    if not os.path.exists(path):
        return False
    os.remove(path)
    if _get_active_location() == name:
        _set_active_location(None)
    return True


def list_locations():
    if not os.path.isdir(DAB_LOCATIONS_DIR):
        return []
    result = []
    for fname in sorted(os.listdir(DAB_LOCATIONS_DIR)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(DAB_LOCATIONS_DIR, fname), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        result.append({
            "name": data.get("name"),
            "station_count": len(data.get("stations", [])),
            "saved_at": data.get("saved_at"),
        })
    return result


def fetch_stations():
    active = _get_active_location()
    if active:
        try:
            data = load_location(active)
            return [tuple(s) for s in data["stations"]]
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass  # saved file is gone/corrupt - fall back to a live scan
    return fetch_live_stations()


def prepare_stations(stations):
    excluded = load_excluded()
    renames = load_renames()
    seen = set()
    result = []
    for raw_name, url in stations:
        name = renames.get(raw_name, raw_name)
        if name in excluded or raw_name in excluded:
            continue
        if name in seen:
            # same station simulcast on another frequency (SFN) - keep only
            # the first one so it doesn't create a duplicate TVHeadend channel
            continue
        seen.add(name)
        result.append((name, url))
    return result


def build_m3u(stations):
    lines = ["#EXTM3U"]
    for name, url in prepare_stations(stations):
        cmd = (
            f"pipe://{FFMPEG_BIN} -loglevel fatal -i {url} "
            f"-metadata {esc('service_name=' + name)} "
            f"-metadata {esc('service_provider=' + SERVICE_PROVIDER)} "
            f"-mpegts_service_type advanced_codec_digital_radio "
            f"-vn -c:a mp2 -b:a {AUDIO_BITRATE} -f mpegts pipe:1"
        )
        lines.append(f"#EXTINF:-1,{name}")
        lines.append(cmd)
    return "\n".join(lines) + "\n"


def build_plain_m3u(stations):
    lines = ["#EXTM3U"]
    for name, url in prepare_stations(stations):
        lines.append(f'#EXTINF:-1 radio="true",{name}')
        lines.append("#EXTVLCOPT:network-caching=1000")
        lines.append(url)
    return "\n".join(lines) + "\n"


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
        if path in ("/dab/status", "/rescan/status"):
            _send_json(self, 200, rescan_status())
            return
        if path == "/dab/locations":
            _send_json(self, 200, list_locations())
            return
        if path == "/dab/stations":
            try:
                _send_json(self, 200, {"stations": list_dab_stations()})
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/dab/signal":
            try:
                _send_json(self, 200, get_dab_signal_info())
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/dab/now_playing":
            try:
                _send_json(self, 200, get_dab_now_playing())
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/dab/artwork_info":
            _send_json(self, 200, get_dab_artwork_info())
            return
        if path == "/dab/digitaldata_debug":
            try:
                with open(DIGITALDATA_DEBUG_LOG, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-200:]
            except FileNotFoundError:
                tail = []
            _send_json(self, 200, {"log_tail": "".join(tail)})
            return
        if path == "/dab/artwork" or path.startswith("/dab/artwork/"):
            if path == "/dab/artwork":
                # No type given - serve whichever one was captured most
                # recently, same as the pre-buffer single-image behaviour.
                with _digitaldata_lock:
                    buffer_snapshot = dict(_artwork_buffer)
                if not buffer_snapshot:
                    self.send_response(404)
                    self.end_headers()
                    return
                artwork_type = max(buffer_snapshot, key=lambda t: buffer_snapshot[t]["updated_at"])
            else:
                artwork_type = path[len("/dab/artwork/"):].strip().lower()
                if not re.fullmatch(r"[a-z0-9]+", artwork_type):
                    self.send_response(400)
                    self.end_headers()
                    return
            artwork_path = _artwork_path_for_type(artwork_type)
            if not os.path.exists(artwork_path):
                self.send_response(404)
                self.end_headers()
                return
            with open(artwork_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if path not in ("/DAB_mp2.m3u", "/DAB_plain.m3u", "/"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            stations = fetch_stations()
            if path == "/DAB_plain.m3u":
                body = build_plain_m3u(stations).encode("utf-8")
            else:
                body = build_m3u(stations).encode("utf-8")
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/x-mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?")[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        if path in ("/dab/rescan", "/rescan"):
            started = start_rescan()
            _send_json(self, 202 if started else 409, {"started": started})
            return

        if path == "/dab/scan_new_location":
            name = (query.get("name") or [None])[0]
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            started = start_scan_new_location(name)
            _send_json(self, 202 if started else 409, {"started": started, "name": name})
            return

        if path == "/dab/activate":
            name = (query.get("name") or [None])[0]
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            if not os.path.exists(_location_path(name)):
                _send_json(self, 404, {"error": f"no saved location named {name!r}"})
                return
            started = start_activate_location(name)
            _send_json(self, 202 if started else 409, {"started": started, "name": name})
            return

        if path == "/dab/delete_location":
            name = (query.get("name") or [None])[0]
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            deleted = delete_location(name)
            _send_json(self, 200 if deleted else 404, {"deleted": deleted, "name": name})
            return

        if path == "/dab/exclude":
            name = (query.get("name") or [None])[0]
            excluded = (query.get("excluded") or ["true"])[0].lower() not in ("false", "0", "no")
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            set_dab_excluded(name, excluded)
            _send_json(self, 200, {"name": name, "excluded": excluded})
            return

        if path == "/dab/rename":
            raw_name = (query.get("raw_name") or [None])[0]
            display_name = (query.get("display_name") or [""])[0]
            if not raw_name:
                _send_json(self, 400, {"error": "missing 'raw_name' query parameter"})
                return
            set_dab_rename(raw_name, display_name)
            _send_json(self, 200, {"raw_name": raw_name, "display_name": display_name or raw_name})
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=mapper_loop, daemon=True).start()
    threading.Thread(target=digital_data_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
