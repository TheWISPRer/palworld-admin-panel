import json
import math
import os
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import threading
import urllib.request
import time
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Configuration ----------------------------------------------------------
#
# Everything host- or server-specific lives here and comes from the
# environment, so the same code runs against any Palworld server. Deployments
# normally set these in an .env file loaded by the systemd unit
# (see deploy/palworld-admin.service and .env.example).
#
# DATA_DIR is kept separate from APP_DIR so application state (pins, trails,
# event history) can live outside the code checkout — that way `git pull`
# never touches your data, and the checkout can be read-only.


def _env_path(name, default):
    return os.path.abspath(os.path.expanduser(os.environ.get(name, default)))


DATA_DIR = _env_path("PANEL_DATA_DIR", APP_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

PINS_FILE = os.path.join(DATA_DIR, "pins.json")
PINS_LOCK = threading.Lock()

# The Palworld server's admin password (ADMIN_PASSWORD in the game server's
# own config). Required — the REST API is useless without it, and defaulting
# it to anything would just produce confusing 401s at runtime.
ADMIN_PASSWORD = os.environ.get("PALWORLD_ADMIN_PASSWORD", "")

# Name of the Docker container running the game server. Every docker exec /
# docker logs call below targets this.
CONTAINER = os.environ.get("PALWORLD_CONTAINER", "palworld")

# The REST API endpoint as seen FROM INSIDE the game container. 8212 is
# deliberately not published to the host on most setups, so calls are proxied
# via `docker exec <container> curl`.
REST_API_BASE = os.environ.get("PALWORLD_REST_URL", "http://127.0.0.1:8212/v1/api")

# Directory holding the game server's docker-compose.yml. Used for
# update/backup/restore operations.
COMPOSE_DIR = _env_path("PALWORLD_COMPOSE_DIR", "/srv/gameservers/palworld")
SERVER_DATA_DIR = _env_path("PALWORLD_DATA_DIR", os.path.join(COMPOSE_DIR, "data"))
BACKUP_DIR = _env_path("PALWORLD_BACKUP_DIR", os.path.join(SERVER_DATA_DIR, "backups"))
ARCHIVE_DIR = _env_path("PALWORLD_ARCHIVE_DIR", os.path.join(COMPOSE_DIR, "archives"))
PAL_DIR = _env_path("PALWORLD_PAL_DIR", os.path.join(SERVER_DATA_DIR, "Pal"))

# Filesystem paths shown in the panel's disk-usage card.
DISK_PATHS = [p.strip() for p in os.environ.get("PANEL_DISK_PATHS", "/").split(",") if p.strip()]

# Where this panel listens. Bind to loopback if a reverse proxy runs on the
# same host; 0.0.0.0 if the proxy is on another machine (see README).
PANEL_BIND = os.environ.get("PANEL_BIND", "0.0.0.0")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8300"))

# Cosmetic: shown in the browser tab and page header.
PANEL_TITLE = os.environ.get("PANEL_TITLE", "Palworld Admin")

# Optional Umami analytics. Left blank = no tracking script is emitted at all.
UMAMI_WEBSITE_ID = os.environ.get("UMAMI_WEBSITE_ID", "")
UMAMI_HOST_URL = os.environ.get("UMAMI_HOST_URL", "")
UMAMI_SCRIPT_URL = os.environ.get("UMAMI_SCRIPT_URL", "/_a/script.js")

# Map calibration. These four constants map in-game coordinates onto YOUR
# map image, so they are only valid for the specific image you ship as
# map.png. Defaults fit a full-bounds (-1000..1000) equirectangular image;
# see docs/MAP_CALIBRATION.md to fit them for a different image.
MAP_FX_SLOPE = float(os.environ.get("MAP_FX_SLOPE", "0.0005"))
MAP_FX_OFFSET = float(os.environ.get("MAP_FX_OFFSET", "0.5"))
MAP_FY_SLOPE = float(os.environ.get("MAP_FY_SLOPE", "-0.0005"))
MAP_FY_OFFSET = float(os.environ.get("MAP_FY_OFFSET", "0.5"))

# --- Optional secondary Valheim server --------------------------------------
# Strictly opt-in. Leave VALHEIM_CONTAINER unset (the default) and the tab is
# never rendered and every route below 404s — this stays a Palworld panel for
# anyone who doesn't run a Valheim box alongside it.
#
# Valheim has no RCON and no REST API, so unlike Palworld there's no live
# control channel: everything here is either Docker-level (state, start/stop)
# or file/log-level (lists, player count). That asymmetry is why these get
# their own routes instead of being folded into the Palworld ones.
VALHEIM_CONTAINER = os.environ.get("VALHEIM_CONTAINER", "")
VALHEIM_ENABLED = bool(VALHEIM_CONTAINER)
VALHEIM_COMPOSE_DIR = _env_path("VALHEIM_COMPOSE_DIR", "/srv/gameservers/valheim")
VALHEIM_CONFIG_DIR = _env_path(
    "VALHEIM_CONFIG_DIR", os.path.join(VALHEIM_COMPOSE_DIR, "config")
)

# --- Optional Minecraft server ----------------------------------------------
# Also strictly opt-in via MINECRAFT_SERVICE. Unlike the other two this one
# isn't Docker at all - it's a systemd unit running Paper under screen - and
# it's the only server here with a real remote console (RCON), so it gets
# genuine command execution rather than file-poking.
MINECRAFT_SERVICE = os.environ.get("MINECRAFT_SERVICE", "")
MINECRAFT_ENABLED = bool(MINECRAFT_SERVICE)
MINECRAFT_DIR = _env_path("MINECRAFT_DIR", "/home/minecraft/server")
MINECRAFT_RCON_HOST = os.environ.get("MINECRAFT_RCON_HOST", "127.0.0.1")
MINECRAFT_RCON_PORT = int(os.environ.get("MINECRAFT_RCON_PORT", "25575"))
MINECRAFT_RCON_PASSWORD = os.environ.get("MINECRAFT_RCON_PASSWORD", "")
# Some plugins (CoreProtect) answer the CONSOLE but not RCON - results go to
# stdout, and therefore latest.log, rather than back down the RCON socket. For
# those, commands are typed into the server's screen session instead. This
# mirrors what the systemd unit's ExecStop already does.
MINECRAFT_USER = os.environ.get("MINECRAFT_USER", "minecraft")
MINECRAFT_SCREEN = os.environ.get("MINECRAFT_SCREEN", "minecraft")


def rest_call(method, path, body=None, timeout=8):
    """Call the Palworld REST API through the container (8212 isn't published
    to the host, by design), returning parsed JSON or raising with the API's
    actual error message.

    Deliberately no curl -f: that flag suppresses the response body on HTTP
    errors, which hid the real {"errorCode", "errorMessage"} payload during
    testing and left only a blank, useless exception message.
    """
    cmd = [
        "sudo", "docker", "exec", CONTAINER,
        "curl", "-s", "-w", "\n%{http_code}", "-u", f"admin:{ADMIN_PASSWORD}",
        "-X", method, f"{REST_API_BASE}{path}",
    ]
    # Palworld's REST server (Epic's httpserver) rejects POSTs with no
    # Content-Length header (411), which curl only sends when -d is used —
    # so bodyless POSTs (e.g. /save) still need an explicit empty body.
    if method == "POST":
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body) if body is not None else ""]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed ({result.returncode}): {result.stderr.strip()}")

    out = result.stdout.rsplit("\n", 1)
    body_text, status = (out[0], out[1]) if len(out) == 2 else (result.stdout, "")
    parsed = json.loads(body_text) if body_text.strip() else {}

    if status and not status.startswith("2"):
        msg = parsed.get("errorMessage") or parsed.get("errorCode") or body_text.strip() or f"HTTP {status}"
        raise RuntimeError(msg)
    return parsed


# Server metrics change slowly and every open browser tab polls independently,
# so cache briefly rather than hitting the REST API once per tab per tick.
METRICS_TTL = 5
_metrics_lock = threading.Lock()
_metrics_cache = {"at": 0.0, "data": None}

# Palworld writes log timestamps in the container's local time (the container
# runs whatever TZ its compose file sets), while this app runs on the host.
# PALWORLD_LOG_TZ must match the GAME container's TZ or every "x ago" skews
# by the offset between them.
LOG_TZ = ZoneInfo(os.environ.get("PALWORLD_LOG_TZ", "UTC"))
JOIN_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[LOG\] (.+?) joined the server\."
)
LEAVE_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[LOG\] (.+?) left the server\."
)
CHAT_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[CHAT\] <(.+?)> (.*)"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Conversion from raw Unreal Engine world coords to the numbers Palworld's
# in-game pause-menu map displays. The game shows them as (second-derived-from-
# raw_y, ...) — i.e. the FIRST number comes from raw_y and the SECOND from
# raw_x. Verified against a live player readout.
OFFSET_X = 123888
OFFSET_Y = -158000
SCALE = 459


def raw_to_game(raw_x, raw_y):
    """Raw Unreal world coords -> the number pair the in-game pause-menu map
    shows, in the order it shows them: (first, second).

    Verified against a live player: raw (-82684, -335) -> (-345, 90), which
    matched the in-game readout of "-345, 89".
    """
    second = round((raw_x + OFFSET_X) / SCALE)
    first = round((raw_y + OFFSET_Y) / SCALE)
    return first, second


# --- Trails: continuous position history --------------------------------
#
# Everything else in this app is either on-demand (button clicks) or reads
# data that already existed for free (server logs). Trails need something
# new: history that exists whether or not a browser tab is open, since the
# whole point is scrubbing back through time later. That means a background
# poller independent of the frontend's polling, plus real storage — hence
# SQLite (stdlib, no new dependency) instead of another JSON file.
#
# Retention is tiered to keep this cheap indefinitely:
#   - last `recent_window_secs`: every poll is kept, full fidelity
#   - older than that: a trim pass collapses runs of small movement down to
#     just the points that represent real travel (sequential decimation —
#     walk each player's aged points in time order, drop any point closer
#     than `decimation_threshold` map-units to the last KEPT point). A player
#     AFK at a base for hours collapses to ~1 point instead of hundreds.
#   - older than `retention_days`: deleted outright, so this can't grow
#     forever even at trivial per-point cost.

TRAILS_DB = os.path.join(DATA_DIR, "trails.db")
TRAILS_CONFIG_FILE = os.path.join(APP_DIR, "trails_config.json")
TRAILS_LOCK = threading.Lock()

DEFAULT_TRAILS_CONFIG = {
    "enabled": True,
    "poll_interval_secs": 15,
    "recent_window_secs": 1800,   # 30 min at full fidelity
    "decimation_threshold": 15,   # map-units; map spans -1000..1000
    "trim_interval_secs": 300,    # how often the trim pass runs
    "retention_days": 30,         # hard cap regardless of decimation
}


def load_trails_config():
    with TRAILS_LOCK:
        if not os.path.exists(TRAILS_CONFIG_FILE):
            return dict(DEFAULT_TRAILS_CONFIG)
        with open(TRAILS_CONFIG_FILE) as f:
            cfg = json.load(f)
    merged = dict(DEFAULT_TRAILS_CONFIG)
    merged.update(cfg)
    return merged


def save_trails_config(cfg):
    with TRAILS_LOCK:
        with open(TRAILS_CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)


