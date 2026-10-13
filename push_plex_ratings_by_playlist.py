import argparse
import json
import os
import sys

import requests
from plexapi.exceptions import BadRequest, NotFound, PlexApiException, Unauthorized
from plexapi.server import PlexServer
from tqdm import tqdm

from get_plex_credentials import get_plex_credentials, retrieve_plex_credentials, save_plex_credentials

# Plex stores ratings as an integer 0-10. Stars (what Plex displays to users)
# are 0-5 in half-star steps: 5 stars = 10, 4.5 stars = 9, 4 stars = 8, etc.
STARS_TO_RATING = 2
MIN_STARS = 0.0
MAX_STARS = 5.0
STAR_STEP = 0.5


def stars_to_rating(stars: float) -> int:
    """Converts a 0-5 half-star value to Plex's native 0-10 integer rating scale."""
    if not isinstance(stars, (int, float)):
        raise ValueError("stars must be a number.")
    if stars < MIN_STARS or stars > MAX_STARS:
        raise ValueError(f"stars must be between {MIN_STARS} and {MAX_STARS}.")
    # Use a small epsilon to avoid float remainder issues (e.g. 3.5 % 0.5 != 0 exactly).
    steps = stars / STAR_STEP
    if abs(steps - round(steps)) > 1e-9:
        raise ValueError(f"stars must be in steps of {STAR_STEP} (e.g. 0, 0.5, 1, ..., 5).")
    return round(stars * STARS_TO_RATING)


def update_playlist_ratings(
    plex: PlexServer,
    playlist_name: str,
    stars: float,
    force_ratings: bool = False,
    dry_run: bool = False,
    backup_ratings_to: str = None,
) -> dict:
    # Add validation that the inputed rating is in 0.5 steps between 0 and 5 stars.
    rating = stars_to_rating(stars)

    playlist = plex.playlist(playlist_name)
    tracks = playlist.items()
    total = len(tracks)

    # Optional backup of each track's current rating before any changes are made.
    if backup_ratings_to:
        backup = {
            str(track.ratingKey): {"title": track.title, "userRating": track.userRating}
            for track in tracks
        }
        directory = os.path.dirname(os.path.abspath(backup_ratings_to))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(backup_ratings_to, "w", encoding="utf-8") as backup_file:
            json.dump(backup, backup_file, indent=2)

    # By default, skip tracks already at the target rating. --force-ratings overrides this.
    if force_ratings:
        tracks_to_update = tracks
        skipped = 0
    else:
        tracks_to_update = [track for track in tracks if track.userRating != rating]
        skipped = total - len(tracks_to_update)

    updated = 0
    failed = []
    progress = tqdm(tracks_to_update, unit="track", disable=total == 0)
    for track in progress:
        progress.set_description(f"Updated {updated} of {len(tracks_to_update)} ratings")
        if dry_run:
            updated += 1
            continue
        try:
            track.rate(rating)
            updated += 1
        except (BadRequest, PlexApiException) as exc:
            failed.append((track.title, str(exc)))
    progress.set_description(f"Updated {updated} of {len(tracks_to_update)} ratings")
    progress.close()

    # Summarize the results, e.g. "Playlist 5stars job run. 123/123 songs updated to star_rating"
    prefix = "[DRY RUN] " if dry_run else ""
    print(
        f'{prefix}Playlist "{playlist_name}" job run. {updated}/{total} songs updated to {stars} stars '
        f"(rating {rating}). {skipped} already at target rating were skipped."
    )
    if failed:
        print(f"{len(failed)} track(s) failed to update:", file=sys.stderr)
        for title, error in failed:
            print(f"  - {title}: {error}", file=sys.stderr)

    return {"total": total, "updated": updated, "skipped": skipped, "failed": failed}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Set a star rating on every track in a Plex playlist.",
    )
    parser.add_argument(
        "--playlist",
        required=True,
        help="Title of the Plex playlist to update.",
    )
    parser.add_argument(
        "--stars",
        required=True,
        type=float,
        help="Star rating from 0 to 5 in half-star steps (e.g. 0, 0.5, 1, ..., 5), as shown in Plex.",
    )
    parser.add_argument(
        "--force-ratings",
        action="store_true",
        help="Update every track even if it is already at the target rating (default: skip them).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without actually updating any ratings.",
    )
    parser.add_argument(
        "--backup-ratings-to",
        default=None,
        help="Optional path to save each track's current rating as JSON before making changes.",
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
            "--save-creds. If given, login is skipped and credentials are "
            "loaded from this file instead."
        ),
    )
    parser.add_argument(
        "--save-creds",
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

        if args.save_creds:
            save_plex_credentials(plex_credentials, args.save_creds)

        plex = PlexServer(plex_credentials["url"], plex_credentials["token"])
        update_playlist_ratings(
            plex,
            args.playlist,
            args.stars,
            force_ratings=args.force_ratings,
            dry_run=args.dry_run,
            backup_ratings_to=args.backup_ratings_to,
        )
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
