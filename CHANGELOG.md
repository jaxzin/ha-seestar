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
