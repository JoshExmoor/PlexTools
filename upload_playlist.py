import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests
from plexapi.exceptions import BadRequest, NotFound, PlexApiException, Unauthorized
from plexapi.library import MusicSection
from plexapi.playlist import Playlist
from plexapi.server import PlexServer
from tqdm import tqdm

from get_plex_credentials import get_plex_credentials, retrieve_plex_credentials, save_plex_credentials
from plex_sections import default_music_section as _default_music_section, resolve_music_section

# A Windows-style path starts with a drive letter, e.g. "E:\..." or "e:/...".
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _iter_track_file_paths(track):
    """Yields every resolvable file path for a track (handles multi-part media)."""
    for part in track.iterParts():
        if part.file:
            yield part.file


# Number of tracks to request per page when fetching a whole section. Plex's
# own default container size is 100; a larger page means fewer round-trips.
_TRACK_PAGE_SIZE = 200


def iter_section_tracks(music_section: MusicSection, page_size: int = _TRACK_PAGE_SIZE):
    """Yields every track in a music section, one page at a time.

    plexapi's searchTracks() fetches the ENTIRE section in a single blocking
    call (it pages internally but returns nothing until done), so it can't
    report progress. This drives the same pagination ourselves - requesting one
    page of `page_size` tracks per call via container_start/container_size - and
    yields each track as its page arrives, so a caller can show real progress as
    the fetch proceeds. Stops when a page comes back empty.
    """
    offset = 0
    while True:
        page = music_section.searchTracks(
            container_start=offset,
            container_size=page_size,
            maxresults=page_size,
        )
        if not page:
            break
        for track in page:
            yield track
        if len(page) < page_size:
            break  # last (partial) page - no need for another round-trip
        offset += len(page)


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
    for track in music_section.searchTracks(maxresults=sample_size):
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


def _cache_dir() -> str:
    """Returns the directory where section track caches are stored."""
    return str(Path.home() / ".cache" / "plextools")


def _cache_path(section_uuid: str) -> str:
    """Returns the cache file path for a given library section uuid."""
    return os.path.join(_cache_dir(), f"section_{section_uuid}.json")


def _section_fingerprint(music_section: MusicSection) -> dict:
    """Returns the identity/validity fields used to decide if a cache is fresh.

    uuid identifies the section; updatedAt and track_count together detect
    content changes. updatedAt is NOT bumped by playlist create/modify/delete
    (verified against a live server), so playlist churn won't invalidate the
    cache - only actual library content changes do.
    """
    try:
        track_count = int(music_section.totalViewSize(libtype="track"))
    except Exception:
        track_count = None
    return {
        "uuid": music_section.uuid,
        "updatedAt": str(music_section.updatedAt),
        "track_count": track_count,
    }


def load_track_cache(music_section: MusicSection) -> dict:
    """Returns the cached {normalized_path: ratingKey} map plus the detected
    path style for `music_section`, or None if there is no valid fresh cache.

    The cache is considered valid only if the section's uuid, updatedAt, and
    track count all match what was stored. Any mismatch (or a missing/corrupt
    file) returns None so the caller does a fresh fetch.
    """
    path = _cache_path(music_section.uuid)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as cache_file:
            cached = json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        return None

    fingerprint = _section_fingerprint(music_section)
    if (
        cached.get("uuid") != fingerprint["uuid"]
        or cached.get("updatedAt") != fingerprint["updatedAt"]
        or cached.get("track_count") != fingerprint["track_count"]
    ):
        return None

    tracks = cached.get("tracks")
    style = cached.get("style")
    if not isinstance(tracks, dict) or style not in ("windows", "posix"):
        return None  # malformed cache
    return {"tracks": tracks, "style": style}


def save_track_cache(music_section: MusicSection, path_to_rating_key: dict, style: str) -> None:
    """Writes the {normalized_path: ratingKey} map and detected style to the
    section's cache file, tagged with the current validity fingerprint.
    """
    os.makedirs(_cache_dir(), exist_ok=True)
    fingerprint = _section_fingerprint(music_section)
    payload = {
        "uuid": fingerprint["uuid"],
        "updatedAt": fingerprint["updatedAt"],
        "track_count": fingerprint["track_count"],
        "style": style,
        "tracks": path_to_rating_key,
    }
    with open(_cache_path(music_section.uuid), "w", encoding="utf-8") as cache_file:
        json.dump(payload, cache_file, indent=2)


