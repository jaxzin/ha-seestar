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
- **Live view** camera fed from seestar_alp's `/vid` stream, live only while
  the imaging session was started from Home Assistant (the firmware serves live
  frames to the owning client only).
- Initial repository scaffold: add-on repository metadata, licensing
  (Apache-2.0 + GPL-3.0 attribution), and the `seestar_bridge` Python package
  skeleton with a pytest setup.
