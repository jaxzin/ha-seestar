# Third-party licenses

This app's own code (the `seestar_bridge` and the add-on packaging) is licensed
under the [Apache License, Version 2.0](LICENSE).

## seestar_alp (GPL-3.0) — not included

The app talks to the [seestar_alp](https://github.com/smart-underworld/seestar_alp)
ASCOM Alpaca driver over the network (its Alpaca HTTP API). seestar_alp is
licensed under the **GNU General Public License, version 3.0** and is **not part
of this app's image**. It runs separately:

- as the **seestar_alp add-on** from
  [jaxzin/ha_seestar_alp](https://github.com/jaxzin/ha_seestar_alp) (GPL-3.0),
  whose image and source obligations are documented there; or
- as any seestar_alp instance you run yourself (`alpaca_host`).

The image is built on the Home Assistant Debian base image, which carries its own
Debian packages and their licenses; the bridge's Python dependencies are listed,
hash-pinned, in [`seestar/bridge/requirements.txt`](seestar/bridge/requirements.txt).

See [NOTICE](NOTICE) for the attribution summary.
