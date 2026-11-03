# Seestar for Home Assistant

[![Build](https://github.com/jaxzin/ha-seestar/actions/workflows/build.yml/badge.svg)](https://github.com/jaxzin/ha-seestar/actions/workflows/build.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A plug-and-play [Home Assistant](https://www.home-assistant.io/) app that bridges
one or more [ZWO Seestar](https://www.seestar.com/) smart telescopes (**S30**,
**S30 Pro**, **S50**) into Home Assistant over MQTT discovery. Point it at your
scope and get live imaging telemetry — targets, stacking progress, plate-solve
results, Alt/Az, mount and health — plus a live stacked-image preview, as
auto-discovered entities. One HA device per telescope, zero hand-edited config
files.

It also drives the scope: HA-driven **control** (sessions, goto, saved plans, settings, power) plus a **live view camera**, gated behind per-scope safety switches that default off — see [Controls (Phase 2)](seestar/DOCS.md#controls-phase-2).

## What it is

A Python telemetry **bridge**, packaged as an HA app, that reads the
[seestar_alp](https://github.com/smart-underworld/seestar_alp) ASCOM Alpaca
driver, taps its event stream, computes Alt/Az, pulls the stacked preview image,
and publishes MQTT discovery. The driver runs as its own add-on,
[**seestar_alp**](https://github.com/jaxzin/ha_seestar_alp), which the bridge
finds automatically; or point it at a seestar_alp you already run.

It runs zero-config against the official **Mosquitto** add-on and supports
**multiple scopes** (one HA device each).

## Architecture

Two add-ons, two containers:

- **seestar_alp** ([jaxzin/ha_seestar_alp](https://github.com/jaxzin/ha_seestar_alp))
  — the ASCOM Alpaca driver and its SSC web UI. It owns the scope list and the
  firmware 7.18+ auth key.
- **seestar_bridge** (this app) — finds the driver by ASCOM Alpaca discovery (or
  `alpaca_host`), enumerates devices via Alpaca, taps the non-blocking event
  stream, computes Alt/Az, discovers each scope's address for the preview, and
  publishes MQTT discovery — one HA device per scope, namespaced by a stable slug
  of the scope name.

```
Seestar(s) ──> seestar_alp add-on ──Alpaca──> seestar_bridge ──MQTT discovery──> Home Assistant
     └──────────────── stacked .jpg (preview) ──────┘
```

## Getting started (Home Assistant OS / Supervised)

New to this? The full, step-by-step walkthrough — with a first-run "what an idle
scope looks like" tour — lives in
**[seestar/DOCS.md → Getting started](seestar/DOCS.md#getting-started)**. The short
version:

**Before you begin**, make sure you have:

- **Home Assistant OS or Supervised**, **2026.7 or later** (the version this is
  developed and tested against; older builds may work but aren't verified). This
  is a Supervisor add-on; for HA Container / Core use the
  [`docker-compose.yml`](docker-compose.yml) path below.
- The official **Mosquitto broker** add-on installed **and** the **MQTT
  integration** configured — the bridge auto-resolves the broker from there.
- A supported **ZWO Seestar** (**S30**, **S30 Pro**, or **S50**), powered on and
  joined to your home Wi-Fi in **station mode** via the Seestar app — *not* its
  default self-hosted **AP mode**, and on the **same subnet** as HA. Have its LAN
  IP ready (Seestar app or your router's DHCP list).

**Then:**

1. Add both repositories — **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories**:

   ```
   https://github.com/jaxzin/ha_seestar_alp
   https://github.com/jaxzin/ha-seestar
   ```

2. Install **Seestar Alpaca driver (seestar_alp)**, add your scope under
   **Telescopes** (name + LAN IP) and, on firmware 7.18+, the interop key, then
   **Start** it.
3. Install **Seestar for Home Assistant** and **Start** it. Leave `alpaca_host`
   blank to use the seestar_alp add-on, or set it to reuse your own seestar_alp.
4. Watch the **Log** tab for the broker connection and `discovered` lines, then
   find the device under **Settings → Devices & Services → MQTT**.

**On the first run**, an idle scope that has never stacked shows a **blank** live
preview and idle/unknown imaging fields — that's normal; they fill in once a
stacking session runs. Note too that on **firmware ≥ 7.18** the telemetry and
preview work without an auth key, but device-state fields and controls need a
challenge-response interop key configured in seestar_alp — see
[Firmware 7.18+ authentication](seestar/DOCS.md#firmware-718-authentication).

See **[seestar/DOCS.md](seestar/DOCS.md)** for the full options reference, how
the driver is found, the Mosquitto zero-config note, and troubleshooting.

## Install (HA Container / Core, or a separate host)

If you run Home Assistant **Container** or **Core**, or want the bridge on a
separate machine (e.g. a dedicated Raspberry Pi), there's no Supervisor — use the
[`docker-compose.yml`](docker-compose.yml) path. It runs the bridge plus a
seestar_alp service, with MQTT and scope config provided explicitly. The compose
file and the [`examples/`](examples/) samples are commented end-to-end.

## Dashboard card

For a purpose-built view of these entities, install the companion
[**seestar-lovelace-card**](https://github.com/jaxzin/seestar-lovelace-card) — a
single self-theming Lovelace card (preview, target, stacking progress, pointing,
mount and health) that you point at one scope with `device: <entity-prefix>`. It's
HACS-installable (add that repo as a custom repository) and is the recommended way
to build a Seestar dashboard.

**No-HACS fallback:** [`examples/stargazing.yaml`](examples/stargazing.yaml) is a
copy-paste, stock-card starter dashboard that maps every published entity. Note
that HA derives entity IDs from the entity **name** — the example explains how to
adapt the prefix to your scope's name and read the exact IDs off the device page.

## Licensing

This project's code is licensed under the
[Apache License, Version 2.0](LICENSE). It does not include `seestar_alp`
(GPL-3.0), which runs as a separate add-on or process that the bridge reaches over
the Alpaca HTTP API; see [NOTICE](NOTICE) and
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
