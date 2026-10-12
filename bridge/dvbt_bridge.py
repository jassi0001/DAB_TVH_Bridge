#!/usr/bin/env python3
import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Central install path - change this if the bridge ever moves to a
# different location on the box; every path below is derived from it
# rather than hardcoded separately. Shared in spirit with BRIDGE_BASE in
# dab_bridge.py (both bridges live under the same directory), kept as its
# own constant here since the two bridges are otherwise independent
# processes/modules.
BRIDGE_BASE = "/storage/dab-tvh-bridge"

LISTEN_PORT = 8082
DVBT_LOG = f"{BRIDGE_BASE}/dvbt.log"

# Channel names to leave disabled/unmapped (substring match, case-insensitive
# - one entry per line, '#' comments allowed). Seed with what's not wanted.
DVBT_EXCLUDE_FILE = f"{BRIDGE_BASE}/dvbt_exclude.txt"
# "raw channel name = display name", one per line, for services whose raw
# SDT name isn't the desired display name (e.g. regional variants like
# "NDR FS NDS" -> "NDR 1 Niedersachsen"). Match key is the RAW, untidied
# service name (tidy_channel_name is deliberately off, see
# DVBT_MAPPER_SETTINGS) - re-check existing entries after upgrading, since
# they may have been written against the old, tidied names.
DVBT_RENAME_FILE = f"{BRIDGE_BASE}/dvbt_renames.txt"

# TVHeadend channel tags this box already has set up; matched by name so a
# manual retag/recreate in the UI doesn't break this.
RADIO_CHANNELS_TAG_NAME = "Radio channels"
TV_CHANNELS_TAG_NAME = "TV channels"
# DVB service_type values that mean "this is radio, not TV" (2 = digital
# radio, 10 = advanced-codec digital radio, used e.g. by the DAB+ bridge).
DVB_RADIO_SERVICE_TYPES = {2, 10}

TVH_HOST = "127.0.0.1"
TVH_PORT = 9981
def _load_credentials(path=f"{BRIDGE_BASE}/credentials.env"):
    """TVHeadend login from credentials.env (KEY=VALUE per line, '#' = comment)
    next to the bridge; TVH_USER/TVH_PASS environment variables override it."""
    creds = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    creds[key.strip()] = value.strip().strip("'\"")
    except OSError:
        print(f"WARNING: {path} not readable - using placeholder TVHeadend login")
    return (os.environ.get("TVH_USER", creds.get("TVH_USER", "claude")),
            os.environ.get("TVH_PASS", creds.get("TVH_PASS", "claude")))


TVH_USER, TVH_PASS = _load_credentials()
# One TVHeadend network per location, named "DVB-T <Ort>" by convention
# (e.g. "DVB-T Example") - either created by hand today (Configuration ->
# DVB inputs -> Networks -> wizard) or, via /dvbt/scan_new_location below,
# generated and scanned automatically. Switching locations = enabling that
# network, disabling every other DVB-T network, and pointing the (single,
# physical) tuner at it - the same two steps done by hand today.
DVBT_NAME_PREFIX = "DVB-T"
DVBT_FRONTEND_UUID = "1fd4df93f92230cf3dc098cea86b4cef"  # TurboSight TBS 5520SE DVB-T/T2/C/C2/ISDB-T #0 : DVB-T #0
DVBT_MAPPER_SETTINGS = {
    "check_availability": False,
    "encrypted": False,
    "merge_same_name": True,
    "merge_same_name_fuzzy": True,
    # Deliberately OFF: TVHeadend's service_mapper.c calls channel_set_name()
    # for every mapped service whenever this is on - and that setter
    # unconditionally overwrites the channel's stored name, even for an
    # EXISTING channel it's merging onto and even if that channel has
    # autoname=False (a custom name locked via apply_dvbt_renames() or set
    # by hand). With it on, any rename we or the user apply gets silently
    # reverted back to the freshly-tidied service name on the very next
    # mapper pass. New channels still get a sensible name from TVHeadend's
    # own autoname fallback (the raw, untidied service name) - our own
    # apply_dvbt_renames() step right after mapping is what turns that into
    # a nice display name, and it's the only thing allowed to touch it.
    "tidy_channel_name": False,
    "type_tags": False,
    "provider_tags": False,
    "network_tags": False,
}

