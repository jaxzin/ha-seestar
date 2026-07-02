# Seestar for Home Assistant

A plug-and-play [Home Assistant](https://www.home-assistant.io/) app that bridges
one or more [Seestar](https://www.seestar.com/) smart telescopes into Home
Assistant over MQTT discovery. Point it at your scope and get live imaging
telemetry — targets, stacking progress, plate-solve results, Alt/Az, mount and
health — plus a live stacked-image preview, as auto-discovered entities. One HA
device per telescope, zero hand-edited config files.

It also drives the scope: HA-driven **control** (sessions, goto, saved plans, settings, power) plus a **live view camera**, gated behind per-scope safety switches that default off — see [Controls (Phase 2)](seestar/DOCS.md#controls-phase-2).

## What it is

A single HA app that bundles everything you need:

- the [seestar_alp](https://github.com/smart-underworld/seestar_alp) ASCOM Alpaca
  driver (started for you, or reuse one you already run), and
- a Python telemetry **bridge** that taps the driver's event stream, computes
  Alt/Az, pulls the stacked preview image, and publishes MQTT discovery.

It runs zero-config against the official **Mosquitto** add-on and supports
**multiple scopes** (one HA device each).

## Architecture

One container, two [s6](https://github.com/just-containers/s6-overlay)-supervised
processes:

- **seestar_alp** — the ASCOM Alpaca driver. Started only in *bundled* mode.
- **seestar_bridge** — the telemetry bridge (always running). It enumerates
  devices via Alpaca, taps the non-blocking event stream, computes Alt/Az,
  discovers each scope's address for the preview, and publishes MQTT discovery —
  one HA device per scope, namespaced by a stable slug of the scope name.

```
Seestar(s) ──Alpaca──> seestar_alp ──event tap──> seestar_bridge ──MQTT discovery──> Home Assistant
     └──────────────── stacked .jpg (preview) ───────────────────┘
```

## Install (Home Assistant OS / Supervised)

Add this repository to Home Assistant by URL — **Settings → Add-ons → Add-on
Store → ⋮ → Repositories**:

```
https://github.com/jaxzin/ha-seestar
```

Then install **Seestar for Home Assistant**, add your scope(s) on the
**Configuration** tab, and start it. See **[seestar/DOCS.md](seestar/DOCS.md)**
for the full options reference, the bundled-vs-external choice, the Mosquitto
zero-config note, and troubleshooting.

## Install (HA Container / Core, or a separate host)

If you run Home Assistant **Container** or **Core**, or want the bridge on a
separate machine (e.g. a dedicated Raspberry Pi), there's no Supervisor — use the
[`docker-compose.yml`](docker-compose.yml) path. It runs the same bridge (and, in
bundled mode, seestar_alp) from the same image, with MQTT and scope config
provided explicitly. The compose file and the [`examples/`](examples/) samples are
commented end-to-end.

## Example dashboard

[`examples/stargazing.yaml`](examples/stargazing.yaml) is a copy-paste Lovelace
starter that maps every published entity. Note that HA derives entity IDs from
the entity **name** — the example explains how to adapt the prefix to your scope's
name and read the exact IDs off the device page.

## Licensing

This project's code is licensed under the
[Apache License, Version 2.0](LICENSE). The bundled `seestar_alp` driver is
licensed under **GPL-3.0** and is included at arm's length (we talk to it over the
Alpaca HTTP socket, as a separate process); see [NOTICE](NOTICE) for attribution
and the preserved component licenses.
