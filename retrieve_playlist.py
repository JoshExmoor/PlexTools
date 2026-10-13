import argparse
import os
import sys

import requests
from plexapi.exceptions import NotFound, PlexApiException, Unauthorized
from plexapi.server import PlexServer

from get_plex_credentials import get_plex_credentials, retrieve_plex_credentials, save_plex_credentials


def _default_save_path(playlist_name: str) -> str:
    """Builds a safe default filename in the current directory for a playlist."""
    safe_name = "".join(c if c.isalnum() or c in " ._-" else "_" for c in playlist_name).strip()
    return os.path.join(os.getcwd(), f"{safe_name or 'playlist'}.m3u")


def retrieve_playlist(plex: PlexServer, playlist_name: str, save_path: str = None) -> str:
    """Retrieves a Plex playlist by name and saves it to a local m3u file.

    NOTE: plexapi has no built-in m3u export, so this writes an m3u file from
    the playlist's track file paths. Those paths are as seen by the Plex
    server (track.media[*].parts[*].file); if this script runs on a different
    machine than the server, the resulting m3u may not resolve locally unless
    that filesystem is shared/mounted.
    """
    playlist = plex.playlist(playlist_name)
    tracks = playlist.items()

    if not save_path:
        save_path = _default_save_path(playlist_name)

    directory = os.path.dirname(os.path.abspath(save_path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    lines = ["#EXTM3U"]
    skipped = 0
    for track in tracks:
        file_paths = [part.file for part in track.iterParts() if part.file]
        if not file_paths:
            skipped += 1
            continue
        lines.append(f"#EXTINF:-1,{track.title}")
        lines.extend(file_paths)

    with open(save_path, "w", encoding="utf-8") as m3u_file:
        m3u_file.write("\n".join(lines) + "\n")

    print(
        f'Playlist "{playlist_name}" saved to {save_path}. '
        f"{len(tracks) - skipped}/{len(tracks)} tracks written."
    )
    if skipped:
        print(f"{skipped} track(s) had no resolvable file path and were skipped.", file=sys.stderr)

    return save_path


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve a Plex playlist by name and save it as a local m3u file.",
    )
    parser.add_argument(
        "--playlist",
        required=True,
        help="Title of the Plex playlist to retrieve.",
    )
    parser.add_argument(
        "--save-path",
        default=None,
        help="Path to save the m3u file to. Defaults to '<playlist name>.m3u' in the current directory.",
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
        retrieve_playlist(plex, args.playlist, save_path=args.save_path)
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
