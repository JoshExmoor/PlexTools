import argparse
import csv
import io
import json
import sys
from collections import defaultdict

import requests
from plexapi.exceptions import NotFound, PlexApiException, Unauthorized
from plexapi.server import PlexServer

from get_plex_credentials import get_plex_credentials, retrieve_plex_credentials, save_plex_credentials

# Metadata fields collected per playlist, in display order.
_FIELDS = [
    "ratingKey",
    "title",
    "playlistType",
    "smart",
    "leafCount",
    "duration",
    "addedAt",
    "updatedAt",
    "guid",
    "key",
]


def _playlist_metadata(playlist) -> dict:
    """Collects the identifying/comparison metadata for one playlist.

    ratingKey is the unique server id - two entries with the same title but
    different ratingKeys are genuinely separate playlists (the duplicate case).
    leafCount/duration/addedAt/updatedAt help tell otherwise-identical-looking
    duplicates apart.
    """
    def _get(attr):
        value = getattr(playlist, attr, None)
        return str(value) if value is not None else None

    return {field: _get(field) for field in _FIELDS}


def list_playlists(
    plex: PlexServer,
    title: str = None,
    title_contains: str = None,
    playlist_type: str = None,
    smart: bool = None,
) -> list:
    """Returns metadata dicts for playlists matching the given filters.

    title           - exact title match, case-insensitive.
    title_contains  - substring match on title, case-insensitive.
    playlist_type   - 'audio', 'video', or 'photo'.
    smart           - True for only smart playlists, False for only regular.

    Results are sorted by title (case-insensitive) then ratingKey.
    """
    playlists = plex.playlists()

    if title is not None:
        wanted = title.casefold()
        playlists = [p for p in playlists if (p.title or "").casefold() == wanted]
    if title_contains is not None:
        needle = title_contains.casefold()
        playlists = [p for p in playlists if needle in (p.title or "").casefold()]
    if playlist_type is not None:
        playlists = [p for p in playlists if p.playlistType == playlist_type]
    if smart is not None:
        playlists = [p for p in playlists if bool(p.smart) is smart]

    rows = [_playlist_metadata(p) for p in playlists]
    rows.sort(key=lambda r: ((r["title"] or "").casefold(), r["ratingKey"] or ""))
    return rows


def find_duplicate_titles(rows: list) -> dict:
    """Groups rows by case-insensitive title and returns only the groups with
    more than one playlist. Keys are the lower-cased titles; values are the
    lists of rows sharing that title.
    """
    by_title = defaultdict(list)
    for row in rows:
        by_title[(row["title"] or "").casefold()].append(row)
    return {title: entries for title, entries in by_title.items() if len(entries) > 1}


def _playlist_track_keys(playlist) -> dict:
    """Returns {identity: display_name} for a playlist's items, where identity
    is the track's file path when available (the meaningful 'same song' key),
    falling back to its ratingKey. Used to diff two playlists' contents.
    """
    keys = {}
    for item in playlist.items():
        identity = None
        for part in getattr(item, "iterParts", lambda: [])():
            if part.file:
                identity = part.file
                break
        if identity is None:
            identity = f"ratingKey:{item.ratingKey}"
        keys[identity] = getattr(item, "title", identity)
    return keys


def diff_playlists(plex: PlexServer, rating_key_a, rating_key_b) -> dict:
    """Compares the track contents of two playlists (by ratingKey).

    Returns a dict with the two playlists' titles and the tracks that are only
    in A, only in B, and in both (by file-path identity).
    """
    wanted = {str(rating_key_a), str(rating_key_b)}
    found = {str(p.ratingKey): p for p in plex.playlists() if str(p.ratingKey) in wanted}
    missing = wanted - set(found)
    if missing:
        raise ValueError(f"No playlist(s) found with ratingKey(s): {', '.join(sorted(missing))}")

    pl_a = found[str(rating_key_a)]
    pl_b = found[str(rating_key_b)]
    keys_a = _playlist_track_keys(pl_a)
    keys_b = _playlist_track_keys(pl_b)
    set_a, set_b = set(keys_a), set(keys_b)

    return {
        "a": {"ratingKey": str(pl_a.ratingKey), "title": pl_a.title, "count": len(keys_a)},
        "b": {"ratingKey": str(pl_b.ratingKey), "title": pl_b.title, "count": len(keys_b)},
        "only_in_a": sorted(keys_a[k] for k in (set_a - set_b)),
        "only_in_b": sorted(keys_b[k] for k in (set_b - set_a)),
        "in_both": len(set_a & set_b),
    }


def _render_table(rows: list) -> str:
    lines = [f"{len(rows)} playlist(s) found.", ""]
    for row in rows:
        smart_tag = " [smart]" if row["smart"] == "True" else ""
        lines.append(
            f'- "{row["title"]}"{smart_tag}  (ratingKey={row["ratingKey"]}, '
            f'type={row["playlistType"]}, items={row["leafCount"]}, '
            f'added={row["addedAt"]}, updated={row["updatedAt"]})'
        )
    return "\n".join(lines)


