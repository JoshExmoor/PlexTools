import argparse
import json
import os
import sys

import requests
from plexapi.exceptions import NotFound, PlexApiException, Unauthorized
from plexapi.library import MusicSection
from plexapi.server import PlexServer
from tqdm import tqdm

from get_plex_credentials import get_plex_credentials, retrieve_plex_credentials, save_plex_credentials
from plex_sections import resolve_music_section

# Number of tracks to request per page. Plex's own default container size is
# 100; a larger page means fewer round-trips.
_TRACK_PAGE_SIZE = 200


def _attr(obj, name):
    """Returns getattr(obj, name), or None if the attribute is absent.

    Tracks returned from a paginated searchTracks() come back as "partial"
    objects: Plex's list view omits attributes that aren't set for that track
    (e.g. `rating`, `audienceRating`, `ratingCount`). plexapi normally
    auto-reloads the full object to fill a missing attribute, but that
    auto-reload is disabled on searched items, so a plain `track.rating`
    raises AttributeError instead of yielding None. Reading through this
    helper treats "absent" as None, which is what we want in the export.
    """
    return getattr(obj, name, None)


def _iso(value):
    """Returns an ISO 8601 string for a datetime, or None.

    plexapi exposes datetimes (addedAt, lastViewedAt, ...) as datetime objects,
    which json.dump can't serialize. This converts them to ISO strings and
    passes through None unchanged.
    """
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return value


def _tag_values(tag_items):
    """Returns a list of tag names from a list of plexapi tag objects.

    Covers genres, moods, collections, labels, etc. - each item exposes its
    display name as `.tag`. Items missing a tag are skipped so the result is a
    clean list of non-empty strings (CSV-friendly once joined).
    """
    if not tag_items:
        return []
    values = []
    for item in tag_items:
        name = getattr(item, "tag", None)
        if name:
            values.append(name)
    return values


def _guid_values(guid_items):
    """Returns a list of external GUID strings (e.g. MusicBrainz IDs).

    Track/album/artist `guids` entries expose the identifier as `.id`
    (e.g. 'mbid://...'), distinct from the single top-level Plex `guid`.
    """
    if not guid_items:
        return []
    values = []
    for item in guid_items:
        gid = getattr(item, "id", None)
        if gid:
            values.append(gid)
    return values


def _describe_file(part) -> dict:
    """Returns a dict of technical data for a single media part (one file).

    `part` is a plexapi MediaPart; `part._parent()` is the Media object that
    carries container/bitrate/codec-level data. The first audio stream (if
    any) carries codec/sample-rate/bit-depth detail.
    """
    media = None
    try:
        media = part._parent()
    except Exception:  # noqa: BLE001 - defensive; missing parent just means less detail
        media = None

    audio_stream = None
    try:
        audio_streams = part.audioStreams()
        if audio_streams:
            audio_stream = audio_streams[0]
    except Exception:  # noqa: BLE001 - stream data is optional
        audio_stream = None

    part_duration = _attr(part, "duration")
    return {
        "path": _attr(part, "file"),
        "fileSize": _attr(part, "size"),
        "container": _attr(part, "container") or (_attr(media, "container") if media else None),
        "bitrate": _attr(media, "bitrate") if media else None,
        "durationMs": part_duration if part_duration is not None else (_attr(media, "duration") if media else None),
        "audioCodec": _attr(media, "audioCodec") if media else None,
        "audioChannels": _attr(media, "audioChannels") if media else None,
        "codec": _attr(audio_stream, "codec") if audio_stream else None,
        "channels": _attr(audio_stream, "channels") if audio_stream else None,
        "samplingRate": _attr(audio_stream, "samplingRate") if audio_stream else None,
        "bitDepth": _attr(audio_stream, "bitDepth") if audio_stream else None,
        "bitrateMode": _attr(audio_stream, "bitrateMode") if audio_stream else None,
        "audioChannelLayout": _attr(audio_stream, "audioChannelLayout") if audio_stream else None,
    }


def _track_files(track) -> list:
    """Returns a list of per-file technical dicts for every part of a track.

    A track can have more than one Media/Part (e.g. duplicate versions); this
    captures them all. Each entry is a dict from _describe_file().
    """
    files = []
    for media in _attr(track, "media") or []:
        for part in _attr(media, "parts") or []:
            files.append(_describe_file(part))
    return files


