import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
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


def _read_compose_env():
    """Parse the `environment:` block of docker-compose.yml into a dict.
    Line-based on purpose, not a YAML parser — this only needs to understand
    the one block this file actually has, and doing it this way means
    _apply_compose_changes can round-trip every comment and unrelated line
    byte-for-byte instead of risking a full YAML re-serialize.
    """
    path = os.path.join(COMPOSE_DIR, "docker-compose.yml")
    values = {}
    with open(path) as f:
        for line in f:
            m = re.match(r"^\s{6}([A-Z_]+):\s*(.+?)\s*$", line)
            if m:
                values[m.group(1)] = _parse_compose_value(m.group(2))
    return values


def _format_compose_value(key, value):
    setting = _WORLD_CONFIG_BY_KEY[key]
    if setting["type"] == "bool":
        return "true" if value else "false"
    if setting["type"] in ("int", "float"):
        return str(value)
    return f'"{value}"'  # enum / string


def _apply_compose_changes(changes):
    """Rewrite docker-compose.yml with `changes` merged into the environment
    block — updating keys that already have a line, appending any that don't,
    every other line untouched. Caller is responsible for backing up first.
    """
    path = os.path.join(COMPOSE_DIR, "docker-compose.yml")
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
                    out.append(f"{env_indent}{key}: {_format_compose_value(key, remaining.pop(key))}\n")
                else:
                    out.append(line)
                continue
            # First non-matching line ends the block — flush anything new.
            for key, value in remaining.items():
                out.append(f"{env_indent}{key}: {_format_compose_value(key, value)}\n")
            remaining = {}
            in_env = False
        out.append(line)

    if remaining:  # environment: was the last block in the file
        for key, value in remaining.items():
            out.append(f"{env_indent}{key}: {_format_compose_value(key, value)}\n")

    backup_path = f"{path}.pre-worldconfig-{datetime.now().strftime('%Y%m%d%H%M%S')}"
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
_jobs = {}  # name -> {state, log, started_at, finished_at}


def _job_start(name):
    with _jobs_lock:
        existing = _jobs.get(name)
        if existing and existing["state"] == "running":
            return False
        _jobs[name] = {
            "state": "running", "log": "",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        }
    return True


def _job_append(name, text):
    with _jobs_lock:
        _jobs[name]["log"] += text


def _job_finish(name, ok):
    with _jobs_lock:
        _jobs[name]["state"] = "success" if ok else "failed"
        _jobs[name]["finished_at"] = datetime.now(timezone.utc).isoformat()


def _job_status(name):
    with _jobs_lock:
        return dict(_jobs.get(name, {"state": "idle", "log": "", "started_at": None, "finished_at": None}))


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
    return jsonify({name: _job_status(name) for name in ("update", "backup", "restore", "reboot")})


def _run_update_job():
    ok = True
    try:
        rc, out = run_cmd(["sudo", "docker", "compose", "pull"], cwd=COMPOSE_DIR, timeout=600)
        _job_append("update", out)
        if rc != 0:
            ok = False
        else:
            rc, out = run_cmd(["sudo", "docker", "compose", "up", "-d"], cwd=COMPOSE_DIR, timeout=120)
            _job_append("update", out)
            ok = rc == 0
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


if __name__ == "__main__":
    start_trails_background_threads()
    start_events_background_threads()
    app.run(host=PANEL_BIND, port=PANEL_PORT)
