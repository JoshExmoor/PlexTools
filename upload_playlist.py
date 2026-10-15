import argparse
import os
import re
import sys

import requests
from plexapi.exceptions import BadRequest, NotFound, PlexApiException, Unauthorized
from plexapi.library import MusicSection
from plexapi.playlist import Playlist
from plexapi.server import PlexServer

from get_plex_credentials import get_plex_credentials, retrieve_plex_credentials, save_plex_credentials

# A Windows-style path starts with a drive letter, e.g. "E:\..." or "e:/...".
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _default_music_section(plex: PlexServer) -> MusicSection:
    """Returns the first music library section, used when --section is not given."""
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


def _iter_track_file_paths(track):
    """Yields every resolvable file path for a track (handles multi-part media)."""
    for part in track.iterParts():
        if part.file:
            yield part.file


def detect_path_style(music_section: MusicSection, sample_size: int = 20) -> str:
    """Detects whether the Plex server stores paths Windows-style or POSIX-style.

    Samples up to `sample_size` tracks already in the library and inspects their
    stored file paths. Returns "windows" if the paths look like drive-letter
    paths (e.g. E:\\Music\\...), otherwise "posix". This matters because path
    case-sensitivity follows the SERVER's filesystem: Windows is
    case-insensitive (so "e:\\" and "E:\\" are the same file and we can safely
    normalize), while Linux is case-sensitive (so case must be preserved).

    Falls back to "posix" if no sample paths are available (the conservative
    choice, since it never rewrites case).
    """
    windows_votes = 0
    posix_votes = 0
    for track in music_section.searchTracks(libtype="track", maxresults=sample_size):
        for file_path in _iter_track_file_paths(track):
            if _WINDOWS_PATH_RE.match(file_path):
                windows_votes += 1
            elif file_path.startswith("/"):
                posix_votes += 1
            break  # one path per track is enough for a vote

    return "windows" if windows_votes > posix_votes else "posix"


def correct_path_for_server(path: str, style: str) -> str:
    """Rewrites a single m3u path into the form the Plex server expects.

    For "windows": upper-cases the drive letter and uses backslash separators,
    since a Windows Plex server stores paths that way and the server-side m3u
    match can fail on a lower-case drive letter.
    For "posix": left unchanged - POSIX paths are case-sensitive, so rewriting
    case would risk pointing at the wrong file or missing the right one.
    """
    path = path.strip()
    if style == "windows":
        if _WINDOWS_PATH_RE.match(path):
            path = path[0].upper() + path[1:]
        path = path.replace("/", "\\")
    return path


def normalize_path_for_compare(path: str, style: str) -> str:
    """Returns a canonical key for comparing two paths that point at the same file.

    For "windows": separators unified to "/" and the whole path lower-cased,
    since a Windows filesystem is case-insensitive.
    For "posix": separators left as "/" and case PRESERVED, since a Linux
    filesystem is case-sensitive and distinct-case paths are distinct files.
    """
    path = path.strip().replace("\\", "/")
    if style == "windows":
        path = path.lower()
    return path


def _parse_m3u_paths(m3u_path: str) -> list:
    """Returns the ordered list of file-path lines from an m3u file.

    Comment/directive lines (starting with '#') and blank lines are skipped;
    everything else is treated as a media path.
    """
    paths = []
    with open(m3u_path, "r", encoding="utf-8-sig") as m3u_file:
        for line in m3u_file:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            paths.append(line)
    return paths


def write_normalized_m3u(source_m3u: str, dest_m3u: str, style: str) -> list:
    """Writes a copy of `source_m3u` to `dest_m3u` with each path corrected for
    the server's path style (see correct_path_for_server). Non-path lines
    (#EXTINF, comments, blanks) are preserved as-is.

    Returns the ordered list of corrected file paths written, so the caller
    knows exactly which tracks the import was asked to add.
    """
    corrected_paths = []
    with open(source_m3u, "r", encoding="utf-8-sig") as src, \
            open(dest_m3u, "w", encoding="utf-8") as dst:
        for line in src:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                dst.write(line if line.endswith("\n") else line + "\n")
                continue
            corrected = correct_path_for_server(stripped, style)
            corrected_paths.append(corrected)
            dst.write(corrected + "\n")
    return corrected_paths


