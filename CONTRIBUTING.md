# Contributing

Thanks for your interest in **Seestar for Home Assistant**. Bug reports, docs
fixes, and pull requests are all welcome.

## Ground rules

- Be kind — this project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
- Found a security issue? Please **don't** open a public issue; see
  [SECURITY.md](SECURITY.md).
- Small fixes (typos, docs, a one-line bug) can go straight to a PR. For anything
  that changes the add-on options, entity set, or behaviour, open an issue first
  so we can agree on the shape before you write code.

## Repository layout

- `seestar/` — the Home Assistant add-on: `config.yaml` (options schema),
  `translations/en.yaml` (option labels), `Dockerfile`, `build.yaml`, the s6
  service `rootfs/`, and `DOCS.md` (the operator manual shown in the add-on).
- `seestar/bridge/` — the Python telemetry + control bridge (`seestar_bridge/`)
  and its test suite (`tests/`).
- `examples/` — the `docker-compose.yml` path samples and the starter dashboard.

## Dev setup

The bridge is pure Python (>= 3.12) and uses [`uv`](https://docs.astral.sh/uv/).
No project install is needed — the test command pulls its own deps into a
throwaway environment.

### Run the tests

```bash
cd seestar/bridge
uv run --python 3.12 --no-project \
  --with pytest --with paho-mqtt --with Pillow \
  python -m pytest -q
```

### Lint

CI pins Ruff to the version seestar_alp ships, so match it locally:

```bash
uvx ruff@0.12.5 check seestar/bridge
```

Both of these run in CI on every push (see
[`.github/workflows/build.yml`](.github/workflows/build.yml)); please make sure
they're green before opening a PR.

## Pull requests

- Branch off `main`; keep the change focused.
- Add or update tests for any behaviour change in the bridge.
- If you touch an add-on option or an entity, update `DOCS.md`,
  `translations/en.yaml`, and `examples/stargazing.yaml` to match, and add a
  `CHANGELOG.md` entry under **Unreleased**.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
  (e.g. `fix:`, `feat:`, `docs:`).

By contributing you agree that your contributions are licensed under the
[Apache License, Version 2.0](LICENSE).
