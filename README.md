# Seestar for Home Assistant

A plug-and-play [Home Assistant](https://www.home-assistant.io/) add-on that
bridges one or more [Seestar](https://www.seestar.com/) smart telescopes into
Home Assistant over MQTT discovery.

The add-on bundles the [seestar_alp](https://github.com/smart-underworld/seestar_alp)
ASCOM Alpaca driver and a Python telemetry bridge. It exposes one HA device per
Seestar — telemetry sensors plus a live stacked-image preview — and runs
zero-config against the Mosquitto add-on.

## Architecture

One container, two s6-supervised processes:

- **seestar_alp** — the ASCOM Alpaca driver (started only in *bundled* mode).
- **seestar_bridge** — the Python telemetry bridge (always running). It
  enumerates devices via Alpaca, taps the non-blocking event stream, computes
  Alt/Az, discovers each scope's address for the preview, and publishes MQTT
  discovery.

## Installation

Add this repository to Home Assistant by URL:

```
https://github.com/jaxzin/ha-seestar
```

Then install the **Seestar for Home Assistant** add-on and configure your
scope(s). See `seestar/DOCS.md` for the full options reference.

## Licensing

This project's code is licensed under the [Apache License, Version 2.0](LICENSE).
The bundled `seestar_alp` driver is licensed under GPL-3.0; see [NOTICE](NOTICE)
for attribution and component licenses.
