# Runbook: retire the standalone seestar_alp on heimdall

Goal: the add-on is the whole stack: scope → bundled seestar_alp → MQTT bridge →
Home Assistant. The standalone `seestar_alp` container on heimdall
(`pi@heimdall`, 192.168.10.72, compose project `seestar-ha` in
`/home/pi/seestar-ha`) is retired.

Conventions: HA is `homeassistant.iot.jaxzin.com`; the add-on slug is
`ff29c31e_seestar`; the HA device id is `seestar_s30_pro` (topics
`seestar/seestar_s30_pro/*`). **Never rename topics, unique ids or the scope
name** — the device id is the slug of the scope name, and renaming orphans the
existing entities and history. `controls_enabled` and `allow_power` are
retained OFF by design; do not arm them as part of this runbook.

> **One control client.** The scope may accept only one control-channel client
> (TCP 4700). Never run the standalone driver and the bundled driver against the
> scope at the same time: stop one before starting the other.

## 0. Prerequisites

1. This change is merged and the add-on image for **1.0.7** is published to GHCR
   (it ships the `deploy-2026.07.17` fork pin and the `interop_pem` option).
   Update the add-on to 1.0.7 in HA. Updating restarts it; in external mode it is
   already offline, so nothing is lost.
2. Understand why the scope's auth stalls **before** cutting over (next section).
   A bundled driver will hit exactly the same wall.

## 1. Pre-checks (read-only)

### 1a. Standalone driver health

```bash
ssh pi@heimdall 'docker ps --format "{{.Names}}\t{{.Status}}"; \
  docker logs --since 10m seestar_alp 2>&1 | grep -ciE "socket not initialized"; \
  docker logs --since 10m seestar_alp 2>&1 | grep -iE "get_verify_str|Exceeded allotted" | tail -3'
```

Observed on 2026-11-02: since 21:58Z the control channel logs "socket not
initialized" and every `get_verify_str` challenge times out, retrying every
~19 s, while SMB to the scope works. Cause **unknown**. Candidates
(hypotheses, not confirmed):

- another client (typically the Seestar phone app) holds the scope's single
  control channel; close the app / disconnect it from the scope and watch whether
  the retry succeeds;
- the scope is mid-session/busy, or wedged and needs a power cycle;
- the fork's reconnect loop is stuck after a transient drop (its socket never
  re-initialises); a plain restart of the driver clears that.

Do **not** open a second connection to scope port 4700 to diagnose; it can
steal the channel. Check phone apps first, then (with approval) restart the
standalone container once and observe whether auth succeeds. If it still times
out with nothing else connected, power-cycle the scope. Only proceed once the
standalone driver logs the successful handshake:

```
Requesting authentication challenge (get_verify_str)
Signing challenge and sending verify_client
```

A working baseline on the standalone driver is the best evidence the bundled one
will work too.

### 1b. HA / broker

- Add-on is `started`, version 1.0.7, options currently external with
  `alpaca_host: heimdall.lan.jaxzin.com:5555` (observed 2026-11-03; an earlier
  `davis-bridge.lan...` value no longer resolves). That value is also the rollback
  target.
- Mosquitto add-on `core_mosquitto` is started. The bundled setup uses the
  Supervisor MQTT service (leave `mqtt_host` blank), so the plaintext/TLS
  question on 1883/8883 is moot.
- Retained `seestar/bridge/availability` is `offline` before cutover (stale
  LWT); it must be `online` after.

### 1c. Network path from the add-on

The add-on is on the Supervisor bridge network (`host_network: false`), same as
the standalone container on heimdall's Docker bridge. Outbound to the scope is
NATed by the HA host, so no `host_network` is needed: the scopes are configured
by IP, so no broadcast discovery is used. Requirements: the HA host can route to
the scope (192.168.10.50) on TCP 4700/80 and UDP 4720. Confirm after step 3 via
the add-on log (see 5).

Only port 7556 is published by the add-on (the browser-side preview/video in the
SSC UI uses it). 5555 (Alpaca) and 5432 (SSC UI, via ingress) stay internal to
the container: nothing needs to reach them externally.

### 1d. Gaps between standalone and bundled (closed in this change)

