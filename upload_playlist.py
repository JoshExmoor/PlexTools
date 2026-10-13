import argparse
import sys

import requests
from plexapi.exceptions import BadRequest, NotFound, PlexApiException, Unauthorized
from plexapi.library import MusicSection
from plexapi.playlist import Playlist
from plexapi.server import PlexServer

from get_plex_credentials import get_plex_credentials, retrieve_plex_credentials, save_plex_credentials


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


def upload_playlist(plex: PlexServer, m3u_path: str, title: str, section: str = None) -> Playlist:
    """Uploads an m3u playlist file to Plex, creating a playlist named `title`.

    NOTE: m3u import is a server-side operation. `m3u_path` must be a path that
    the Plex Media Server process itself can read (its local filesystem, or a
    volume it has mounted) - it does not need to be reachable from the machine
    running this script, and a path that only exists locally here will fail
    with a BadRequest from the server. Music libraries only.
    """
    music_section = plex.library.section(section) if section else _default_music_section(plex)

    if not isinstance(music_section, MusicSection):
        raise ValueError(f'Library section "{music_section.title}" is not a music library.')

    try:
        playlist = Playlist.create(plex, title=title, section=music_section, m3ufilepath=m3u_path)
    except BadRequest as exc:
        raise BadRequest(
            f"Failed to import m3u playlist from {m3u_path}. Note: this path must be "
            f"readable by the Plex server itself, not just this machine. ({exc})"
        ) from exc

    print(f'Uploaded playlist "{title}" from {m3u_path} ({len(playlist.items())} tracks).')
    return playlist


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Upload an m3u playlist file to Plex. NOTE: the m3u path must be "
            "readable by the Plex server process itself, not just this machine."
        ),
    )
    parser.add_argument(
        "--m3u-path",
        required=True,
        help="Path to the m3u playlist file, as seen by the Plex server.",
    )
    parser.add_argument(
        "--title",
        required=True,
        help="Title to give the created playlist in Plex.",
    )
    parser.add_argument(
        "--section",
        default=None,
        help="Music library section name to import into. Defaults to the server's only music library.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "Base URL of the Plex server, e.g. http://localhost:32400. "
            "Required unless --credential-file is given."
        ),
    )
    parser.add_argument(
        "--username",
        default=None,
        help="Plex username. Prompted via console input if omitted.",
    )
    parser.add_argument(
        "--password",
        default=None,
        help=(
            "Plex password. Prompted via a hidden console prompt if omitted. "
            "NOTE: passing this on the command line may expose it in shell "
            "history and process listings; prefer omitting it."
        ),
    )
    parser.add_argument(
        "--credential-file",
        default=None,
        help=(
            "Path to a JSON credential file previously saved with "
            "--save-to. If given, login is skipped and credentials are "
            "loaded from this file instead."
        ),
    )
    parser.add_argument(
        "--save-to",
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
            plex_credentials = retrieve_plex_credentials(args.credential_file)
        else:
            plex_credentials = get_plex_credentials(
                url=args.url,
                username=args.username,
                password=args.password,
            )

        if args.save_to:
            save_plex_credentials(plex_credentials, args.save_to)

        plex = PlexServer(plex_credentials["url"], plex_credentials["token"])
        upload_playlist(plex, args.m3u_path, args.title, section=args.section)
    except (
        Unauthorized,
        NotFound,
        PlexApiException,
        requests.exceptions.RequestException,
        RuntimeError,
        FileNotFoundError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
