# Palworld Admin Panel

A self-hosted web panel for a dockerised Palworld dedicated server: a live
player map, position history, in-game chat log, and one-click
update/backup/restore.

Built for real day-to-day server admin — everything here exists because it was
needed to run an actual server, not as a feature checklist.

> **Status:** works, in daily use, but it's a personal-scale tool. No test
> suite, no auth of its own (see [Security](#security)), and the Restore path
> has been exercised far less than the rest.

---

## What it does

**Map**
- Live player positions, polled every 1–60s (slider)
- Click to drop pins — label, category, colour, show/hide
- **Trails**: continuous position history recorded in the background whether
  or not a browser is open, with a playback scrubber. Tiered retention keeps
  it cheap indefinitely (recent history at full fidelity, older points
  decimated down to actual movement, hard cap after N days).

**Players**
- Who's online, with per-player Kick / Ban and a copy-ready teleport command
- In-game chat log and join/leave history, both persisted to SQLite so they
  survive Docker log rotation, with date-range filtering

**Admin**
- Broadcast, Save World, Shutdown (with countdown), Unban
- Server Update (`compose pull && up -d`) and Backup Now, as tracked
  background jobs with live logs
- Restore from any backup, with a type-to-confirm gate and an automatic
  pre-restore safety backup

**Stats**
- Daily active players, chat volume, time-of-day and day-of-week histograms,
  most-active-player leaderboard — all computed from the panel's own recorded
  history

---

## Requirements

- A Palworld dedicated server running in Docker (built against
  [`thijsvanloef/palworld-server-docker`](https://github.com/thijsvanloef/palworld-server-docker))
- The game server's **REST API enabled** (`RESTAPIEnabled=true`) and its
  `ADMIN_PASSWORD` set
- Linux host with `python3` (3.9+), `python3-venv`, and `docker`
- The panel must run on the **same host** as the game server — it uses
  `docker exec` / `docker logs` / `docker compose` directly

## Install

```bash
git clone https://github.com/TheWISPRer/palworld-admin-panel.git
cd palworld-admin-panel
./deploy/install.sh
```

Then:

```bash
$EDITOR .env                      # set PALWORLD_ADMIN_PASSWORD at minimum
cp your-map.png /var/lib/palworld-admin/map.png   # see docs/MAP_CALIBRATION.md
sudo systemctl start palworld-admin
```

The panel is now on `http://<host>:8300`. Re-run `./deploy/install.sh` any
time to pick up code or config changes — it's idempotent.

Every setting lives in `.env`; see [`.env.example`](.env.example), which
documents each one. The only required value is `PALWORLD_ADMIN_PASSWORD`.

### Two gotchas worth reading

**`PALWORLD_LOG_TZ` must match the game container's timezone**, not the
host's. Palworld writes log timestamps in the container's local time, so a
mismatch silently skews every "x minutes ago" in Chat and Recent Players by
the offset.

**The map image is not included.** Palworld's map is Pocketpair's artwork, so
you supply your own and calibrate it — see
[docs/MAP_CALIBRATION.md](docs/MAP_CALIBRATION.md). Without one, the map tab
shows a placeholder and everything else still works.

## Permissions

The panel shells out to `sudo docker …` for the REST API, logs, and compose
operations, so its user needs passwordless sudo for those. Rather than
granting blanket `ALL`, scope it:

```sudoers
# /etc/sudoers.d/palworld-admin   (edit with: sudo visudo -f /etc/sudoers.d/palworld-admin)
palworld-admin ALL=(root) NOPASSWD: /usr/bin/docker exec palworld *, \
                                    /usr/bin/docker logs *, \
                                    /usr/bin/docker compose *
```

Adjust the container name and the `docker` path (`command -v docker`) to
match your host.

## Security

**The panel has no login of its own, by design.** Anyone who can reach the
port can shut down, ban, and restore your server. It expects to sit behind a
reverse proxy that handles authentication.

At minimum, do both of these:

1. **Put it behind an authenticating proxy.**
   [`deploy/Caddyfile.example`](deploy/Caddyfile.example) shows a Caddy +
   [Authentik](https://goauthentik.io/) forward-auth setup restricted to one
   group.
2. **Firewall the panel port** so only the proxy can reach it. If the proxy
   runs on the same host, set `PANEL_BIND=127.0.0.1` and you're done. If it's
   on another machine:
   ```bash
   sudo ufw allow from <proxy-ip> to any port 8300 proto tcp
   ```
   Otherwise anyone on your LAN or VPN bypasses the proxy — and therefore all
   authentication — entirely.

Other notes:
- `.env` is written `600` and gitignored. It holds your server's admin
  password in plaintext, as does the game server's own compose file.
- Player-supplied text (names, chat) renders via `textContent`, never
  `innerHTML`.
- Restore filenames are validated against a strict pattern *and* re-checked to
  resolve inside the backup directory, so a crafted name can't escape it.

## Running more than one server

The panel manages the server on its own host, so run **one instance per
game server**. Give each its own checkout (or `PANEL_DATA_DIR`), `.env`, port,
and systemd unit name, then route them by subdomain or path at your proxy.

There's no built-in multi-server switcher. Aggregating several servers in one
UI would mean reaching them over the network instead of via local
`docker exec` — the REST API could work that way, but logs, backups, updates
and restores could not without an agent on each host. That's a real
rearchitecture, not a config flag, so it's deliberately out of scope.

## Layout

```
app.py                     Flask backend — REST proxy, trails, events, jobs
index.html                 Single-page frontend (no build step, no deps)
deploy/install.sh          Idempotent installer
deploy/*.service           systemd unit template
deploy/Caddyfile.example   Reverse proxy + SSO example
docs/MAP_CALIBRATION.md    Fitting a map image to in-game coordinates
docs/ADMIN_GUIDE.md        Operating notes, backups, troubleshooting
```

State (`pins.json`, `trails.db`, `events.db`, `map.png`) lives in
`PANEL_DATA_DIR`, outside the checkout, so `git pull` never touches your data.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with, endorsed by, or connected to Pocketpair. "Palworld" is
their trademark. No game assets are included in this repository.
