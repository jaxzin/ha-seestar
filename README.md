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

1. Add this repository — **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories**:

   ```
   https://github.com/jaxzin/ha-seestar
   ```

2. Install **Seestar for Home Assistant**.
3. On the **Configuration** tab, add your scope under **Telescopes** (name + LAN
   IP) for **bundled** mode, or set **External Alpaca driver** (`alpaca_host`) to
   reuse your own seestar_alp. **Start** the app.
4. Watch the **Log** tab for the broker connection and `discovered` lines, then
   find the device under **Settings → Devices & Services → MQTT**.

**On the first run**, an idle scope that has never stacked shows a **blank** live
preview and idle/unknown imaging fields — that's normal; they fill in once a
stacking session runs. Note too that on **firmware ≥ 7.18** the telemetry and
preview work without an auth key, but device-state fields and controls need a
challenge-response interop key configured in seestar_alp — see
[Firmware 7.18+ authentication](seestar/DOCS.md#firmware-718-authentication).

See **[seestar/DOCS.md](seestar/DOCS.md)** for the full options reference, the
bundled-vs-external choice, the Mosquitto zero-config note, and troubleshooting.

## Install (HA Container / Core, or a separate host)

If you run Home Assistant **Container** or **Core**, or want the bridge on a
separate machine (e.g. a dedicated Raspberry Pi), there's no Supervisor — use the
[`docker-compose.yml`](docker-compose.yml) path. It runs the same bridge (and, in
bundled mode, seestar_alp) from the same image, with MQTT and scope config
provided explicitly. The compose file and the [`examples/`](examples/) samples are
commented end-to-end.

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
[Apache License, Version 2.0](LICENSE). The bundled `seestar_alp` driver is
licensed under **GPL-3.0** and is included at arm's length (we talk to it over the
Alpaca HTTP socket, as a separate process); see [NOTICE](NOTICE) and
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) for attribution and the
preserved component licenses.