def _render_csv(rows: list) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().rstrip("\n")


def _render(rows: list, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(rows, indent=2)
    if fmt == "csv":
        return _render_csv(rows)
    return _render_table(rows)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List, filter, and compare Plex playlists (read-only).",
    )
    parser.add_argument(
        "-t", "--title",
        default=None,
        help="Only list playlists with this exact title (case-insensitive).",
    )
    parser.add_argument(
        "--title-contains",
        default=None,
        help="Only list playlists whose title contains this substring (case-insensitive).",
    )
    parser.add_argument(
        "--type",
        dest="playlist_type",
        choices=["audio", "video", "photo"],
        default=None,
        help="Only list playlists of this type.",
    )
    smart_group = parser.add_mutually_exclusive_group()
    smart_group.add_argument(
        "--smart", dest="smart", action="store_true", default=None,
        help="Only list smart playlists.",
    )
    smart_group.add_argument(
        "--no-smart", dest="smart", action="store_false",
        help="Only list regular (non-smart) playlists.",
    )
    parser.add_argument(
        "-d", "--duplicates-only",
        action="store_true",
        help="Only show playlists whose title is shared by more than one playlist.",
    )
    parser.add_argument(
        "--diff",
        nargs=2,
        metavar=("RATINGKEY_A", "RATINGKEY_B"),
        default=None,
        help="Compare the track contents of two playlists by ratingKey.",
    )
    parser.add_argument(
        "-f", "--format",
        choices=["table", "json", "csv"],
        default="table",
        help="Output format for the listing (default: table).",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Also write the rendered output to this file.",
    )
    parser.add_argument("-l", "--url", default=None,
                        help="Base URL of the Plex server. Required unless --credential-file is given.")
    parser.add_argument("-u", "--username", default=None, help="Plex username. Prompted if omitted.")
    parser.add_argument("-p", "--password", default=None, help="Plex password. Prompted (hidden) if omitted.")
    parser.add_argument("-c", "--credential-file", default=None,
                        help="JSON credential file; if given, login is skipped.")
    parser.add_argument("-S", "--save-creds", default=None,
                        help="Also cache the retrieved credentials to this JSON file.")
    args = parser.parse_args(argv)

    if not args.credential_file and not args.url:
        parser.error("--url is required unless --credential-file is given.")

    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        if args.credential_file:
            creds = retrieve_plex_credentials(args.credential_file, url=args.url)
        else:
            creds = get_plex_credentials(url=args.url, username=args.username, password=args.password)
        if args.save_creds:
            save_plex_credentials(creds, args.save_creds)

        plex = PlexServer(creds["url"], creds["token"])

        # Diff mode: compare two playlists and exit (ignores listing filters).
        if args.diff:
            result = diff_playlists(plex, args.diff[0], args.diff[1])
            print(f'A: "{result["a"]["title"]}" (ratingKey={result["a"]["ratingKey"]}, {result["a"]["count"]} tracks)')
            print(f'B: "{result["b"]["title"]}" (ratingKey={result["b"]["ratingKey"]}, {result["b"]["count"]} tracks)')
            print(f'In both: {result["in_both"]}')
            print(f'\nOnly in A ({len(result["only_in_a"])}):')
            for name in result["only_in_a"]:
                print(f"  - {name}")
            print(f'\nOnly in B ({len(result["only_in_b"])}):')
            for name in result["only_in_b"]:
                print(f"  - {name}")
            return 0

        rows = list_playlists(
            plex,
            title=args.title,
            title_contains=args.title_contains,
            playlist_type=args.playlist_type,
            smart=args.smart,
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

    dupes = find_duplicate_titles(rows)

    if args.duplicates_only:
        dupe_keys = {row["ratingKey"] for entries in dupes.values() for row in entries}
        rows = [row for row in rows if row["ratingKey"] in dupe_keys]

    rendered = _render(rows, args.format)
    print(rendered)

    # Always surface duplicate titles as a warning (even in json/csv mode, to
    # stderr so it doesn't corrupt machine-readable stdout).
    if dupes:
        print("\nDUPLICATE TITLES DETECTED:", file=sys.stderr)
        for entries in dupes.values():
            title = entries[0]["title"]
            print(
                f'  "{title}" -> {len(entries)} playlists; ratingKeys '
                f'{[e["ratingKey"] for e in entries]}, item counts {[e["leafCount"] for e in entries]}',
                file=sys.stderr,
            )
        print(
            "  (Plex keys m3u-imported playlists by m3u path, not title. Use "
            "upload_playlist.py --remove-duplicates to clean up, or --diff here to compare.)",
            file=sys.stderr,
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as output_file:
            output_file.write(rendered + "\n")
        print(f"\nOutput written to {args.output}.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
