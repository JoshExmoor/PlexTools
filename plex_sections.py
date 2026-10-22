"""Shared helpers for resolving a Plex music library section.

Several scripts need the same behavior: use the section the user named with
``--section`` if they gave one, otherwise fall back to the server's single
music library (erroring if there are zero or more than one). This module holds
that logic in one place so the scripts stay consistent.
"""

from plexapi.library import MusicSection
from plexapi.server import PlexServer


def default_music_section(plex: PlexServer) -> MusicSection:
    """Returns the server's only music library section.

    Used when no section name is given. Raises ValueError if the server has no
    music library, or more than one (in which case the caller must disambiguate
    with an explicit section name).
    """
    music_sections = [s for s in plex.library.sections() if isinstance(s, MusicSection)]
    if not music_sections:
        raise ValueError("No music library section found on this Plex server.")
    if len(music_sections) > 1:
        names = ", ".join(s.title for s in music_sections)
        raise ValueError(
            f"Multiple music library sections found ({names}). "
            "Specify which one to use with --section."
        )
    return music_sections[0]


def resolve_music_section(plex: PlexServer, section: str = None) -> MusicSection:
    """Returns the music library section to operate on.

    If `section` is given, that named section is looked up (raising
    plexapi.exceptions.NotFound if the server has no such section). If `section`
    is None, falls back to :func:`default_music_section` - the server's single
    music library.

    In both cases the resolved section is validated to actually be a music
    library; a ValueError is raised if a non-music section was named.
    """
    if section:
        resolved = plex.library.section(section)
    else:
        resolved = default_music_section(plex)

    if not isinstance(resolved, MusicSection):
        raise ValueError(f'Library section "{resolved.title}" is not a music library.')

    return resolved
