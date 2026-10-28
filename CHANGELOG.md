# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Phase-2 control layer: safety-gated MQTT command entities per scope (imaging
  sessions, goto, saved-plan execution, settings, power) behind two default-off
  safety switches, with every dispatch outcome surfaced on a **Last command
  result** sensor.
- **Gain**, **Tracking**, **Wide camera**, **Record video**, and **Auto-focus**
  control entities (all behind the same safety gate); *Capture photo* is
  deliberately omitted (upstream's route is an unimplemented placeholder) and
  documented as such.
- **Live view** camera fed from seestar_alp's `/vid` stream, live only while
  the imaging session was started from Home Assistant (the firmware serves live
  frames to the owning client only).
- Initial repository scaffold: add-on repository metadata, licensing
  (Apache-2.0 + GPL-3.0 attribution), and the `seestar_bridge` Python package
  skeleton with a pytest setup.

### Fixed

- The retained `seestar/bridge/availability` topic is now republished `online` on
  every MQTT connect **and reconnect**, not only at startup. Previously a broker
  blip left the Last-Will `offline` retained, greying out every control entity
  until the add-on restarted.
- **Dew heater** and **Tracking** switches follow the scope's own reading
  instead of only echoing the last accepted command, so they no longer show *on*
  while the heater is off (changed in the app, after a reboot, or ignored).

### Changed (breaking)

- The seestar_alp driver is no longer bundled. It runs as its own add-on,
  **Seestar Alpaca driver (seestar_alp)** from
  [jaxzin/ha_seestar_alp](https://github.com/jaxzin/ha_seestar_alp), which now
  owns the scope list (`scopes`), the firmware 7.18+ key (`interop_pem`) and the
  SSC web UI. This app is the bridge only: the `scopes` and `interop_pem` options,
  the ingress panel and the published imaging port are gone.
- With `alpaca_host` blank the bridge finds the driver itself: ASCOM Alpaca UDP
  discovery plus the Supervisor add-on list (`*_seestar_alp`), accepting only an
  endpoint whose telescope answers seestar_alp's `get_event_state`. Exactly one
  match is used; more than one asks you to set `alpaca_host`.
- **Moving from bundled mode:** install the seestar_alp add-on, copy your
  `scopes` (keep the same scope name, so the HA device and entity IDs stay the
  same) and `interop_pem` into it, start it, then update this app. Run only one
  driver against a scope at a time.

### Changed

- Last-known telemetry survives driver and add-on restarts; unknown values stay
  honestly unknown. The bridge merges each poll cycle into a persistent
  per-scope snapshot (seeded once from its own retained MQTT state topic at
  startup), so a seestar_alp restart's wiped event cache no longer blanks the
  dashboard; `connected` stays computed fresh every cycle, and keys never
  observed stay absent.
- Mount mode now reads instantly from the fork's `get_event_state` mount block
  when available (falls back to `get_device_state` on stock seestar_alp).
- The add-on image now builds from the Home Assistant **Debian** base
  (`base-debian:trixie`, Python 3.13) instead of Alpine: `opencv-python`
  publishes no musllinux wheels, so the Alpine base forced an hours-long
  source build, while Debian pulls prebuilt manylinux wheels for every
  compiled dependency on both amd64 and aarch64. pip installs now live in a
  `/opt/venv` virtualenv (Debian's system Python is PEP 668
  externally-managed).

### Fixed

- **Storage used** is now also filled from `get_device_state` (mounted volume's
  `used_percent`, or computed from free/total MB); it stayed unknown on firmware
  that rarely or never sends the `DiskSpace` event (seen on 9.75).
- External mode: the Open Web UI panel now shows a link to the external
  seestar_alp web UI instead of a 502.
- The per-scope worker loop is now unkillable by a single cycle's exception: a
  truncated imaging-server reply (`http.client.IncompleteRead`) or any other
  unexpected error logs its traceback, publishes `offline` for that cycle, and
  the next cycle runs as scheduled.
- The **Live view** camera no longer turns off mid-plan (View events pass
  through terminal states *between* scheduler targets) or at session start
  (a stale terminal View retained from a previous session); stuck ownership
  after a driver restart clears itself after a bounded number of polls.
- Goto, Start mosaic, and Start spectra now turn the **Live view** camera on
  (they all start an owned imaging session upstream).