# German DVB-T2 HD uses channels 21-59 (474-778 MHz, 8 MHz steps, 39
# candidates total) nationwide - see TVHeadend's own bundled
# dvb-scan/dvb-t/de-All predefined mux file, which this mirrors. Tuning
# every candidate and keeping only the ones that actually lock is how the
# equivalent "predefined mux list" scan wizard in the TVHeadend UI works;
# no per-region guessing needed since it's the same handful of national
# frequencies almost everywhere in Germany.
DVBT_SCAN_FREQ_START = 474_000_000
DVBT_SCAN_FREQ_END = 778_000_000
DVBT_SCAN_FREQ_STEP = 8_000_000
DVBT_MUX_CONF_TEMPLATE = {
    "delsys": "DVB-T2",
    "bandwidth": "8MHz",
    "constellation": "QAM/AUTO",
    "transmission_mode": "AUTO",
    "guard_interval": "AUTO",
    "hierarchy": "AUTO",
    "fec_hi": "AUTO",
    "fec_lo": "AUTO",
}
DVBT_SCAN_RESULT_ERROR = 2  # TVHeadend's "scan_result" enum: no lock / failed


def _tvh_req(path, data=None, timeout=15):
    auth = base64.b64encode(f"{TVH_USER}:{TVH_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}"}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        r = urllib.request.Request(f"http://{TVH_HOST}:{TVH_PORT}{path}", data=body, headers=headers, method="POST")
    else:
        r = urllib.request.Request(f"http://{TVH_HOST}:{TVH_PORT}{path}", headers=headers)
    return urllib.request.urlopen(r, timeout=timeout)


def _dvbt_networks():
    with _tvh_req("/api/mpegts/network/grid?limit=100") as resp:
        grid = json.loads(resp.read())
    return [e for e in grid["entries"] if (e.get("networkname") or "").upper().startswith(DVBT_NAME_PREFIX.upper())]


def get_dvbt_signal_info():
    """Live tuner signal quality for the physical DVB-T frontend, straight
    from TVHeadend's own input status API (/api/status/inputs) - unlike the
    DAB+ bridge, there's no separate low-level SDK for this tuner (it's a
    standard TBS DVB-T/T2/C/C2 USB card using TVHeadend's own linuxdvb
    driver), so TVHeadend's own reporting is the only source.

    Important, unlike DAB+'s SDK-based /dab/signal: this only reports
    anything meaningful while a client is actively subscribed/streaming
    (subs > 0) - idle reads back all-zero fields, which does NOT mean "no
    signal", just "not currently tuned to anything". signal_scale/snr_scale
    follow TVHeadend's own enum: 0=unknown (no value), 1=relative (0..65535,
    treat as roughly a percentage), 2=decibel (raw value in thousandths of
    a dB, i.e. divide by 1000 for the real dB figure)."""
    with _tvh_req("/api/status/inputs", timeout=10) as resp:
        data = json.loads(resp.read())
    entry = next((e for e in data.get("entries", []) if e.get("uuid") == DVBT_FRONTEND_UUID), None)
    if not entry:
        return {
            "active": False, "stream": None,
            "signal": None, "signal_scale": None, "signal_db": None,
            "snr": None, "snr_scale": None, "snr_db": None,
            "ber": None, "unc": None,
        }
    active = (entry.get("subs") or 0) > 0
    signal_scale = entry.get("signal_scale")
    snr_scale = entry.get("snr_scale")
    return {
        "active": active,
        "stream": entry.get("stream") if active else None,
        "signal": entry.get("signal") if active else None,
        "signal_scale": signal_scale if active else None,
        "signal_db": (entry.get("signal") / 1000) if active and signal_scale == 2 else None,
        "snr": entry.get("snr") if active else None,
        "snr_scale": snr_scale if active else None,
        "snr_db": (entry.get("snr") / 1000) if active and snr_scale == 2 else None,
        "ber": entry.get("ber") if active else None,
        "unc": entry.get("unc") if active else None,
    }


def list_dvbt_networks():
    return [
        {
            "uuid": e["uuid"],
            "name": e.get("networkname"),
            "enabled": e.get("enabled"),
            "num_mux": e.get("num_mux"),
            "num_svc": e.get("num_svc"),
            "num_chn": e.get("num_chn"),
        }
        for e in _dvbt_networks()
    ]


def _scan_frequencies():
    freqs = []
    f = DVBT_SCAN_FREQ_START
    while f <= DVBT_SCAN_FREQ_END:
        freqs.append(f)
        f += DVBT_SCAN_FREQ_STEP
    return freqs


DVBT_SCAN_STATE_PENDING = 1
DVBT_SCAN_STATE_ACTIVE = 3