def _track_record(track) -> dict:
    """Builds a single flat, denormalized record for one track.

    The record is designed to double as a CSV row: every scalar a CSV export
    would want is a top-level key (the first file's technical fields are
    flattened up to the top level via `path`, `fileSize`, ...). The nested
    `files` list and the tag lists (`genres`, `moods`, ...) are richer extras
    for the JSON case; a CSV export can join the lists with a separator and
    use the top-level file fields for the single-row-per-track view.
    """
    files = _track_files(track)
    primary = files[0] if files else {}

    # Tracks from a paginated search come back partial: any attribute Plex
    # didn't include in the list view raises AttributeError on access (the
    # usual auto-reload is disabled on searched items). Read every track
    # attribute through _attr so an absent value becomes None instead.
    return {
        # Identity / sort
        "title": _attr(track, "title"),
        "titleSort": _attr(track, "titleSort"),
        "trackNumber": _attr(track, "index"),
        "discNumber": _attr(track, "parentIndex"),
        "ratingKey": _attr(track, "ratingKey"),
        "guid": _attr(track, "guid"),
        # Artist / album (denormalized onto every track)
        "artist": _attr(track, "grandparentTitle"),
        "albumArtist": _attr(track, "grandparentTitle"),
        "trackArtist": _attr(track, "originalTitle"),
        "album": _attr(track, "parentTitle"),
        "year": _attr(track, "year"),
        "artistRatingKey": _attr(track, "grandparentRatingKey"),
        "albumRatingKey": _attr(track, "parentRatingKey"),
        # Ratings / plays
        "userRating": _attr(track, "userRating"),
        "rating": _attr(track, "rating"),
        "audienceRating": _attr(track, "audienceRating"),
        "ratingCount": _attr(track, "ratingCount"),
        "playCount": _attr(track, "viewCount"),
        "skipCount": _attr(track, "skipCount"),
        "lastViewedAt": _iso(_attr(track, "lastViewedAt")),
        "lastRatedAt": _iso(_attr(track, "lastRatedAt")),
        "addedAt": _iso(_attr(track, "addedAt")),
        "updatedAt": _iso(_attr(track, "updatedAt")),
        # Tags (lists of strings; CSV export joins with a separator)
        "genres": _tag_values(_attr(track, "genres")),
        "moods": _tag_values(_attr(track, "moods")),
        "collections": _tag_values(_attr(track, "collections")),
        "labels": _tag_values(_attr(track, "labels")),
        "guids": _guid_values(_attr(track, "guids")),
        # Primary file technical data flattened to top level (CSV-ready)
        "path": primary.get("path"),
        "fileSize": primary.get("fileSize"),
        "container": primary.get("container"),
        "bitrate": primary.get("bitrate"),
        "durationMs": primary.get("durationMs"),
        "audioCodec": primary.get("audioCodec"),
        "audioChannels": primary.get("audioChannels"),
        "codec": primary.get("codec"),
        "channels": primary.get("channels"),
        "samplingRate": primary.get("samplingRate"),
        "bitDepth": primary.get("bitDepth"),
        "bitrateMode": primary.get("bitrateMode"),
        "audioChannelLayout": primary.get("audioChannelLayout"),
        # Full per-file detail (JSON only; covers multi-part tracks)
        "files": files,
    }


def iter_tracks(
    music_section: MusicSection,
    search: str = None,
    artist: str = None,
    album: str = None,
    page_size: int = _TRACK_PAGE_SIZE,
):
    """Yields every track in the section (optionally scoped), one page at a time.

    plexapi's searchTracks() fetches the whole result set in a single blocking
    call (it pages internally but returns nothing until done), so it can't
    report progress - which is slow and silent on a large library. This drives
    the pagination ourselves, requesting one page of `page_size` tracks per
    call, and yields each track as its page arrives so a caller can show live
    progress. Stops when a page comes back empty.

    Optional scoping (all applied server-side, matching Plex's own filters):
        search  - case-insensitive substring match on the track title
        artist  - case-insensitive substring match on the (album) artist name
        album   - case-insensitive substring match on the album name
    """
    filters = {}
    if search:
        filters["title__icontains"] = search
    if artist:
        filters["artist.title__icontains"] = artist
    if album:
        filters["album.title__icontains"] = album

    offset = 0
    while True:
        page = music_section.searchTracks(
            filters=filters,
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


def retrieve_plex_library(
    plex: PlexServer,
    section: str = None,
    search: str = None,
    artist: str = None,
    album: str = None,
) -> list:
    """Retrieves a flat, denormalized record for every track in a music section.

    Returns a list of dicts, one per track, each carrying its own artist and
    album metadata (denormalized so albums/artists split across Plex groupings
    are still visible per track), ratings, play/skip counts, tag lists, and
    full per-file technical data. See _track_record() for the exact shape.

    If `section` is None (the default), the server's single music library is
    used; pass a name to pick one out of a server with several.

    By default every track in the section is returned. The optional `search`,
    `artist`, and `album` arguments scope the dump via Plex's server-side
    filters (case-insensitive substring match on title / artist / album).

    Tracks are fetched page-by-page with a progress bar, since pulling them -
    with full media/stream detail - from a large library is slow; the bar
    shows a live count and rate (there's no cheap server-side count to use as a
    percentage total).

    NOTE: `path` (and each entry in `files`) is the file location as the Plex
    server sees it, which may not resolve locally if this script runs on a
    different machine than the server.
    """
    music_section = resolve_music_section(plex, section)

    records = []
    for track in tqdm(
        iter_tracks(music_section, search=search, artist=artist, album=album),
        desc="Fetching tracks",
        unit=" tracks",
    ):
        records.append(_track_record(track))

    return records


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Retrieve a flat, denormalized record of every track in a Plex "
            "music library (ratings, play counts, tags, and full per-file "
            "technical data) and save it to a JSON file."
        ),
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        help="Path to write the library export to, as a JSON file.",
    )
    parser.add_argument(
        "-s", "--section",
        default=None,
        help="Music library section name to read from. Defaults to the server's only music library.",
    )
    parser.add_argument(
        "--search",
        default=None,
        help="Limit the export to tracks whose title contains this string (case-insensitive).",
    )
    parser.add_argument(
        "--artist",
        default=None,
        help="Limit the export to tracks whose artist name contains this string (case-insensitive).",
    )
    parser.add_argument(
        "--album",
        default=None,
        help="Limit the export to tracks whose album name contains this string (case-insensitive).",
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
        records = retrieve_plex_library(
            plex,
            section=args.section,
            search=args.search,
            artist=args.artist,
            album=args.album,
        )

        directory = os.path.dirname(os.path.abspath(args.output))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as output_file:
            json.dump(records, output_file, indent=2)
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

    missing_paths = sum(1 for entry in records if not entry["path"])
    print(f"Exported {len(records)} track(s) to {args.output}.")
    if missing_paths:
        print(
            f"{missing_paths} track(s) had no resolvable file path (saved with path=null).",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
