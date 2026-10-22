from unittest.mock import MagicMock

import pytest
from plexapi.library import MusicSection

import plex_sections as ps


# ---------------------------------------------------------------------------
# default_music_section()
# ---------------------------------------------------------------------------

def test_default_music_section_returns_only_music_section():
    music_section = MagicMock(spec=MusicSection)
    plex = MagicMock()
    plex.library.sections.return_value = [music_section]

    assert ps.default_music_section(plex) is music_section


def test_default_music_section_raises_when_none_found():
    plex = MagicMock()
    plex.library.sections.return_value = []

    with pytest.raises(ValueError, match="No music library section"):
        ps.default_music_section(plex)


def test_default_music_section_raises_when_ambiguous():
    m1 = MagicMock(spec=MusicSection)
    m1.title = "Music A"
    m2 = MagicMock(spec=MusicSection)
    m2.title = "Music B"
    plex = MagicMock()
    plex.library.sections.return_value = [m1, m2]

    with pytest.raises(ValueError, match="Multiple music library sections"):
        ps.default_music_section(plex)


def test_default_music_section_ignores_non_music_sections():
    music_section = MagicMock(spec=MusicSection)
    non_music = MagicMock()  # not spec'd as MusicSection
    plex = MagicMock()
    plex.library.sections.return_value = [non_music, music_section]

    assert ps.default_music_section(plex) is music_section


# ---------------------------------------------------------------------------
# resolve_music_section()
# ---------------------------------------------------------------------------

def test_resolve_named_section_looks_up_by_name():
    named = MagicMock(spec=MusicSection)
    plex = MagicMock()
    plex.library.section.return_value = named

    assert ps.resolve_music_section(plex, "My Music") is named
    plex.library.section.assert_called_once_with("My Music")
    plex.library.sections.assert_not_called()


def test_resolve_without_name_falls_back_to_default():
    only = MagicMock(spec=MusicSection)
    plex = MagicMock()
    plex.library.sections.return_value = [only]

    assert ps.resolve_music_section(plex) is only
    plex.library.section.assert_not_called()
    plex.library.sections.assert_called_once()


def test_resolve_named_non_music_section_raises():
    not_music = MagicMock()  # not spec'd as MusicSection
    not_music.title = "Movies"
    plex = MagicMock()
    plex.library.section.return_value = not_music

    with pytest.raises(ValueError, match="is not a music library"):
        ps.resolve_music_section(plex, "Movies")


def test_resolve_default_propagates_ambiguous_error():
    m1 = MagicMock(spec=MusicSection)
    m1.title = "Music A"
    m2 = MagicMock(spec=MusicSection)
    m2.title = "Music B"
    plex = MagicMock()
    plex.library.sections.return_value = [m1, m2]

    with pytest.raises(ValueError, match="Multiple music library sections"):
        ps.resolve_music_section(plex)