def _force_mux_scan(mux_uuids, network_name, max_attempts=12, wait_seconds=10):
    """Freshly created muxes sit in scan_state=0 (INAKTIV); TVHeadend's own
    "new mux -> auto PEND" queueing doesn't fire for muxes added at runtime
    via this API, so scan_state has to be forced to ACTIVE by hand.

    Must be called AFTER the owning network has been enabled (see
    activate_dvbt_network()) - a disabled network's muxes are invisible to
    (or at least unreliably returned by) /api/mpegts/mux/grid, so calling
    this while the network is still disabled reliably does nothing at all,
    with no error. The retry loop here is a safety net for ordinary
    settling time, not a substitute for getting that ordering right."""
    for attempt in range(max_attempts):
        time.sleep(wait_seconds)
        node = json.dumps([{"uuid": u, "scan_state": DVBT_SCAN_STATE_ACTIVE} for u in mux_uuids])
        with _tvh_req("/api/idnode/save", data={"node": node}):
            pass
        with _tvh_req("/api/mpegts/mux/grid?start=0&limit=500") as resp:
            grid = json.loads(resp.read())
        states = [e.get("scan_state") for e in grid["entries"] if e.get("network") == network_name]
        queued = sum(1 for s in states if s in (DVBT_SCAN_STATE_PENDING, DVBT_SCAN_STATE_ACTIVE))
        if not states:
            _dvbt_log(f"  scan_state force attempt {attempt + 1}: network {network_name!r} not visible in mux grid yet")
        else:
            _dvbt_log(f"  scan_state force attempt {attempt + 1}: {queued} of {len(states)} mux(es) queued")
        if queued > 0:
            return True
    return False


def create_dvbt_network(location_name):
    """Creates a brand-new (disabled) network with every candidate German
    DVB-T2 mux pre-populated. Deliberately does NOT try to queue the muxes
    for scanning here - a disabled network's muxes are invisible to (or at
    least unreliably returned by) /api/mpegts/mux/grid, so forcing
    scan_state at this point is a guaranteed no-op. That has to happen
    after activate_dvbt_network() enables the network - see
    _run_dvbt_new_location()."""
    full_name = f"{DVBT_NAME_PREFIX} {location_name}"
    conf = {"enabled": False, "networkname": full_name, "skipinitscan": True}
    with _tvh_req("/api/mpegts/network/create", data={"class": "dvb_network_dvbt", "conf": json.dumps(conf)}) as resp:
        net_uuid = json.loads(resp.read())["uuid"]

    mux_uuids = []
    for freq in _scan_frequencies():
        mux_conf = dict(DVBT_MUX_CONF_TEMPLATE, frequency=freq)
        with _tvh_req("/api/mpegts/network/mux_create", data={"uuid": net_uuid, "conf": json.dumps(mux_conf)}) as resp:
            mux_uuids.append(json.loads(resp.read())["uuid"])

    return net_uuid, full_name, mux_uuids


def prune_failed_dvbt_muxes(network_name):
    """Removes candidate muxes that didn't lock onto anything - there's no
    broadcast on that frequency at this location - so the network ends up
    holding only the muxes actually usable here, same as a manually curated
    one."""
    with _tvh_req("/api/mpegts/mux/grid?start=0&limit=500") as resp:
        grid = json.loads(resp.read())
    failed = [e["uuid"] for e in grid["entries"] if e.get("network") == network_name and e.get("scan_result") == DVBT_SCAN_RESULT_ERROR]
    if not failed:
        return 0
    with _tvh_req("/api/idnode/delete", data={"uuid": json.dumps(failed)}):
        pass
    return len(failed)


def activate_dvbt_network(name_or_uuid):
    """Enables the named DVB-T network, disables every other DVB-T network,
    and points the tuner exclusively at it - the same two steps done by hand
    today (Configuration -> DVB inputs -> Networks -> enable/disable;
    Configuration -> DVB inputs -> TV adapters -> Networks)."""
    nets = _dvbt_networks()
    target = next(
        (e for e in nets if e["uuid"] == name_or_uuid or (e.get("networkname") or "").lower() == name_or_uuid.lower()),
        None,
    )
    if target is None:
        available = [e.get("networkname") for e in nets]
        raise ValueError(f"unknown DVB-T network {name_or_uuid!r}, available: {available}")

    node = json.dumps(
        [{"uuid": target["uuid"], "enabled": True}]
        + [{"uuid": e["uuid"], "enabled": False} for e in nets if e["uuid"] != target["uuid"]]
    )
    with _tvh_req("/api/idnode/save", data={"node": node}):
        pass

    with _tvh_req("/api/idnode/save", data={"node": json.dumps({"uuid": DVBT_FRONTEND_UUID, "networks": [target["uuid"]]})}):
        pass

    return target