| | Standalone | Bundled add-on 1.0.7 |
|---|---|---|
| seestar_alp source | fork `deploy-2026.07.17` | same (was upstream `v3.2.2`) |
| requirements.txt | n/a | identical between the two pins |
| interop PEM | file from OpenBao via `fetch-secrets.sh` | `interop_pem` option → `/run/seestar/seestar_client_key.pem` |
| `save_all_frames` | `false` | `false` (upstream default is true) |
| other `[seestar_initialization]` keys (gain 80, dither, guest mode, ...) | explicit | equal to upstream defaults |
| `lat`/`long` | 0 (guess from public IP) | 0 (default) |
| scope address | `seestar.lan.jaxzin.com` | scope `host`: use the IP (add-on DNS may not resolve `.lan`) |

Not carried over (not needed for telemetry/controls): `stport`/`sthost`
(Stellarium), `rtsp_udp`, `uitheme`, `[server] location`.

## 2. Stop the standalone driver (needs approval)

```bash
ssh pi@heimdall 'cd /home/pi/seestar-ha && docker compose stop seestar_alp'
```

`stop`, not `down`: the container stays for instant rollback.

## 3. Switch the add-on to bundled mode (needs approval)

Options to set (everything else unchanged):

```yaml
alpaca_host: ""                      # blank = bundled
scopes:
  - name: "Seestar S30 Pro"          # EXACT: slug -> device id seestar_s30_pro
    host: "192.168.10.50"
mqtt_host: ""                        # Supervisor Mosquitto service
interop_pem: <the PEM>               # see below
```

Supply the PEM without printing it or leaving it on disk, using
[`scripts/ha-supervisor-api.py`](../../scripts/ha-supervisor-api.py): it calls the
Supervisor through the HA WebSocket (`supervisor/api`) from heimdall (which has
`websocket-client`). The token and PEM go in over stdin only (never argv/files);
the PEM is sent base64-encoded, one of the three accepted forms, so it survives
the single-line option field; it is redacted in the output.

```bash
export VAULT_ADDR=https://openbao.forest-draconis.ts.net
B=$(base64 < scripts/ha-supervisor-api.py | tr -d '\n')
REQ='{"endpoint": "/addons/ff29c31e_seestar/options", "method": "post", "data": {"options": { ...FULL options object... }}}'
{ printf '%s\n' "$(bao kv get -field=token secret/homeassistant/claude-code)"
  printf '%s\n' "$REQ"
  bao kv get -field=interop_pem secret/davis-bridge/seestar_alp | base64 | tr -d '\n'; echo
} | ssh pi@heimdall "python3 -c \"import base64;exec(base64.b64decode('$B'))\""
```

Send the **full** options object to be safe (the Supervisor validates the posted
options against the schema; not verified whether omitted keys keep their
values): first `get` `/addons/ff29c31e_seestar/info` (same script, no PEM line)
and send back the current options with the changes above.

Then restart the add-on (needs approval):
`supervisor/api` endpoint `/addons/ff29c31e_seestar/restart`, method `post`.

Alternatively do both in the HA UI: Settings → Add-ons → Seestar for Home
Assistant → Configuration (paste the PEM as a single line with `\n` escapes or
as base64), then Restart.

## 4. Rollback

Any time before decommissioning (section 6):

1. Set the add-on back to external mode: `alpaca_host:
   heimdall.lan.jaxzin.com:5555`, `scopes: []`, `interop_pem: ""`, and restart it.
2. Bring the standalone driver back — stop the bundled one first (step 1 of this
   list does that by leaving bundled mode):
   ```bash
   ssh pi@heimdall 'cd /home/pi/seestar-ha && ./scripts/fetch-secrets.sh && docker compose up -d seestar_alp'
   ```
   (`docker compose up -d` without `seestar_events`, which was removed on
   2026-11-03 and must not be revived: the add-on's bridge is the sole MQTT
   publisher.)

## 5. Verify

1. Add-on log shows, in order: `Using the Supervisor MQTT service`, `Interop PEM
   supplied`, `Rendering bundled seestar_alp config.toml for 1 scope(s)`,
   `Starting bundled seestar_alp driver.`, then `Loaded interop PEM key` and the
   handshake lines from 1a. Neither `No interop PEM key available` nor repeated
   `get_verify_str` timeouts.
