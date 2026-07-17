<!-- Thanks for contributing! Keep the change focused; see CONTRIBUTING.md. -->

## What & why

<!-- What does this change do, and what problem does it solve? Link any issue. -->

## Checklist

- [ ] Tests pass: `cd seestar/bridge && uv run --python 3.12 --no-project --with pytest --with paho-mqtt --with Pillow python -m pytest -q`
- [ ] Lint clean: `uvx ruff@0.12.5 check seestar/bridge`
- [ ] If an add-on option or entity changed: `DOCS.md`, `translations/en.yaml`, and `examples/stargazing.yaml` updated to match
- [ ] `CHANGELOG.md` updated under **Unreleased** (for user-facing changes)
- [ ] Commits use Conventional Commit prefixes