def delete_dvbt_network(name_or_uuid):
    """Deletes a "DVB-T ..."-prefixed network outright (same effect as the
    trash icon in Configuration -> DVB inputs -> Networks) - only ever looks
    among our own networks, never an arbitrary TVHeadend network name/uuid.
    TVHeadend cascades the delete to the network's muxes and their services,
    but NOT to the channels those services were mapped to - a channel is a
    separate, shareable object (could in theory still have services from
    another network), so TVHeadend leaves it behind even with zero services
    left. Confirmed live: deleting a network this way left 45 orphaned
    channels (many showing as "{name-not-set}") sitting in the channel list.
    So: snapshot which channels this network's services feed BEFORE
    deleting, then afterwards remove any of them left with no service from
    any network - same orphan-cleanup logic disable_excluded_dvbt_services()
    already uses for the exclude-a-single-service case."""
    nets = _dvbt_networks()
    target = next(
        (e for e in nets if e["uuid"] == name_or_uuid or (e.get("networkname") or "").lower() == name_or_uuid.lower()),
        None,
    )
    if target is None:
        available = [e.get("networkname") for e in nets]
        raise ValueError(f"unknown DVB-T network {name_or_uuid!r}, available: {available}")

    with _tvh_req("/api/mpegts/service/grid?start=0&limit=1000") as resp:
        grid_before = json.loads(resp.read())
    channel_uuids_before = sorted({
        c for e in grid_before["entries"] if e.get("network") == target["networkname"] for c in e.get("channel", [])
    })

    with _tvh_req("/api/idnode/delete", data={"uuid": json.dumps([target["uuid"]])}):
        pass

    orphaned = []
    if channel_uuids_before:
        with _tvh_req("/api/mpegts/service/grid?start=0&limit=1000") as resp:
            grid_after = json.loads(resp.read())
        still_used = {c for e in grid_after["entries"] for c in e.get("channel", [])}
        orphaned = [u for u in channel_uuids_before if u not in still_used]
        if orphaned:
            with _tvh_req("/api/idnode/delete", data={"uuid": json.dumps(orphaned)}):
                pass

    if _dvbt_state.get("active_network") == target.get("networkname"):
        _dvbt_state["active_network"] = None

    return target, len(orphaned)