def reconcile_playlist(
    plex: PlexServer,
    playlist: Playlist,
    music_section: MusicSection,
    expected_paths: list,
    style: str,
) -> dict:
    """Compares a playlist's actual contents against the paths it was meant to
    contain, and back-fills any tracks the m3u import silently dropped.

    Plex's m3u import matches each line to a library item server-side and
    silently skips any it can't match, with no per-track feedback. This builds
    a {normalized_path: Track} map of every track in `music_section`, finds the
    `expected_paths` entries that are not already in `playlist`, looks each one
    up in that map, and adds the ones it can find via Playlist.addItems.

    Returns a report dict with keys:
        already_present - count of expected paths already in the playlist
        added           - list of paths that were found and added
        unresolvable    - list of paths not found anywhere in the section
    """
    # Build a lookup of every track in the section keyed by normalized path.
    # NOTE: this fetches the whole section once; on a large library this is the
    # slowest/most memory-intensive step.
    path_to_track = {}
    for track in music_section.searchTracks(libtype="track"):
        for file_path in _iter_track_file_paths(track):
            path_to_track[normalize_path_for_compare(file_path, style)] = track

    # What the playlist actually contains right now.
    present_keys = set()
    for track in playlist.items():
        for file_path in _iter_track_file_paths(track):
            present_keys.add(normalize_path_for_compare(file_path, style))

    already_present = 0
    to_add = []
    unresolvable = []
    seen = set()
    for path in expected_paths:
        key = normalize_path_for_compare(path, style)
        if key in seen:
            continue  # avoid double-adding a duplicate m3u entry
        seen.add(key)
        if key in present_keys:
            already_present += 1
        elif key in path_to_track:
            to_add.append((path, path_to_track[key]))
        else:
            unresolvable.append(path)

    if to_add:
        playlist.addItems([track for _, track in to_add])

    return {
        "already_present": already_present,
        "added": [path for path, _ in to_add],
        "unresolvable": unresolvable,
    }


def delete_existing_playlist(plex: PlexServer, title: str) -> bool:
    """Deletes the playlist with the given title if one exists.

    Returns True if a playlist was deleted, False if none existed. Used to make
    an upload start from scratch rather than merging into/duplicating a
    pre-existing playlist of the same name.
    """
    try:
        existing = plex.playlist(title)
    except NotFound:
        return False
    existing.delete()
    return True