def _trails_db():
    # Fresh connection per call rather than one shared across threads — sqlite
    # handles concurrent access fine via WAL mode; a shared connection object
    # is what actually isn't thread-safe here.
    conn = sqlite3.connect(TRAILS_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            userid TEXT,
            x REAL NOT NULL,
            y REAL NOT NULL,
            ts REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_name_ts ON positions(name, ts)")
    return conn


def _trails_poll_once():
    cfg = load_trails_config()
    if not cfg["enabled"]:
        return
    try:
        data = rest_call("GET", "/players")
    except Exception:
        return  # transient REST hiccup; just skip this tick, next one retries

    now = time.time()
    conn = _trails_db()
    try:
        for p in data.get("players", []):
            first, second = raw_to_game(p["location_x"], p["location_y"])
            conn.execute(
                "INSERT INTO positions (name, userid, x, y, ts) VALUES (?, ?, ?, ?, ?)",
                (p["name"], p.get("userId"), first, second, now),
            )
        conn.commit()
    finally:
        conn.close()


def _trails_trim_once():
    cfg = load_trails_config()
    cutoff = time.time() - cfg["recent_window_secs"]
    retention_cutoff = time.time() - cfg["retention_days"] * 86400
    threshold = cfg["decimation_threshold"]

    conn = _trails_db()
    try:
        # Hard cap first — no point decimating data we're about to delete anyway.
        conn.execute("DELETE FROM positions WHERE ts < ?", (retention_cutoff,))

        names = [r[0] for r in conn.execute("SELECT DISTINCT name FROM positions WHERE ts < ?", (cutoff,))]
        for name in names:
            rows = conn.execute(
                "SELECT id, x, y FROM positions WHERE name = ? AND ts < ? ORDER BY ts ASC",
                (name, cutoff),
            ).fetchall()
            if len(rows) < 2:
                continue
            keep_x, keep_y = rows[0][1], rows[0][2]
            to_delete = []
            for row_id, x, y in rows[1:]:
                if math.hypot(x - keep_x, y - keep_y) < threshold:
                    to_delete.append(row_id)
                else:
                    keep_x, keep_y = x, y
            if to_delete:
                conn.executemany("DELETE FROM positions WHERE id = ?", [(i,) for i in to_delete])
        conn.commit()
    finally:
        conn.close()


def _trails_poller_loop():
    while True:
        try:
            _trails_poll_once()
        except Exception:
            pass  # background loop must never die from a transient error
        time.sleep(load_trails_config()["poll_interval_secs"])


def _trails_trimmer_loop():
    while True:
        try:
            _trails_trim_once()
        except Exception:
            pass
        time.sleep(load_trails_config()["trim_interval_secs"])


def start_trails_background_threads():
    threading.Thread(target=_trails_poller_loop, daemon=True).start()
    threading.Thread(target=_trails_trimmer_loop, daemon=True).start()


def load_pins():
    with PINS_LOCK:
        if not os.path.exists(PINS_FILE):
            return []
        with open(PINS_FILE) as f:
            pins = json.load(f)
    # Backfill fields for pins created before category/color/hidden existed.
    for p in pins:
        p.setdefault("category", "Other")
        p.setdefault("color", DEFAULT_CATEGORY_COLORS.get(p["category"], "#d9a3ff"))
        p.setdefault("hidden", False)
    return pins


def save_pins(pins):
    with PINS_LOCK:
        with open(PINS_FILE, "w") as f:
            json.dump(pins, f, indent=2)


app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    """Serve index.html with deployment config substituted in.

    Done server-side rather than via a client-side /api/config fetch on
    purpose: the map calibration constants are needed by the very first
    draw() call, so fetching them asynchronously would race the initial
    render. Templating here keeps the frontend free of config plumbing and
    makes the served page self-contained.

    index.html is read per request (it's a few dozen KB, and this keeps
    edit-refresh workflows instant), so no restart is needed after
    front-end-only changes.
    """
    with open(os.path.join(APP_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()

    if UMAMI_WEBSITE_ID and UMAMI_HOST_URL:
        analytics = (
            f'<script defer src="{UMAMI_SCRIPT_URL}" '
            f'data-website-id="{UMAMI_WEBSITE_ID}" '
            f'data-host-url="{UMAMI_HOST_URL}"></script>'
        )
    else:
        analytics = "<!-- analytics disabled (UMAMI_WEBSITE_ID unset) -->"

    replacements = {
        "{{PANEL_TITLE}}": PANEL_TITLE,
        "{{ANALYTICS_TAG}}": analytics,
        "{{MAP_FX_SLOPE}}": repr(MAP_FX_SLOPE),
        "{{MAP_FX_OFFSET}}": repr(MAP_FX_OFFSET),
        "{{MAP_FY_SLOPE}}": repr(MAP_FY_SLOPE),
        "{{MAP_FY_OFFSET}}": repr(MAP_FY_OFFSET),
        "{{VALHEIM_ENABLED}}": "true" if VALHEIM_ENABLED else "false",
        "{{MINECRAFT_ENABLED}}": "true" if MINECRAFT_ENABLED else "false",
    }
    for token, value in replacements.items():
        html = html.replace(token, value)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/map.png")
def map_image():
    """The map background image.

    Not shipped with this project — Palworld's map is Pocketpair's artwork,
    so you supply your own (see docs/MAP_CALIBRATION.md). Missing image is
    handled gracefully: the frontend draws a placeholder and everything else
    (players, pins, trails) keeps working on a blank canvas.
    """
    path = os.path.join(DATA_DIR, "map.png")
    if not os.path.exists(path):
        path = os.path.join(APP_DIR, "map.png")
    if not os.path.exists(path):
        return "map.png not found — see docs/MAP_CALIBRATION.md", 404
    return send_from_directory(os.path.dirname(path), "map.png")


@app.route("/api/players")
def api_players():
    try:
        data = rest_call("GET", "/players")
    except Exception as e:
        return jsonify({"error": str(e), "players": []}), 200

    players = []
    for p in data.get("players", []):
        first, second = raw_to_game(p["location_x"], p["location_y"])
        players.append({
            "name": p["name"],
            "level": p.get("level"),
            "userid": p.get("userId"),
            # Must match the convention pins use, since both feed the same
            # mapToPixel(): x drives horizontal, y drives vertical. Per the
            # frontend calibration, the in-game map's FIRST number drives
            # horizontal and its SECOND drives vertical.
            "x": first,
            "y": second,
        })
    return jsonify({"players": players})


@app.route("/api/metrics")
def api_metrics():
    now = time.time()
    with _metrics_lock:
        fresh = _metrics_cache["data"] is not None and (now - _metrics_cache["at"]) < METRICS_TTL
        if fresh:
            return jsonify(_metrics_cache["data"])
    try:
        data = rest_call("GET", "/metrics")
    except Exception as e:
        data = {"error": str(e)}
    with _metrics_lock:
        _metrics_cache["at"] = now
        _metrics_cache["data"] = data
    return jsonify(data)


@app.route("/api/announce", methods=["POST"])
def api_announce():
    body = request.get_json(force=True)
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message required"}), 400
    try:
        rest_call("POST", "/announce", {"message": message})
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/api/save", methods=["POST"])
def api_save():
    try:
        rest_call("POST", "/save")
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/api/kick", methods=["POST"])
def api_kick():
    body = request.get_json(force=True)
    userid = body.get("userid")
    if not userid:
        return jsonify({"error": "userid required"}), 400
    payload = {"userid": userid}
    if body.get("message"):
        payload["message"] = body["message"]
    try:
        rest_call("POST", "/kick", payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/api/ban", methods=["POST"])
def api_ban():
    body = request.get_json(force=True)
    userid = body.get("userid")
    if not userid:
        return jsonify({"error": "userid required"}), 400
    payload = {"userid": userid}
    if body.get("message"):
        payload["message"] = body["message"]
    try:
        rest_call("POST", "/ban", payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/api/unban", methods=["POST"])
def api_unban():
    body = request.get_json(force=True)
    userid = body.get("userid")
    if not userid:
        return jsonify({"error": "userid required"}), 400
    try:
        rest_call("POST", "/unban", {"userid": userid})
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    body = request.get_json(force=True)
    waittime = int(body.get("waittime", 30))
    payload = {"waittime": waittime}
    if body.get("message"):
        payload["message"] = body["message"]
    try:
        rest_call("POST", "/shutdown", payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/api/whoami")
def api_whoami():
    # Caddy's forward_auth copies this from Authentik on every request. Used
    # only to tag Umami events with who performed an action — never for
    # access control, since that's already enforced upstream by Authentik.
    return jsonify({"username": request.headers.get("X-Authentik-Username")})


@app.route("/api/diskusage")
def api_diskusage():
    # Cheap, local, no game-server involvement at all — same TTL pattern.
    now = time.time()
    with _metrics_lock:
        cached = _metrics_cache.get("disk")
        if cached and (now - cached["at"]) < 30:
            return jsonify(cached["data"])
    try:
        out = subprocess.run(
            ["df", "-h", "--output=target,pcent,avail", *DISK_PATHS],
            capture_output=True, text=True, timeout=5,
        ).stdout
        lines = [l.split() for l in out.strip().splitlines()[1:]]
        data = {"volumes": [{"path": l[0], "used_pct": l[1], "avail": l[2]} for l in lines]}
    except Exception as e:
        data = {"error": str(e)}
    with _metrics_lock:
        _metrics_cache["disk"] = {"at": now, "data": data}
    return jsonify(data)


def _parse_ts(ts_raw):
    try:
        return datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOG_TZ)
    except ValueError:
        return None


# --- Chat & join/leave history: persisted, independent of Docker log rotation --
#
# This used to re-parse `docker logs --tail N` on every request. Once the
# container's json-file log rotates past that tail window (10MB x 3 files,
# per the compose config), that history is gone for good — chat and recent
# players would silently fall off the older they got, with no way to page
# back. Same fix as Trails: a background poller tails the log and persists
# new lines to SQLite (deduped via a UNIQUE constraint + INSERT OR IGNORE, so
# re-scanning the overlapping tail on every poll is harmless), so history
# survives log rotation and can be queried by date range.

EVENTS_DB = os.path.join(DATA_DIR, "events.db")
EVENTS_POLL_INTERVAL = 20
EVENTS_RETENTION_DAYS = 90


def _events_db():
    conn = sqlite3.connect(EVENTS_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            UNIQUE(ts, name, message)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_messages(ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            name TEXT NOT NULL,
            event TEXT NOT NULL,
            UNIQUE(ts, name, event)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON player_events(ts)")
    return conn


def _events_poll_once():
    try:
        result = subprocess.run(
            ["sudo", "docker", "logs", "--tail", "3000", CONTAINER],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return
    lines = ANSI_RE.sub("", (result.stdout or "") + "\n" + (result.stderr or ""))

    conn = _events_db()
    try:
        for match in JOIN_RE.finditer(lines):
            dt = _parse_ts(match.group(1))
            if dt:
                conn.execute(
                    "INSERT OR IGNORE INTO player_events (ts, name, event) VALUES (?, ?, 'joined')",
                    (dt.timestamp(), match.group(2).strip()),
                )
        for match in LEAVE_RE.finditer(lines):
            dt = _parse_ts(match.group(1))
            if dt:
                conn.execute(
                    "INSERT OR IGNORE INTO player_events (ts, name, event) VALUES (?, ?, 'left')",
                    (dt.timestamp(), match.group(2).strip()),
                )
        for match in CHAT_RE.finditer(lines):
            dt = _parse_ts(match.group(1))
            if dt:
                conn.execute(
                    "INSERT OR IGNORE INTO chat_messages (ts, name, message) VALUES (?, ?, ?)",
                    (dt.timestamp(), match.group(2).strip(), match.group(3)),
                )
        conn.commit()
    finally:
        conn.close()


def _events_trim_once():
    cutoff = time.time() - EVENTS_RETENTION_DAYS * 86400
    conn = _events_db()
    try:
        conn.execute("DELETE FROM chat_messages WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM player_events WHERE ts < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def _events_poller_loop():
    while True:
        try:
            _events_poll_once()
        except Exception:
            pass  # background loop must never die from a transient error
        time.sleep(EVENTS_POLL_INTERVAL)


def _events_trimmer_loop():
    while True:
        try:
            _events_trim_once()
        except Exception:
            pass
        time.sleep(3600)


def start_events_background_threads():
    threading.Thread(target=_events_poller_loop, daemon=True).start()
    threading.Thread(target=_events_trimmer_loop, daemon=True).start()


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _latest_per_player(limit=10):
    """Most recent activity per player, newest first — based on when they
    LEFT, not joined, so "time ago" reflects last-seen rather than
    session-start. Players still in their first tracked session (no leave
    logged yet — e.g. currently online) fall back to their join time,
    flagged via "based_on" so the frontend can word it accurately."""
    conn = _events_db()
    try:
        rows = conn.execute(
            "SELECT ts, name, event FROM player_events ORDER BY ts DESC"
        ).fetchall()
    finally:
        conn.close()
    seen = set()
    entries = []
    for ts, name, event in rows:
        if name in seen:
            continue
        seen.add(name)
        entries.append({"name": name, "at": _iso(ts), "based_on": event})
        if len(entries) >= limit:
            break
    return entries


def _query_player_events(since=None, until=None, limit=500):
    """Full chronological (not deduped) join/leave history for browsing a
    specific date range — the most recent `limit` events in range, oldest
    first."""
    query = "SELECT ts, name, event FROM player_events WHERE 1=1"
    params = []
    if since is not None:
        query += " AND ts >= ?"
        params.append(since)
    if until is not None:
        query += " AND ts <= ?"
        params.append(until)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    conn = _events_db()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    rows.reverse()
    return [{"at": _iso(ts), "name": name, "event": event} for ts, name, event in rows]


@app.route("/api/recent")
def api_recent():
    since = request.args.get("since", type=float)
    until = request.args.get("until", type=float)
    if since is not None or until is not None:
        limit = min(request.args.get("limit", type=int) or 500, 5000)
        return jsonify({"events": _query_player_events(since, until, limit)})
    return jsonify({"players": _latest_per_player(10)})


def _query_chat(since=None, until=None, limit=200):
    """The most recent `limit` messages in range, oldest first."""
    query = "SELECT ts, name, message FROM chat_messages WHERE 1=1"
    params = []
    if since is not None:
        query += " AND ts >= ?"
        params.append(since)
    if until is not None:
        query += " AND ts <= ?"
        params.append(until)
    query += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    conn = _events_db()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    rows.reverse()
    return [{"at": _iso(ts), "name": name, "message": message} for ts, name, message in rows]


@app.route("/api/chat")
def api_chat():
    since = request.args.get("since", type=float)
    until = request.args.get("until", type=float)
    limit = min(request.args.get("limit", type=int) or 200, 5000)
    return jsonify({"messages": _query_chat(since, until, limit)})


# --- Player-side stats -------------------------------------------------------
#
# Deliberately separate from the Umami page-visit analytics wired up for this
# app: Umami covers who's using the *admin panel* and how, this covers
# player activity on the *server*, computed straight from events.db (now that
# join/leave/chat history is actually persisted). No new storage, no new
# background thread — just aggregate queries over what's already collected.

def _stats_day_key(ts):
    return datetime.fromtimestamp(ts, tz=LOG_TZ).strftime("%Y-%m-%d")


@app.route("/api/stats")
def api_stats():
    now = time.time()
    week_ago = now - 7 * 86400
    month_ago = now - 30 * 86400
    today_start = datetime.now(LOG_TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    conn = _events_db()
    try:
        total_players = conn.execute("SELECT COUNT(DISTINCT name) FROM player_events").fetchone()[0]
        sessions_today = conn.execute(
            "SELECT COUNT(*) FROM player_events WHERE event = 'joined' AND ts >= ?", (today_start,)
        ).fetchone()[0]
        chat_today = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE ts >= ?", (today_start,)
        ).fetchone()[0]
        first_join = dict(conn.execute(
            "SELECT name, MIN(ts) FROM player_events WHERE event = 'joined' GROUP BY name"
        ).fetchall())
        join_rows = conn.execute(
            "SELECT ts, name FROM player_events WHERE event = 'joined' AND ts >= ?", (month_ago,)
        ).fetchall()
        chat_rows = conn.execute(
            "SELECT ts FROM chat_messages WHERE ts >= ?", (month_ago,)
        ).fetchall()
        top_players = conn.execute(
            "SELECT name, COUNT(*) FROM player_events WHERE event = 'joined' GROUP BY name ORDER BY COUNT(*) DESC LIMIT 15"
        ).fetchall()
    finally:
        conn.close()

    new_players_7d = sum(1 for ts in first_join.values() if ts >= week_ago)

    daily_active = {}
    hour_hist = [0] * 24
    dow_hist = [0] * 7  # Monday=0 .. Sunday=6
    for ts, name in join_rows:
        dt = datetime.fromtimestamp(ts, tz=LOG_TZ)
        daily_active.setdefault(_stats_day_key(ts), set()).add(name)
        hour_hist[dt.hour] += 1
        dow_hist[dt.weekday()] += 1

    daily_chat = {}
    for (ts,) in chat_rows:
        key = _stats_day_key(ts)
        daily_chat[key] = daily_chat.get(key, 0) + 1

    return jsonify({
        "total_players": total_players,
        "sessions_today": sessions_today,
        "chat_messages_today": chat_today,
        "new_players_7d": new_players_7d,
        "daily_active_30d": [{"day": d, "count": len(names)} for d, names in sorted(daily_active.items())],
        "daily_chat_30d": [{"day": d, "count": c} for d, c in sorted(daily_chat.items())],
        "hour_of_day_30d": hour_hist,
        "day_of_week_30d": dow_hist,
        "top_players": [{"name": n, "sessions": s} for n, s in top_players],
    })


@app.route("/api/pins", methods=["GET"])
def get_pins():
    return jsonify(load_pins())


DEFAULT_CATEGORY_COLORS = {
    "Base": "#ffd166",
    "Statue": "#7fd1ff",
    "Resource": "#8bd17f",
    "Other": "#d9a3ff",
}


@app.route("/api/pins", methods=["POST"])
def add_pin():
    body = request.get_json(force=True)
    pins = load_pins()
    new_id = (max((p["id"] for p in pins), default=0)) + 1
    category = body.get("category", "Other")
    pin = {
        "id": new_id,
        "label": body.get("label", "Pin"),
        "category": category,
        "color": body.get("color") or DEFAULT_CATEGORY_COLORS.get(category, "#d9a3ff"),
        "hidden": False,
        "x": body["x"],
        "y": body["y"],
    }
    pins.append(pin)
    save_pins(pins)
    return jsonify(pin), 201


@app.route("/api/pins/<int:pin_id>", methods=["PATCH"])
def update_pin(pin_id):
    body = request.get_json(force=True)
    pins = load_pins()
    for p in pins:
        if p["id"] == pin_id:
            for field in ("label", "category", "color", "hidden"):
                if field in body:
                    p[field] = body[field]
            save_pins(pins)
            return jsonify(p)
    return jsonify({"error": "not found"}), 404


@app.route("/api/pins/<int:pin_id>", methods=["DELETE"])
def delete_pin(pin_id):
    pins = load_pins()
    pins = [p for p in pins if p["id"] != pin_id]
    save_pins(pins)
    return "", 204


@app.route("/api/trails/config", methods=["GET"])
def get_trails_config():
    return jsonify(load_trails_config())


@app.route("/api/trails/config", methods=["POST"])
def set_trails_config():
    body = request.get_json(force=True)
    cfg = load_trails_config()
    try:
        if "enabled" in body:
            cfg["enabled"] = bool(body["enabled"])
        if "poll_interval_secs" in body:
            v = int(body["poll_interval_secs"])
            if v < 5:
                return jsonify({"error": "poll_interval_secs must be >= 5"}), 400
            cfg["poll_interval_secs"] = v
        if "recent_window_secs" in body:
            v = int(body["recent_window_secs"])
            if v < 60:
                return jsonify({"error": "recent_window_secs must be >= 60"}), 400
            cfg["recent_window_secs"] = v
        if "decimation_threshold" in body:
            v = float(body["decimation_threshold"])
            if v <= 0:
                return jsonify({"error": "decimation_threshold must be > 0"}), 400
            cfg["decimation_threshold"] = v
        if "trim_interval_secs" in body:
            v = int(body["trim_interval_secs"])
            if v < 30:
                return jsonify({"error": "trim_interval_secs must be >= 30"}), 400
            cfg["trim_interval_secs"] = v
        if "retention_days" in body:
            v = int(body["retention_days"])
            if v < 1:
                return jsonify({"error": "retention_days must be >= 1"}), 400
            cfg["retention_days"] = v
    except (TypeError, ValueError):
        return jsonify({"error": "invalid value"}), 400

    save_trails_config(cfg)
    return jsonify(cfg)


@app.route("/api/trails/players")
def get_trails_players():
    conn = _trails_db()
    try:
        names = [r[0] for r in conn.execute(
            "SELECT DISTINCT name FROM positions ORDER BY name"
        )]
    finally:
        conn.close()
    return jsonify({"players": names})


@app.route("/api/trails")
def get_trails():
    name = request.args.get("player")
    since = request.args.get("since", type=float)
    until = request.args.get("until", type=float)
    if not name:
        return jsonify({"error": "player required"}), 400

    query = "SELECT x, y, ts FROM positions WHERE name = ?"
    params = [name]
    if since is not None:
        query += " AND ts >= ?"
        params.append(since)
    if until is not None:
        query += " AND ts <= ?"
        params.append(until)
    query += " ORDER BY ts ASC"

    conn = _trails_db()
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return jsonify({"points": [{"x": x, "y": y, "ts": ts} for x, y, ts in rows]})


@app.route("/api/trails/stats")
def get_trails_stats():
    conn = _trails_db()
    try:
        count, min_ts = conn.execute("SELECT COUNT(*), MIN(ts) FROM positions").fetchone()
    finally:
        conn.close()
    db_size = os.path.getsize(TRAILS_DB) if os.path.exists(TRAILS_DB) else 0
    return jsonify({
        "point_count": count,
        "oldest_ts": min_ts,
        "db_size_bytes": db_size,
    })


@app.route("/api/trails/clear", methods=["POST"])
def clear_trails():
    conn = _trails_db()
    try:
        conn.execute("DELETE FROM positions")
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


# --- World config --------------------------------------------------------
#
# Palworld reads its settings once, at server start, from environment
# variables in docker-compose.yml (the thijsvanloef/palworld-server-docker
# image compiles these into PalWorldSettings.ini on container start). There's
# no live-reload — changing a value only takes effect on the next full
# `docker compose up -d`. So edits here are queued (written to a small JSON
# file, not the compose file) until the operator explicitly reboots, via
# /api/server/reboot below, which applies the queue to docker-compose.yml
# and then restarts.
#
# The whitelist below is deliberately a curated subset of everything the
# image supports — the settings admins actually reach for — not a full
# PalWorldSettings.ini editor. Notably absent: bIsMultiplay. It looks like a
# multiplayer toggle but isn't one (multiplayer works fine with it False);
# exposing it invites someone "fixing" a setting that was never broken.

WORLD_CONFIG_SETTINGS = [
    {"key": "PLAYERS", "label": "Max Players", "category": "Population",
     "type": "int", "min": 1, "max": 32, "default": 16,
     "help": "Maximum concurrent players on the server."},
    {"key": "COOP_PLAYER_MAX_NUM", "label": "Max Players per Squad", "category": "Population",
     "type": "int", "min": 1, "max": 4, "default": 4,
     "help": "Max players sharing one drop-in co-op squad."},
    {"key": "GUILD_PLAYER_MAX_NUM", "label": "Max Players per Guild", "category": "Population",
     "type": "int", "min": 1, "max": 100, "default": 20,
     "help": "Max members in a single guild."},

    {"key": "BASE_CAMP_MAX_NUM_IN_GUILD", "label": "Max Bases per Guild", "category": "Bases & Pals",
     "type": "int", "min": 1, "max": 40, "default": 4,
     "help": "Ceiling on base camps a guild can place. This is only a ceiling — "
             "actual current capacity is separately gated in-game by each "
             "guild's Base Level, raised by completing Base Missions. "
             "Raising this number doesn't grant slots by itself, it just "
             "raises how high Base Level progression is allowed to go."},
    {"key": "BASE_CAMP_MAX_NUM", "label": "Max Bases (server-wide)", "category": "Bases & Pals",
     "type": "int", "min": 1, "max": 300, "default": 128,
     "help": "Total base camps allowed across every guild on the server."},
    {"key": "BASE_CAMP_WORKER_MAX_NUM", "label": "Max Working Pals per Base", "category": "Bases & Pals",
     "type": "int", "min": 1, "max": 30, "default": 15,
     "help": "Max Pals that can be assigned to work at one base camp."},
    {"key": "PAL_CAPTURE_RATE", "label": "Capture Rate", "category": "Bases & Pals",
     "type": "float", "min": 0.1, "max": 10, "default": 1.0,
     "help": "Multiplier on the chance a thrown Pal Sphere succeeds."},
    {"key": "PAL_SPAWN_NUM_RATE", "label": "Pal Spawn Rate", "category": "Bases & Pals",
     "type": "float", "min": 0.1, "max": 5, "default": 1.0,
     "help": "Multiplier on how many wild Pals spawn in the world."},

    {"key": "EXP_RATE", "label": "Player EXP Rate", "category": "Rates & Experience",
     "type": "float", "min": 0.1, "max": 20, "default": 1.0,
     "help": "Multiplier on player experience gain."},
    {"key": "WORK_SPEED_RATE", "label": "Work Speed Rate", "category": "Rates & Experience",
     "type": "float", "min": 0.1, "max": 20, "default": 1.0,
     "help": "Multiplier on Pal work speed at bases."},
    {"key": "DAY_TIME_SPEED_RATE", "label": "Day Speed", "category": "Rates & Experience",
     "type": "float", "min": 0.1, "max": 10, "default": 1.0,
     "help": "How fast in-game daytime passes."},
    {"key": "NIGHT_TIME_SPEED_RATE", "label": "Night Speed", "category": "Rates & Experience",
     "type": "float", "min": 0.1, "max": 10, "default": 1.0,
     "help": "How fast in-game nighttime passes."},

    {"key": "PLAYER_DAMAGE_RATE_ATTACK", "label": "Player Attack Damage", "category": "Damage & Difficulty",
     "type": "float", "min": 0.1, "max": 10, "default": 1.0,
     "help": "Multiplier on damage players deal."},
    {"key": "PLAYER_DAMAGE_RATE_DEFENSE", "label": "Player Damage Taken", "category": "Damage & Difficulty",
     "type": "float", "min": 0.1, "max": 10, "default": 1.0,
     "help": "Multiplier on damage players receive."},
    {"key": "PAL_DAMAGE_RATE_ATTACK", "label": "Pal Attack Damage", "category": "Damage & Difficulty",
     "type": "float", "min": 0.1, "max": 10, "default": 1.0,
     "help": "Multiplier on damage Pals deal."},
    {"key": "PAL_DAMAGE_RATE_DEFENSE", "label": "Pal Damage Taken", "category": "Damage & Difficulty",
     "type": "float", "min": 0.1, "max": 10, "default": 1.0,
     "help": "Multiplier on damage Pals receive."},
    {"key": "ENABLE_PLAYER_TO_PLAYER_DAMAGE", "label": "PvP Damage", "category": "Damage & Difficulty",
     "type": "bool", "default": False,
     "help": "Allow players to damage each other."},
    {"key": "ENABLE_FRIENDLY_FIRE", "label": "Friendly Fire", "category": "Damage & Difficulty",
     "type": "bool", "default": False,
     "help": "Allow a Pal to damage its own owner's allies."},
    {"key": "DEATH_PENALTY", "label": "Death Penalty", "category": "Damage & Difficulty",
     "type": "enum", "options": ["None", "Item", "ItemAndEquipment", "All"], "default": "Item",
     "help": "What a player loses on death."},

    {"key": "DROP_ITEM_MAX_NUM", "label": "Max Dropped Items", "category": "Items & World",
     "type": "int", "min": 100, "max": 20000, "default": 3000,
     "help": "Cap on items lying on the ground at once, server-wide."},
    {"key": "ITEM_WEIGHT_RATE", "label": "Item Weight Rate", "category": "Items & World",
     "type": "float", "min": 0.1, "max": 5, "default": 1.0,
     "help": "Multiplier on carried item weight."},
    {"key": "AUTO_SAVE_SPAN", "label": "Autosave Interval (minutes)", "category": "Items & World",
     "type": "float", "min": 1, "max": 120, "default": 30,
     "help": "How often the server autosaves."},
]
_WORLD_CONFIG_BY_KEY = {s["key"]: s for s in WORLD_CONFIG_SETTINGS}

WORLD_CONFIG_PENDING_FILE = os.path.join(DATA_DIR, "world_config_pending.json")
_world_config_lock = threading.Lock()


def _load_pending_config():
    with _world_config_lock:
        if not os.path.exists(WORLD_CONFIG_PENDING_FILE):
            return {}
        with open(WORLD_CONFIG_PENDING_FILE) as f:
            return json.load(f)


def _save_pending_config(pending):
    with _world_config_lock:
        with open(WORLD_CONFIG_PENDING_FILE, "w") as f:
            json.dump(pending, f, indent=2)


def _coerce_setting_value(setting, raw):
    t = setting["type"]
    if t == "int":
        v = int(raw)
    elif t == "float":
        v = float(raw)
    elif t == "bool":
        v = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("true", "1", "yes", "on")
        return v
    elif t == "enum":
        if raw not in setting["options"]:
            raise ValueError(f"must be one of {setting['options']}")
        return raw
    else:
        raise ValueError(f"unknown setting type {t!r}")
    if "min" in setting and v < setting["min"]:
        raise ValueError(f"must be >= {setting['min']}")
    if "max" in setting and v > setting["max"]:
        raise ValueError(f"must be <= {setting['max']}")
    return v


def _parse_compose_value(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1]
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _read_compose_env(compose_dir=None):
    """Parse the `environment:` block of docker-compose.yml into a dict.
    Line-based on purpose, not a YAML parser — this only needs to understand
    the one block this file actually has, and doing it this way means
    _apply_compose_changes can round-trip every comment and unrelated line
    byte-for-byte instead of risking a full YAML re-serialize.
    """
    path = os.path.join(compose_dir or COMPOSE_DIR, "docker-compose.yml")
    values = {}
    with open(path) as f:
        for line in f:
            m = re.match(r"^\s{6}([A-Z_]+):\s*(.+?)\s*$", line)
            if m:
                values[m.group(1)] = _parse_compose_value(m.group(2))
    return values


def _format_compose_value(key, value, by_key=None):
    setting = (by_key or _WORLD_CONFIG_BY_KEY)[key]
    if setting["type"] == "bool":
        return "true" if value else "false"
    if setting["type"] in ("int", "float"):
        return str(value)
    return f'"{value}"'  # enum / string


def _apply_compose_changes(changes, compose_dir=None, by_key=None, tag="worldconfig"):
    """Rewrite docker-compose.yml with `changes` merged into the environment
    block — updating keys that already have a line, appending any that don't,
    every other line untouched. Caller is responsible for backing up first.
    """
    path = os.path.join(compose_dir or COMPOSE_DIR, "docker-compose.yml")
    with open(path) as f:
        lines = f.readlines()

    remaining = dict(changes)
    out = []
    in_env = False
    env_indent = "      "  # fallback if environment: has zero entries somehow
    for line in lines:
        if line.strip() == "environment:":
            in_env = True
            out.append(line)
            continue
        if in_env:
            m = re.match(r"^(\s+)([A-Z_]+):\s*.*$", line)
            if m:
                env_indent, key = m.group(1), m.group(2)
                if key in remaining:
                    out.append(
                        f"{env_indent}{key}: "
                        f"{_format_compose_value(key, remaining.pop(key), by_key)}\n"
                    )
                else:
                    out.append(line)
                continue
            # First non-matching line ends the block — flush anything new.
            for key, value in remaining.items():
                out.append(f"{env_indent}{key}: {_format_compose_value(key, value, by_key)}\n")
            remaining = {}
            in_env = False
        out.append(line)

    if remaining:  # environment: was the last block in the file
        for key, value in remaining.items():
            out.append(f"{env_indent}{key}: {_format_compose_value(key, value, by_key)}\n")

    backup_path = f"{path}.pre-{tag}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.copy(path, backup_path)
    with open(path, "w") as f:
        f.writelines(out)


@app.route("/api/world-config")
def api_world_config_get():
    current = _read_compose_env()
    pending = _load_pending_config()
    settings = [
        {**s, "current": current.get(s["key"], s["default"]), "pending": pending.get(s["key"])}
        for s in WORLD_CONFIG_SETTINGS
    ]
    return jsonify({"settings": settings, "pending_count": len(pending)})


@app.route("/api/world-config", methods=["POST"])
def api_world_config_post():
    body = request.get_json(force=True) or {}
    changes = body.get("changes", {})
    pending = _load_pending_config()
    errors = {}
    for key, raw_value in changes.items():
        setting = _WORLD_CONFIG_BY_KEY.get(key)
        if not setting:
            errors[key] = "unknown setting"
            continue
        try:
            pending[key] = _coerce_setting_value(setting, raw_value)
        except (ValueError, TypeError) as e:
            errors[key] = str(e)
    if errors:
        return jsonify({"error": "invalid values", "details": errors}), 400
    _save_pending_config(pending)
    return jsonify({"queued": True, "pending_count": len(pending)})


@app.route("/api/world-config/clear", methods=["POST"])
def api_world_config_clear():
    _save_pending_config({})
    return jsonify({"cleared": True})


# --- Server update / backup / restore ------------------------------------
#
# These take real time (image pulls, SteamCMD downloads, tar extraction), so
# each runs in a background thread with polled status rather than blocking
# the request — the same reasoning as the trails poller. Update and restore
# also both disrupt the live server (container recreate / stop-start), so the
# frontend is expected to confirm heavily before calling these; the backend
# doesn't second-guess that, but it does refuse to start a second job of the
# same kind while one is already running.

SAVED_DIR = os.path.join(PAL_DIR, "Saved")
BACKUP_NAME_RE = re.compile(r"^palworld-save-[\w\-.]+\.tar\.gz$")

_jobs_lock = threading.Lock()
_jobs = {}  # name -> {state, log, started_at, finished_at, progress}
_JOB_DEFAULT = {"state": "idle", "log": "", "started_at": None, "finished_at": None, "progress": None}


def _job_start(name):
    with _jobs_lock:
        existing = _jobs.get(name)
        if existing and existing["state"] == "running":
            return False
        _jobs[name] = {
            "state": "running", "log": "",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "progress": None,
        }
    return True


def _job_append(name, text):
    with _jobs_lock:
        _jobs[name]["log"] += text


def _job_set_progress(name, percent, stage=None, done_bytes=None, total_bytes=None):
    with _jobs_lock:
        job = _jobs.get(name)
        if job is not None:
            job["progress"] = {
                "percent": percent, "stage": stage,
                "done_bytes": done_bytes, "total_bytes": total_bytes,
            }


def _job_finish(name, ok):
    with _jobs_lock:
        _jobs[name]["state"] = "success" if ok else "failed"
        _jobs[name]["finished_at"] = datetime.now(timezone.utc).isoformat()
        _jobs[name]["progress"] = None


def _job_status(name):
    with _jobs_lock:
        return dict(_jobs.get(name, _JOB_DEFAULT))


def run_cmd(cmd, cwd=None, timeout=None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    out = f"$ {' '.join(cmd)}\n{result.stdout}{result.stderr}\n"
    return result.returncode, out


@app.route("/api/server/version")
def api_server_version():
    try:
        data = rest_call("GET", "/info")
        return jsonify({"version": data.get("version"), "servername": data.get("servername")})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/server/jobs")
def api_server_jobs():
    names = ["update", "backup", "restore", "reboot"]
    if VALHEIM_ENABLED:
        names.append("valheim")
    if MINECRAFT_ENABLED:
        names.append("minecraft")
    return jsonify({name: _job_status(name) for name in names})


# Matches SteamCMD's progress lines, e.g.:
#   Update state (0x61) downloading, progress: 54.11 (2789182242 / 5154574210)
UPDATE_PROGRESS_RE = re.compile(
    r"Update state \(0x[0-9a-fA-F]+\) ([\w\s]+?), progress: ([\d.]+) \((\d+) / (\d+)\)"
)
UPDATE_READY_RE = re.compile(r"REST API\(\d+\) port is open")
UPDATE_CRASH_RE = re.compile(r"LowLevelFatalError|Segmentation fault")


def _wait_for_update_completion(timeout=1200):
    """`docker compose up -d` returning just means Docker started the
    container — the actual work (SteamCMD verifying/downloading the game
    itself, which can be several GB) happens asynchronously inside it
    afterward and used to be invisible to this job entirely, which is why
    the panel could show "Succeeded" while the real update was still
    downloading in the background.

    Tails ONLY new log output (--tail 0, same fix as the crash-detection
    false-positive earlier tonight — matching stale lines from a previous
    run is exactly the bug to avoid here) until the server is confirmed
    back up, a crash is seen, or `timeout` elapses.
    """
    deadline = time.time() + timeout
    proc = subprocess.Popen(
        ["sudo", "docker", "logs", "-f", "--tail", "0", CONTAINER],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    _job_append("update", "\ndocker logs stream ended unexpectedly.\n")
                    return False
                continue
            clean = ANSI_RE.sub("", line)

            m = UPDATE_PROGRESS_RE.search(clean)
            if m:
                stage, percent, done_bytes, total_bytes = m.groups()
                # Progress goes in the structured field, not the text log —
                # SteamCMD emits one of these every couple seconds, and
                # appending every line would flood the log box for the
                # entire download.
                _job_set_progress("update", float(percent), stage.strip(), int(done_bytes), int(total_bytes))
                continue

            if UPDATE_READY_RE.search(clean):
                _job_append("update", clean)
                return True
            if UPDATE_CRASH_RE.search(clean):
                _job_append("update", clean)
                return False
        _job_append("update", f"\nTimed out after {timeout}s waiting for the server to finish starting.\n")
        return False
    finally:
        proc.terminate()


def _run_update_job():
    ok = True
    try:
        rc, out = run_cmd(["sudo", "docker", "compose", "pull"], cwd=COMPOSE_DIR, timeout=600)
        _job_append("update", out)
        if rc != 0:
            ok = False
        else:
            # --force-recreate: without it, `up -d` is a no-op whenever the
            # Docker image itself didn't change, even if Palworld shipped a
            # game patch — the community image doesn't rebuild on every
            # patch day. That left "Update" silently doing nothing on a
            # real update, same failure shape as the UPDATE_ON_START bug.
            rc, out = run_cmd(["sudo", "docker", "compose", "up", "-d", "--force-recreate"],
                               cwd=COMPOSE_DIR, timeout=120)
            _job_append("update", out)
            if rc != 0:
                ok = False
            else:
                _job_append("update", "\nContainer restarted — waiting for the game server to verify/update and come back up...\n")
                ok = _wait_for_update_completion()
    except Exception as e:
        _job_append("update", f"\nEXCEPTION: {e}\n")
        ok = False
    _job_finish("update", ok)


@app.route("/api/server/update", methods=["POST"])
def api_server_update():
    if not _job_start("update"):
        return jsonify({"error": "update already running"}), 409
    threading.Thread(target=_run_update_job, daemon=True).start()
    return jsonify({"started": True})


def _run_reboot_job():
    ok = True
    try:
        pending = _load_pending_config()
        if pending:
            _job_append("reboot", f"Applying {len(pending)} queued setting(s): {', '.join(pending)}\n")
            _apply_compose_changes(pending)
            _save_pending_config({})
        else:
            _job_append("reboot", "No queued world-config changes — restarting as-is.\n")
        rc, out = run_cmd(["sudo", "docker", "compose", "up", "-d"], cwd=COMPOSE_DIR, timeout=120)
        _job_append("reboot", out)
        ok = rc == 0
    except Exception as e:
        _job_append("reboot", f"\nEXCEPTION: {e}\n")
        ok = False
    _job_finish("reboot", ok)


@app.route("/api/server/reboot", methods=["POST"])
def api_server_reboot():
    """Restart the server, applying any queued world-config changes first.

    Refuses to run if players are online unless `force` is set — the
    frontend is expected to show who's online and get explicit confirmation
    before retrying with force. This mirrors update/restore: the backend
    enforces the online-player check itself (rather than trusting the
    frontend to always ask) since this is exactly the kind of action a
    stray click shouldn't be able to trigger silently.
    """
    body = request.get_json(force=True) or {}
    force = bool(body.get("force"))

    if not force:
        try:
            online = [p["name"] for p in rest_call("GET", "/players").get("players", [])]
        except Exception:
            online = []
        if online:
            return jsonify({"error": "players_online", "players": online}), 409

    if not _job_start("reboot"):
        return jsonify({"error": "reboot already running"}), 409
    threading.Thread(target=_run_reboot_job, daemon=True).start()
    return jsonify({"started": True})


def _run_backup_job():
    ok = True
    try:
        rc, out = run_cmd(["sudo", "docker", "exec", CONTAINER, "bash", "/usr/local/bin/backup"], timeout=120)
        _job_append("backup", out)
        ok = rc == 0
    except Exception as e:
        _job_append("backup", f"\nEXCEPTION: {e}\n")
        ok = False
    _job_finish("backup", ok)


@app.route("/api/server/backup", methods=["POST"])
def api_server_backup():
    if not _job_start("backup"):
        return jsonify({"error": "backup already running"}), 409
    threading.Thread(target=_run_backup_job, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/server/backups")
def api_server_backups():
    entries = []
    for directory, kind in [(BACKUP_DIR, "daily"), (ARCHIVE_DIR, "archive")]:
        if not os.path.isdir(directory):
            continue
        for fn in os.listdir(directory):
            if not BACKUP_NAME_RE.match(fn):
                continue
            fp = os.path.join(directory, fn)
            st = os.stat(fp)
            entries.append({
                "filename": fn,
                "kind": kind,
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return jsonify({"backups": entries})


def _resolve_backup_path(filename, kind):
    # Filename is matched against a strict pattern (no "/", no "..") before
    # ever touching the filesystem, and the resolved path is re-checked to
    # land exactly inside the expected directory — belt and suspenders
    # against path traversal via a crafted filename.
    if not filename or not BACKUP_NAME_RE.match(filename):
        return None
    base_dir = {"daily": BACKUP_DIR, "archive": ARCHIVE_DIR}.get(kind)
    if base_dir is None:
        return None
    path = os.path.join(base_dir, filename)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(base_dir):
        return None
    return path if os.path.isfile(path) else None


def _run_restore_job(filename, kind):
    ok = True
    try:
        backup_path = _resolve_backup_path(filename, kind)
        if not backup_path:
            _job_append("restore", f"Backup not found: {filename} ({kind})\n")
            _job_finish("restore", False)
            return

        # Safety net #1: snapshot current state before changing anything. If
        # this fails, abort — better to refuse the restore than proceed
        # without a way back.
        _job_append("restore", "Taking safety backup of current state before restoring...\n")
        rc, out = run_cmd(["sudo", "docker", "exec", CONTAINER, "bash", "/usr/local/bin/backup"], timeout=120)
        _job_append("restore", out)
        if rc != 0:
            _job_append("restore", "Safety backup failed — aborting restore without touching anything.\n")
            _job_finish("restore", False)
            return

        _job_append("restore", "Stopping server...\n")
        rc, out = run_cmd(["sudo", "docker", "compose", "stop"], cwd=COMPOSE_DIR, timeout=60)
        _job_append("restore", out)
        if rc != 0:
            ok = False

        # Safety net #2: rename rather than delete the live save, so a bad
        # restore is still recoverable by hand even if the safety backup
        # above somehow turns out to be unusable.
        if os.path.isdir(SAVED_DIR):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            moved_to = f"{SAVED_DIR}_prerestore_{ts}"
            os.rename(SAVED_DIR, moved_to)
            _job_append("restore", f"Moved current save to {moved_to}\n")

        _job_append("restore", f"Extracting {backup_path}...\n")
        rc, out = run_cmd(["tar", "-xzf", backup_path, "-C", PAL_DIR], timeout=120)
        _job_append("restore", out)
        if rc != 0:
            ok = False

        _job_append("restore", "Starting server...\n")
        rc, out = run_cmd(["sudo", "docker", "compose", "up", "-d"], cwd=COMPOSE_DIR, timeout=60)
        _job_append("restore", out)
        if rc != 0:
            ok = False
    except Exception as e:
        _job_append("restore", f"\nEXCEPTION: {e}\n")
        ok = False
    _job_finish("restore", ok)


@app.route("/api/server/restore", methods=["POST"])
def api_server_restore():
    body = request.get_json(force=True)
    filename = body.get("filename")
    kind = body.get("kind")
    if not _resolve_backup_path(filename, kind):
        return jsonify({"error": "backup not found"}), 404
    if not _job_start("restore"):
        return jsonify({"error": "restore already running"}), 409
    threading.Thread(target=_run_restore_job, args=(filename, kind), daemon=True).start()
    return jsonify({"started": True})


# --- Valheim (optional secondary server) ------------------------------------
#
# Valheim exposes no RCON/REST, so every signal here is scraped from Docker or
# from the server's own log lines:
#   "Connections 3 ZDOS:1842 sent:.. recv:.."  -> authoritative player COUNT,
#      but only emitted every ~10 minutes, so it's reported with its own age.
#   "Got character ZDOID from Dan : -123:4"    -> character NAME, emitted on
#      spawn/respawn. Names are the only identity the log gives us.
VALHEIM_CONN_RE = re.compile(r"Connections (\d+) ZDOS:(\d+)")
VALHEIM_ZDOID_RE = re.compile(r"Got character ZDOID from (.+?) : [-\d]+:\d+")
# Unity spam that says nothing about server health — dropped from the log view
# so the useful lines aren't buried.
VALHEIM_LOG_NOISE_RE = re.compile(
    r"(shader|image effect|Fallback handler|Unloading|\bGC\b|preloaded)", re.I
)
# Valheim's list files hold one SteamID64 per line, with // comments. Anything
# written back is validated against this — these files are read by the game
# server, so never write an unvalidated string into them.
VALHEIM_STEAMID_RE = re.compile(r"^\d{5,20}$")
VALHEIM_LISTS = {
    "admin": "adminlist.txt",
    "banned": "bannedlist.txt",
    "permitted": "permittedlist.txt",
}


# --- Valheim world modifiers -------------------------------------------------
# Valheim 0.217+ takes world modifiers as launch arguments, and this image
# appends $SERVER_ARGS verbatim to the server command line. So the whole
# "god mode" surface is really just one env var we compose and decompose:
#
#   -preset hammer -modifier combat veryeasy -setkey nobuildcost
#
# Anything in SERVER_ARGS we don't recognise is preserved verbatim as `extra`,
# so hand-added flags survive a round trip through the panel.
VALHEIM_MODIFIERS = {
    "combat": ["veryeasy", "easy", "normal", "hard", "veryhard"],
    "deathpenalty": ["casual", "veryeasy", "easy", "normal", "hard", "hardcore"],
    "resources": ["muchless", "less", "normal", "more", "muchmore", "most"],
    "raids": ["none", "muchless", "less", "normal", "more", "muchmore"],
    "portals": ["casual", "normal", "hard", "veryhard"],
}
# Boolean world keys, set with -setkey. Presence = on; there is no "off" form,
# so turning one off means omitting it entirely.
VALHEIM_KEYS = ["nobuildcost", "passivemobs", "playerevents", "nomap"]
VALHEIM_PRESETS = ["normal", "casual", "easy", "hard", "hardcore", "immersive", "hammer"]

# Settings that live as plain compose env vars rather than launch args.
VALHEIM_SETTINGS = [
    {"key": "SERVER_NAME", "type": "string", "label": "Server name"},
    {"key": "WORLD_NAME", "type": "string", "label": "World name",
     "help": "Switching this loads a DIFFERENT world - the current one is not deleted, but nobody will see it until you switch back."},
    {"key": "SERVER_PASS", "type": "string", "label": "Password",
     "help": "Minimum 5 characters, and it may not appear inside the server name."},
    {"key": "SERVER_PUBLIC", "type": "enum", "label": "Listed publicly",
     "options": ["0", "1"],
     "help": "0 = unlisted (join by IP). 1 = shown in the community browser."},
]
_VALHEIM_SETTINGS_BY_KEY = {s["key"]: s for s in VALHEIM_SETTINGS}


def _valheim_parse_args(raw):
    """SERVER_ARGS string -> {preset, modifiers{}, keys[], extra}."""
    tokens = str(raw or "").split()
    preset, modifiers, keys, extra = None, {}, [], []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "-preset" and i + 1 < len(tokens):
            preset = tokens[i + 1]
            i += 2
        elif t == "-modifier" and i + 2 < len(tokens):
            name, value = tokens[i + 1], tokens[i + 2]
            if name in VALHEIM_MODIFIERS:
                modifiers[name] = value
            else:
                extra.extend([t, name, value])
            i += 3
        elif t == "-setkey" and i + 1 < len(tokens):
            if tokens[i + 1] in VALHEIM_KEYS:
                keys.append(tokens[i + 1])
            else:
                extra.extend([t, tokens[i + 1]])
            i += 2
        else:
            extra.append(t)
            i += 1
    return {"preset": preset, "modifiers": modifiers, "keys": keys,
            "extra": " ".join(extra)}


def _valheim_build_args(cfg):
    """{preset, modifiers{}, keys[], extra} -> SERVER_ARGS string."""
    parts = []
    if cfg.get("preset"):
        parts += ["-preset", cfg["preset"]]
    for name, value in (cfg.get("modifiers") or {}).items():
        # "normal" is the engine default; omitting it keeps the arg list short
        # and makes "unset" and "explicitly normal" behave identically.
        if value and value != "normal":
            parts += ["-modifier", name, value]
    for key in (cfg.get("keys") or []):
        parts += ["-setkey", key]
    extra = (cfg.get("extra") or "").strip()
    if extra:
        parts.append(extra)
    return " ".join(parts)


def _valheim_validate_cfg(cfg):
    """Returns an error string, or None if the config is safe to write.

    This ends up on a shell-expanded command line ($SERVER_ARGS is unquoted in
    the image's launcher), so every value is checked against a fixed allow-list
    and `extra` is restricted to plain flag characters. Nothing user-supplied
    reaches the command line unvalidated.
    """
    preset = cfg.get("preset")
    if preset and preset not in VALHEIM_PRESETS:
        return f"unknown preset: {preset}"
    for name, value in (cfg.get("modifiers") or {}).items():
        if name not in VALHEIM_MODIFIERS:
            return f"unknown modifier: {name}"
        if value and value not in VALHEIM_MODIFIERS[name]:
            return f"invalid value for {name}: {value}"
    for key in (cfg.get("keys") or []):
        if key not in VALHEIM_KEYS:
            return f"unknown key: {key}"
    if not re.fullmatch(r"[A-Za-z0-9 _.:\-]*", cfg.get("extra") or ""):
        return "extra args may only contain letters, digits, spaces, - _ . :"
    return None


def _valheim_guard():
    """None if Valheim support is on, else a ready-to-return 404 response."""
    if not VALHEIM_ENABLED:
        return jsonify({"error": "valheim support not configured"}), 404
    return None


def _valheim_run(cmd, timeout=20):
    """run_cmd, but never raises — a missing docker binary, a missing
    container or a timeout should degrade the Valheim tab, not 500 the panel
    (which is shared with the Palworld tabs)."""
    try:
        return run_cmd(cmd, timeout=timeout)
    except Exception as e:
        return 1, f"ERROR: {e}\n"


def _valheim_logs(tail=800):
    rc, out = _valheim_run(
        ["sudo", "docker", "logs", "--tail", str(tail), VALHEIM_CONTAINER], timeout=20
    )
    return out


def _valheim_list_path(kind):
    """Resolve a list file, refusing anything outside the config dir."""
    name = VALHEIM_LISTS.get(kind)
    if not name:
        return None
    path = os.path.realpath(os.path.join(VALHEIM_CONFIG_DIR, name))
    if os.path.dirname(path) != os.path.realpath(VALHEIM_CONFIG_DIR):
        return None
    return path


def _valheim_read_list(kind):
    path = _valheim_list_path(kind)
    if not path or not os.path.exists(path):
        return []
    ids = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            entry = line.split("//")[0].strip()
            if VALHEIM_STEAMID_RE.match(entry):
                ids.append(entry)
    return ids


def _valheim_write_list(kind, ids):
    path = _valheim_list_path(kind)
    if not path:
        return False
    header = f"// {kind} list - one SteamID64 per line. Managed by the admin panel.\n"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(header)
        for entry in ids:
            f.write(entry + "\n")
    os.replace(tmp, path)  # atomic: the game server may read this at any moment
    return True


@app.route("/api/valheim/status")
def api_valheim_status():
    if not VALHEIM_ENABLED:
        return jsonify({"enabled": False})

    # .State.Health is absent when the image ships no healthcheck (this one
    # doesn't), so it's queried separately rather than in one template that
    # would fail to parse wholesale.
    rc, out = _valheim_run(
        ["sudo", "docker", "inspect", "-f",
         "{{.State.Status}}|{{.State.StartedAt}}|{{.State.Running}}",
         VALHEIM_CONTAINER],
        timeout=15,
    )
    status, started_at, running = "missing", None, False
    for line in out.splitlines():
        if "|" in line and not line.startswith("$"):
            parts = line.strip().split("|")
            if len(parts) >= 3:
                status, started_at, running = parts[0], parts[1], parts[2] == "true"
            break

    cpu = mem = None
    if running:
        rc2, out2 = _valheim_run(
            ["sudo", "docker", "stats", "--no-stream", "--format",
             "{{.CPUPerc}}|{{.MemUsage}}", VALHEIM_CONTAINER],
            timeout=25,
        )
        for line in out2.splitlines():
            if "|" in line and not line.startswith("$"):
                cpu, mem = line.strip().split("|", 1)
                break

    players, zdos, characters = None, None, []
    if running:
        logs = _valheim_logs()
        for m in VALHEIM_CONN_RE.finditer(logs):
            players, zdos = int(m.group(1)), int(m.group(2))  # last match wins
        seen = []
        for m in VALHEIM_ZDOID_RE.finditer(logs):
            name = m.group(1).strip()
            if name and name not in seen:
                seen.append(name)
        characters = seen[-10:]

    return jsonify({
        "enabled": True,
        "container": VALHEIM_CONTAINER,
        "status": status,
        "running": running,
        "started_at": started_at,
        "cpu": cpu,
        "mem": mem,
        "players": players,
        "zdos": zdos,
        "characters": characters,
    })


@app.route("/api/valheim/logs")
def api_valheim_logs():
    guard = _valheim_guard()
    if guard:
        return guard
    raw = _valheim_logs(tail=400)
    lines = [
        ln for ln in raw.splitlines()
        if ln.strip() and not ln.startswith("$") and not VALHEIM_LOG_NOISE_RE.search(ln)
    ]
    return jsonify({"lines": lines[-120:]})


@app.route("/api/valheim/lists")
def api_valheim_lists_get():
    guard = _valheim_guard()
    if guard:
        return guard
    return jsonify({kind: _valheim_read_list(kind) for kind in VALHEIM_LISTS})


@app.route("/api/valheim/lists", methods=["POST"])
def api_valheim_lists_post():
    guard = _valheim_guard()
    if guard:
        return guard
    body = request.get_json(force=True)
    kind = body.get("kind")
    action = body.get("action")
    steamid = str(body.get("steamid", "")).strip()

    if kind not in VALHEIM_LISTS:
        return jsonify({"error": "unknown list"}), 400
    if action not in ("add", "remove"):
        return jsonify({"error": "action must be add or remove"}), 400
    if not VALHEIM_STEAMID_RE.match(steamid):
        return jsonify({"error": "SteamID must be 5-20 digits"}), 400

    ids = _valheim_read_list(kind)
    if action == "add":
        if steamid in ids:
            return jsonify({"error": "already in list"}), 409
        ids.append(steamid)
    else:
        if steamid not in ids:
            return jsonify({"error": "not in list"}), 404
        ids = [i for i in ids if i != steamid]

    if not _valheim_write_list(kind, ids):
        return jsonify({"error": "could not write list"}), 500
    return jsonify({"ok": True, kind: ids})


@app.route("/api/valheim/backups")
def api_valheim_backups():
    guard = _valheim_guard()
    if guard:
        return guard
    # The backup directory only appears after the first scheduled run, so a
    # missing dir is normal-and-not-an-error here.
    out = []
    for root in (os.path.join(VALHEIM_CONFIG_DIR, "backups"),
                 os.path.join(VALHEIM_CONFIG_DIR, "worlds_local")):
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            if not (name.endswith(".tar.gz") or name.endswith(".zip")
                    or "_backup_" in name):
                continue
            st = os.stat(path)
            out.append({
                "name": name,
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                "dir": os.path.basename(root),
            })
    out.sort(key=lambda b: b["modified"], reverse=True)
    return jsonify({"backups": out[:40]})


# --- Valheim live player tracking -------------------------------------------
# There is no query protocol to ask (A2S doesn't answer on an unlisted server),
# and the server's own "Connections N" line only lands every ~10 minutes. So a
# background thread follows the log and reconstructs sessions from three lines:
#
#   Got connection SteamID 7656119...      -> someone connected
#   Got character ZDOID from Dan : 123:4   -> that connection's character name
#   Closing socket 7656119...              -> they left
#
# The name arrives on a SEPARATE line from the SteamID, so attribution is
# best-effort: a ZDOID is credited to the most recent connection that doesn't
# have a name yet. With people joining seconds apart that can mis-pair, which
# is why the DB stores the steamid as the identity and the name is a label.
VALHEIM_CONNECT_RE = re.compile(r"Got connection SteamID (\d+)")
VALHEIM_CLOSING_RE = re.compile(r"Closing socket (\d+)")
VALHEIM_DISCONNECT_RE = re.compile(r"Got disconnect from user (\d+)")

_valheim_online = {}          # steamid -> {"name": str|None, "since": iso}
_valheim_online_lock = threading.Lock()
VALHEIM_DB = os.path.join(DATA_DIR, "valheim_events.db")


def _valheim_db():
    conn = sqlite3.connect(VALHEIM_DB, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " steamid TEXT NOT NULL,"
        " name TEXT,"
        " joined_ts REAL NOT NULL,"
        " left_ts REAL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vh_join ON sessions(joined_ts)")
    return conn


def _valheim_handle_line(line):
    now = time.time()
    m = VALHEIM_CONNECT_RE.search(line)
    if m:
        steamid = m.group(1)
        with _valheim_online_lock:
            _valheim_online[steamid] = {
                "name": None,
                "since": datetime.now(timezone.utc).isoformat(),
            }
        conn = _valheim_db()
        with conn:
            conn.execute(
                "INSERT INTO sessions (steamid, name, joined_ts) VALUES (?, NULL, ?)",
                (steamid, now),
            )
        conn.close()
        return

    m = VALHEIM_ZDOID_RE.search(line)
    if m:
        name = m.group(1).strip()
        with _valheim_online_lock:
            # Credit the newest still-unnamed connection.
            target = None
            for steamid, info in _valheim_online.items():
                if info["name"] is None:
                    target = steamid
            if target is None and _valheim_online:
                # Respawn of someone already named - keep the mapping current.
                target = next(iter(_valheim_online))
            if target:
                _valheim_online[target]["name"] = name
                steamid = target
            else:
                steamid = None
        if steamid:
            conn = _valheim_db()
            with conn:
                conn.execute(
                    "UPDATE sessions SET name = ? WHERE id = ("
                    "  SELECT id FROM sessions WHERE steamid = ? AND left_ts IS NULL"
                    "  ORDER BY joined_ts DESC LIMIT 1)",
                    (name, steamid),
                )
            conn.close()
        return

    m = VALHEIM_CLOSING_RE.search(line) or VALHEIM_DISCONNECT_RE.search(line)
    if m:
        steamid = m.group(1)
        with _valheim_online_lock:
            _valheim_online.pop(steamid, None)
        conn = _valheim_db()
        with conn:
            conn.execute(
                "UPDATE sessions SET left_ts = ? WHERE id = ("
                "  SELECT id FROM sessions WHERE steamid = ? AND left_ts IS NULL"
                "  ORDER BY joined_ts DESC LIMIT 1)",
                (now, steamid),
            )
        conn.close()


def _valheim_tail_loop():
    """Follow the container log forever, restarting the follow if it dies.

    Uses --since so a restarted panel doesn't replay old joins as new ones.
    """
    while True:
        try:
            since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            proc = subprocess.Popen(
                ["sudo", "docker", "logs", "-f", "--since", since, VALHEIM_CONTAINER],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in proc.stdout:
                try:
                    _valheim_handle_line(line)
                except Exception:
                    pass  # one malformed line must never kill the tailer
        except Exception:
            pass
        time.sleep(15)  # container down / docker hiccup - back off and retry


# Valheim stamps its own lines "08/04/2026 20:20:16", which (unlike the syslog
# prefix) carries the year, so it can be turned into a real timestamp.
VALHEIM_TS_RE = re.compile(r"(\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})")


def _valheim_line_ts(line):
    m = VALHEIM_TS_RE.search(line)
    if not m:
        return None
    try:
        return datetime.strptime(
            m.group(1), "%m/%d/%Y %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def _valheim_bootstrap_online():
    """Rebuild the online set by replaying recent log history.

    The tailer only follows NEW lines, so without this a player who connected
    before the panel last restarted would never appear as online - the panel
    would show "nobody online" while the server reported a live connection.
    Replays into memory (and re-opens a session row) rather than re-emitting
    every historical join.
    """
    rc, logs = _valheim_run(
        ["sudo", "docker", "logs", "--tail", "20000", VALHEIM_CONTAINER], timeout=60)
    live = {}
    for line in logs.splitlines():
        m = VALHEIM_CONNECT_RE.search(line)
        if m:
            live[m.group(1)] = {"name": None, "ts": _valheim_line_ts(line)}
            continue
        m = VALHEIM_ZDOID_RE.search(line)
        if m:
            name = m.group(1).strip()
            target = None
            for sid, info in live.items():
                if info["name"] is None:
                    target = sid
            if target is None and live:
                target = next(reversed(list(live)))
            if target:
                live[target]["name"] = name
            continue
        m = VALHEIM_CLOSING_RE.search(line) or VALHEIM_DISCONNECT_RE.search(line)
        if m:
            live.pop(m.group(1), None)

    if not live:
        return
    now = time.time()
    with _valheim_online_lock:
        for sid, info in live.items():
            _valheim_online[sid] = {
                "name": info["name"],
                "since": datetime.fromtimestamp(
                    info["ts"] or now, timezone.utc).isoformat(),
            }
    try:
        conn = _valheim_db()
        with conn:
            for sid, info in live.items():
                conn.execute(
                    "INSERT INTO sessions (steamid, name, joined_ts) VALUES (?, ?, ?)",
                    (sid, info["name"], info["ts"] or now))
        conn.close()
    except Exception:
        pass


def start_valheim_background_threads():
    if not VALHEIM_ENABLED:
        return
    # Mark any session left open by a previous panel run as ended, so stale
    # rows don't show as permanently online...
    try:
        conn = _valheim_db()
        with conn:
            conn.execute(
                "UPDATE sessions SET left_ts = joined_ts WHERE left_ts IS NULL")
        conn.close()
    except Exception:
        pass
    # ...then re-open the ones the server actually still has connected. This
    # reads a lot of log behind a generous timeout, so it runs in the worker
    # rather than delaying the panel coming up.
    def _boot():
        try:
            _valheim_bootstrap_online()
        except Exception:
            pass
        _valheim_tail_loop()

    threading.Thread(target=_boot, daemon=True).start()


@app.route("/api/valheim/players")
def api_valheim_players():
    guard = _valheim_guard()
    if guard:
        return guard
    with _valheim_online_lock:
        online = [
            {"steamid": sid, "name": info["name"], "since": info["since"]}
            for sid, info in _valheim_online.items()
        ]
    # The server's own periodic "Connections N" line is an independent count.
    # Surfacing it means a tracking bug shows up as a visible disagreement
    # instead of the panel quietly insisting nobody is online.
    server_count = None
    try:
        for m in VALHEIM_CONN_RE.finditer(_valheim_logs(tail=400)):
            server_count = int(m.group(1))
    except Exception:
        pass
    conn = _valheim_db()
    rows = conn.execute(
        "SELECT steamid, name, joined_ts, left_ts FROM sessions"
        " ORDER BY joined_ts DESC LIMIT 50"
    ).fetchall()
    conn.close()
    recent = [
        {
            "steamid": r[0], "name": r[1],
            "joined": _iso(r[2]),
            "left": _iso(r[3]) if r[3] else None,
            "minutes": round(((r[3] or time.time()) - r[2]) / 60, 1),
        }
        for r in rows
    ]
    return jsonify({"online": online, "recent": recent,
                    "server_count": server_count})


@app.route("/api/valheim/config")
def api_valheim_config_get():
    guard = _valheim_guard()
    if guard:
        return guard
    env = _read_compose_env(VALHEIM_COMPOSE_DIR)
    return jsonify({
        "settings": VALHEIM_SETTINGS,
        "values": {s["key"]: env.get(s["key"]) for s in VALHEIM_SETTINGS},
        "args": _valheim_parse_args(env.get("SERVER_ARGS", "")),
        "catalog": {
            "modifiers": VALHEIM_MODIFIERS,
            "keys": VALHEIM_KEYS,
            "presets": VALHEIM_PRESETS,
        },
        "raw_args": env.get("SERVER_ARGS", ""),
    })


@app.route("/api/valheim/config", methods=["POST"])
def api_valheim_config_post():
    guard = _valheim_guard()
    if guard:
        return guard
    body = request.get_json(force=True) or {}
    changes = {}

    # Plain settings
    for key, value in (body.get("settings") or {}).items():
        setting = _VALHEIM_SETTINGS_BY_KEY.get(key)
        if not setting:
            return jsonify({"error": f"unknown setting: {key}"}), 400
        value = str(value)
        if setting["type"] == "enum" and value not in setting["options"]:
            return jsonify({"error": f"invalid value for {key}"}), 400
        if key == "SERVER_PASS" and len(value) < 5:
            return jsonify({"error": "password must be at least 5 characters"}), 400
        if '"' in value:
            return jsonify({"error": "values may not contain double quotes"}), 400
        changes[key] = value

    # Valheim refuses to start if the password appears inside the server name,
    # so check the merged result rather than only what changed.
    env = _read_compose_env(VALHEIM_COMPOSE_DIR)
    name = changes.get("SERVER_NAME", env.get("SERVER_NAME", "")) or ""
    pw = changes.get("SERVER_PASS", env.get("SERVER_PASS", "")) or ""
    if pw and str(pw).lower() in str(name).lower():
        return jsonify({"error": "password may not appear inside the server name"}), 400

    if "args" in body:
        err = _valheim_validate_cfg(body["args"])
        if err:
            return jsonify({"error": err}), 400
        changes["SERVER_ARGS"] = _valheim_build_args(body["args"])

    if not changes:
        return jsonify({"error": "nothing to change"}), 400

    by_key = dict(_VALHEIM_SETTINGS_BY_KEY)
    by_key["SERVER_ARGS"] = {"key": "SERVER_ARGS", "type": "string"}
    _apply_compose_changes(changes, compose_dir=VALHEIM_COMPOSE_DIR,
                           by_key=by_key, tag="valheimconfig")
    return jsonify({"ok": True, "changed": sorted(changes), "restart_required": True})


def _run_valheim_job(action):
    ok = True
    try:
        cmds = {
            "start": ["sudo", "docker", "compose", "start"],
            "stop": ["sudo", "docker", "compose", "stop"],
            "restart": ["sudo", "docker", "compose", "restart"],
            # Config edits change the compose file, and `restart` reuses the
            # existing container with its old environment — only `up -d`
            # recreates it, so applying settings has to go through this.
            "recreate": ["sudo", "docker", "compose", "up", "-d"],
        }[action]
        rc, out = run_cmd(cmds, cwd=VALHEIM_COMPOSE_DIR, timeout=600)
        _job_append("valheim", out)
        ok = rc == 0
        if ok and action in ("start", "restart", "recreate"):
            # `compose start` returns once Docker has started the container,
            # long before Valheim has loaded the world and reached Steam — the
            # same premature-success trap the Palworld update job hit. Wait for
            # the real ready line instead.
            _job_append("valheim", "\nwaiting for server to connect…\n")
            ok = _wait_for_valheim_ready()
            _job_append(
                "valheim",
                "server connected\n" if ok else "timed out waiting for connect\n",
            )
    except Exception as e:
        _job_append("valheim", f"\nERROR: {e}\n")
        ok = False
    _job_finish("valheim", ok)


def _wait_for_valheim_ready(timeout=420):
    """Tail only NEW log lines until Valheim reports it reached Steam.

    --since (not --tail N) so a stale 'Game server connected' from a previous
    boot can never be mistaken for this one's.
    """
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    deadline = time.time() + timeout
    proc = subprocess.Popen(
        ["sudo", "docker", "logs", "-f", "--since", since, VALHEIM_CONTAINER],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "Game server connected" in line:
                return True
        return False
    finally:
        proc.kill()


@app.route("/api/valheim/world")
def api_valheim_world():
    """World files on disk, with the seed name read out of the .fwl header.

    .fwl is a tiny binary: <int32 length><int32 version><len-prefixed name>
    <len-prefixed seed name>... Only the seed NAME is parsed (the printable
    part players actually share); anything unexpected yields None rather than
    guessing, since a bad parse here would be silently wrong.
    """
    guard = _valheim_guard()
    if guard:
        return guard
    worlds_dir = os.path.join(VALHEIM_CONFIG_DIR, "worlds_local")
    out = []
    if os.path.isdir(worlds_dir):
        for name in sorted(os.listdir(worlds_dir)):
            if not name.endswith(".fwl") or "_backup_" in name:
                continue
            base = name[:-4]
            fwl = os.path.join(worlds_dir, name)
            db = os.path.join(worlds_dir, base + ".db")
            seed = None
            try:
                with open(fwl, "rb") as f:
                    raw = f.read(256)
                pos = 8  # skip outer length + version
                n = raw[pos]
                pos += 1 + n          # world name (length-prefixed)
                n = raw[pos]
                pos += 1
                candidate = raw[pos:pos + n].decode("ascii")
                if candidate.isprintable():
                    seed = candidate
            except Exception:
                seed = None
            st_db = os.stat(db) if os.path.exists(db) else None
            out.append({
                "name": base,
                "seed": seed,
                "size": st_db.st_size if st_db else 0,
                "modified": (datetime.fromtimestamp(st_db.st_mtime, timezone.utc).isoformat()
                             if st_db else None),
            })
    env = _read_compose_env(VALHEIM_COMPOSE_DIR)
    return jsonify({"worlds": out, "active": env.get("WORLD_NAME")})


def _valheim_resolve_backup(filename):
    """Resolve a backup filename to a real path inside the config dir."""
    if not filename or "/" in filename or "\\" in filename:
        return None
    for sub in ("backups", "worlds_local"):
        root = os.path.realpath(os.path.join(VALHEIM_CONFIG_DIR, sub))
        path = os.path.realpath(os.path.join(root, filename))
        if os.path.dirname(path) == root and os.path.isfile(path):
            return path
    return None


def _run_valheim_restore_job(filename):
    """Stop, snapshot the current world, swap files in, start.

    Only handles Valheim's own rolling .fwl/.db pairs. A safety copy of the
    live world is taken first and the replaced files are kept (renamed), so
    this is reversible even if the chosen backup turns out to be wrong.
    """
    ok = True
    try:
        path = _valheim_resolve_backup(filename)
        if not path:
            raise RuntimeError("backup not found")

        worlds_dir = os.path.join(VALHEIM_CONFIG_DIR, "worlds_local")
        base = os.path.basename(path)
        if "_backup_auto-" not in base:
            raise RuntimeError(
                "only Valheim's own _backup_auto- world files can be restored here")
        world = base.split("_backup_auto-")[0]
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")

        _job_append("valheim", f"restoring {world} from {base}\n")
        rc, out = run_cmd(["sudo", "docker", "compose", "stop"],
                          cwd=VALHEIM_COMPOSE_DIR, timeout=300)
        _job_append("valheim", out)
        if rc != 0:
            raise RuntimeError("could not stop the server")

        for ext in (".fwl", ".db"):
            src = os.path.join(worlds_dir, base.replace(".fwl", ext).replace(".db", ext))
            dst = os.path.join(worlds_dir, world + ext)
            if not os.path.exists(src):
                _job_append("valheim", f"  skip {ext}: no matching backup file\n")
                continue
            if os.path.exists(dst):
                keep = f"{dst}.pre-restore-{stamp}"
                shutil.copy2(dst, keep)
                _job_append("valheim", f"  kept current {ext} as {os.path.basename(keep)}\n")
            shutil.copy2(src, dst)
            _job_append("valheim", f"  restored {ext}\n")

        rc, out = run_cmd(["sudo", "docker", "compose", "up", "-d"],
                          cwd=VALHEIM_COMPOSE_DIR, timeout=300)
        _job_append("valheim", out)
        ok = rc == 0
        if ok:
            _job_append("valheim", "waiting for server to connect…\n")
            ok = _wait_for_valheim_ready()
            _job_append("valheim",
                        "server connected\n" if ok else "timed out waiting for connect\n")
    except Exception as e:
        _job_append("valheim", f"\nERROR: {e}\n")
        ok = False
    _job_finish("valheim", ok)


@app.route("/api/valheim/restore", methods=["POST"])
def api_valheim_restore():
    guard = _valheim_guard()
    if guard:
        return guard
    filename = (request.get_json(force=True) or {}).get("filename")
    if not _valheim_resolve_backup(filename):
        return jsonify({"error": "backup not found"}), 404
    if not _job_start("valheim"):
        return jsonify({"error": "a valheim job is already running"}), 409
    threading.Thread(target=_run_valheim_restore_job, args=(filename,), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/valheim/control", methods=["POST"])
def api_valheim_control():
    guard = _valheim_guard()
    if guard:
        return guard
    action = (request.get_json(force=True) or {}).get("action")
    if action not in ("start", "stop", "restart", "recreate"):
        return jsonify({"error": "action must be start, stop, restart or recreate"}), 400
    if not _job_start("valheim"):
        return jsonify({"error": "a valheim job is already running"}), 409
    threading.Thread(target=_run_valheim_job, args=(action,), daemon=True).start()
    return jsonify({"started": True, "action": action})


# --- Minecraft ---------------------------------------------------------------
#
# The one server here with a real remote console. Everything that CHANGES state
# goes through RCON rather than editing ops.json/banned-players.json directly,
# because the server holds those in memory while running - a file edit would
# either be ignored or clobbered on shutdown. Reads come from the files (they
# are world-readable and cheaper than an RCON round trip).
MINECRAFT_JOIN_RE = re.compile(r"\]: ([A-Za-z0-9_]{1,16}) joined the game")
MINECRAFT_LEAVE_RE = re.compile(r"\]: ([A-Za-z0-9_]{1,16}) left the game")
# "There are 3 of a max of 64 players online: alice, bob" - colour codes (§x)
# are stripped before this runs.
MINECRAFT_LIST_RE = re.compile(r"There are (\d+)[^:]*?(\d+)[^:]*?(?::\s*(.*))?$")
MINECRAFT_COLOUR_RE = re.compile(r"§.")
# Commands the panel will not send, because they are either destructive in a
# way that belongs at a real console or would take the server away from the
# panel that is driving it.
MINECRAFT_BLOCKED_CMDS = {"stop", "restart", "reload"}

MINECRAFT_DB = os.path.join(DATA_DIR, "minecraft_events.db")
_minecraft_online = {}
_minecraft_online_lock = threading.Lock()


class RconError(Exception):
    pass


def _rcon_command(command, timeout=8, drain_secs=0):
    """Minimal RCON client (stdlib only).

    Packet: <int32 length><int32 id><int32 type><body NUL><NUL>
    type 3 = auth, 2 = command, 0/2 = response. A response id of -1 means the
    password was rejected.
    """
    if not MINECRAFT_RCON_PASSWORD:
        raise RconError("MINECRAFT_RCON_PASSWORD is not set")

    def pack(req_id, req_type, body):
        payload = struct.pack("<ii", req_id, req_type) + body.encode("utf-8") + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    def read(sock):
        raw = b""
        while len(raw) < 4:
            chunk = sock.recv(4 - len(raw))
            if not chunk:
                raise RconError("connection closed")
            raw += chunk
        length = struct.unpack("<i", raw)[0]
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise RconError("connection closed")
            data += chunk
        req_id, req_type = struct.unpack("<ii", data[:8])
        return req_id, req_type, data[8:-2].decode("utf-8", "replace")

    sock = socket.create_connection(
        (MINECRAFT_RCON_HOST, MINECRAFT_RCON_PORT), timeout=timeout)
    try:
        sock.sendall(pack(1, 3, MINECRAFT_RCON_PASSWORD))
        req_id, _, _ = read(sock)
        if req_id == -1:
            raise RconError("RCON authentication failed")
        sock.sendall(pack(2, 2, command))
        _, _, body = read(sock)
        parts = [body]
        # Some plugin commands (CoreProtect lookups, `version`) reply
        # immediately with "please wait" and deliver the real answer in later
        # packets. Keep reading for drain_secs so those aren't lost.
        if drain_secs:
            deadline = time.time() + drain_secs
            while time.time() < deadline:
                sock.settimeout(max(0.2, min(1.5, deadline - time.time())))
                try:
                    _, _, extra = read(sock)
                except Exception:
                    continue
                if extra.strip():
                    parts.append(extra)
        return MINECRAFT_COLOUR_RE.sub("", "\n".join(parts)).strip()
    finally:
        sock.close()



def _minecraft_console(command, wait_secs=10, done_markers=()):
    """Type a command into the server console and return what it printed.

    Used for plugin commands that reply to the sender rather than over RCON.
    The command is passed as a single argv element (no shell), and every caller
    validates its parameters, so nothing here can smuggle in a second command
    via a stray carriage return.
    """
    log_path = os.path.join(MINECRAFT_DIR, "logs", "latest.log")
    try:
        start = os.path.getsize(log_path)
    except Exception:
        start = None

    rc, out = run_cmd(
        ["sudo", "-u", MINECRAFT_USER, "screen", "-S", MINECRAFT_SCREEN,
         "-X", "stuff", command + "\r"], timeout=20)
    if rc != 0:
        raise RuntimeError(f"could not reach the server console: {out.strip()}")
    if start is None:
        return ""

    deadline = time.time() + wait_secs
    text = ""
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                f.seek(start)
                text = f.read()
        except Exception:
            break
        if any(m in text for m in done_markers):
            break
    lines = []
    for raw in text.splitlines():
        if "RCON" in raw:
            continue  # our own polling, not the answer
        lines.append(re.sub(r"^\[[^\]]*\]\s*\[[^\]]*\]:\s?", "", raw))
    return "\n".join(l for l in lines if l.strip())


def _minecraft_guard():
    if not MINECRAFT_ENABLED:
        return jsonify({"error": "minecraft support not configured"}), 404
    return None


def _minecraft_json(name):
    path = os.path.join(MINECRAFT_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _minecraft_db():
    conn = sqlite3.connect(MINECRAFT_DB, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL,"
        " joined_ts REAL NOT NULL,"
        " left_ts REAL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_join ON sessions(joined_ts)")
    return conn


def _minecraft_handle_line(line):
    now = time.time()
    m = MINECRAFT_JOIN_RE.search(line)
    if m:
        name = m.group(1)
        with _minecraft_online_lock:
            _minecraft_online[name] = datetime.now(timezone.utc).isoformat()
        conn = _minecraft_db()
        with conn:
            conn.execute("INSERT INTO sessions (name, joined_ts) VALUES (?, ?)",
                         (name, now))
        conn.close()
        return
    m = MINECRAFT_LEAVE_RE.search(line)
    if m:
        name = m.group(1)
        with _minecraft_online_lock:
            _minecraft_online.pop(name, None)
        conn = _minecraft_db()
        with conn:
            conn.execute(
                "UPDATE sessions SET left_ts = ? WHERE id = ("
                "  SELECT id FROM sessions WHERE name = ? AND left_ts IS NULL"
                "  ORDER BY joined_ts DESC LIMIT 1)",
                (now, name),
            )
        conn.close()


def _minecraft_tail_loop():
    """Follow latest.log, surviving log rotation.

    Paper rotates latest.log on restart, so this reopens when the inode
    changes rather than holding a handle to a file nobody writes to anymore.
    """
    path = os.path.join(MINECRAFT_DIR, "logs", "latest.log")
    handle, inode = None, None
    while True:
        try:
            if handle is None:
                if not os.path.exists(path):
                    time.sleep(10)
                    continue
                handle = open(path, "r", encoding="utf-8", errors="replace")
                handle.seek(0, os.SEEK_END)  # only new lines
                inode = os.fstat(handle.fileno()).st_ino
            line = handle.readline()
            if line:
                try:
                    _minecraft_handle_line(line)
                except Exception:
                    pass
                continue
            time.sleep(1)
            try:
                if os.stat(path).st_ino != inode:
                    handle.close()
                    handle = None
            except FileNotFoundError:
                handle.close()
                handle = None
        except Exception:
            try:
                if handle:
                    handle.close()
            except Exception:
                pass
            handle = None
            time.sleep(10)


def start_minecraft_background_threads():
    if not MINECRAFT_ENABLED:
        return
    try:
        conn = _minecraft_db()
        with conn:
            conn.execute(
                "UPDATE sessions SET left_ts = joined_ts WHERE left_ts IS NULL")
        conn.close()
    except Exception:
        pass
    threading.Thread(target=_minecraft_tail_loop, daemon=True).start()


@app.route("/api/minecraft/status")
def api_minecraft_status():
    if not MINECRAFT_ENABLED:
        return jsonify({"enabled": False})
    rc, out = _valheim_run(
        ["systemctl", "show", MINECRAFT_SERVICE,
         "--property=ActiveState,SubState,ExecMainStartTimestamp"], timeout=15)
    state = {}
    for line in out.splitlines():
        if "=" in line and not line.startswith("$"):
            k, v = line.split("=", 1)
            state[k.strip()] = v.strip()
    running = state.get("ActiveState") == "active"

    players, max_players, names = None, None, []
    if running:
        try:
            body = _rcon_command("list")
            m = MINECRAFT_LIST_RE.search(body)
            if m:
                players, max_players = int(m.group(1)), int(m.group(2))
                if m.group(3):
                    names = [n.strip() for n in m.group(3).split(",") if n.strip()]
        except Exception:
            pass

    props = {}
    try:
        with open(os.path.join(MINECRAFT_DIR, "server.properties"),
                  encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    # Never surface the RCON password to the browser.
                    if "password" in k:
                        continue
                    props[k.strip()] = v.strip()
    except Exception:
        pass

    return jsonify({
        "enabled": True,
        "running": running,
        "state": state.get("ActiveState"),
        "sub_state": state.get("SubState"),
        "started_at": state.get("ExecMainStartTimestamp") or None,
        "players": players,
        "max_players": max_players,
        "names": names,
        "world": props.get("level-name"),
        "motd": props.get("motd"),
        "port": props.get("server-port"),
    })


@app.route("/api/minecraft/players")
def api_minecraft_players():
    guard = _minecraft_guard()
    if guard:
        return guard
    # The log tailer only sees lines written since the panel started, so a
    # player already connected across a panel restart would be invisible.
    # RCON `list` is authoritative, so reconcile against it and keep our own
    # "since" timestamps only for players it confirms.
    authoritative = None
    try:
        body = _rcon_command("list")
        m = MINECRAFT_LIST_RE.search(body)
        if m:
            authoritative = [n.strip() for n in (m.group(3) or "").split(",")
                             if n.strip()]
    except Exception:
        pass

    with _minecraft_online_lock:
        if authoritative is not None:
            for name in list(_minecraft_online):
                if name not in authoritative:
                    _minecraft_online.pop(name, None)
            for name in authoritative:
                _minecraft_online.setdefault(
                    name, datetime.now(timezone.utc).isoformat())
        online = [{"name": n, "since": s} for n, s in _minecraft_online.items()]

    # usercache is Mojang's name<->uuid cache; it doubles as a "everyone who
    # has ever connected" roster, which is exactly the last-seen list.
    roster = []
    for entry in _minecraft_json("usercache.json"):
        if isinstance(entry, dict) and entry.get("name"):
            roster.append({"name": entry["name"], "uuid": entry.get("uuid")})

    conn = _minecraft_db()
    rows = conn.execute(
        "SELECT name, joined_ts, left_ts FROM sessions"
        " ORDER BY joined_ts DESC LIMIT 100"
    ).fetchall()
    last_seen = {
        r[0]: _iso(r[1]) for r in conn.execute(
            "SELECT name, MAX(joined_ts) FROM sessions GROUP BY name").fetchall()
    }
    conn.close()
    sessions = [
        {"name": r[0], "joined": _iso(r[1]),
         "left": _iso(r[2]) if r[2] else None,
         "minutes": round(((r[2] or time.time()) - r[1]) / 60, 1)}
        for r in rows
    ]
    for entry in roster:
        entry["last_seen"] = last_seen.get(entry["name"])
    return jsonify({"online": online, "roster": roster, "sessions": sessions})


@app.route("/api/minecraft/lists")
def api_minecraft_lists():
    guard = _minecraft_guard()
    if guard:
        return guard

    def names(data, key="name"):
        return [e.get(key) for e in data if isinstance(e, dict) and e.get(key)]

    return jsonify({
        "ops": names(_minecraft_json("ops.json")),
        "banned": names(_minecraft_json("banned-players.json")),
        "whitelist": names(_minecraft_json("whitelist.json")),
    })


# Mutations go through RCON so the running server applies them immediately.
MINECRAFT_LIST_CMDS = {
    ("ops", "add"): "op {name}",
    ("ops", "remove"): "deop {name}",
    ("banned", "add"): "ban {name}",
    ("banned", "remove"): "pardon {name}",
    ("whitelist", "add"): "whitelist add {name}",
    ("whitelist", "remove"): "whitelist remove {name}",
}
MINECRAFT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


@app.route("/api/minecraft/lists", methods=["POST"])
def api_minecraft_lists_post():
    guard = _minecraft_guard()
    if guard:
        return guard
    body = request.get_json(force=True) or {}
    kind, action = body.get("kind"), body.get("action")
    name = str(body.get("name", "")).strip()
    template = MINECRAFT_LIST_CMDS.get((kind, action))
    if not template:
        return jsonify({"error": "unknown list or action"}), 400
    # Names go into an RCON command string, so they are constrained to the
    # Minecraft username charset - no spaces, no command separators.
    if not MINECRAFT_NAME_RE.match(name):
        return jsonify({"error": "invalid Minecraft username"}), 400
    try:
        out = _rcon_command(template.format(name=name))
    except Exception as e:
        return jsonify({"error": f"RCON: {e}"}), 502
    return jsonify({"ok": True, "output": out})


@app.route("/api/minecraft/command", methods=["POST"])
def api_minecraft_command():
    guard = _minecraft_guard()
    if guard:
        return guard
    command = str((request.get_json(force=True) or {}).get("command", "")).strip()
    if not command:
        return jsonify({"error": "empty command"}), 400
    if command.startswith("/"):
        command = command[1:]
    if command.split()[0].lower() in MINECRAFT_BLOCKED_CMDS:
        return jsonify({
            "error": f"'{command.split()[0]}' is blocked here - use the power "
                     f"controls so the panel can track the job"
        }), 400
    try:
        out = _rcon_command(command)
    except Exception as e:
        return jsonify({"error": f"RCON: {e}"}), 502
    trackable = command.split()[0].lower()
    return jsonify({"ok": True, "command": trackable, "output": out})


@app.route("/api/minecraft/logs")
def api_minecraft_logs():
    guard = _minecraft_guard()
    if guard:
        return guard
    path = os.path.join(MINECRAFT_DIR, "logs", "latest.log")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-150:]
    except Exception as e:
        return jsonify({"lines": [f"could not read log: {e}"]})
    return jsonify({"lines": [l.rstrip("\n") for l in lines]})


def _run_minecraft_job(action):
    ok = True
    try:
        rc, out = run_cmd(["sudo", "systemctl", action, MINECRAFT_SERVICE], timeout=300)
        _job_append("minecraft", out)
        ok = rc == 0
        if ok and action in ("start", "restart"):
            # systemctl returns as soon as the unit is active, but Paper needs
            # ~50s to load the world. Poll RCON until it actually answers.
            _job_append("minecraft", "\nwaiting for the server to accept RCON…\n")
            deadline = time.time() + 300
            ready = False
            while time.time() < deadline:
                time.sleep(5)
                try:
                    _rcon_command("list", timeout=4)
                    ready = True
                    break
                except Exception:
                    continue
            ok = ready
            _job_append("minecraft",
                        "server is up\n" if ready else "timed out waiting for RCON\n")
    except Exception as e:
        _job_append("minecraft", f"\nERROR: {e}\n")
        ok = False
    _job_finish("minecraft", ok)


@app.route("/api/minecraft/control", methods=["POST"])
def api_minecraft_control():
    guard = _minecraft_guard()
    if guard:
        return guard
    action = (request.get_json(force=True) or {}).get("action")
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "action must be start, stop or restart"}), 400
    if not _job_start("minecraft"):
        return jsonify({"error": "a minecraft job is already running"}), 409
    threading.Thread(target=_run_minecraft_job, args=(action,), daemon=True).start()
    return jsonify({"started": True, "action": action})


# --- Minecraft backups -------------------------------------------------------
# Hot backups: RCON save-off + save-all flush, tar, then save-on. That's the
# standard safe sequence - it guarantees the region files on disk are complete
# and stops the server writing mid-archive, without any downtime. The save-on
# is in a finally block because leaving saving disabled would silently lose
# every subsequent world change.
MINECRAFT_BACKUP_DIR = _env_path(
    "MINECRAFT_BACKUP_DIR", "/srv/gameservers/minecraft-backups/panel")
MINECRAFT_BACKUP_KEEP = int(os.environ.get("MINECRAFT_BACKUP_KEEP", "10"))


def _minecraft_world_name():
    try:
        with open(os.path.join(MINECRAFT_DIR, "server.properties"),
                  encoding="utf-8") as f:
            for line in f:
                if line.startswith("level-name="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return "world"


def _run_minecraft_backup_job():
    ok = True
    saving_off = False
    try:
        os.makedirs(MINECRAFT_BACKUP_DIR, exist_ok=True)
        world = _minecraft_world_name()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = os.path.join(MINECRAFT_BACKUP_DIR, f"mc-{world}-{stamp}.tar.gz")

        running = False
        try:
            _rcon_command("list", timeout=5)
            running = True
        except Exception:
            _job_append("minecraft", "server not reachable over RCON — "
                                     "taking a cold backup instead\n")

        if running:
            _job_append("minecraft", "pausing world saves…\n")
            _job_append("minecraft", "  " + _rcon_command("save-off") + "\n")
            saving_off = True
            _job_append("minecraft", "  " + _rcon_command("save-all flush",
                                                          timeout=120) + "\n")

        members = [d for d in (world, f"{world}_nether", f"{world}_the_end",
                               "plugins", "server.properties", "ops.json",
                               "banned-players.json", "whitelist.json",
                               "usercache.json", "bukkit.yml", "spigot.yml")
                   if os.path.exists(os.path.join(MINECRAFT_DIR, d))]
        _job_append("minecraft", f"archiving: {', '.join(members)}\n")
        # --warning=no-file-changed: plugins may touch their own files even
        # with world saving paused; that is a warning, not a failed backup.
        # Run as root: the world and player data are owned by the game user
        # and not group/world readable, so tar as the panel user silently
        # cannot read level.dat, players/data/*.dat or plugin temp files.
        rc, out = run_cmd(
            ["sudo", "tar", "czf", target, "--warning=no-file-changed",
             "-C", MINECRAFT_DIR] + members,
            timeout=3600,
        )
        # tar exits 1 for "file changed as we read it" — the archive is still
        # valid, so only a hard failure (2) is treated as an error.
        if rc not in (0, 1):
            _job_append("minecraft", out)
            raise RuntimeError(
                f"tar failed (rc={rc}) - no usable backup was written")
        if "Cannot open" in out or "Permission denied" in out:
            _job_append("minecraft", out)
            raise RuntimeError(
                "some files could not be read, so this archive would be "
                "incomplete - refusing to present it as a backup")
        size = os.path.getsize(target)
        _job_append("minecraft", f"wrote {os.path.basename(target)} ({size:,} bytes)\n")
    except Exception as e:
        _job_append("minecraft", f"\nERROR: {e}\n")
        ok = False
    finally:
        if saving_off:
            try:
                _job_append("minecraft", "  " + _rcon_command("save-on") + "\n")
            except Exception as e:
                # Loud, because the server would otherwise silently stop saving.
                _job_append("minecraft",
                            f"\nCRITICAL: could not re-enable world saving: {e}\n"
                            f"Run 'save-on' from the console NOW.\n")
                ok = False
    if ok:
        try:
            existing = sorted(
                (f for f in os.listdir(MINECRAFT_BACKUP_DIR)
                 if f.startswith("mc-") and f.endswith(".tar.gz")),
                reverse=True)
            for stale in existing[MINECRAFT_BACKUP_KEEP:]:
                os.remove(os.path.join(MINECRAFT_BACKUP_DIR, stale))
                _job_append("minecraft", f"pruned old backup {stale}\n")
        except Exception:
            pass
    _job_finish("minecraft", ok)


@app.route("/api/minecraft/backups")
def api_minecraft_backups():
    guard = _minecraft_guard()
    if guard:
        return guard
    out = []
    if os.path.isdir(MINECRAFT_BACKUP_DIR):
        for name in os.listdir(MINECRAFT_BACKUP_DIR):
            path = os.path.join(MINECRAFT_BACKUP_DIR, name)
            if os.path.isfile(path) and name.endswith(".tar.gz"):
                st = os.stat(path)
                out.append({
                    "name": name, "size": st.st_size,
                    "modified": datetime.fromtimestamp(
                        st.st_mtime, timezone.utc).isoformat(),
                })
    out.sort(key=lambda b: b["modified"], reverse=True)
    return jsonify({"backups": out, "dir": MINECRAFT_BACKUP_DIR,
                    "keep": MINECRAFT_BACKUP_KEEP})


@app.route("/api/minecraft/backup", methods=["POST"])
def api_minecraft_backup():
    guard = _minecraft_guard()
    if guard:
        return guard
    if not _job_start("minecraft"):
        return jsonify({"error": "a minecraft job is already running"}), 409
    threading.Thread(target=_run_minecraft_backup_job, daemon=True).start()
    return jsonify({"started": True})


def _minecraft_resolve_backup(filename):
    if not filename or "/" in filename or "\\" in filename:
        return None
    root = os.path.realpath(MINECRAFT_BACKUP_DIR)
    path = os.path.realpath(os.path.join(root, filename))
    if os.path.dirname(path) == root and os.path.isfile(path):
        return path
    return None


def _run_minecraft_restore_job(filename):
    ok = True
    try:
        path = _minecraft_resolve_backup(filename)
        if not path:
            raise RuntimeError("backup not found")
        _job_append("minecraft", f"restoring from {os.path.basename(path)}\n")

        # Safety snapshot of what we're about to overwrite, so a wrong choice
        # of backup is recoverable.
        world = _minecraft_world_name()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safety = os.path.join(MINECRAFT_BACKUP_DIR, f"mc-PRE-RESTORE-{stamp}.tar.gz")

        rc, out = run_cmd(["sudo", "systemctl", "stop", MINECRAFT_SERVICE], timeout=300)
        _job_append("minecraft", out)
        if rc != 0:
            raise RuntimeError("could not stop the server")

        members = [d for d in (world, f"{world}_nether", f"{world}_the_end", "plugins")
                   if os.path.exists(os.path.join(MINECRAFT_DIR, d))]
        if members:
            run_cmd(["sudo", "tar", "czf", safety, "--warning=no-file-changed",
                     "-C", MINECRAFT_DIR] + members, timeout=3600)
            _job_append("minecraft",
                        f"safety snapshot: {os.path.basename(safety)}\n")

        rc, out = run_cmd(["sudo", "-u", "minecraft", "tar", "xzf", path,
                           "-C", MINECRAFT_DIR], timeout=3600)
        _job_append("minecraft", out)
        if rc != 0:
            raise RuntimeError(f"extract failed (rc={rc})")
        _job_append("minecraft", "extracted\n")

        rc, out = run_cmd(["sudo", "systemctl", "start", MINECRAFT_SERVICE], timeout=300)
        _job_append("minecraft", out)
        ok = rc == 0
        if ok:
            _job_append("minecraft", "waiting for the server to accept RCON…\n")
            deadline = time.time() + 300
            ready = False
            while time.time() < deadline:
                time.sleep(5)
                try:
                    _rcon_command("list", timeout=4)
                    ready = True
                    break
                except Exception:
                    continue
            ok = ready
            _job_append("minecraft",
                        "server is up\n" if ready else "timed out waiting for RCON\n")
    except Exception as e:
        _job_append("minecraft", f"\nERROR: {e}\n")
        ok = False
    _job_finish("minecraft", ok)


@app.route("/api/minecraft/restore", methods=["POST"])
def api_minecraft_restore():
    guard = _minecraft_guard()
    if guard:
        return guard
    filename = (request.get_json(force=True) or {}).get("filename")
    if not _minecraft_resolve_backup(filename):
        return jsonify({"error": "backup not found"}), 404
    if not _job_start("minecraft"):
        return jsonify({"error": "a minecraft job is already running"}), 409
    threading.Thread(target=_run_minecraft_restore_job,
                     args=(filename,), daemon=True).start()
    return jsonify({"started": True})


# --- Minecraft plugins -------------------------------------------------------
def _plugin_meta(path):
    """Read name/version out of a plugin jar's plugin.yml (jars are zips)."""
    name = version = None
    try:
        with zipfile.ZipFile(path) as z:
            for candidate in ("plugin.yml", "paper-plugin.yml"):
                if candidate in z.namelist():
                    text = z.read(candidate).decode("utf-8", "replace")
                    for line in text.splitlines():
                        if line.startswith("name:") and not name:
                            name = line.split(":", 1)[1].strip().strip("'\"")
                        elif line.startswith("version:") and not version:
                            version = line.split(":", 1)[1].strip().strip("'\"")
                    break
    except Exception:
        pass
    return name, version


@app.route("/api/minecraft/plugins")
def api_minecraft_plugins():
    guard = _minecraft_guard()
    if guard:
        return guard
    out = []
    for enabled, folder in ((True, "plugins"), (False, "disabled-plugins")):
        root = os.path.join(MINECRAFT_DIR, folder)
        if not os.path.isdir(root):
            continue
        for fname in sorted(os.listdir(root)):
            if not fname.endswith(".jar"):
                continue
            path = os.path.join(root, fname)
            st = os.stat(path)
            name, version = _plugin_meta(path)
            out.append({
                "file": fname,
                "name": name or fname[:-4],
                "version": version,
                "enabled": enabled,
                "size": st.st_size,
                "modified": datetime.fromtimestamp(
                    st.st_mtime, timezone.utc).isoformat(),
            })
    loaded = []
    try:
        body = _rcon_command("plugins")
        # "Server Plugins (14): Bukkit Plugins: - A, B, C"
        tail = body.split(":", 2)[-1]
        loaded = [p.strip(" -\n") for p in tail.replace("\n", ",").split(",")
                  if p.strip(" -\n")]
    except Exception:
        pass
    return jsonify({"plugins": out, "loaded": loaded})


@app.route("/api/minecraft/plugins", methods=["POST"])
def api_minecraft_plugins_post():
    guard = _minecraft_guard()
    if guard:
        return guard
    body = request.get_json(force=True) or {}
    fname, action = body.get("file"), body.get("action")
    if action not in ("enable", "disable"):
        return jsonify({"error": "action must be enable or disable"}), 400
    # Filename comes from the browser and is used to build a path, so it must
    # be a bare jar name with no traversal.
    if not fname or "/" in fname or "\\" in fname or not fname.endswith(".jar"):
        return jsonify({"error": "invalid plugin file"}), 400

    src_dir = "disabled-plugins" if action == "enable" else "plugins"
    dst_dir = "plugins" if action == "enable" else "disabled-plugins"
    src = os.path.realpath(os.path.join(MINECRAFT_DIR, src_dir, fname))
    dst_root = os.path.realpath(os.path.join(MINECRAFT_DIR, dst_dir))
    if os.path.dirname(src) != os.path.realpath(os.path.join(MINECRAFT_DIR, src_dir)):
        return jsonify({"error": "invalid plugin file"}), 400
    if not os.path.isfile(src):
        return jsonify({"error": "plugin not found"}), 404

    os.makedirs(dst_root, exist_ok=True)
    rc, out = run_cmd(["sudo", "-u", "minecraft", "mv", src,
                       os.path.join(dst_root, fname)], timeout=60)
    if rc != 0:
        return jsonify({"error": f"could not move plugin: {out}"}), 500
    return jsonify({"ok": True, "restart_required": True})


# --- Minecraft / Paper updates -----------------------------------------------
# PaperMC's v2 API is retired (it 410s); v3 lives on the "fill" host and
# returns builds newest-first, each carrying its own download URL - so the URL
# is taken from the response rather than being constructed by hand.
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
PAPER_UA = "palworld-admin-panel (+https://github.com/TheWISPRer/palworld-admin-panel)"
# "This server is running Paper version 26.1.2-70-ver/26.1.2@70eaed6 ..."
PAPER_VERSION_RE = re.compile(r"Paper version (\S+?)-(\d+)-")


def _paper_installed():
    """(minecraft_version, build) for the RUNNING server.

    The build number isn't in paper.jar's version.json, so this asks the server
    itself. `version` is answered asynchronously the first time ("Checking
    version, please wait..."), hence the retry.
    """
    for attempt in range(4):
        try:
            body = _rcon_command("version", timeout=10, drain_secs=4)
        except Exception:
            break
        m = PAPER_VERSION_RE.search(body)
        if m:
            return m.group(1), m.group(2)
        time.sleep(2)
    # Fall back to the jar, which at least gives the Minecraft version.
    try:
        with zipfile.ZipFile(os.path.join(MINECRAFT_DIR, "paper.jar")) as z:
            if "version.json" in z.namelist():
                return json.loads(z.read("version.json").decode("utf-8")).get("id"), None
    except Exception:
        pass
    return None, None


def _paper_builds(mc_version):
    req = urllib.request.Request(
        f"{PAPER_API}/versions/{mc_version}/builds",
        headers={"User-Agent": PAPER_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


@app.route("/api/minecraft/update/check")
def api_minecraft_update_check():
    guard = _minecraft_guard()
    if guard:
        return guard
    mc_version, build = _paper_installed()
    latest_build = latest_channel = None
    error = None
    try:
        if mc_version:
            builds = _paper_builds(mc_version)
            if builds:
                newest = builds[0]  # v3 returns newest-first
                latest_build = newest.get("id")
                latest_channel = newest.get("channel")
    except Exception as e:
        error = str(e)
    behind = None
    if latest_build is not None and build is not None:
        try:
            behind = int(latest_build) - int(build)
        except Exception:
            behind = None
    return jsonify({
        "mc_version": mc_version,
        "installed_build": build,
        "latest_build": latest_build,
        "latest_channel": latest_channel,
        "behind": behind,
        "update_available": bool(behind and behind > 0),
        "error": error,
    })


def _run_minecraft_update_job(target_build):
    ok = True
    try:
        mc_version, current = _paper_installed()
        if not mc_version:
            raise RuntimeError("could not determine the installed Paper version")

        # Take the download URL from the API rather than assembling it: v3
        # serves jars from a content-addressed host with a hash in the path.
        url = name = None
        for b in _paper_builds(mc_version):
            if str(b.get("id")) == str(target_build):
                dl = (b.get("downloads") or {})
                entry = dl.get("server:default") or (list(dl.values()) or [None])[0]
                if entry:
                    url, name = entry.get("url"), entry.get("name")
                break
        if not url:
            raise RuntimeError(f"build {target_build} not found for {mc_version}")

        _job_append("minecraft",
                    f"downloading Paper {mc_version} build {target_build} ({name})\n")
        # Download somewhere the panel user owns; the game directory belongs
        # to the game user, so the jar is put in place with sudo below.
        tmp = os.path.join(DATA_DIR, f".paper-{target_build}.jar.part")
        req = urllib.request.Request(url, headers={"User-Agent": PAPER_UA})
        with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        size = os.path.getsize(tmp)
        if size < 1_000_000:
            os.remove(tmp)
            raise RuntimeError(f"downloaded jar looks wrong ({size} bytes)")
        _job_append("minecraft", f"downloaded {size:,} bytes\n")

        rc, out = run_cmd(["sudo", "systemctl", "stop", MINECRAFT_SERVICE], timeout=300)
        _job_append("minecraft", out)
        if rc != 0:
            raise RuntimeError("could not stop the server")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        jar = os.path.join(MINECRAFT_DIR, "paper.jar")
        if os.path.exists(jar):
            keep = os.path.join(MINECRAFT_DIR, f"paper.jar.pre-update-{stamp}")
            run_cmd(["sudo", "cp", "-p", jar, keep], timeout=120)
            _job_append("minecraft", f"kept previous jar as {os.path.basename(keep)}\n")
        rc, out = run_cmd(["sudo", "install", "-o", MINECRAFT_USER,
                           "-g", MINECRAFT_USER, "-m", "644", tmp, jar],
                          timeout=120)
        _job_append("minecraft", out)
        if rc != 0:
            raise RuntimeError("could not install the new jar")
        os.remove(tmp)

        rc, out = run_cmd(["sudo", "systemctl", "start", MINECRAFT_SERVICE], timeout=300)
        _job_append("minecraft", out)
        ok = rc == 0
        if ok:
            _job_append("minecraft", "waiting for the server to accept RCON…\n")
            deadline = time.time() + 420
            ready = False
            while time.time() < deadline:
                time.sleep(5)
                try:
                    _rcon_command("list", timeout=4)
                    ready = True
                    break
                except Exception:
                    continue
            ok = ready
            _job_append(
                "minecraft",
                "server is up on the new build\n" if ready else
                "timed out waiting for RCON - the previous jar is kept next to "
                "paper.jar if you need to roll back\n")
    except Exception as e:
        _job_append("minecraft", f"\nERROR: {e}\n")
        ok = False
    _job_finish("minecraft", ok)


@app.route("/api/minecraft/update", methods=["POST"])
def api_minecraft_update():
    guard = _minecraft_guard()
    if guard:
        return guard
    build = (request.get_json(force=True) or {}).get("build")
    if not re.fullmatch(r"\d{1,6}", str(build or "")):
        return jsonify({"error": "invalid build number"}), 400
    if not _job_start("minecraft"):
        return jsonify({"error": "a minecraft job is already running"}), 409
    threading.Thread(target=_run_minecraft_update_job,
                     args=(str(build),), daemon=True).start()
    return jsonify({"started": True})


# --- CoreProtect -------------------------------------------------------------
# Driven over RCON rather than by reading its database: CoreProtect can be
# backed by SQLite or MySQL (this deploy uses MySQL) and its schema is an
# internal detail, whereas `co lookup` is a stable, supported interface.
COREPROTECT_SAFE_ACTIONS = {
    "block", "+block", "-block", "click", "container", "chat", "command",
    "session", "sign", "kill", "inventory", "item", "username",
}


@app.route("/api/minecraft/coreprotect", methods=["POST"])
def api_minecraft_coreprotect():
    guard = _minecraft_guard()
    if guard:
        return guard
    body = request.get_json(force=True) or {}
    parts = ["co", "lookup"]

    user = str(body.get("user", "")).strip()
    if user:
        if not MINECRAFT_NAME_RE.match(user):
            return jsonify({"error": "invalid username"}), 400
        parts.append(f"user:{user}")

    action = str(body.get("action", "")).strip()
    if action:
        if action not in COREPROTECT_SAFE_ACTIONS:
            return jsonify({"error": "unsupported action"}), 400
        parts.append(f"action:{action}")

    time_spec = str(body.get("time", "")).strip()
    if time_spec:
        if not re.fullmatch(r"\d{1,4}[smhdw]", time_spec):
            return jsonify({"error": "time must look like 30m, 6h, 7d"}), 400
        parts.append(f"time:{time_spec}")

    radius = str(body.get("radius", "")).strip()
    if radius:
        if not re.fullmatch(r"\d{1,4}", radius):
            return jsonify({"error": "radius must be a number"}), 400
        parts.append(f"radius:#{radius}")

    if len(parts) == 2:
        return jsonify({"error": "give at least one filter"}), 400

    command = " ".join(parts)
    # CoreProtect returns lookup results to whoever asked - and over RCON that
    # is nobody: the socket only ever gets "Lookup searching. Please wait...".
    # Verified there are no follow-up RCON packets and nothing lands in the log
    # from an RCON-issued lookup. Typed at the console it works, so that is
    # what this does.
    try:
        out = _minecraft_console(
            command, wait_secs=15,
            done_markers=("Page 1/", "No results", "Lookup Results -----"))
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    out = "\n".join(
        ln for ln in out.splitlines() if "please wait" not in ln.lower()
    ) or out or "(no results)"

    return jsonify({"ok": True, "command": command, "output": out})


# --- Plugin tools (LuckPerms / WorldGuard) -----------------------------------
# Same story as CoreProtect: these answer the console, not RCON. Each entry is
# a fixed command template with typed slots, so the browser picks an operation
# from a menu rather than sending a command string - nothing here interpolates
# free text into the console.
MINECRAFT_WORLD_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,48}$")
MINECRAFT_GROUP_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
MINECRAFT_REGION_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,48}$")

PLUGIN_TOOLS = {
    # key: (template, {slot: regex}, done-markers, human label)
    "lp_groups": ("lp listgroups", {}, ("page 1 of",), "LuckPerms: list groups"),
    "lp_user_info": ("lp user {user} info", {"user": MINECRAFT_NAME_RE},
                     ("[LP]",), "LuckPerms: user info"),
    "lp_group_info": ("lp group {group} info", {"group": MINECRAFT_GROUP_RE},
                      ("[LP]",), "LuckPerms: group info"),
    "lp_user_addgroup": ("lp user {user} parent add {group}",
                         {"user": MINECRAFT_NAME_RE, "group": MINECRAFT_GROUP_RE},
                         ("[LP]",), "LuckPerms: add user to group"),
    "lp_user_removegroup": ("lp user {user} parent remove {group}",
                            {"user": MINECRAFT_NAME_RE, "group": MINECRAFT_GROUP_RE},
                            ("[LP]",), "LuckPerms: remove user from group"),
    "wg_regions": ("rg list -w {world}", {"world": MINECRAFT_WORLD_RE},
                   ("Regions", "No regions"), "WorldGuard: list regions"),
    "wg_region_info": ("rg info {region} -w {world}",
                       {"region": MINECRAFT_REGION_RE, "world": MINECRAFT_WORLD_RE},
                       ("Region:", "does not exist"), "WorldGuard: region info"),
    "wg_flags": ("rg flags {region} -w {world}",
                 {"region": MINECRAFT_REGION_RE, "world": MINECRAFT_WORLD_RE},
                 ("Flags", "does not exist"), "WorldGuard: region flags"),
}


# Suggestions are console round-trips (a couple of seconds each), so they're
# cached briefly - the dropdowns are a convenience, not live state, and groups
# and regions change rarely.
_mc_suggest_cache = {}
_mc_suggest_lock = threading.Lock()
MC_SUGGEST_TTL = 60

# "[LP] -  admin - 0"
LP_GROUP_RE = re.compile(r"^\[LP\]\s*-\s+(\S+)\s+-\s+\d+")
# "1. wispmanor"
WG_REGION_RE = re.compile(r"^\s*\d+\.\s+(\S+)")


def _mc_worlds():
    """Directories that actually contain a level.dat."""
    out = []
    try:
        for name in sorted(os.listdir(MINECRAFT_DIR)):
            path = os.path.join(MINECRAFT_DIR, name)
            if os.path.isdir(path) and os.path.exists(os.path.join(path, "level.dat")):
                out.append(name)
    except Exception:
        pass
    return out


@app.route("/api/minecraft/suggestions")
def api_minecraft_suggestions():
    guard = _minecraft_guard()
    if guard:
        return guard
    world = request.args.get("world") or _minecraft_world_name()
    if not MINECRAFT_WORLD_RE.match(world):
        return jsonify({"error": "invalid world"}), 400

    with _mc_suggest_lock:
        hit = _mc_suggest_cache.get(world)
        if hit and time.time() - hit[0] < MC_SUGGEST_TTL:
            return jsonify(hit[1])

    users = []
    for entry in _minecraft_json("usercache.json"):
        if isinstance(entry, dict) and entry.get("name"):
            users.append(entry["name"])
    with _minecraft_online_lock:
        for name in _minecraft_online:
            if name not in users:
                users.insert(0, name)

    groups, regions = [], []
    try:
        body = _minecraft_console("lp listgroups", wait_secs=8,
                                  done_markers=("page 1 of",))
        groups = [m.group(1) for m in
                  (LP_GROUP_RE.match(l) for l in body.splitlines()) if m]
    except Exception:
        pass
    try:
        body = _minecraft_console(f"rg list -w {world}", wait_secs=8,
                                  done_markers=("Regions", "No regions"))
        regions = [m.group(1) for m in
                   (WG_REGION_RE.match(l) for l in body.splitlines()) if m]
    except Exception:
        pass

    payload = {
        "world": world,
        "users": sorted(set(users)),
        "groups": groups,
        "regions": regions,
        "worlds": _mc_worlds() or [world],
    }
    with _mc_suggest_lock:
        _mc_suggest_cache[world] = (time.time(), payload)
    return jsonify(payload)


@app.route("/api/minecraft/tools")
def api_minecraft_tools_list():
    guard = _minecraft_guard()
    if guard:
        return guard
    return jsonify({
        "tools": [
            {"key": k, "label": v[3], "slots": sorted(v[1]), "template": v[0]}
            for k, v in sorted(PLUGIN_TOOLS.items())
        ],
        "default_world": _minecraft_world_name(),
    })


@app.route("/api/minecraft/tools", methods=["POST"])
def api_minecraft_tools_run():
    guard = _minecraft_guard()
    if guard:
        return guard
    body = request.get_json(force=True) or {}
    tool = PLUGIN_TOOLS.get(body.get("tool"))
    if not tool:
        return jsonify({"error": "unknown tool"}), 400
    template, slots, markers, label = tool

    values = {}
    for slot, pattern in slots.items():
        raw = str(body.get(slot, "")).strip()
        if not pattern.match(raw):
            return jsonify({"error": f"invalid {slot}"}), 400
        values[slot] = raw

    command = template.format(**values)
    try:
        out = _minecraft_console(command, wait_secs=12, done_markers=markers)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True, "label": label, "command": command,
                    "output": out or "(no output)"})


# --- Unified recent players --------------------------------------------------
@app.route("/api/recent-players")
def api_recent_players():
    """One roster across every configured server.

    Each server stores history differently - Palworld in events.db, Valheim
    keyed by SteamID, Minecraft by username - so this normalises them to
    {server, name, last_seen, sessions, minutes} rather than trying to identify
    the same human across games, which nothing in the data supports.
    """
    out = []

    try:
        conn = _events_db()
        # Events are stored as 'joined'/'left'. Walk them per player in time
        # order and pair each join with the next leave to get real playtime;
        # an unmatched join means they're still on (or the panel missed the
        # leave), so it's measured to now rather than dropped.
        rows = conn.execute(
            "SELECT name, event, ts FROM player_events ORDER BY name, ts"
        ).fetchall()
        conn.close()
        agg = {}
        now = time.time()
        for name, event, ts in rows:
            rec = agg.setdefault(
                name, {"last": 0.0, "sessions": 0, "secs": 0.0, "open": None})
            rec["last"] = max(rec["last"], ts)
            if event == "joined":
                if rec["open"] is not None:      # join with no matching leave
                    rec["secs"] += max(0.0, ts - rec["open"])
                rec["open"] = ts
                rec["sessions"] += 1
            elif event == "left" and rec["open"] is not None:
                rec["secs"] += max(0.0, ts - rec["open"])
                rec["open"] = None
        for name, rec in agg.items():
            secs = rec["secs"]
            if rec["open"] is not None:
                secs += max(0.0, now - rec["open"])
            out.append({
                "server": "palworld", "name": name,
                "last_seen": _iso(rec["last"]),
                "sessions": rec["sessions"],
                "minutes": round(secs / 60, 1) if secs else None,
            })
    except Exception:
        pass

    if VALHEIM_ENABLED:
        try:
            conn = _valheim_db()
            rows = conn.execute(
                "SELECT COALESCE(name, steamid), MAX(joined_ts), COUNT(*),"
                " SUM(COALESCE(left_ts, joined_ts) - joined_ts)"
                " FROM sessions GROUP BY COALESCE(name, steamid)").fetchall()
            conn.close()
            for name, ts, count, secs in rows:
                out.append({"server": "valheim", "name": name,
                            "last_seen": _iso(ts), "sessions": count,
                            "minutes": round((secs or 0) / 60, 1)})
        except Exception:
            pass

    if MINECRAFT_ENABLED:
        seen = {}
        try:
            conn = _minecraft_db()
            rows = conn.execute(
                "SELECT name, MAX(joined_ts), COUNT(*),"
                " SUM(COALESCE(left_ts, joined_ts) - joined_ts)"
                " FROM sessions GROUP BY name").fetchall()
            conn.close()
            for name, ts, count, secs in rows:
                seen[name] = {"server": "minecraft", "name": name,
                              "last_seen": _iso(ts), "sessions": count,
                              "minutes": round((secs or 0) / 60, 1)}
        except Exception:
            pass
        # usercache covers everyone who ever joined, including before this
        # panel started logging - they show with no last_seen rather than
        # being invisible.
        for entry in _minecraft_json("usercache.json"):
            if isinstance(entry, dict) and entry.get("name"):
                seen.setdefault(entry["name"], {
                    "server": "minecraft", "name": entry["name"],
                    "last_seen": None, "sessions": 0, "minutes": None})
        out.extend(seen.values())

    # Newest first; never-seen players (empty string) fall to the bottom.
    out.sort(key=lambda r: r["last_seen"] or "", reverse=True)
    return jsonify({"players": out})


if __name__ == "__main__":
    start_trails_background_threads()
    start_events_background_threads()
    start_valheim_background_threads()
    start_minecraft_background_threads()
    app.run(host=PANEL_BIND, port=PANEL_PORT)