def reconcile_playlist(
    plex: PlexServer,
    playlist: Playlist,
    music_section: MusicSection,
    expected_paths: list,
    style: str,
    use_cache: bool = True,
) -> dict:
    """Compares a playlist's actual contents against the paths it was meant to
    contain, and back-fills any tracks the m3u import silently dropped.

    Plex's m3u import matches each line to a library item server-side and
    silently skips any it can't match, with no per-track feedback. This builds
    a {normalized_path: ratingKey} map of every track in `music_section`, finds
    the `expected_paths` entries that are not already in `playlist`, resolves
    those to Track objects, and adds them via Playlist.addItems.

    When `use_cache` is True (default), the section's path->ratingKey map is
    loaded from a local cache if one exists and is still valid (same uuid,
    updatedAt, and track count); on a miss it is fetched from the server and the
    cache is refreshed. Pass use_cache=False to always fetch fresh.

    Returns a report dict with keys:
        already_present - count of expected paths already in the playlist
        added           - list of paths that were found and added
        unresolvable    - list of paths not found anywhere in the section
    """
    # path_to_rating_key maps normalized file path -> track ratingKey (a stable
    # server id). We key on ratingKey rather than live Track objects so the map
    # can be cached as plain JSON; the few tracks actually being added are
    # resolved back to Track objects at the end via a single bulk fetch.
    path_to_rating_key = None

    if use_cache:
        cached = load_track_cache(music_section)
        if cached is not None:
            path_to_rating_key = cached["tracks"]
            style = cached["style"]  # trust the cached style for this section
            print(f"Using cached library track index ({len(path_to_rating_key)} tracks).")

    if path_to_rating_key is None:
        # Cache miss (or disabled): fetch the whole section. This is the slowest
        # step on a large library, so we page through it with iter_section_tracks
        # (rather than searchTracks(), which blocks until the entire fetch
        # completes) so the progress bar advances as each page actually arrives.
        try:
            total_tracks = int(music_section.totalViewSize(libtype="track"))
        except Exception:
            total_tracks = None

        path_to_rating_key = {}
        for track in tqdm(
            iter_section_tracks(music_section),
            total=total_tracks,
            desc="Fetching library tracks",
            unit="track",
        ):
            for file_path in _iter_track_file_paths(track):
                path_to_rating_key[normalize_path_for_compare(file_path, style)] = int(track.ratingKey)

        if use_cache:
            save_track_cache(music_section, path_to_rating_key, style)

    # What the playlist actually contains right now.
    present_keys = set()
    for track in playlist.items():
        for file_path in _iter_track_file_paths(track):
            present_keys.add(normalize_path_for_compare(file_path, style))

    already_present = 0
    added_paths = []
    rating_keys_to_add = []
    unresolvable = []
    seen = set()
    for path in expected_paths:
        key = normalize_path_for_compare(path, style)
        if key in seen:
            continue  # avoid double-adding a duplicate m3u entry
        seen.add(key)
        if key in present_keys:
            already_present += 1
        elif key in path_to_rating_key:
            added_paths.append(path)
            rating_keys_to_add.append(int(path_to_rating_key[key]))
        else:
            unresolvable.append(path)

    if rating_keys_to_add:
        # Resolve only the tracks being added (one bulk request), then add them.
        # Build the /library/metadata/<k1,k2,...> key string explicitly rather
        # than passing a list of ints: the string form of fetchItems is the
        # universally-supported contract, while the list-of-ints convenience is
        # version-dependent (older plexapi builds the URL wrong and raises
        # "Failed to parse: <baseurl>[...]").
        metadata_key = "/library/metadata/" + ",".join(str(k) for k in rating_keys_to_add)
        tracks_to_add = plex.fetchItems(metadata_key)
        playlist.addItems(tracks_to_add)

    return {
        "already_present": already_present,
        "added": added_paths,
        "unresolvable": unresolvable,
    }


def find_playlists_by_title(plex: PlexServer, title: str) -> list:
    """Returns all playlists whose title matches `title` case-insensitively.

    Plex keys m3u-imported playlists by the m3u file PATH, not the title, so a
    changed m3u path can leave multiple playlists sharing one name. This finds
    every one of them so callers can warn about or clean up duplicates.
    """
    wanted = title.casefold()
    return [p for p in plex.playlists() if (p.title or "").casefold() == wanted]


def _format_playlist_line(playlist) -> str:
    """One-line human-readable summary of a playlist's identifying metadata."""
    return (
        f'  - "{playlist.title}" (ratingKey={playlist.ratingKey}, '
        f"items={playlist.leafCount}, added={playlist.addedAt}, "
        f"updated={playlist.updatedAt})"
    )