def upload_playlist(
    plex: PlexServer,
    m3u_path: str,
    title: str,
    section: str = None,
    reconcile: bool = True,
    replace: bool = False,
) -> Playlist:
    """Uploads an m3u playlist file to Plex, creating a playlist named `title`.

    NOTE: m3u import is a server-side operation. `m3u_path` must be a path that
    the Plex Media Server process itself can read (its local filesystem, or a
    volume it has mounted) - it does not need to be reachable from the machine
    running this script, and a path that only exists locally here will fail
    with a BadRequest from the server. Music libraries only.

    When `replace` is True, any existing playlist with the same `title` is
    deleted first, so the upload starts from scratch rather than merging new
    tracks into (or duplicating) a pre-existing playlist of that name. This is
    destructive to the old playlist and is off by default.

    Before importing, the server's path style is detected from existing tracks
    and a normalized copy of the m3u is written alongside the original (with
    drive-letter case / separators corrected on Windows servers), which the
    import uses instead of the raw file. The temp copy is always deleted
    afterward.

    When `reconcile` is True (default), after the import the resulting playlist
    is compared against the m3u's track list and any tracks the import silently
    dropped - but which do exist in the library - are added via the playlist
    API. See reconcile_playlist for the report shape.
    """
    music_section = plex.library.section(section) if section else _default_music_section(plex)

    if not isinstance(music_section, MusicSection):
        raise ValueError(f'Library section "{music_section.title}" is not a music library.')

    if replace and delete_existing_playlist(plex, title):
        print(f'Removed existing playlist "{title}" before upload.')

    style = detect_path_style(music_section)

    # Write a normalized temp m3u next to the original (same directory, so a
    # same-host Plex server can read it just like the original), import that,
    # then always clean it up.
    source_dir = os.path.dirname(os.path.abspath(m3u_path))
    source_name = os.path.basename(m3u_path)
    normalized_m3u = os.path.join(source_dir, f".normalized_{source_name}")

    try:
        expected_paths = write_normalized_m3u(m3u_path, normalized_m3u, style)
        try:
            playlist = Playlist.create(
                plex, title=title, section=music_section, m3ufilepath=normalized_m3u
            )
        except BadRequest as exc:
            raise BadRequest(
                f"Failed to import m3u playlist from {m3u_path}. Note: this path must be "
                f"readable by the Plex server itself, not just this machine. ({exc})"
            ) from exc
    finally:
        if os.path.exists(normalized_m3u):
            os.remove(normalized_m3u)

    imported_count = len(playlist.items())
    print(
        f'Uploaded playlist "{title}" from {m3u_path} '
        f"(detected {style} path style, {imported_count}/{len(expected_paths)} tracks "
        f"matched on import)."
    )

    if reconcile:
        report = reconcile_playlist(plex, playlist, music_section, expected_paths, style)
        if report["added"]:
            print(f"Reconciliation added {len(report['added'])} track(s) the import missed.")
        if report["unresolvable"]:
            print(
                f"{len(report['unresolvable'])} track(s) in the m3u could not be found in "
                f'the "{music_section.title}" library and were not added:',
                file=sys.stderr,
            )
            for path in report["unresolvable"]:
                print(f"  - {path}", file=sys.stderr)

    return playlist


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Upload an m3u playlist file to Plex. NOTE: the m3u path must be "
            "readable by the Plex server process itself, not just this machine."
        ),
    )
    parser.add_argument(
        "-i", "--m3u-path",
        required=True,
        help="Path to the m3u playlist file, as seen by the Plex server.",
    )
    parser.add_argument(
        "-t", "--title",
        required=True,
        help="Title to give the created playlist in Plex.",
    )
    parser.add_argument(
        "-s", "--section",
        default=None,
        help="Music library section name to import into. Defaults to the server's only music library.",
    )
    parser.add_argument(
        "-R", "--no-reconcile",
        action="store_true",
        help=(
            "Skip the post-import reconciliation step that back-fills tracks "
            "the m3u import silently dropped. By default reconciliation runs."
        ),
    )
    parser.add_argument(
        "-x", "--replace",
        action="store_true",
        help=(
            "Delete any existing playlist with the same title before uploading, "
            "so the playlist starts from scratch instead of merging into or "
            "duplicating the existing one. Destructive to the old playlist."
        ),
    )
    parser.add_argument(
        "-l", "--url",
        default=None,
        help=(
            "Base URL of the Plex server, e.g. http://localhost:32400. "
            "Required unless --credential-file is given."
        ),
    )
    parser.add_argument(
        "-u", "--username",
        default=None,
        help="Plex username. Prompted via console input if omitted.",
    )
    parser.add_argument(
        "-p", "--password",
        default=None,
        help=(
            "Plex password. Prompted via a hidden console prompt if omitted. "
            "NOTE: passing this on the command line may expose it in shell "
            "history and process listings; prefer omitting it."
        ),
    )
    parser.add_argument(
        "-c", "--credential-file",
        default=None,
        help=(
            "Path to a JSON credential file previously saved with "
            "--save-creds. If given, login is skipped and credentials are "
            "loaded from this file instead."
        ),
    )
    parser.add_argument(
        "-S", "--save-creds",
        default=None,
        help="Path to save the retrieved credentials to as a JSON file cache.",
    )
    args = parser.parse_args(argv)

    if not args.credential_file and not args.url:
        parser.error("--url is required unless --credential-file is given.")

    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        if args.credential_file:
            plex_credentials = retrieve_plex_credentials(args.credential_file, url=args.url)
        else:
            plex_credentials = get_plex_credentials(
                url=args.url,
                username=args.username,
                password=args.password,
            )

        if args.save_creds:
            save_plex_credentials(plex_credentials, args.save_creds)

        plex = PlexServer(plex_credentials["url"], plex_credentials["token"])
        upload_playlist(
            plex,
            args.m3u_path,
            args.title,
            section=args.section,
            reconcile=not args.no_reconcile,
            replace=args.replace,
        )
    except (
        Unauthorized,
        NotFound,
        PlexApiException,
        requests.exceptions.RequestException,
        RuntimeError,
        FileNotFoundError,
        KeyError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
