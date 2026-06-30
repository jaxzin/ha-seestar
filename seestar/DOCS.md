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

Per telescope, one HA device with ~46 entities plus a `camera` for the live
stacked preview:

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

## Running without the Supervisor

For **Home Assistant Container / Core** or a **separate host**, the repo ships a
[`docker-compose.yml`](../docker-compose.yml) that runs the same bridge (and, in
bundled mode, seestar_alp) from the same image. There is no Supervisor there, so:

- **MQTT must be provided explicitly** via `MQTT_HOST` / `MQTT_PORT` /
  `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_SSL` environment variables (there is
  no Mosquitto add-on to borrow).
- **Scopes** are configured in seestar_alp directly: in bundled mode via a
  bind-mounted `config.toml` ([sample](../examples/config.toml.sample)); in
  external mode by pointing `alpaca_host` at your existing instance.
- The bridge's non-MQTT options come from a mounted `/data/options.json`
  ([sample](../examples/options.json.sample)).

The compose file is commented top-to-bottom with the bundled-vs-external choice.

## Troubleshooting

### Entity IDs are derived from the entity NAME, not the discovery ID

Home Assistant builds each `entity_id` from the entity's **name**, ignoring the
MQTT discovery `object_id`. The bridge names entities `<Scope name> <Entity>`, so
for a scope named **"Seestar S30 Pro"** the IDs look like
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
5. **A scope's name is identity-bearing.** The HA device id is derived from the
   scope name, and HA derives entity IDs from it too, so **renaming a scope
   creates a new device with new entity IDs** — the old ones go stale. Pick a
   stable name up front; if you must rename, expect to re-point dashboards and
   automations at the new IDs.

   <a id="renaming-a-scope-creates-a-new-device"></a>

## Licensing

This app's code is licensed under the [Apache License, Version 2.0](../LICENSE).
The bundled `seestar_alp` driver is GPL-3.0; see [NOTICE](../NOTICE) for attribution
and the preserved component licenses.
