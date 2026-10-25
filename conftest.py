import os
import sys

import pytest

# Make the project root importable as top-level modules (get_plex_credentials,
# push_plex_ratings_by_playlist) regardless of where pytest is invoked from.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plex_style  # noqa: E402


@pytest.fixture(autouse=True)
def _plain_output(monkeypatch):
    """Keeps color off (and a fixed 120-column width) around every test so a main() run can't leak it."""
    monkeypatch.setattr(plex_style, "_vt_state", "")
    plex_style.reset(width=120)
    yield
    plex_style.reset(width=120)
