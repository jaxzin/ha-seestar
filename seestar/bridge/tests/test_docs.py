"""The operator docs stay in lock-step with the Phase-2 control catalog.

``seestar/DOCS.md`` promises specific entity names, ranges, and modes; these
tests parse the file so the promises can't silently drift from the code:

- every markdown table is well-formed (each row matches the header's column
  count, so nothing renders as a broken table);
- every Phase-2 command entity name appears in the docs;
- the documented Stack-exposure range (ms) and imaging-mode options are the
  catalog's own values, not hand-copied numbers that could go stale.
"""
from __future__ import annotations

from pathlib import Path

from seestar_bridge import control
from seestar_bridge.entities import CONTROL_ENTITIES

# tests/ -> bridge/ -> seestar/ (the add-on directory that ships DOCS.md).
_DOCS_PATH = Path(__file__).resolve().parents[2] / "DOCS.md"

#: The select whose options feed 'Start live view'; its docs row must list them.
_IMAGING_MODE_KEY = "imaging_mode"


def _docs_text() -> str:
    return _DOCS_PATH.read_text(encoding="utf-8")


def _tables(text: str) -> list[list[str]]:
    """Group consecutive ``|``-prefixed lines into markdown tables."""
    tables: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            current.append(line.strip())
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _column_count(row: str) -> int:
    # Every DOCS.md table row uses leading + trailing pipes and contains no
    # escaped ``\|``, so the column count is the delimiter count minus one.
    return row.count("|") - 1


def test_docs_tables_are_well_formed():
    tables = _tables(_docs_text())
    assert tables, "DOCS.md should contain at least one markdown table"
    for table in tables:
        header, *rest = table
        assert rest, f"table has a header but no delimiter/body rows: {header!r}"
        columns = _column_count(header)
        assert columns >= 2, f"not a plausible table header: {header!r}"
        for row in rest:
            assert _column_count(row) == columns, (
                f"row has {_column_count(row)} columns where the header has "
                f"{columns}: {row!r}")


def test_every_control_entity_is_documented():
    text = _docs_text()
    for entity in CONTROL_ENTITIES:
        assert f"**{entity.name}**" in text, (
            f"control entity {entity.name!r} is missing from DOCS.md")


def test_documented_exposure_range_matches_catalog():
    # The Controls table documents 'Stack exposure' as ms with the catalog's
    # exact bounds (an en-dash range, e.g. ``1–60000``).
    expected = f"{control.EXPOSURE_MIN_MS}–{control.EXPOSURE_MAX_MS}"
    assert expected in _docs_text()


def test_documented_gain_range_matches_catalog():
    # The Controls table documents 'Gain' with the catalog's exact bounds
    # (an en-dash range, e.g. ``0–300``), not hand-copied numbers.
    expected = f"{control.GAIN_MIN}–{control.GAIN_MAX}"
    assert expected in _docs_text()


def test_documented_imaging_modes_match_catalog():
    text = _docs_text()
    imaging_mode = next(
        entry for entry in CONTROL_ENTITIES if entry.key == _IMAGING_MODE_KEY)
    assert imaging_mode.options, "imaging_mode select must declare options"
    for mode in imaging_mode.options:
        assert f"`{mode}`" in text, f"imaging mode {mode!r} missing from DOCS.md"
