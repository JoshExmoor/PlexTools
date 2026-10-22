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


def _first_file_path(track) -> str:
    """Returns the first resolvable file path for a track, or None if there is none."""
    for part in track.iterParts():
        if part.file:
            return part.file
    return None


def iter_played_tracks(music_section: MusicSection, page_size: int = _TRACK_PAGE_SIZE):
    """Yields every track in the section with a play count >= 1, one page at a
    time.

    plexapi's searchTracks() fetches the whole result set in a single blocking
    call (it pages internally but returns nothing until done), so it can't
    report progress - which is slow and silent on a large library. This drives
    the pagination ourselves, requesting one page of `page_size` played tracks
    per call (the viewCount>=1 filter is applied server-side), and yields each
    track as its page arrives so a caller can show live progress. Stops when a
    page comes back empty.
    """
    offset = 0
    while True:
        page = music_section.searchTracks(
            viewCount__gte=1,
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


def retrieve_plex_playcounts(plex: PlexServer, section: str = None) -> list:
    """Retrieves all songs with a play count >= 1 from the given music section.

    Returns a list of dicts, one per played track, each containing:
        path       - the track's file location (as seen by the Plex server)
        artist     - album artist (Track.grandparentTitle)
        album      - album name (Track.parentTitle)
        title      - song title
        rating     - user rating on Plex's 0-10 scale (None if unrated)
        playcount  - number of times the track has been played (>= 1)

    The played tracks are fetched page-by-page with a progress bar, since
    pulling them from a large library is slow; the bar shows a live count and
    rate (there's no cheap server-side count of only-played tracks to use as a
    percentage total).

    If `section` is None (the default), the server's single music library is
    used; pass a name to pick one out of a server with several.

    NOTE: `path` is the file location as the Plex server sees it, which may not
    resolve locally if this script runs on a different machine than the server.
    """
    music_section = resolve_music_section(plex, section)

    playcounts = []
    for track in tqdm(
        iter_played_tracks(music_section),
        desc="Fetching played tracks",
        unit="track",
    ):
        playcounts.append(
            {
                "path": _first_file_path(track),
                "artist": track.grandparentTitle,
                "album": track.parentTitle,
                "title": track.title,
                "rating": track.userRating,
                "playcount": track.viewCount,
            }
        )

    return playcounts


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve Plex play counts for all played songs and save them to a JSON file.",
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        help="Path to write the play count backup to, as a JSON file.",
    )
    parser.add_argument(
        "-s", "--section",
        default=None,
        help="Music library section name to read from. Defaults to the server's only music library.",
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
        playcounts = retrieve_plex_playcounts(plex, section=args.section)

        directory = os.path.dirname(os.path.abspath(args.output))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as output_file:
            json.dump(playcounts, output_file, indent=2)
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

    missing_paths = sum(1 for entry in playcounts if not entry["path"])
    print(f"Backed up play counts for {len(playcounts)} song(s) to {args.output}.")
    if missing_paths:
        print(
            f"{missing_paths} song(s) had no resolvable file path (saved with path=null).",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