def warn_if_duplicate_playlists(plex: PlexServer, title: str) -> list:
    """Prints a warning (to stderr) if more than one playlist shares `title`.

    Returns the list of matching playlists. The warning lists each duplicate's
    metadata and tells the user how to resolve it (re-run with
    --remove-duplicates). Does not delete anything.
    """
    matches = find_playlists_by_title(plex, title)
    if len(matches) > 1:
        print(
            f'\nWARNING: {len(matches)} playlists named "{title}" exist on the server '
            f"(Plex keys m3u-imported playlists by m3u file path, not title, so a "
            f"changed m3u path can create a duplicate):",
            file=sys.stderr,
        )
        for playlist in sorted(matches, key=lambda p: p.addedAt or ""):
            print(_format_playlist_line(playlist), file=sys.stderr)
        print(
            "To remove all but the just-uploaded copy, re-run this command with "
            "--remove-duplicates.",
            file=sys.stderr,
        )
    return matches


def remove_duplicate_playlists(plex: PlexServer, title: str, keep: Playlist) -> list:
    """Deletes every playlist titled `title` (case-insensitive) EXCEPT `keep`.

    `keep` is identified by ratingKey, so the just-uploaded playlist is always
    preserved. Returns the list of deleted playlists' ratingKeys. Intended to be
    run after an upload+reconcile to clean up pre-existing same-named duplicates.
    """
    deleted = []
    for playlist in find_playlists_by_title(plex, title):
        if playlist.ratingKey == keep.ratingKey:
            continue
        playlist.delete()
        deleted.append(playlist.ratingKey)
    return deleted


def upload_playlist(
    plex: PlexServer,
    m3u_path: str,
    title: str,
    section: str = None,
    reconcile: bool = True,
    use_cache: bool = True,
    remove_duplicates: bool = False,
) -> Playlist:
    """Uploads an m3u playlist file to Plex, creating a playlist named `title`.

    NOTE: m3u import is a server-side operation. `m3u_path` must be a path that
    the Plex Media Server process itself can read (its local filesystem, or a
    volume it has mounted) - it does not need to be reachable from the machine
    running this script, and a path that only exists locally here will fail
    with a BadRequest from the server. Music libraries only.

    Plex identifies an m3u-imported playlist by the m3u file PATH (not its
    title), overwriting the playlist previously imported from that same path.
    Because this tool imports a normalized temp copy at a stable path per source
    m3u, repeated uploads of the same m3u overwrite the same playlist. But if
    the m3u path ever changes, Plex creates a NEW playlist with the same title
    rather than overwriting - leaving same-named duplicates.

    After upload (and reconcile), if more than one playlist shares `title` a
    warning is printed describing them. When `remove_duplicates` is True, all
    but the just-uploaded playlist are then deleted (identified by ratingKey, so
    the fresh upload is always kept). remove_duplicates is off by default
    because a same-named playlist could be an unrelated (or smart) playlist the
    user wants to keep.

    Before importing, the server's path style is detected from existing tracks
    and a normalized copy of the m3u is written alongside the original (with
    drive-letter case / separators corrected on Windows servers), which the
    import uses instead of the raw file. The temp copy is always deleted
    afterward.

    When `reconcile` is True (default), after the import the resulting playlist
    is compared against the m3u's track list and any tracks the import silently
    dropped - but which do exist in the library - are added via the playlist
    API. See reconcile_playlist for the report shape.

    When `use_cache` is True (default), reconciliation reuses a local cache of
    the section's track index if one is still valid, avoiding a full re-fetch
    of the library on repeated runs. Pass use_cache=False to force a fresh
    fetch (see reconcile_playlist / load_track_cache).
    """
    music_section = resolve_music_section(plex, section)

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
        report = reconcile_playlist(
            plex, playlist, music_section, expected_paths, style, use_cache=use_cache
        )
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

    # Detect same-named duplicates (Plex m3u imports are keyed by path, not
    # title). Optionally clean them up, keeping only this upload.
    if remove_duplicates:
        removed = remove_duplicate_playlists(plex, title, keep=playlist)
        if removed:
            print(
                f'Removed {len(removed)} duplicate playlist(s) named "{title}" '
                f"(kept the just-uploaded one, ratingKey={playlist.ratingKey})."
            )
    else:
        warn_if_duplicate_playlists(plex, title)

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
        "-D", "--remove-duplicates",
        action="store_true",
        help=(
            "After uploading, delete any OTHER playlists sharing this title "
            "(case-insensitive), keeping only the just-uploaded one. Use this to "
            "resolve the duplicate-playlist warning. Destructive: a same-named "
            "playlist you created separately (including a smart playlist) would "
            "also be deleted, so review the warning's list first."
        ),
    )
    parser.add_argument(
        "-C", "--no-cache",
        action="store_true",
        help=(
            "Force reconciliation to re-fetch the whole library instead of "
            "reusing the local section track cache. The cache is validated "
            "against the server (uuid/updatedAt/track count) and refreshed "
            "automatically, so this is rarely needed."
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
            use_cache=not args.no_cache,
            remove_duplicates=args.remove_duplicates,
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
