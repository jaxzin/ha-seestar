# Seestar for Home Assistant

Live Seestar imaging telemetry and a stacked-image preview in Home Assistant,
published as auto-discovered MQTT entities — one HA device per telescope. The app
reads your scope through the [seestar_alp](https://github.com/smart-underworld/seestar_alp)
Alpaca driver: the **seestar_alp add-on** (found automatically), or one you
already run.

## Getting started

New to this? Walk it top to bottom: check the prerequisites, install the add-on,
confirm the entities appear, then note what an idle scope looks like on the first
run.

### Before you begin

Tick all of these off first — the install assumes them:

- [ ] **Home Assistant OS or Supervised.** This is a Supervisor add-on. On **HA
  Container / Core**, or to run it on a separate machine (e.g. a dedicated
  Raspberry Pi), skip the add-on and use the
  [`docker-compose.yml`](../docker-compose.yml) path instead — see
  [Running without the Supervisor](#running-without-the-supervisor).
- [ ] **An MQTT broker.** Install the official **Mosquitto broker** add-on **and**
  add the **MQTT integration** (**Settings → Devices & Services → Add integration
  → MQTT**). The bridge auto-resolves Mosquitto's host/port/credentials from the
  Supervisor, so with those two in place you configure nothing else for MQTT.
- [ ] **A supported ZWO Seestar, on your network in station mode.** The **S30**,
  **S30 Pro**, and **S50** are supported. Power it on and, in the **Seestar
  app**, join it to your home Wi-Fi in **station mode** — *not* its default
  self-hosted **AP mode**, where the scope is its own hotspot and Home Assistant
  can't reach it. Put it on the **same subnet** as Home Assistant.
- [ ] **The scope's LAN IP.** Find it in the **Seestar app** (device/network
  settings) or your router's **DHCP client list**. You'll paste it into the
  add-on. A DHCP reservation for the scope is worth setting so the address
  doesn't move.

### Install the add-ons

Two add-ons work together: **Seestar Alpaca driver (seestar_alp)** talks to the
telescope, and **Seestar for Home Assistant** (this app) turns it into Home
Assistant entities.

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store** (on newer
   builds, **Settings → Apps**), open the **⋮** menu (top right), choose
   **Repositories**, and add both:

   ```
   https://github.com/jaxzin/ha_seestar_alp
   https://github.com/jaxzin/ha-seestar
   ```

2. Install **Seestar Alpaca driver (seestar_alp)**. On its **Configuration** tab,
   add your telescope under **Telescopes** (a name and the scope's LAN IP from the
   checklist above) and, on firmware 7.18+, the interop key. **Start** it. Its
   own [documentation](https://github.com/jaxzin/ha_seestar_alp/blob/main/seestar_alp/DOCS.md)
   covers the options.
3. Install **Seestar for Home Assistant** and **Start** it. Leave **seestar_alp
   driver** (`alpaca_host`) blank: it finds the driver add-on by itself (see
   [Finding the seestar_alp driver](#finding-the-seestar_alp-driver)).
4. Open its **Log** tab to watch it find the driver, enumerate the scope, and
   publish MQTT discovery.

### Verify it worked

- In the **Log** tab you should see the **broker connection** succeed followed by
  **`discovered`** lines — one burst per scope as it publishes its entities. If
  the log complains about MQTT, revisit the Mosquitto/MQTT-integration
  prerequisite; if it can't reach the scope, re-check station mode and the IP.
- The new device appears under **Settings → Devices & Services → MQTT** — open it
  to see the ~46 telemetry entities, the control entities, and the two cameras.
  (Entity IDs are derived from the device **name**, not the discovery ID — see
  [the note below](#entity-ids-are-derived-from-the-entity-name-not-the-discovery-id).)

### What to expect on the first run

An **idle scope that has never stacked** looks half-empty, and that's normal:

- The **Live stacked preview** camera is **blank** until the scope has a saved
  stack — the preview shows the *last* stacked image, and there isn't one yet.
- Imaging fields (target, stack state, frames, plate-solve) read **idle /
  unknown** until a session is running.

Point the scope at something and start a stacking session (from the Seestar app,
or from Home Assistant once you've armed [Controls](#controls-phase-2)); the
telemetry and preview fill in within a few seconds. Two behaviours are worth
knowing up front so they don't read as bugs: **controls and some live device-state
fields need a firmware auth key on firmware ≥ 7.18** (telemetry and the preview do
not — see [Firmware 7.18+ authentication](#firmware-718-authentication)), and
**during an exposure the slower device-state fields refresh only at idle gaps**
between frames (see [Known caveats](#known-caveats)).

## MQTT — zero-config with the Mosquitto add-on

Leave the `mqtt_*` options blank and the app uses your **Mosquitto broker
add-on** automatically: it declares `services: ["mqtt:want"]`, so the Supervisor
hands it the broker host, port, username, and password at startup. Install the
official Mosquitto add-on and you need to configure nothing else for MQTT.

**To use a different broker** (e.g. an external Mosquitto, or EMQX), set
`mqtt_host` (and `mqtt_port` / `mqtt_username` / `mqtt_password` / `mqtt_ssl` as
needed). Any `mqtt_host` you set overrides the Supervisor service. If MQTT can be
resolved from neither the Supervisor nor the options, the app fails fast with a
message telling you to install Mosquitto or set `mqtt_host`.

## Finding the seestar_alp driver

With `alpaca_host` blank (the default), the app looks for the driver at startup,
retrying for up to three minutes while the driver add-on starts:

1. **ASCOM Alpaca discovery** — a UDP broadcast on port 32227 that every Alpaca
   server answers. This finds the seestar_alp add-on over the Supervisor's
   internal network.
2. **The Supervisor add-on list** — a started add-on whose slug ends in
   `_seestar_alp`, from any repository. When it is also the one that answered
   discovery, the app uses the add-on's stable internal hostname instead of its
   container IP.

A server only counts if its Alpaca API has a telescope that answers seestar_alp's
`get_event_state` action. Other Alpaca drivers on the network are ignored. If
exactly one qualifies, the app uses it and logs which; **if more than one
qualifies, it stops and asks you to set `alpaca_host`**.

**Your own seestar_alp** (another machine, a different port, or a different
broadcast domain): set `alpaca_host` to its `host:port` (e.g.
`192.168.1.50:5555`). Discovery is skipped. Don't run two drivers against the
same scope: a Seestar serves its command channel to one client at a time.

## Options reference

| Option | Type | Default | Description |
|---|---|---|---|
| `alpaca_host` | string | `""` | Blank = find the seestar_alp add-on automatically (see [Finding the seestar_alp driver](#finding-the-seestar_alp-driver)). Set to `host:port` of a specific seestar_alp to use that one. |
| `alpaca_webui_port` | port | `5432` | The port the seestar_alp web UI is served on, where each scope's address is read (`/config.json`) for the preview. Change only for a seestar_alp rebound elsewhere. |
| `imaging_port` | port | `7556` | Port of seestar_alp's imaging server on the Alpaca host, where the Live view camera grabs its `/vid` MJPEG frames. Change only for an external seestar_alp bound to a non-stock imaging port. |
| `mqtt_host` | string | `""` | Blank = use the Mosquitto add-on via the Supervisor. Set to override with another broker. |
| `mqtt_port` | port | `1883` | Ignored while `mqtt_host` is blank (the Supervisor service supplies the port). Set `8883` for a TLS broker when overriding `mqtt_host`. |
| `mqtt_username` | string | `""` | Broker username. Leave blank with the Mosquitto add-on. |
| `mqtt_password` | password | `""` | Broker password. Leave blank with the Mosquitto add-on. |
| `mqtt_ssl` | bool | `false` | Connect to the broker over TLS. Relevant only when overriding `mqtt_host`. |
| `discovery_prefix` | string | `homeassistant` | MQTT discovery topic prefix. Must match Home Assistant's MQTT integration setting; change only if you customized it there. |
| `event_poll_sec` | int (1–3600) | `10` | How often to poll the fast Alpaca event stream for imaging telemetry. Lower is more responsive; higher is gentler on the driver. |
| `state_poll_sec` | int (1–3600) | `30` | How often to poll the slower device-state call (mount mode, focuser, battery, firmware). These fields refresh only at idle gaps during a capture — see [Behavior during active imaging](#known-caveats). |
| `preview_max_px` | int (256–4096) | `1280` | Longest-edge limit for the stacked preview before publishing; larger previews are downscaled to this. |
| `log_level` | enum | `info` | Verbosity of the bridge. One of `trace`, `debug`, `info`, `notice`, `warning`, `error`, `fatal`. Use `debug` or `trace` to diagnose a connection problem. |

## What gets published

Per telescope, one HA device with ~46 telemetry entities, the Phase-2 control
entities and their **Last command result** sensor
(see [Controls (Phase 2)](#controls-phase-2)), plus two `camera` entities: the
saved stacked preview, and a **Live view** camera fed from seestar_alp's `/vid`
stream — live only for sessions started from Home Assistant
(see [Live view camera](#live-view-camera)). Telemetry entities:

- **Cameras**: telephoto (cam 0) and wide-field (cam 1) target / state / mode /
  gain / LP-filter, plus the active camera.
- **Stacking**: stack state, stacked / dropped / total frames, exposure,
  integration time.
- **Plate solve**: RA, Dec, field of view, field rotation, focal length, stars
  detected.
- **Plan & objects**: plan name, plan running, objects in frame, catalog objects,
  last saved file.
- **Pointing & mount**: altitude, azimuth (computed from the plate solve + the
  scope's GPS, so they keep updating during capture — unlike the slower
  device-state fields; see [Behavior during active imaging](#known-caveats)),
  tracking, slewing, goto state, parked, at-home, mount mode, focuser, filter
  position.
- **Health**: connected, sensor temperature, battery, charger, storage used,
  dew heater, firmware, last alert.

The recommended way to put these entities on a dashboard is the purpose-built
companion card, [**seestar-lovelace-card**](https://github.com/jaxzin/seestar-lovelace-card)
— one scope per card, HACS-installable, no YAML wrangling. If you'd rather not use
HACS, the repo also ships a copy-paste, stock-card starter dashboard in
[`examples/stargazing.yaml`](../examples/stargazing.yaml) that maps every published
entity.

## Controls (Phase 2)

> **⚠️ These controls MOVE A PHYSICAL TELESCOPE.** A goto slews the mount, Park
> stows the arm, Shutdown powers the whole device off. Keep **Controls enabled**
> and **Allow power actions** OFF whenever you are not actively driving the
> scope, and write automations that target the safety switches **deliberately**
> — arm before commanding, disarm after — rather than leaving the scope
> permanently armed.

> **Firmware ≥ 7.18 needs an auth key for controls.** These commands use the
> scope's synchronous COMMAND channel, which firmware 7.18+ gates behind a
> challenge-response interop auth key configured in seestar_alp. Without it,
> commands are refused by the scope even with both safety switches on. Telemetry
> and the preview are unaffected. See
> [Firmware 7.18+ authentication](#firmware-718-authentication).

### Safety model — read this first

Each scope gets two safety switches, **both OFF by default**:

- **Controls enabled** gates *everything*. While it is off, every command —
  button, number, switch, select, or text — is refused before anything reaches
  the driver, so a stray automation or a mis-tap can't move the scope.
- **Allow power actions** *additionally* gates the destructive power actions:
  **Startup sequence**, **Park**, and **Shutdown**. Both switches must be on
  for those three.

A refused command is never silent: the refusal and its reason are published to
the **Last command result** sensor, e.g.
`refused: goto: controls are disabled (arm 'Controls enabled' first)`. The same
sensor shows `ok: …` for accepted commands and `error: …` when the scope itself
rejected or failed one — it is the first place to look when a button "did
nothing".

### Control entities

All controls are per-scope: arming or commanding one telescope never affects
another.

| Entity | What it does | Notes |
|---|---|---|
| **Controls enabled** (switch) | Master safety gate for every command. | Default OFF — nothing dispatches while off. |
| **Allow power actions** (switch) | Second gate for Startup sequence / Park / Shutdown. | Default OFF — both switches must be on for power actions. |
| **Imaging mode** (select) | Chooses the mode (`star` / `scenery` / `planet` / `sun` / `moon`) that **Start live view** will use. | **Value-only**: changing it does nothing until *Start live view* is pressed. Defaults to `star`. |
| **Start live view** (button) | Starts an imaging session in the selected imaging mode. | Makes this bridge the session's owning client, which is what turns the [Live view camera](#live-view-camera) on. |
| **Start stacking** (button) | Starts (restarts) stacking on the current target. | Also starts an HA-owned session. |
| **Stop** (button) | Stops the running scheduler session. | Only stops scheduler-driven sessions — see [Rough edges](#rough-edges). |
| **Start mosaic** (button) | Starts a mosaic capture. | |
| **Start spectra** (button) | Starts a spectra capture. | |
| **Goto target** (text) | Label for the goto session (e.g. `M31`). | **Name only** — seestar_alp does not resolve names to coordinates. |
| **Goto RA** (text) | Right ascension for **Goto**. | Decimal hours (`0.7123`) or sexagesimal (`0h42m44s`, `0:42:44`); 0–24 h, J2000. |
| **Goto Dec** (text) | Declination for **Goto**. | Decimal degrees (`41.269`) or sexagesimal (`+41d16m9s`, `-05:23:28`); ±90°, J2000. |
| **Goto** (button) | Slews to the stored RA/Dec, labelled with the stored target name. | **Refuses without parseable, in-range coordinates** — it never guesses where a name is. |
| **Stop goto** (button) | Aborts an in-progress goto. | |
| **Stack exposure** (number) | Sets the stacking exposure **in milliseconds** (1–60000). | ms end-to-end: solar work is ~1–5 ms; deep-sky stacking is tens of seconds (`10000`, `30000`). |
| **Gain** (number) | Sets the camera ISP gain (0–300). | The same `set_setting` call seestar_alp's own web UI makes. |
| **Focus** (number) | Nudges the focuser by a relative number of steps (−500…500). | |
| **Auto-focus** (button) | Runs the scope's auto-focus routine. | |
| **Mag declination** (number) | Magnetic-declination fudge angle for the compass calibration (±180°). | |
| **Tracking** (switch) | Turns mount tracking on or off. | |
| **Dew heater** (switch) | Turns the dew heater on or off. | ON applies a fixed power level of 90 (scale 0–100). |
| **Wide camera** (switch) | Enables/disables the wide-angle camera. | S30-series only; upstream marks the setting experimental — see [Rough edges](#rough-edges). |
| **Record video** (switch) | Starts/stops planetary video recording. | Only records during a planetary live session — see [Rough edges](#rough-edges). |
| **Plate-solve loop** (switch) | Starts/stops the polar-align plate-solve loop. | ON is a no-op on firmware > 2.47 — see [Rough edges](#rough-edges). |
| **Run plan** (text) | Runs a saved plan **by name**: imports it, then starts the scheduler. | The name resolves under seestar_alp's own `schedule/` directory (where its SSC web UI saves plans); `.json` is appended if omitted. **No paths** — a name containing `/`, `\`, `..`, or starting with `~`/`.` is refused. |
| **Pause plan** (button) | Pauses the running plan. | |
| **Continue plan** (button) | Resumes a paused plan. | |
| **Skip current target** (button) | Skips the plan's current item. | |
| **Reset current item** (button) | Resets the plan's current item. | |
| **Startup sequence** (button) | Runs the scope's startup sequence. | **Power-gated.** |
| **Park** (button) | Stows the mount arm. | **Power-gated.** Park only stows — it never powers off. |
| **Shutdown** (button) | Powers off the **whole device** (parks, then halts). | **Power-gated.** |

### Live view camera

The **Live view** camera shows seestar_alp's real-time `/vid` stacking stream —
but **only while the imaging session was started from Home Assistant** (via
*Start live view*, *Start stacking*, *Goto*, *Run plan*, *Start mosaic*, or
*Start spectra*). The scope's firmware serves
live frames only to the session's **owning client**; when this bridge starts the
session, it is that owner. A session started from the phone app belongs to the
phone, so the camera reads **unavailable** and you get only the saved-stack
**Live stacked preview** — that is a firmware boundary the bridge surfaces
rather than works around, and the saved-stack preview keeps working
independently either way.

### Rough edges

Per the Phase-2 "expose everything" directive, every control with a clean
seestar_alp path is published even where it is rough; anything deliberately
left out is listed under
[Deliberate omissions](#deliberate-omissions) below. Known edges, verified
against seestar_alp:

- **Stop only stops scheduler-driven sessions.** The *Stop* button maps to
  seestar_alp's `stop_scheduler`, so a live view or stack started outside the
  scheduler is not stopped by it.
- **Plate-solve loop ON is a no-op on firmware newer than 2.47.** seestar_alp
  answers `start_plate_solve_loop` with a "Deprecated" warning and does nothing;
  turning the switch OFF still calls `stop_plate_solve_loop`.
- **Wide camera is S30-series only.** The `wide_cam` setting exists on the
  S30/S30 Pro; seestar_alp's own web UI additionally hides the toggle behind an
  experimental flag. Other models refuse or ignore it.
- **Record video only records during a planetary live session.** Outside one,
  the scope refuses `start_record_avi` in-band and the refusal shows as
  `error: …` on **Last command result**.
- **In-band scope refusals surface via Last command result.** seestar_alp
  reports many refusals as an HTTP 200 with an error body (e.g. importing a
  plan while a scheduler is already active, or running the startup sequence
  while busy). The bridge detects that shape and publishes it as `error: …` on
  the **Last command result** sensor — the command reached the driver, but the
  scope said no.
- **Restarting the app mid-session turns the Live view camera off.** Session
  ownership lives in the bridge's memory, so an app restart during an
  HA-started session forgets that HA owns it: telemetry and the saved-stack
  preview keep working, but the **Live view** camera stays unavailable until
  the **next** session started from Home Assistant.
- **Mount mode updates instantly only on drivers that publish the
  `get_event_state` mount block** (the seestar_alp fork, or an upstream release
  containing [PR #750](https://github.com/smart-underworld/seestar_alp/pull/750));
  on stock releases it falls back to the slower `get_device_state` poll.

### Deliberate omissions

- **Capture photo.** There is no clean seestar_alp path to map a button to:
  the upstream `live/photo` route is an unimplemented placeholder
  (`LivePhotoResource` in seestar_alp's front server does nothing). Rather
  than bolt a new HTTP surface onto the bridge, the control is omitted until
  upstream implements it.

## Running without the Supervisor

For **Home Assistant Container / Core** or a **separate host**, the repo ships a
[`docker-compose.yml`](../docker-compose.yml) that runs the bridge plus a
seestar_alp service. There is no Supervisor there, so:

- **MQTT must be provided explicitly** via the `MQTT_HOST` / `MQTT_PORT` /
  `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_SSL` environment variables (there is
  no Mosquitto add-on to borrow). These are the **only** environment variables
  the bridge reads ([sample `.env`](../examples/env.sample)).
- **Scopes** are configured in seestar_alp directly, via a bind-mounted
  `config.toml` ([sample](../examples/config.toml.sample)).
- The bridge's **non-MQTT options live in `/data/options.json`**
  ([sample](../examples/options.json.sample)). The sample sets `alpaca_host` to
  the `seestar_alp` service by name, so the bridge doesn't depend on discovery.

> **Create the bind-mount source files before the first `docker compose up`.**
> Copy them from `examples/`:
>
> ```
> cp examples/config.toml.sample   ./config.toml
> cp examples/options.json.sample  ./options.json
> cp examples/env.sample           ./.env   # then edit MQTT_* + ARCH
> ```
>
> A bind-mount whose source path is missing is created by Docker as an empty
> **directory**, which silently breaks the container (the process tries to read a
> directory as its config file). Create the files first.

> **`aarch64` hosts must set `ARCH`.** The bridge image is published per-arch and
> `ARCH` defaults to `amd64`. On 64-bit ARM (e.g. a dedicated Raspberry Pi) set
> `ARCH=aarch64` in `.env` (see [sample](../examples/env.sample)) or you will pull
> the amd64 image and it won't run.

The `seestar_alp` service is built from seestar_alp's own `docker/Dockerfile` at
the pinned tag. The bridge reads each scope's preview address from that service's
`/config.json` on port **5432**. Telemetry never needs the preview address. To use
a seestar_alp you already run instead, delete the `seestar_alp` service and point
`alpaca_host` at yours.

## Troubleshooting

### Entity IDs are derived from the entity NAME, not the discovery ID

Home Assistant builds each `entity_id` from the entity's **name**, ignoring the
MQTT discovery `object_id`. The bridge sets only the per-entity name (e.g.
`Telephoto target`); Home Assistant then prepends the device name to form the
friendly name `<device name> <entity name>` and slugs *that* into the
`entity_id`. So for a scope named **"Seestar S30 Pro"** the IDs come out as
`sensor.seestar_s30_pro_telephoto_target`. With a different scope name the prefix
differs (`"Backyard Seestar"` → `sensor.backyard_seestar_*`).

**Do not assume the IDs** — read the real ones off the device page:
**Settings → Devices & Services → Devices → your Seestar**. Each entity row shows
its exact `entity_id`. If HA had to de-duplicate a name, it may have appended a
suffix (e.g. `_2`); only the device page shows the truth. The example dashboard
calls this out and tells you which prefix to replace.

### No entities appear

- Confirm MQTT is connected: the **Log** tab should show the broker connection and
  "discovered" lines. If it complains about MQTT, install the Mosquitto add-on or
  set `mqtt_host`.
- Confirm the app found the driver: the **Log** tab names the seestar_alp it uses,
  or says none was found. Check the seestar_alp add-on is started, or set
  `alpaca_host`.
- Confirm the scope is reachable from the driver: the scope's `host` in the
  seestar_alp add-on must be correct and on the LAN. A Seestar in its default
  **AP mode** is its own Wi-Fi hotspot and is **not reachable** from Home
  Assistant — it must first be joined to your home Wi-Fi in **station mode** via
  the Seestar app, on the same subnet as HA (see
  [Before you begin](#before-you-begin)).
- Check Home Assistant's MQTT integration `discovery_prefix` matches the
  `discovery_prefix` option (both default to `homeassistant`).
- On **firmware ≥ 7.18**, the telemetry entities still appear without an auth key,
  but device-state fields and controls need one — see
  [Firmware 7.18+ authentication](#firmware-718-authentication).

### The preview camera is blank

On a fresh install the most common cause is simply that **no stacking session has
run yet.** The preview camera shows the scope's *last saved stack*, so it stays
blank until the scope has stacked at least once, and the imaging telemetry reads
idle/unknown until a session is running. Point the scope at a target and start
stacking (from the Seestar app, or from Home Assistant) and the preview fills in —
this is expected, not a fault.

If the preview stays blank *after* a session has stacked, it's an address problem.
Telemetry never depends on the preview: it needs each scope's own HTTP address,
discovered separately (see [Known caveats](#known-caveats)). If discovery can't
resolve it, that one scope's preview is skipped while all its other entities keep
updating.

## Known caveats

These are deliberate v1 limitations, documented so they don't surprise you:

1. **Older seestar_alp releases have no `/config.json`.** It arrived in upstream
   [#749](https://github.com/smart-underworld/seestar_alp/pull/749). Against an
   older seestar_alp the bridge falls back to scraping the `/config` HTML page for
   the preview address.
2. **The base image is pinned by tag, not by digest.** The Dockerfile builds from
   `ghcr.io/home-assistant/<arch>-base-debian:trixie-2026.06.1` (a tag), so a
   rebuild could pick up an updated base under the same tag.
3. **No web UI of its own.** The seestar_alp SSC web UI is in the seestar_alp
   add-on's sidebar panel.

   <a id="renaming-a-scope-creates-a-new-device"></a>

4. **A scope's name is identity-bearing.** The HA device id is derived from the
   scope name you set in seestar_alp, and HA derives entity IDs from it too, so
   **renaming a scope
   creates a new device with new entity IDs** — the old ones go stale. Pick a
   stable name up front; if you must rename, expect to re-point dashboards and
   automations at the new IDs.

   <a id="firmware-718-authentication"></a>

5. **Firmware 7.18+ authentication.** The telemetry (~46 entities) and the
   stacked-image **preview** work with **no auth key** — they ride the scope's
   **event stream**, which is unauthenticated. But on **firmware ≥ 7.18** the
   scope gates its synchronous COMMAND channel behind a **challenge-response
   interop auth key**, so **device-state fields** (mount mode, focuser, battery,
   firmware, park/home, tracking) and **all** [Phase-2 controls](#controls-phase-2)
   require that key to be configured **in seestar_alp** (the seestar_alp add-on's
   `interop_pem` option). Without it those entities
   stay unknown and commands are refused by the scope, while telemetry and the
   preview keep working. Obtaining the key is not something this add-on ships —
   see the [seestar_alp project](https://github.com/smart-underworld/seestar_alp)
   and its community for how.

   <a id="behavior-during-active-imaging"></a>

6. **Behavior during active imaging.** During an exposure the scope firmware
   **stalls synchronous `get_device_state` queries**, so the slower device-state
   fields (mount mode, focuser, battery, firmware, park/home, tracking) refresh
   only at the **idle gaps between frames** — not continuously. This is expected,
   not a stuck sensor. The **event-stream** telemetry is unaffected and keeps
   updating live throughout a capture: target, stacking frames, plate-solve,
   computed Alt/Az, and the preview.

## Licensing

This app's code is licensed under the [Apache License, Version 2.0](../LICENSE).
It does not include seestar_alp, which is GPL-3.0 and runs separately (the
seestar_alp add-on, or your own instance); see [NOTICE](../NOTICE) and
[THIRD_PARTY_LICENSES.md](../THIRD_PARTY_LICENSES.md).