2. MQTT (read-only):
   - `seestar/bridge/availability` = `online` (retained);
   - `seestar/seestar_s30_pro/availability` = `online`;
   - `seestar/seestar_s30_pro/state` updating.
   ```bash
   ssh pi@heimdall 'mosquitto_sub -h homeassistant.iot.jaxzin.com -t "seestar/#" -C 5 -W 20 -v' 
   ```
   (supply broker credentials via `-u`/`-P` from the environment, not argv history;
   skip if `mosquitto_sub` is not installed there).
3. HA: the existing `seestar_s30_pro` entities are live, not greyed out;
   controls (15 buttons, 7 switches, 4 numbers, 1 select, 4 texts, live camera,
   last_command_result) are no longer unavailable. `controls_enabled` and
   `allow_power` are still OFF.
4. Reconnect behaviour (the fixed bug): restart the Mosquitto add-on and confirm
   `seestar/bridge/availability` returns to `online` without restarting the
   Seestar add-on. Optional; it briefly disrupts all MQTT.
5. Let it soak for at least one stacking session and a night of 15-minute
   `seestar-sync` runs (independent of this stack; do not touch it).

## 6. Decommission (after a clean soak; each step needs approval)

On heimdall:

```bash
ssh pi@heimdall 'cd /home/pi/seestar-ha && docker compose rm -sf seestar_alp && docker image rm jaxzin/seestar_alp:latest'
# stale livetest of this add-on's bridge in external mode:
ssh pi@heimdall 'cd /home/pi/ha-seestar/livetest && docker compose -p livetest down && docker image rm jaxzin/ha-seestar-bridge:livetest'
# or, if the project is gone: docker rm seestar_bridge; docker image rm jaxzin/ha-seestar-bridge:livetest
```

In the `fallen-leaf/seestar-ha` repo (PR there): remove the `seestar_alp` and
`seestar_events` services from `docker-compose.yml`, plus `seestar_events/`,
`scripts/fetch-secrets.sh` and the PEM docs if nothing else uses them; keep the
OpenBao secret `secret/davis-bridge/seestar_alp` (the add-on's `interop_pem`
source of truth). Archive the repo's compose when empty.

HA cleanup candidates (registry-only orphans with no state; separate, approved
change): `binary_sensor.seestar_for_home_assistant_running`,
`sensor.seestar_for_home_assistant_{version,newest_version,cpu_percent,memory_percent}`,
`switch.seestar_for_home_assistant`, `device_tracker.seestar`.

## Executed 2026-11-03: what actually happened

Cutover done in one session; verified live: `seestar/bridge/availability` and
`seestar/seestar_s30_pro/availability` `online`, `connected` on, firmware /
battery / mount mode populated (these need the authenticated command channel, so
the PEM handshake worked), controls still OFF, live-view camera unavailable
(expected: session not owned by the bridge).

Corrections to the plan above:

- **The auth stall had two causes, neither a competing client.** (1) The scope
  was mid-stacking; the firmware stalls synchronous queries during an exposure
  (known caveat), so `get_verify_str` timed out. (2) The standalone container
  could never read its PEM: `secrets/seestar_client_key.pem` was mode 600 owned
  by uid 1000 while the container ran as uid 1001 (`Permission denied`). The add-on
  runs as root, so bundled mode does not have this problem.
- **The add-on does not show the new version until the store reloads.** Call
  `supervisor/api` `/store/reload` (POST), wait until it stops answering "Supervisor
  is not ready", then `/addons/ff29c31e_seestar/update` (POST).
- **Posting options does not restart the add-on**, and a restart issued while the
  Supervisor is busy can be rejected silently. Check that the call returned
  `"success": true`; a bundled add-on shows the "Simple Seestar Controller" page
  through ingress, while an add-on still in external mode shows "External driver
  mode".
- **Logs are not reachable over the `supervisor/api` WebSocket** (text responses
  fail). Verify through MQTT (retained topics under `seestar/#`) and HA entity
  states instead; the SSC UI is reachable through an ingress session
  (`/ingress/session` POST, then the `ingress_entry` path with
  `Cookie: ingress_session=<session>`).
- Port 7556 is not mapped to the host by default; the SSC UI's browser-side
  preview needs it only if you open the UI from outside ingress.

## Out of scope

`seestar-sync` (k3s, SMB, every 15 min) is unrelated and untouched. Renaming the
add-on/repo (e.g. `seestar2mqtt`) and exposing any alp on `seestar.jaxzin.casa`
are later decisions.
