# Operating guide

Day-to-day notes for running the panel. See [README](../README.md) for
install and security.

## Service management

```bash
sudo systemctl status palworld-admin
sudo systemctl restart palworld-admin      # needed after app.py or .env changes
sudo journalctl -u palworld-admin -f
```

`index.html` is read per request, so front-end-only edits need no restart —
just reload the page.

## How it talks to the game server

Three separate channels, which is why the panel must run on the game host:

| Feature | Mechanism |
|---|---|
| Players, metrics, kick/ban/broadcast/save/shutdown | Palworld REST API, proxied via `docker exec <container> curl` |
| Chat log, join/leave history | Parsed from `docker logs`, persisted to SQLite |
| Update / Backup / Restore | `docker compose` + the image's backup script |
| Disk usage | `df` on `PANEL_DISK_PATHS` |

Port 8212 (REST) is normally unpublished, so calls tunnel through the
container rather than hitting the host. Two REST quirks the code works around,
worth knowing if you extend it:

- **Bodyless POSTs fail with HTTP 411.** Palworld's REST server rejects POSTs
  with no `Content-Length`, so even `/save` sends an explicit empty body.
- **Never use `curl -f`.** It suppresses the response body on errors, hiding
  the actual `{"errorCode","errorMessage"}` payload.

## Chat and player history

Originally these re-parsed `docker logs` on every request, which meant history
vanished as soon as Docker rotated its logs. Now a background poller tails the
log every 20s and writes new lines into `events.db`, deduplicated by a UNIQUE
constraint — so re-scanning the overlapping tail is harmless, and history
survives rotation. Retention is 90 days.

Both views support date-range queries. Recent Players in range mode shows the
full chronological join/leave log rather than one row per player.

**This only protects history from the point of install onward.** Anything that
already rotated out of Docker's logs before you deployed is gone.

## Trails

A background thread records player positions independently of any open
browser. Defaults (all editable in the panel, applied live):

| Setting | Default |
|---|---|
| Poll interval | 15s |
| Full-fidelity window | 30 min |
| Decimation threshold | 15 map-units |
| Trim interval | 5 min |
| Retention hard cap | 30 days |

Older points get decimated: walking each player's aged points in time order,
any point closer than the threshold to the last *kept* point is dropped. A
player idle at base for hours collapses to ~1 point; real travel survives
intact.

If a selected player's trail looks empty, check the time range — an offline
player's history often predates the default 30-minute window. The panel says
so inline rather than silently drawing nothing.

## Backups and restore

The panel exposes the game image's own backup script plus whatever schedule
you configure on the server side (`BACKUP_ENABLED`, `OLD_BACKUP_DAYS`).

**Restore** is deliberately awkward: it requires typing `RESTORE` to confirm,
then:

1. Takes a **fresh safety backup first** — aborts the whole restore if that fails
2. Stops the server
3. **Renames** (never deletes) the live save to `Saved_prerestore_<timestamp>`
4. Extracts the chosen backup
5. Starts the server

So there are always two independent ways back: the safety backup and the
renamed directory. Those `Saved_prerestore_*` directories accumulate — prune
them manually once a restore is confirmed good.

> Restore is the least-exercised path in this project. Treat your first real
> one as a test, ideally when nobody's online.

## A note on editing save files

Not something the panel does, but if you ever hand-edit `Level.sav`
(e.g. with [palworld-save-pal](https://github.com/oMaN-Rod/palworld-save-pal)),
two hard-won warnings:

- **Always stop the server first and re-copy the save.** It autosaves
  periodically even with nobody online, so a file you copied ten minutes ago
  is already stale — deploying an edit built from it silently rolls back
  everyone's progress.
- **Respect container capacity fields.** Item containers carry a `SlotNum`
  field alongside their slot array. Adding items past that number without
  raising it crashes the server on load with a `LowLevelFatalError` from
  `PalItemContainer.cpp` — the engine bounds-checks and aborts rather than
  failing gracefully.

## Troubleshooting

**Everything 502s / "metrics unavailable"** — the REST API isn't answering.
Check the game container is up (`docker ps`), that `RESTAPIEnabled=true`, and
that `PALWORLD_ADMIN_PASSWORD` matches the server's actual admin password.

**Timestamps are off by a fixed number of hours** — `PALWORLD_LOG_TZ` doesn't
match the game container's `TZ`. Set them equal and restart.

**Players render offset on the map** — calibration constants don't match your
`map.png`. See [MAP_CALIBRATION.md](MAP_CALIBRATION.md).

**Container shows `unhealthy` but the server is fine** — check whether a
healthcheck override is curling the game port over HTTP. Palworld's game port
is UDP, so such a check can never pass. The upstream image ships a working
`pgrep`-based check; removing the override is usually the fix.

**Update/Backup buttons do nothing** — passwordless `sudo docker` isn't
working for the service user. Check `sudo -n docker ps` as that user and see
"Permissions" in the README.
