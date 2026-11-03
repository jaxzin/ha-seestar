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

## The Seestar stack

This repository is the entry point for four projects that together put a
Seestar into Home Assistant:

| Piece | What it does | Install from |
|---|---|---|
| **Seestar Alpaca driver (seestar_alp)** | The [seestar_alp](https://github.com/smart-underworld/seestar_alp) ASCOM Alpaca driver and its SSC web UI (in the HA sidebar). Talks to the scopes; holds the scope list and the firmware 7.18+ interop key. | Add-on, [jaxzin/ha_seestar_alp](https://github.com/jaxzin/ha_seestar_alp) |
| **Seestar for Home Assistant** (this repo) | MQTT bridge: telemetry, live stacked preview, live view and safety-gated controls, as one HA device per scope. Finds the driver automatically. | Add-on, this repository |
| **Seestar Card** | One Lovelace card for a live session: preview, target, stacking, pointing, mount and health. | HACS, [jaxzin/seestar-lovelace-card](https://github.com/jaxzin/seestar-lovelace-card) |
| **Seestar Sync** (optional, experimental) | Copies the scope's image library over SMB to an archive (ideally a NAS), and can later free space on the scope once copies are verified. | Add-on, [jaxzin/seestar-sync](https://github.com/jaxzin/seestar-sync) |

```
Seestar(s) ──> seestar_alp add-on ──Alpaca──> Seestar bridge ──MQTT discovery──┐
   │  └───────────── stacked .jpg (preview) ─────┘                             ├──> Home Assistant ──> Seestar Card
   └──SMB──> Seestar Sync ──> archive (/media) ─────────────MQTT discovery──────┘
```

Only the driver talks to the scopes' control API. The bridge reads the driver,
namespaces each scope by a stable slug of its name and publishes MQTT discovery;
the card only reads the entities the bridge publishes. Seestar Sync is
independent of the other three: it reads the scope's SMB share, not Alpaca.

## Quick Start (Home Assistant OS / Supervised)

This sets up the whole stack in one pass. Each step links to the full reference;
[seestar/DOCS.md → Getting started](seestar/DOCS.md#getting-started) has the
longer walkthrough with a first-run tour.

### Before you begin

- **Home Assistant OS or Supervised**, **2026.7 or later** (the version this is
  developed and tested against). For HA Container / Core, see
  [Install (HA Container / Core, or a separate host)](#install-ha-container--core-or-a-separate-host).
- **HACS**, for the dashboard card (optional: there is a no-HACS fallback in step 5).
- A **ZWO Seestar** (**S30**, **S30 Pro** or **S50**) joined to your home Wi-Fi
  in **station mode** with the Seestar app (*not* its default **AP mode**), on the
  **same subnet** as HA. Note its LAN IP (Seestar app or your router) and give it
  a DHCP reservation so it keeps it.
- On **firmware 7.18 or later**: the seestar_alp **interop key** (a PEM private
  key) if you want device state and controls. It is not distributed with these
  projects; without it, telemetry and the preview still work. See
  [Firmware 7.18+ authentication](seestar/DOCS.md#firmware-718-authentication).

### 1. MQTT broker

Install the official **Mosquitto broker** add-on and start it, then accept the
**MQTT** integration that HA offers under **Settings → Devices & Services**. With
both in place the bridge needs no MQTT settings.

### 2. Add the add-on repositories

**Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add both:

```
https://github.com/jaxzin/ha_seestar_alp
https://github.com/jaxzin/ha-seestar
```

### 3. Seestar Alpaca driver

1. Install **Seestar Alpaca driver (seestar_alp)**.
2. On its **Configuration** tab, add each scope under **Telescopes**: a name and
   its LAN IP. Pick the name carefully: the bridge derives the HA device and every
   entity ID from it, and renaming later creates a new device.
3. On firmware 7.18+, paste the key into **Interop auth key (firmware 7.18+)**.
4. **Start** it. **Seestar ALP** appears in the sidebar: the SSC web UI, which
   should show your scope.

Options and ports: [ha_seestar_alp → DOCS.md](https://github.com/jaxzin/ha_seestar_alp/blob/main/seestar_alp/DOCS.md).

### 4. Seestar bridge

1. Install **Seestar for Home Assistant**.
2. Leave every option at its default: blank `alpaca_host` finds the driver
   add-on, blank `mqtt_host` uses Mosquitto.
3. **Start** it and watch its **Log** tab for the broker connection and a
   `discovered` line per scope.
4. The scope appears as a device under **Settings → Devices & Services → MQTT**.
   Its entity IDs start with the slug of the scope name, e.g.
   `sensor.seestar_s30_pro_telephoto_target` for a scope named *Seestar S30 Pro*.
   That prefix (`seestar_s30_pro`) is what the card needs.

All options: [Options reference](seestar/DOCS.md#options-reference).

### 5. Dashboard card

1. **HACS → ⋮ → Custom repositories**: add
   `https://github.com/jaxzin/seestar-lovelace-card`, category **Dashboard**.
2. Install **Seestar Card** and reload the browser.
3. Edit a dashboard → **Add card** → **Seestar Card**, or in YAML:

   ```yaml
   type: custom:seestar-card
   device: seestar_s30_pro   # the entity prefix from step 4
   ```

Options (accent colour, camera overrides, actions):
[seestar-lovelace-card README](https://github.com/jaxzin/seestar-lovelace-card#readme).
**No HACS?** [`examples/stargazing.yaml`](examples/stargazing.yaml) is a
stock-card dashboard that maps every published entity.

### 6. Check it works

- The card shows the scope's battery, storage and pointing; the target and
  stacking fields stay idle or unknown until a session runs.
- An idle scope that has never stacked has a **blank** live preview. That's
  normal: it fills in once a stacking session runs.
- Without the interop key on firmware 7.18+, device-state fields stay unknown
  and commands are refused.

### 7. Controls (when you want them)

Every command is gated by two per-scope switches that default **off**:
**Controls enabled** gates all commands, and **Allow power actions** also gates
startup, park and shutdown. The card adds its own **Controls** arm toggle. Turn
them on when you are at the scope, and off afterwards. See
[Safety model](seestar/DOCS.md#safety-model--read-this-first).

### 8. Archive the library with Seestar Sync (optional)

**Experimental.** Tested with the Seestar S30 Pro only. On Home Assistant it has
been run as a dry run: it starts, finds Mosquitto, sets up its archive under
`/media` and publishes its device. Not yet confirmed from HA: reading the scope
over SMB (TCP 445), a real copy, and an archive on network storage added under
**Settings → System → Storage**. The scope needs a fixed IP, and a full library
is hundreds of GB.

> **Already run seestar-sync somewhere else** (Docker, Kubernetes, another
> add-on)? Give this one its own `archive_dir` **and** its own
> `mqtt_topic_prefix` (e.g. `seestar-sync-ha`) before you start it. The prefix
> also sets the MQTT client ID, so two instances sharing the default
> `seestar-sync` keep knocking each other off the broker and share one HA
> device, even in dry run. Two instances on one archive risk two reclaimers.
> With separate prefixes you get two devices that are both named
> **Seestar Sync**; rename one on its device page to tell them apart.

1. Give it storage: **Settings → System → Storage → Add network storage**,
   name `seestar`, usage **Media**, pointing at a share on your NAS. It mounts at
   `/media/seestar`, the add-on's default archive folder. The folder must exist
   before you start the add-on; it will not create it.
2. Add the repository `https://github.com/jaxzin/seestar-sync` (as in step 2)
   and install **Seestar Sync**.
3. Set **`seestar_host`** to the scope's IP and **Start** it. If this is your
   only instance, leave the rest at its defaults: **dry run on** (it plans and
   logs, copying and deleting nothing) and **reclaim off** (it never deletes from
   the scope). With the MQTT options blank it uses Mosquitto and publishes a
   **Seestar Sync** device.
4. Read its **Log** tab for a few runs, then turn **dry run** off to make real
   copies. Deletion is still impossible until you turn reclaim on.

Run only one Seestar Sync per archive. Freeing space on the scope is a separate,
deliberate step: read the safe first-run procedure in its
[DOCS.md](https://github.com/jaxzin/seestar-sync/blob/main/addon/DOCS.md)
before enabling reclaim.

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
