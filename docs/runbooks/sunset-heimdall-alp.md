# Runbook: seestar_alp after the heimdall sunset

The standalone `seestar_alp` container on heimdall (`pi@heimdall`, compose project
`seestar-ha` in `/home/pi/seestar-ha`) is retired. seestar_alp now runs on Home
Assistant as its own add-on, and this repo's add-on is a bridge only.

Conventions: HA is `homeassistant.iot.jaxzin.com`; the HA device id is
`seestar_s30_pro` (topics `seestar/seestar_s30_pro/*`). **Never rename topics,
unique ids or the scope name**: the device id is the slug of the scope name, and
renaming orphans the existing entities and history. `controls_enabled` and
`allow_power` are retained OFF by design; do not arm them as part of this runbook.

> **One control client.** The scope may accept only one control-channel client
> (TCP 4700). Never run a second seestar_alp (for example a restored heimdall
> container) against the scope while the HA add-on is running: stop one before
> starting the other.

## Current topology

```
Seestar S30 Pro (192.168.10.50) ──4700/80/UDP 4720──> seestar_alp add-on
                                                         │ Alpaca (5555)
                                                         ▼
                              Seestar for Home Assistant add-on (bridge) ──MQTT──> HA
```

| | Add-on | Repo | Slug | Version | Owns |
|---|---|---|---|---|---|
| Driver | Seestar Alpaca driver (seestar_alp) | [`jaxzin/ha_seestar_alp`](https://github.com/jaxzin/ha_seestar_alp) | `b193f375_seestar_alp` | 0.1.4 | scope list (`scopes`), firmware 7.18+ key (`interop_pem`), SSC web UI (ingress) |
| Bridge | Seestar for Home Assistant | this repo | `ff29c31e_seestar` | 1.1.0 | MQTT discovery, telemetry, controls, live-view camera |

- The bridge no longer bundles the driver and has no `scopes`, `interop_pem` or
  ingress options. With `alpaca_host` blank (the default), it finds the driver at
  startup through ASCOM Alpaca UDP discovery plus the Supervisor add-on list
  (`*_seestar_alp`), and accepts a candidate only if a telescope answers
  `get_event_state`. Exactly one candidate is auto-selected; with more than one, set
  `alpaca_host` (see [`seestar/DOCS.md`](../../seestar/DOCS.md)). This came in
  with 1.1.0 (#9).
- The bridge add-on runs on the Supervisor bridge network (`host_network: false`,
  verified 2026-11-07; the driver add-on's setting was not read). The HA host must
  route to the scope on TCP 4700/80 and UDP 4720.
- The scope is configured by IP, so no broadcast discovery is needed to reach it.

## History

- **2026-11-02** (#7, bridge 1.0.7): the bridge add-on ran seestar_alp bundled
  in itself, so the heimdall container could be stopped.
- **2026-11-03**: cutover executed in one session (see "Cutover learnings"). The same
  day, #9 (bridge 1.1.0) moved the driver out into its own add-on.
- **Verified 2026-11-07**: heimdall has no seestar containers (running or stopped)
  and no seestar images, and the `seestar-ha` compose project is empty. Both add-ons
  are `started` and up to date.

## Verify (read-only)

Logs are not reachable over the `supervisor/api` WebSocket (text responses fail), so
verify through HA state and MQTT.

1. Both add-ons `started` and `update_available` false: `supervisor/api`
   `/addons/ff29c31e_seestar/info` and `/addons/b193f375_seestar_alp/info`, method
   `get`, with [`scripts/ha-supervisor-api.py`](../../scripts/ha-supervisor-api.py).
   It reads the HA token from stdin (never argv or a file), so run it from a host
   that can reach HA, for example over ssh to heimdall (which has
   `websocket-client`).
2. Retained `seestar/bridge/availability` and `seestar/seestar_s30_pro/availability`
   are `online`.
3. Telemetry is live, not just retained. Check `last_reported` on
   `sensor.seestar_s30_pro_azimuth` and `_altitude`; on 2026-11-07 they were seconds
   to minutes old.
4. Firmware, battery and mount mode are populated. They need the authenticated
   command channel, so this proves the PEM handshake worked.
5. `binary_sensor.seestar_s30_pro_connected` is on;
   `switch.seestar_s30_pro_controls_enabled` and `..._allow_power_actions` are off.
6. `camera.seestar_s30_pro_live_view` is `unavailable` unless an imaging session was
   started from Home Assistant (expected: the firmware serves live frames to the
   owning client only).

Not verified on 2026-11-07: that the bridge picked the sibling add-on by discovery
rather than another candidate (check its log in the HA UI), and the driver add-on's
own `scopes` and `interop_pem` values. The Supervisor `/info` call returned empty
options for it, so read them in the add-on's Configuration page. 30 of 105 seestar
entities were `unknown` or `unavailable`; only the live-view camera is explained above.

## Operating notes

- **Changing the scope list or the PEM** is done on the driver add-on
  (`b193f375_seestar_alp`), not the bridge. Supply the PEM without printing it or
  leaving it on disk: post it through `scripts/ha-supervisor-api.py` (line 3 of stdin,
  base64, redacted in the output), or paste it in the HA UI (single line with `\n`
  escapes, or base64). Send the **full** options object (not verified whether omitted
  keys keep their values): `get` `/addons/<slug>/info` first and send back the current
  options with the change.
- **Posting options does not restart an add-on.** Restart separately
  (`/addons/<slug>/restart`, `post`), and check the reply says `"success": true`: a
  restart issued while the Supervisor is busy can be rejected silently.
- **A new add-on version is not offered until the store reloads.** Call
  `supervisor/api` `/store/reload` (POST), wait until it stops answering "Supervisor
  is not ready", then `/addons/<slug>/update` (POST).
- **Do not open a second connection to scope port 4700 to diagnose**; it can steal the
  control channel. Check phone apps first. A mid-exposure scope also stalls
  synchronous queries (known firmware caveat), which looks like an auth timeout.
- Source of truth for the PEM is OpenBao `secret/davis-bridge/seestar_alp`.
- The SSC web UI is on the driver add-on's ingress. Its browser-side preview needs
  the imaging port (7556) reachable; the bridge add-on no longer publishes it (#9).
  Whether the driver add-on publishes it was not checked.

## Cutover learnings (2026-11-03)

These still apply to any future restart of this stack.

- **The auth stall had two causes, neither a competing client.** (1) The scope was
  mid-stacking, so `get_verify_str` timed out. (2) The standalone container could
  never read its PEM: `secrets/seestar_client_key.pem` was mode 600 owned by uid 1000
  while the container ran as uid 1001 (`Permission denied`). Add-ons run as root, so
  they do not have this problem.
- Symptom of the stall on the old container: "socket not initialized" on the control
  channel with every `get_verify_str` challenge timing out and retrying about every
  19 s, while SMB to the scope worked.
- An add-on still in external mode shows an "External driver mode" ingress page
  instead of the SSC UI (this applied to bridge versions before 1.1.0).

## Rollback

There is no quick rollback any more: the heimdall container is removed. To run
seestar_alp off Home Assistant again, use the compose path in the repo root
(`docker-compose.yml`, which builds seestar_alp at the pinned fork tag), set
`alpaca_host` on the bridge to it, and **stop the driver add-on first** (one control
client). Whether `fallen-leaf/seestar-ha` still has the old compose services was not
checked.

## Remaining cleanup (each step needs approval; status not verified unless stated)

- `fallen-leaf/seestar-ha`: remove the `seestar_alp` and `seestar_events` services,
  `seestar_events/`, `scripts/fetch-secrets.sh` and the PEM docs if nothing else uses
  them; keep the OpenBao secret above. Archive the repo's compose when empty.
- HA registry orphans from the old standalone setup (registry-only, no state;
  separate change): `binary_sensor.seestar_for_home_assistant_running`,
  `sensor.seestar_for_home_assistant_{version,newest_version,cpu_percent,memory_percent}`,
  `switch.seestar_for_home_assistant`, `device_tracker.seestar`. Not re-checked
  against the current registry.

## Out of scope

`seestar-sync` (k3s, SMB, every 15 minutes) is unrelated and untouched. Renaming the
add-on or repo (for example `seestar2mqtt`) and exposing any alp on
`seestar.jaxzin.casa` are later decisions.
