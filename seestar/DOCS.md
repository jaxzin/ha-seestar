# Seestar for Home Assistant

Live Seestar imaging telemetry and a stacked-image preview in Home Assistant,
published as auto-discovered MQTT entities — one HA device per telescope. The app
bundles the [seestar_alp](https://github.com/smart-underworld/seestar_alp) Alpaca
driver, or reuses one you already run.

## Install

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store** (on newer
   builds, **Settings → Apps**), open the **⋮** menu (top right), choose
   **Repositories**, and add:

   ```
   https://github.com/jaxzin/ha-seestar
   ```

2. The **Seestar for Home Assistant** app appears in the store. Install it.
3. Open the **Configuration** tab, add your telescope under **Telescopes**
   (a name and the scope's LAN IP), and **Start** the app.
4. Open the **Log** tab to watch it connect, enumerate the scope, and publish
   MQTT discovery. The new device shows up under
   **Settings → Devices & Services → MQTT**.

> This app runs under the Home Assistant **Supervisor** (Home Assistant OS or
> Supervised). On **HA Container / Core**, or to run it on a separate machine
> (e.g. a dedicated Raspberry Pi), use the [`docker-compose.yml`](../docker-compose.yml)
> path instead — see [Running without the Supervisor](#running-without-the-supervisor).

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

## Bundled vs external driver

The app talks to your telescope through the seestar_alp Alpaca driver. You choose
where that driver runs with the single `alpaca_host` option:

| | `alpaca_host` blank (default) — **bundled** | `alpaca_host` set — **external** |
|---|---|---|
| seestar_alp | runs **inside this app** | you run it yourself elsewhere |
| Scope list | the app's **Telescopes** (`scopes`) option seeds it | already configured on your instance |
| Web UI | the seestar_alp **SSC web UI**, in the HA sidebar (ingress) | open your own instance's UI |
| Preview address | read from the bundled `config.toml` | read from your instance's `/config.json` (or `/config` page) |

- **Bundled** (most users): leave `alpaca_host` blank and list your scope(s) under
  **Telescopes**. The app starts seestar_alp for you and writes its config.
- **External**: if you already run seestar_alp on the network, set
  `alpaca_host` to its `host:port` (e.g. `192.168.1.50:5555`) and leave
  **Telescopes** empty — that instance already knows your scopes.

Setting **both** `scopes` and `alpaca_host` is rejected at startup; so is setting
**neither**.

## Options reference

| Option | Type | Default | Description |
|---|---|---|---|
| `scopes` | list of `{name, host}` | `[]` | **Bundled mode only.** One entry per telescope. `name` is the HA device name (see the [naming caveat](#renaming-a-scope-creates-a-new-device)); `host` is the scope's LAN IP or hostname. Each becomes its own HA device. Leave empty in external mode. |
| `alpaca_host` | string | `""` | Blank = run the bundled driver. Set to `host:port` of your own seestar_alp to reuse it (and leave `scopes` empty). |
| `alpaca_webui_port` | port | `5432` | **External mode only.** The port your seestar_alp serves its config on, used to look up each scope's address for the preview. |
| `imaging_port` | port | `7556` | Port of seestar_alp's imaging server on the Alpaca host, where the Live view camera grabs its `/vid` MJPEG frames. Change only for an external seestar_alp bound to a non-stock imaging port. |
| `mqtt_host` | string | `""` | Blank = use the Mosquitto add-on via the Supervisor. Set to override with another broker. |
| `mqtt_port` | port | `0` | `0` = take the port from the Supervisor service. Set explicitly (e.g. `1883`, or `8883` for TLS) when overriding `mqtt_host`. |
| `mqtt_username` | string | `""` | Broker username. Leave blank with the Mosquitto add-on. |
| `mqtt_password` | password | `""` | Broker password. Leave blank with the Mosquitto add-on. |
| `mqtt_ssl` | bool | `false` | Connect to the broker over TLS. Relevant only when overriding `mqtt_host`. |
| `discovery_prefix` | string | `homeassistant` | MQTT discovery topic prefix. Must match Home Assistant's MQTT integration setting; change only if you customized it there. |
| `event_poll_sec` | int (1–3600) | `10` | How often to poll the fast Alpaca event stream for imaging telemetry. Lower is more responsive; higher is gentler on the driver. |
| `state_poll_sec` | int (1–3600) | `30` | How often to poll the slower device-state call (mount mode, focuser, battery, firmware). |
| `preview_max_px` | int (256–4096) | `1280` | Longest-edge limit for the stacked preview before publishing; larger previews are downscaled to this. |
| `log_level` | enum | `info` | Verbosity of the bridge and bundled driver. One of `trace`, `debug`, `info`, `notice`, `warning`, `error`, `fatal`. Use `debug` or `trace` to diagnose a connection problem. |

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
  scope's GPS, so they stay correct during capture), tracking, slewing, goto
  state, parked, at-home, mount mode, focuser, filter position.
- **Health**: connected, sensor temperature, battery, charger, storage used,
  dew heater, firmware, last alert.

A copy-paste starter dashboard is in [`examples/stargazing.yaml`](../examples/stargazing.yaml).

## Controls (Phase 2)

> **⚠️ These controls MOVE A PHYSICAL TELESCOPE.** A goto slews the mount, Park
> stows the arm, Shutdown powers the whole device off. Keep **Controls enabled**
> and **Allow power actions** OFF whenever you are not actively driving the
> scope, and write automations that target the safety switches **deliberately**
> — arm before commanding, disarm after — rather than leaving the scope
> permanently armed.

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
| **Focus** (number) | Nudges the focuser by a relative number of steps (−500…500). | |
| **Mag declination** (number) | Magnetic-declination fudge angle for the compass calibration (±180°). | |
| **Dew heater** (switch) | Turns the dew heater on or off. | ON applies a fixed power level of 90 (scale 0–100). |
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
*Start live view*, *Start stacking*, or *Run plan*). The scope's firmware serves
live frames only to the session's **owning client**; when this bridge starts the
session, it is that owner. A session started from the phone app belongs to the
phone, so the camera reads **unavailable** and you get only the saved-stack
**Live stacked preview** — that is a firmware boundary the bridge surfaces
rather than works around, and the saved-stack preview keeps working
independently either way.

### Rough edges

Per the Phase-2 "expose everything" directive, the full control surface is
published even where it is rough. Known edges, verified against seestar_alp:

- **Stop only stops scheduler-driven sessions.** The *Stop* button maps to
  seestar_alp's `stop_scheduler`, so a live view or stack started outside the
  scheduler is not stopped by it.
- **Plate-solve loop ON is a no-op on firmware newer than 2.47.** seestar_alp
  answers `start_plate_solve_loop` with a "Deprecated" warning and does nothing;
  turning the switch OFF still calls `stop_plate_solve_loop`.
- **In-band scope refusals surface via Last command result.** seestar_alp
  reports many refusals as an HTTP 200 with an error body (e.g. importing a
  plan while a scheduler is already active, or running the startup sequence
  while busy). The bridge detects that shape and publishes it as `error: …` on
  the **Last command result** sensor — the command reached the driver, but the
  scope said no.

## Running without the Supervisor

For **Home Assistant Container / Core** or a **separate host**, the repo ships a
[`docker-compose.yml`](../docker-compose.yml) that runs the same bridge (and, in
bundled mode, seestar_alp) from the same image. There is no Supervisor there, so:

- **MQTT must be provided explicitly** via the `MQTT_HOST` / `MQTT_PORT` /
  `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_SSL` environment variables (there is
  no Mosquitto add-on to borrow). These are the **only** environment variables
  the bridge reads ([sample `.env`](../examples/env.sample)).
- **Scopes** are configured in seestar_alp directly: in bundled mode via a
  bind-mounted `config.toml` ([sample](../examples/config.toml.sample)); in
  external mode by pointing `alpaca_host` at your existing instance.
- The bridge's **non-MQTT options live in `/data/options.json`**
  ([sample](../examples/options.json.sample)) — `alpaca_host`,
  `alpaca_webui_port`, and the tuning knobs are set there, **not** as environment
  variables.

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

> **`aarch64` hosts must set `ARCH`.** The image is published per-arch and `ARCH`
> defaults to `amd64`. On 64-bit ARM (e.g. a dedicated Raspberry Pi) set
> `ARCH=aarch64` in `.env` (see [sample](../examples/env.sample)) or you will pull
> the amd64 image and it won't run.

Under compose, even the **bundled** setup runs the bridge in *external* mode:
`options.json` sets `alpaca_host: "seestar_alp:5555"`, so the bridge resolves each
scope's preview address from the **seestar_alp** service's `/config.json` (or the
`/config` HTML page on the pinned `v3.2.2`) web UI on port **5432** — which is why
the `seestar_alp` service publishes `5432`. It does **not** read the bind-mounted
`config.toml` for the preview address (only seestar_alp reads `config.toml`).
Telemetry is unaffected — it never needs the preview address.

The compose file is commented top-to-bottom with the bundled-vs-external choice.

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
- Confirm the scope is reachable: the bridge enumerates scopes from seestar_alp,
  so in bundled mode the scope `host` must be correct and on the LAN.
- Check Home Assistant's MQTT integration `discovery_prefix` matches the
  `discovery_prefix` option (both default to `homeassistant`).

### The preview camera is blank

Telemetry never depends on the preview. The preview needs each scope's own HTTP
address, discovered separately (see [Known caveats](#known-caveats)). If discovery
can't resolve it, that one scope's preview is skipped while all its other entities
keep updating.

## Known caveats

These are deliberate v1 limitations, documented so they don't surprise you:

1. **Bundled seestar_alp is pinned to `v3.2.2`.** That release predates the
   machine-readable `/config.json` endpoint
   ([smart-underworld/seestar_alp#749](https://github.com/smart-underworld/seestar_alp/pull/749)).
   This does **not** affect bundled mode — the bridge reads the scope address
   straight from `config.toml` inside the container. In **external** mode against
   an older seestar_alp, the bridge falls back to scraping the `/config` HTML page
   for the preview address; once you run a seestar_alp new enough to expose
   `/config.json`, it uses that automatically.
2. **seestar_alp's own dependencies are not hash-pinned.** They are installed from
   the upstream `requirements.txt` at its pinned tag. The bridge's own
   dependencies **are** hash-pinned (`--require-hashes`).
3. **The base image is pinned by tag, not by digest.** The Dockerfile builds from
   `ghcr.io/home-assistant/<arch>-base-python:3.12-alpine3.20` (a tag), so a
   rebuild could pick up an updated base under the same tag.
4. **The bundled web UI / ingress is live in BUNDLED mode only.** The HA sidebar
   ingress surfaces the *bundled* seestar_alp SSC web UI. In **external** mode the
   app does not start a driver, so there is no UI to ingress — open your own
   seestar_alp instance's web UI directly.

   <a id="renaming-a-scope-creates-a-new-device"></a>

5. **A scope's name is identity-bearing.** The HA device id is derived from the
   scope name, and HA derives entity IDs from it too, so **renaming a scope
   creates a new device with new entity IDs** — the old ones go stale. Pick a
   stable name up front; if you must rename, expect to re-point dashboards and
   automations at the new IDs.

## Licensing

This app's code is licensed under the [Apache License, Version 2.0](../LICENSE).
The bundled `seestar_alp` driver is GPL-3.0; see [NOTICE](../NOTICE) for attribution
and the preserved component licenses.