def _load_dvbt_excluded():
    try:
        with open(DVBT_EXCLUDE_FILE, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except FileNotFoundError:
        return []


def _save_dvbt_excluded(terms):
    os.makedirs(os.path.dirname(DVBT_EXCLUDE_FILE), exist_ok=True)
    with open(DVBT_EXCLUDE_FILE, "w", encoding="utf-8") as f:
        f.write("# DVB-T service names (substring match, case-insensitive) to disable.\n")
        f.write("# Managed via /dvbt/exclude - editing by hand is also fine.\n")
        for term in sorted(terms):
            f.write(term + "\n")


def set_dvbt_excluded(name, excluded):
    """Adds/removes an exact-name exclude term. Takes effect on the next
    mapper pass (network activation or new-location scan), same as
    dvbt_exclude.txt edited by hand."""
    terms = set(_load_dvbt_excluded())
    if excluded:
        terms.add(name)
    else:
        terms.discard(name)
    _save_dvbt_excluded(terms)


def _load_dvbt_renames():
    # same "raw = display" format as the DAB+ bridge's renames.txt, matched
    # against the channel name the mapper already produced (post-tidy).
    renames = {}
    try:
        with open(DVBT_RENAME_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                raw, _, display = line.partition("=")
                renames[raw.strip().lower()] = display.strip()
    except FileNotFoundError:
        pass
    return renames


def _save_dvbt_renames(renames):
    os.makedirs(os.path.dirname(DVBT_RENAME_FILE), exist_ok=True)
    with open(DVBT_RENAME_FILE, "w", encoding="utf-8") as f:
        f.write("# raw channel name = display name (case-insensitive match)\n")
        f.write("# Managed via /dvbt/rename - editing by hand is also fine.\n")
        for raw, display in sorted(renames.items()):
            f.write(f"{raw}={display}\n")


def set_dvbt_rename(raw_name, display_name):
    renames = _load_dvbt_renames()
    key = raw_name.strip().lower()
    if display_name and display_name != raw_name:
        renames[key] = display_name
    else:
        renames.pop(key, None)
    _save_dvbt_renames(renames)


def _is_dvbt_excluded(name, excluded_terms):
    upper = (name or "").upper()
    if "(INTERNET)" in upper:
        return True
    return any(term.upper() in upper for term in excluded_terms)


def list_dvbt_services(network_name=None):
    """Current live services on the given (or currently active) DVB-T
    network, with exclude/rename status, for building a management UI (e.g.
    in Home Assistant) on top of. Reflects TVHeadend's live state, not a
    separate scan snapshot like the DAB+ equivalent."""
    if network_name is None:
        active = next((e for e in _dvbt_networks() if e.get("enabled")), None)
        if active is None:
            return []
        network_name = active["networkname"]

    excluded_terms = _load_dvbt_excluded()
    renames = _load_dvbt_renames()
    with _tvh_req("/api/mpegts/service/grid?start=0&limit=1000") as resp:
        grid = json.loads(resp.read())
    result = []
    for e in grid["entries"]:
        if e.get("network") != network_name or not e.get("enabled"):
            continue
        raw_name = e.get("svcname") or ""
        display_name = renames.get(raw_name.strip().lower(), raw_name)
        result.append({
            "raw_name": raw_name,
            "display_name": display_name,
            "excluded": _is_dvbt_excluded(raw_name, excluded_terms),
        })
    return result


def list_dvbt_channels(network_name=None):
    """Channels currently backed by at least one service from the given (or
    currently active) DVB-T network - the scope used by the channel-editing
    functions below, so they can never accidentally touch a DAB+/CONFIG
    channel that happens to share a name. A channel with zero services
    right now (excluded, or simply not received at the moment) still shows
    up here as long as it was ever fed by this network, since we
    deliberately never delete those (see disable_excluded_dvbt_services)."""
    if network_name is None:
        active = next((e for e in _dvbt_networks() if e.get("enabled")), None)
        if active is None:
            return []
        network_name = active["networkname"]

    with _tvh_req("/api/mpegts/service/grid?start=0&limit=1000") as resp:
        grid = json.loads(resp.read())
    channel_uuids = sorted({c for e in grid["entries"] if e.get("network") == network_name for c in e.get("channel", [])})
    if not channel_uuids:
        return []

    # "all=1" is required - TVHeadend's /api/channel/grid silently omits
    # disabled channels otherwise, which would make a disabled channel
    # permanently unfindable (and so un-re-enable-able) through this API.
    with _tvh_req("/api/channel/grid?start=0&limit=2000&all=1") as resp:
        chgrid = json.loads(resp.read())
    by_uuid = {c["uuid"]: c for c in chgrid["entries"]}
    return [
        {
            "uuid": u,
            "name": by_uuid[u].get("name"),
            "number": by_uuid[u].get("number"),
            "enabled": by_uuid[u].get("enabled"),
        }
        for u in channel_uuids if u in by_uuid
    ]


def _find_dvbt_channel_uuid(name, network_name=None):
    needle = name.strip().lower()
    for ch in list_dvbt_channels(network_name):
        if (ch.get("name") or "").strip().lower() == needle:
            return ch["uuid"]
    return None


def set_dvbt_channel_number(name, number):
    uuid = _find_dvbt_channel_uuid(name)
    if uuid is None:
        raise ValueError(f"unknown DVB-T channel {name!r}")
    with _tvh_req("/api/idnode/save", data={"node": json.dumps({"uuid": uuid, "number": number})}):
        pass
    return uuid


def set_dvbt_channel_name(name, new_name):
    """Direct, immediate rename of the TVHeadend channel object itself
    (autoname locked off) - distinct from set_dvbt_rename(), which only
    writes a rule applied on the next scan/activate pass."""
    uuid = _find_dvbt_channel_uuid(name)
    if uuid is None:
        raise ValueError(f"unknown DVB-T channel {name!r}")
    with _tvh_req("/api/idnode/save", data={"node": json.dumps({"uuid": uuid, "name": new_name, "autoname": False})}):
        pass
    return uuid


def set_dvbt_channel_enabled(name, enabled):
    """Direct per-channel enable/disable - independent of mux/network
    enable state and of the per-service exclude mechanism."""
    uuid = _find_dvbt_channel_uuid(name)
    if uuid is None:
        raise ValueError(f"unknown DVB-T channel {name!r}")
    with _tvh_req("/api/idnode/save", data={"node": json.dumps({"uuid": uuid, "enabled": enabled})}):
        pass
    return uuid


def _get_tag_uuid(name):
    with _tvh_req("/api/channeltag/grid?start=0&limit=200") as resp:
        grid = json.loads(resp.read())
    match = next((t for t in grid["entries"] if t.get("name") == name), None)
    return match["uuid"] if match else None


def disable_excluded_dvbt_services(network_name):
    """Disables any service matching dvbt_exclude.txt or the hardcoded
    "(Internet)" simulcast-feed pattern - checked every pass, so it also
    catches services that were mapped before this filter existed.

    Deliberately does NOT delete the channel this leaves with zero enabled
    services: a channel that currently has no signal (excluded, or just not
    receivable at this location/moment) should stay put with its number,
    name and enabled/disabled state exactly as configured, so that if its
    service is found again later (revisited location, broadcaster back on
    air, exclude lifted again) it gets re-mapped onto the SAME channel
    object instead of spawning a duplicate. Only an explicit, whole-network
    delete_dvbt_network() call removes channels - see
    TVHEADEND_QUIRKS.md Punkt 10."""
    excluded_terms = _load_dvbt_excluded()
    with _tvh_req("/api/mpegts/service/grid?start=0&limit=1000") as resp:
        grid = json.loads(resp.read())
    net_services = [e for e in grid["entries"] if e.get("network") == network_name]
    matches = [e for e in net_services if e.get("enabled") and _is_dvbt_excluded(e.get("svcname"), excluded_terms)]
    if not matches:
        return 0

    node = json.dumps([{"uuid": e["uuid"], "enabled": False} for e in matches])
    with _tvh_req("/api/idnode/save", data={"node": node}):
        pass

    return len(matches)


def apply_dvbt_renames(network_name):
    renames = _load_dvbt_renames()
    if not renames:
        return 0
    with _tvh_req("/api/mpegts/service/grid?start=0&limit=1000") as resp:
        grid = json.loads(resp.read())
    channel_uuids = sorted({c for e in grid["entries"] if e.get("network") == network_name for c in e.get("channel", [])})

    updates = []
    for ch_uuid in channel_uuids:
        with _tvh_req(f"/api/idnode/load?uuid={ch_uuid}") as resp:
            node = json.loads(resp.read())
        params = node["entries"][0]["params"]
        name = next((p["value"] for p in params if p["id"] == "name"), None)
        if not name:
            continue
        target = renames.get(name.strip().lower())
        if target and target != name:
            # autoname=False "locks" the custom name - otherwise the next
            # tidy/mapper pass just overwrites it back to the raw name.
            updates.append({"uuid": ch_uuid, "name": target, "autoname": False})
    if updates:
        with _tvh_req("/api/idnode/save", data={"node": json.dumps(updates)}):
            pass
    return len(updates)


def tag_dvbt_channels(network_name):
    """Applies "Radio channels"/"TV channels" per-channel based on the
    underlying service's real dvb_servicetype (authoritative broadcast
    metadata) - mirrors the DAB+ bridge's own radio tagging."""
    radio_tag = _get_tag_uuid(RADIO_CHANNELS_TAG_NAME)
    tv_tag = _get_tag_uuid(TV_CHANNELS_TAG_NAME)
    if not radio_tag and not tv_tag:
        return 0

    with _tvh_req("/api/mpegts/service/grid?start=0&limit=1000") as resp:
        grid = json.loads(resp.read())
    svc_by_channel = {}
    for e in grid["entries"]:
        if e.get("network") != network_name:
            continue
        for c in e.get("channel", []):
            # prefer an enabled service for the tag decision - a disabled/
            # excluded simulcast happening to enumerate first must not
            # decide the channel's Radio/TV tag over its real service
            current = svc_by_channel.get(c)
            if current is None or (not current.get("enabled") and e.get("enabled")):
                svc_by_channel[c] = e

    # all=1: a disabled channel should still get the correct tag applied,
    # so it's already right whenever it's re-enabled later (see
    # list_dvbt_channels() for why the plain grid call isn't enough).
    with _tvh_req("/api/channel/grid?start=0&limit=2000&all=1") as resp:
        chgrid = json.loads(resp.read())
    by_uuid = {c["uuid"]: c for c in chgrid["entries"]}

    updates = []
    for ch_uuid, svc in svc_by_channel.items():
        ch = by_uuid.get(ch_uuid)
        if not ch:
            continue
        is_radio = svc.get("dvb_servicetype") in DVB_RADIO_SERVICE_TYPES
        wanted_tag = radio_tag if is_radio else tv_tag
        if not wanted_tag:
            continue
        current = set(ch.get("tags") or [])
        if wanted_tag not in current:
            updates.append({"uuid": ch_uuid, "tags": sorted(current | {wanted_tag})})
    if updates:
        with _tvh_req("/api/idnode/save", data={"node": json.dumps(updates)}):
            pass
    return len(updates)


def _wait_for_mapper_settle(service_uuids, max_attempts=6, wait_seconds=2):
    """The mapper call (/api/service/mapper/save) returns before the new
    service->channel links are reliably visible via the API - same class of
    propagation delay as the mux scan_state issue (see
    TVHEADEND_QUIRKS.md Punkt 5). Poll briefly so the rename/tag steps right
    after this don't read stale data and silently miss freshly-mapped
    channels."""
    for _ in range(max_attempts):
        with _tvh_req("/api/mpegts/service/grid?start=0&limit=1000") as resp:
            grid = json.loads(resp.read())
        mapped = {e["uuid"] for e in grid["entries"] if e.get("channel")}
        if service_uuids <= mapped:
            return True
        time.sleep(wait_seconds)
    return False


def map_unmapped_dvbt_services(network_name):
    """Same idea as the DAB+ bridge's service mapper, scoped to one DVB-T
    network. Unlike DAB+, DVB-T services carry real broadcaster EPG/EIT
    data, so epgauto is intentionally left at its default (on) here."""
    disabled = disable_excluded_dvbt_services(network_name)
    if disabled:
        _dvbt_log(f"  disabled {disabled} excluded/Internet-simulcast service(s)")

    with _tvh_req("/api/mpegts/service/grid?start=0&limit=1000") as resp:
        grid = json.loads(resp.read())
    unmapped = [
        e["uuid"] for e in grid["entries"]
        if e.get("network") == network_name and e.get("enabled") and not e.get("channel")
    ]
    if unmapped:
        node = {"services": unmapped, **DVBT_MAPPER_SETTINGS}
        with _tvh_req("/api/service/mapper/save", data={"node": json.dumps(node)}):
            pass
        if not _wait_for_mapper_settle(set(unmapped)):
            _dvbt_log("  WARNING: not all newly mapped services showed a channel link within the wait window - rename/tag below may miss some")

    renamed = apply_dvbt_renames(network_name)
    if renamed:
        _dvbt_log(f"  renamed {renamed} channel(s) via dvbt_renames.txt")

    tagged = tag_dvbt_channels(network_name)
    if tagged:
        _dvbt_log(f"  tagged {tagged} channel(s) as Radio/TV channels")

    return len(unmapped)


def _wait_for_scan_queue(network_name, max_wait_iterations):
    # scanq_length on the network itself isn't reliable right after muxes
    # are created at runtime (see create_dvbt_network) - poll each mux's own
    # scan_state instead and wait until none are still pending/active.
    for _ in range(max_wait_iterations):
        time.sleep(5)
        with _tvh_req("/api/mpegts/mux/grid?start=0&limit=500") as resp:
            grid = json.loads(resp.read())
        states = [e.get("scan_state") for e in grid["entries"] if e.get("network") == network_name]
        pending = sum(1 for s in states if s in (DVBT_SCAN_STATE_PENDING, DVBT_SCAN_STATE_ACTIVE))
        _dvbt_log(f"  mux(es) still pending/active: {pending} of {len(states)}")
        if pending == 0:
            return True
    return False


_dvbt_lock = threading.Lock()
_dvbt_state = {"running": False, "returncode": None, "active_network": None}


def _dvbt_log(line):
    try:
        with open(DVBT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _run_dvbt_activation(name_or_uuid):
    try:
        _dvbt_log(f"[1/3] activating DVB-T network {name_or_uuid!r} ...")
        target = activate_dvbt_network(name_or_uuid)
        _dvbt_state["active_network"] = target["networkname"]
        _dvbt_log(f"activated {target['networkname']!r} ({target['num_mux']} known mux(es)), tuner reassigned")

        _dvbt_log("[2/3] waiting for TVHeadend's mux scan queue to settle (up to 5 min) ...")
        _wait_for_scan_queue(target["networkname"], max_wait_iterations=60)

        _dvbt_log("[3/3] mapping newly discovered services to channels ...")
        n = map_unmapped_dvbt_services(target["networkname"])
        _dvbt_log(f"done. mapped {n} new service(s).")
        _dvbt_state["returncode"] = 0
    except Exception as e:
        _dvbt_log(f"ERROR: {e}")
        _dvbt_state["returncode"] = 1
    finally:
        _dvbt_state["running"] = False


def _run_dvbt_new_location(location_name):
    try:
        freqs = _scan_frequencies()
        _dvbt_log(f"[1/5] creating network '{DVBT_NAME_PREFIX} {location_name}' with {len(freqs)} candidate mux(es) (474-778 MHz) ...")
        net_uuid, full_name, mux_uuids = create_dvbt_network(location_name)
        _dvbt_log(f"created {full_name!r} ({net_uuid})")

        _dvbt_log("[2/5] activating it (disables other DVB-T networks, reassigns tuner) ...")
        target = activate_dvbt_network(net_uuid)
        _dvbt_state["active_network"] = target["networkname"]

        _dvbt_log("[3/5] queueing candidate muxes for scanning ...")
        if not _force_mux_scan(mux_uuids, full_name):
            _dvbt_log("  WARNING: could not get any mux queued for scanning after repeated attempts")

        _dvbt_log("[4/5] scanning all candidate muxes for signal (this can take up to ~20 min) ...")
        settled = _wait_for_scan_queue(full_name, max_wait_iterations=240)
        if not settled:
            _dvbt_log("  scan queue did not fully settle within the time limit - continuing anyway")

        _dvbt_log("[5/5] removing muxes without signal, mapping found services to channels ...")
        removed = prune_failed_dvbt_muxes(full_name)
        _dvbt_log(f"removed {removed} mux(es) without signal")
        n = map_unmapped_dvbt_services(full_name)
        _dvbt_log(f"done. mapped {n} new service(s).")
        _dvbt_state["returncode"] = 0
    except Exception as e:
        _dvbt_log(f"ERROR: {e}")
        _dvbt_state["returncode"] = 1
    finally:
        _dvbt_state["running"] = False


def _start_background(target, args):
    with _dvbt_lock:
        if _dvbt_state["running"]:
            return False
        _dvbt_state["running"] = True
        _dvbt_state["returncode"] = None
        try:
            open(DVBT_LOG, "w").close()
        except OSError:
            pass
        threading.Thread(target=target, args=args, daemon=True).start()
        return True


def start_dvbt_activation(name_or_uuid):
    return _start_background(_run_dvbt_activation, (name_or_uuid,))


def start_dvbt_new_location(location_name):
    return _start_background(_run_dvbt_new_location, (location_name,))


def dvbt_status():
    with _dvbt_lock:
        running = _dvbt_state["running"]
        returncode = _dvbt_state["returncode"]
        active_network = _dvbt_state["active_network"]

    tail = ""
    try:
        with open(DVBT_LOG, "r", encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-40:])
    except FileNotFoundError:
        pass

    return {
        "running": running,
        "returncode": returncode,
        "active_network": active_network,
        "networks": list_dvbt_networks(),
        "log_tail": tail,
    }


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
        if path == "/dvbt/networks":
            try:
                _send_json(self, 200, list_dvbt_networks())
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/dvbt/status":
            _send_json(self, 200, dvbt_status())
            return
        if path == "/dvbt/services":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            network = (query.get("network") or [None])[0]
            try:
                _send_json(self, 200, {"stations": list_dvbt_services(network)})
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/dvbt/channels":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            network = (query.get("network") or [None])[0]
            try:
                _send_json(self, 200, {"channels": list_dvbt_channels(network)})
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        if path == "/dvbt/signal":
            try:
                _send_json(self, 200, get_dvbt_signal_info())
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

        if path == "/dvbt/activate":
            network = (query.get("network") or [None])[0]
            if not network:
                _send_json(self, 400, {"error": "missing 'network' query parameter"})
                return
            try:
                started = start_dvbt_activation(network)
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
                return
            _send_json(self, 202 if started else 409, {"started": started, "network": network})
            return

        if path == "/dvbt/scan_new_location":
            name = (query.get("name") or [None])[0]
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            try:
                started = start_dvbt_new_location(name)
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
                return
            _send_json(self, 202 if started else 409, {"started": started, "name": name})
            return

        if path == "/dvbt/delete_network":
            network = (query.get("network") or [None])[0]
            if not network:
                _send_json(self, 400, {"error": "missing 'network' query parameter"})
                return
            try:
                target, orphaned_channels = delete_dvbt_network(network)
            except ValueError as e:
                _send_json(self, 404, {"error": str(e)})
                return
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
                return
            _send_json(self, 200, {
                "deleted": True,
                "network": target["networkname"],
                "orphaned_channels_removed": orphaned_channels,
            })
            return

        if path == "/dvbt/exclude":
            name = (query.get("name") or [None])[0]
            excluded = (query.get("excluded") or ["true"])[0].lower() not in ("false", "0", "no")
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            set_dvbt_excluded(name, excluded)
            _send_json(self, 200, {"name": name, "excluded": excluded})
            return

        if path == "/dvbt/rename":
            raw_name = (query.get("raw_name") or [None])[0]
            display_name = (query.get("display_name") or [""])[0]
            if not raw_name:
                _send_json(self, 400, {"error": "missing 'raw_name' query parameter"})
                return
            set_dvbt_rename(raw_name, display_name)
            _send_json(self, 200, {"raw_name": raw_name, "display_name": display_name or raw_name})
            return

        if path == "/dvbt/channel_number":
            name = (query.get("name") or [None])[0]
            number_raw = (query.get("number") or [None])[0]
            if not name or number_raw is None:
                _send_json(self, 400, {"error": "missing 'name' or 'number' query parameter"})
                return
            try:
                number = int(number_raw)
            except ValueError:
                _send_json(self, 400, {"error": f"'number' must be an integer, got {number_raw!r}"})
                return
            try:
                set_dvbt_channel_number(name, number)
            except ValueError as e:
                _send_json(self, 404, {"error": str(e)})
                return
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
                return
            _send_json(self, 200, {"name": name, "number": number})
            return

        if path == "/dvbt/channel_name":
            name = (query.get("name") or [None])[0]
            new_name = (query.get("new_name") or [None])[0]
            if not name or not new_name:
                _send_json(self, 400, {"error": "missing 'name' or 'new_name' query parameter"})
                return
            try:
                set_dvbt_channel_name(name, new_name)
            except ValueError as e:
                _send_json(self, 404, {"error": str(e)})
                return
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
                return
            _send_json(self, 200, {"name": name, "new_name": new_name})
            return

        if path == "/dvbt/channel_enabled":
            name = (query.get("name") or [None])[0]
            enabled = (query.get("enabled") or ["true"])[0].lower() not in ("false", "0", "no")
            if not name:
                _send_json(self, 400, {"error": "missing 'name' query parameter"})
                return
            try:
                set_dvbt_channel_enabled(name, enabled)
            except ValueError as e:
                _send_json(self, 404, {"error": str(e)})
                return
            except Exception as e:
                _send_json(self, 502, {"error": str(e)})
                return
            _send_json(self, 200, {"name": name, "enabled": enabled})
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
