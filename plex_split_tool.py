"""Find Plex music albums whose tracks live in more than one album folder, and split them.

Plex sometimes combines two different releases into one album entry (for
example "Siamese Dream" and "Siamese Dream (Reissue)" sitting in two separate
folders), so one album ratingKey ends up holding the tracks of both. This tool
is the opposite of plex_merge_tool.py: it finds such albums and offers Plex's
own "Split Apart" action on each one, after confirmation.

How it works:

1. Scan. One paged track scan (the whole section, or scoped by ``--artist`` on
   the album artist and/or ``--album`` on the album title, both
   case-insensitive substrings applied server-side) groups every track by its
   album ratingKey and records the folder of each track file. No per-album
   requests are made during the scan.
2. Detect. Each track folder is reduced to its album root folder (see the
   disc rule below). An album whose tracks come from 2 or more album roots is
   queued as a candidate. Only candidates are then fetched from Plex (one
   request each), because a split needs the real album object.
3. Review. Every queued candidate is listed with its ratingKey and year in a
   header line, then a table of its album folders (track count and number of
   disc folders per folder). With ``--apply`` each one is followed by
   ``Split "[Album]" (N tracks across M folders)? [y/N]``.
   Answer ``y``/``yes`` to split, ``n``/``no`` or just Enter to skip (the
   default is no). Without ``--apply`` (the default dry run) nothing is
   prompted or changed.
4. Report. After each split the album's tracks are re-read by ratingKey and the
   albums they now belong to are listed in a table. A summary closes the run.

Disc folders are not separate albums:

- A leaf folder that starts with a disc marker ("CD1", "CD 2", "cd-01",
  "Disc 2", "Disk3", "[Disc 1]", "(CD 2)", "CD1 - Title", "Disc 1 of 2") belongs
  to its parent folder, so ``Album\\CD1`` and ``Album\\CD2`` (and ``Album`` plus
  ``Album\\CD2``) count as one album folder.
- A leaf folder that is a title followed by a disc marker ("Album (Disc 1)",
  "Album - CD2", "Album CD 2", "Album [Disc 1]") belongs to the title without
  the marker, so "X (Disc 1)" and "X (Disc 2)" siblings count as one folder.
- Volumes and parts ("Vol. 1", "Volume 2", "Part 1") are NOT treated as discs:
  they are usually separate releases, exactly the kind of wrongly combined
  albums this tool should find. Every split still needs a confirmation.
- Windows-style paths compare case- and separator-insensitively; POSIX paths
  are case-sensitive.

Split caveats:

- plexapi's ``Album.split()`` sends ``PUT /library/metadata/<ratingKey>/split``
  with no parameters. Plex decides how the album is divided; it is not
  documented that the result follows these folders, and a later scan or match
  may combine the albums again. The post-split report shows what actually
  happened; re-run the tool to see the current state.
- A track with files in several album folders counts once per folder, so the
  per-folder counts can add up to more than the album's track count.

Display:

- Tables (built with rich) fit the terminal width; long folder paths are
  shortened in the middle, keeping the drive/root and the album folder
  (``E:\\MP3\\…\\Siamese Dream``).
- Colors: on a terminal, names, years, counts, folders and notes are colored
  and tables have rounded borders. Use --no-color or set NO_COLOR (or
  TERM=dumb) to turn it off; output that is piped or redirected is never
  colored and uses plain ASCII borders (see plex_style.py).
"""

import argparse
import ntpath
import posixpath
import re
import sys
from dataclasses import dataclass, field

import requests
from plexapi.exceptions import NotFound, PlexApiException, Unauthorized
from plexapi.library import MusicSection
from plexapi.server import PlexServer
from tqdm import tqdm

import plex_style as style
from get_plex_credentials import get_plex_credentials, retrieve_plex_credentials, save_plex_credentials
from plex_common import (
    MAX_FOLDERS_SHOWN,
    album_year,
    ask_yes_no,
    attr,
    dedupe_folders,
    folder_key,
    is_windows_path,
    question,
    server_dirname,
    shorten_path,
)
from plex_sections import resolve_music_section
from retrieve_plex_library import iter_tracks

# Number of tracks to request per page during the scan.
_PAGE_SIZE = 200

# Most track ratingKeys re-read per request after a split.
_FETCH_CHUNK = 100

# A disc marker: "CD", "Disc" or "Disk", optional separators, then a 1-3 digit disc number that is
# not followed by another letter or digit. Matches "CD1", "CD 1", "cd-01", "Disc 2", "Disk3", "disc.01".
_DISC_MARKER = r"(?:cd|dis[ck])[\s._-]*[0-9]{1,3}(?![0-9a-z])"

# Leaf folder that IS a disc folder: the marker at the start, optionally in brackets, optionally followed
# by anything (a title or qualifier). "CD1", "[Disc 1]", "(CD 2)", "CD1 - Title", "Disc 2 (Bonus)",
# "Disc 1 of 2". The album root of such a folder is its parent.
DISC_FOLDER_PATTERN = re.compile(r"^\s*[\[(]?\s*" + _DISC_MARKER, re.IGNORECASE)

# Leaf folder that is an album title followed by a disc marker: "Mellon Collie (Disc 1)",
# "Album - CD2", "Album CD 2", "Album [Disc 1]". The album root is parent + the title with the marker
# removed, so "X (Disc 1)" and "X (Disc 2)" (and plain "X") collapse to one root.
DISC_SUFFIX_PATTERN = re.compile(
    r"^(?P<title>.*?\S)(?:[\s._-]+[\[(]?|[\[(])\s*" + _DISC_MARKER + r"\s*[\])]?\s*$",
    re.IGNORECASE,
)

SPLIT_CAVEAT = "Plex decides how a split divides an album; the result may not match these folders."


def album_root(folder: str) -> tuple:
    """Returns (root_key, root_display) for a track folder.

    Disc folders map to their album folder (see DISC_FOLDER_PATTERN and
    DISC_SUFFIX_PATTERN); any other folder is its own root. `root_key` is the
    folder_key() of the root. `root_display` is the root path for a disc
    folder like "Album\\CD1" or a plain folder, and the original folder for a
    "Title (Disc 1)" folder, since the stripped path may not exist on disk.
    """
    pathmod = ntpath if is_windows_path(folder) else posixpath
    trimmed = folder.rstrip("\\/" if pathmod is ntpath else "/") or folder
    parent, leaf = pathmod.split(trimmed)

    if leaf and parent and DISC_FOLDER_PATTERN.match(leaf):
        return folder_key(parent), parent
    match = DISC_SUFFIX_PATTERN.match(leaf) if leaf else None
    if match:
        root = pathmod.join(parent, match["title"].rstrip(" ._-")) if parent else match["title"].rstrip(" ._-")
        return folder_key(root), folder
    return folder_key(folder), folder


@dataclass
class AlbumRoot:
    """One album folder (after disc folders are collapsed) holding some of an album's tracks."""

    key: str  # folder_key of the root
    display: str  # path shown to the user (original string)
    track_count: int = 0
    folders: list = field(default_factory=list)  # distinct original track folders mapped here


@dataclass
class SplitCandidate:
    """An album (one ratingKey) and the album folders its tracks come from."""

    rating_key: int
    title: str
    artist: str
    year: int = None
    track_count: int = 0  # every track seen for the album, incl. ones without a file
    pathless_tracks: int = 0
    track_keys: list = field(default_factory=list)
    roots: list = field(default_factory=list)  # AlbumRoot, sorted by display
    obj: object = None  # fetched plexapi Album (needed for split); None if not loaded
    load_error: str = None


@dataclass
class SplitSummary:
    """Counts of what happened during the review."""

    split: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class SplitMode:
    """Per-mode hooks used by run_split_tool() and review_and_split()."""

    find: object  # (music_section, artist, album) -> list[SplitCandidate]
    describe: object  # (candidate) -> list[str] context lines
    format_prompt: object  # (candidate) -> str


def _track_paths(track) -> list:
    """Returns the file paths of every media part of a track (attribute reads only)."""
    paths = []
    for media in attr(track, "media") or []:
        for part in attr(media, "parts") or []:
            path = attr(part, "file")
            if path:
                paths.append(path)
    return paths


def scan_album_tracks(music_section: MusicSection, artist: str = None, album: str = None) -> tuple:
    """Groups every track in scope by album ratingKey in one paged scan.

    Returns ({album ratingKey: SplitCandidate with roots filled in}, number of
    tracks skipped because they had no album ratingKey). Tracks without a file
    path count toward track_count and pathless_tracks but not toward any root.
    """
    albums = {}
    roots_by_album = {}
    skipped_tracks = 0
    for track in tqdm(
        iter_tracks(music_section, artist=artist, album=album, page_size=_PAGE_SIZE),
        desc="Scanning tracks",
        unit=" tracks",
    ):
        album_key = attr(track, "parentRatingKey")
        if album_key is None:
            skipped_tracks += 1
            continue

        candidate = albums.get(album_key)
        if candidate is None:
            candidate = albums[album_key] = SplitCandidate(rating_key=album_key, title=None, artist=None)
            roots_by_album[album_key] = {}
        candidate.title = candidate.title or attr(track, "parentTitle")
        candidate.artist = candidate.artist or attr(track, "grandparentTitle")
        candidate.year = candidate.year or attr(track, "parentYear")
        candidate.track_count += 1
        track_key = attr(track, "ratingKey")
        if track_key is not None:
            candidate.track_keys.append(track_key)

        paths = _track_paths(track)
        if not paths:
            candidate.pathless_tracks += 1
            continue

        roots = roots_by_album[album_key]
        seen_here = set()
        for path in paths:
            directory = server_dirname(path)
            key, display = album_root(directory)
            root = roots.get(key)
            if root is None:
                root = roots[key] = AlbumRoot(key=key, display=display)
            else:
                root.display = min(root.display, display)
            root.folders.append(directory)
            if key not in seen_here:
                seen_here.add(key)
                root.track_count += 1

    for album_key, candidate in albums.items():
        roots = list(roots_by_album[album_key].values())
        for root in roots:
            root.folders = dedupe_folders(root.folders)
        candidate.roots = sorted(roots, key=lambda r: (r.display.casefold(), r.display))
    return albums, skipped_tracks


def _load_candidates(music_section: MusicSection, candidates: list) -> None:
    """Fetches the plexapi Album for each candidate (needed for split()).

    A failure is reported as a warning and recorded in load_error; the
    candidate is still listed. Only Exception is caught, so Ctrl+C still stops.
    """
    for candidate in candidates:
        try:
            obj = music_section.fetchItem(candidate.rating_key)
        except Exception as exc:  # noqa: BLE001 - one bad album must not abort the review
            style.eprint(style.warn("Warning:"), ' could not load album "', style.album(candidate.title), f'": {exc}')
            candidate.load_error = str(exc)
            continue
        candidate.obj = obj
        candidate.year = album_year(obj) or candidate.year
        if candidate.title == "Unknown album":
            candidate.title = attr(obj, "title") or candidate.title
        if candidate.artist == "Unknown artist":
            candidate.artist = attr(obj, "parentTitle") or candidate.artist


def find_albums_by_folder(music_section: MusicSection, artist: str = None, album: str = None) -> list:
    """Scans the section and returns albums whose tracks span 2+ album folders.

    The whole scan finishes before any album is fetched. Candidates are sorted
    by artist, title and ratingKey.
    """
    albums, skipped_tracks = scan_album_tracks(music_section, artist=artist, album=album)
    if skipped_tracks:
        style.eprint(style.warn("Skipped"), " ", style.count(skipped_tracks), " track(s) with no album ratingKey.")

    candidates = [c for c in albums.values() if len(c.roots) >= 2]
    for candidate in candidates:
        candidate.title = candidate.title or "Unknown album"
        candidate.artist = candidate.artist or "Unknown artist"
    candidates.sort(key=lambda c: (c.artist.casefold(), c.title.casefold(), c.rating_key))
    _load_candidates(music_section, candidates)
    return candidates


def _tracks_text(count: int) -> str:
    return "1 track" if count == 1 else f"{count} tracks"


def _year_text(year) -> str:
    return str(year) if year else "unknown year"


# Yes/no question for a candidate (plex_style.fill template; " [y/N] " is appended).
SPLIT_QUESTION = 'Split "{title}" ({tracks} across {folders})?'


def format_candidate_header(index: int, total: int, candidate: SplitCandidate):
    """Returns the header line printed before a candidate."""
    return style.line(
        f"[{index}/{total}] \"", style.album(candidate.title), '" by ', style.artist(candidate.artist),
        "  (ratingKey ", style.key(candidate.rating_key), ", ", style.year(_year_text(candidate.year)), ")",
    )


def _folder_width(width: int = None) -> int:
    width = style.width() if width is None else width
    # 2 indent; 3 columns -> 4 border characters + 6 padding; "Tracks" 6 + "Disc folders" 12.
    return max(20, width - 2 - 28)


def candidate_table(candidate: SplitCandidate, width: int = None):
    """Returns the Folder / Tracks / Disc folders table for a candidate.

    One row per album folder (up to MAX_FOLDERS_SHOWN); long paths are
    middle-truncated to fit `width` (default: the stdout console width).
    """
    folder_width = _folder_width(width)
    table = style.new_table("Folder", ("Tracks", {"justify": "right"}), ("Disc folders", {"justify": "right"}))
    for root in candidate.roots[:MAX_FOLDERS_SHOWN]:
        table.add_row(
            style.path(shorten_path(root.display, folder_width, style.ellipsis())),
            style.count(root.track_count),
            style.count(len(root.folders)) if len(root.folders) > 1 else "",
        )
    if len(candidate.roots) > MAX_FOLDERS_SHOWN:
        more = style.count(len(candidate.roots) - MAX_FOLDERS_SHOWN)
        table.add_row(style.line(f"{style.ellipsis()} and ", more, " more"), "", "")
    return table


def describe_candidate(candidate: SplitCandidate, width: int = None) -> list:
    """Returns the renderables printed for a candidate (in dry run too)."""
    items = [style.indent(candidate_table(candidate, width))]
    if candidate.pathless_tracks:
        items.append(style.line(
            "  ", style.warn("Note:"), " ", style.count(candidate.pathless_tracks),
            " track(s) have no file path and were not checked.",
        ))
    if candidate.load_error:
        items.append(style.line(
            "  ", style.error("Note:"), f" could not load this album from Plex: {candidate.load_error}"
        ))
    return items


def format_split_prompt(candidate: SplitCandidate):
    """Returns the confirmation prompt (rich Text) for splitting a candidate album."""
    return question(
        SPLIT_QUESTION,
        title=style.album(candidate.title),
        tracks=style.count(_tracks_text(candidate.track_count)),
        folders=style.count(f"{len(candidate.roots)} folders"),
    )


def split_report_table(rows: list, original_key):
    """Returns the post-split table: one row per (album ratingKey, title, track count)."""
    table = style.new_table("ratingKey", "Title", ("Tracks", {"justify": "right"}), "Note")
    for album_key, title, track_count in rows:
        table.add_row(
            style.key(album_key),
            style.album(title),
            style.count(track_count),
            style.key("original") if album_key == original_key else "",
        )
    return table


ALBUMS_BY_FOLDER_MODE = SplitMode(
    find=find_albums_by_folder,
    describe=describe_candidate,
    format_prompt=format_split_prompt,
)

# Split modes by --mode value.
SPLIT_MODES = {"albums-by-folder": ALBUMS_BY_FOLDER_MODE}


def split_album(candidate: SplitCandidate) -> None:
    """Asks Plex to split the candidate album (PUT /library/metadata/<key>/split)."""
    candidate.obj.split()


def report_split(music_section: MusicSection, candidate: SplitCandidate) -> str:
    """Re-reads the album's tracks after a split and prints where they are now.

    Returns "unchanged" when every track found is still under the original
    ratingKey, else "split". Track keys are re-read in chunks of _FETCH_CHUNK.
    """
    keys = candidate.track_keys
    albums = {}
    found = set()
    for start in range(0, len(keys), _FETCH_CHUNK):
        for track in music_section.fetchItems(keys[start:start + _FETCH_CHUNK]):
            track_key = attr(track, "ratingKey")
            if track_key in found:
                continue
            found.add(track_key)
            album_key = attr(track, "parentRatingKey")
            entry = albums.setdefault(album_key, {"title": attr(track, "parentTitle"), "count": 0})
            entry["title"] = entry["title"] or attr(track, "parentTitle")
            entry["count"] += 1

    missing = len(keys) - len(found)
    result = "split"
    if not albums:
        style.say(style.warn(f"Split requested. None of the album's {_tracks_text(len(keys))} could be found yet."))
    elif set(albums) == {candidate.rating_key}:
        style.say(
            style.warn("Plex did not divide this album:"), " all ", style.count(_tracks_text(len(found))),
            " are still under ratingKey ", style.key(candidate.rating_key), ".",
        )
        result = "unchanged"
    else:
        style.say(style.ok("Split requested. Plex now lists these tracks under:"))
        ordered = sorted(albums, key=lambda k: (k != candidate.rating_key, str(k)))
        rows = [
            (album_key, albums[album_key]["title"] or "Unknown album", albums[album_key]["count"])
            for album_key in ordered
        ]
        style.out(style.indent(split_report_table(rows, candidate.rating_key)))
    if missing and albums:
        style.say("  ", style.warn("Note:"), " ", style.count(missing), " track(s) could not be found after the split.")
    style.say("  Plex may still be processing; re-run this tool to see the current state.")
    return result


def review_and_split(
    candidates: list,
    input_fn=None,
    apply: bool = False,
    mode: SplitMode = None,
    music_section: MusicSection = None,
) -> SplitSummary:
    """Presents each queued candidate and splits the ones confirmed.

    Without `apply` (dry run) every candidate is listed but nothing is
    prompted or split. A failed split is reported and counted, and the review
    continues. EOF or Ctrl+C at a prompt stops the review; that candidate and
    all remaining ones count as skipped.
    """
    mode = mode or ALBUMS_BY_FOLDER_MODE
    summary = SplitSummary()
    style.say("Found ", style.count(len(candidates)), " album(s) whose tracks span more than one album folder.")
    if not candidates:
        return summary
    if apply:
        style.say(style.warn(f"Note: {SPLIT_CAVEAT}"))

    total = len(candidates)
    for index, candidate in enumerate(candidates, start=1):
        style.blank()
        style.out(format_candidate_header(index, total, candidate))
        style.out(*mode.describe(candidate))
        if not apply:
            continue
        if candidate.obj is None:
            style.say(style.error("Cannot split: album could not be loaded."))
            summary.failed += 1
            continue
        try:
            confirmed = ask_yes_no(mode.format_prompt(candidate), input_fn)
        except (EOFError, KeyboardInterrupt):
            style.say("\n", style.warn("Stopping review."))
            summary.skipped += total - index + 1
            break
        if not confirmed:
            summary.skipped += 1
            continue
        try:
            split_album(candidate)
        except (PlexApiException, requests.exceptions.RequestException) as exc:
            style.eprint(style.error("Error:"), f" {exc}")
            summary.failed += 1
            continue
        try:
            outcome = report_split(music_section, candidate)
        except Exception as exc:  # noqa: BLE001 - the split itself succeeded
            style.eprint(style.warn("Warning:"), f" split requested, but the tracks could not be re-read: {exc}")
            outcome = "split"
        if outcome == "unchanged":
            summary.unchanged += 1
        else:
            summary.split += 1

    if not apply:
        offered = sum(1 for c in candidates if c.obj is not None)
        style.say(
            "Dry run: ", style.count(offered), " album(s) would be offered for splitting. "
            "Nothing was changed. Use --apply to split."
        )
        return summary

    unchanged = style.warn(summary.unchanged) if summary.unchanged else style.count(summary.unchanged)
    failed = style.error(summary.failed) if summary.failed else style.count(summary.failed)
    style.say(
        "Summary: ", style.ok(summary.split), " split, ", unchanged, " unchanged, ",
        style.count(summary.skipped), " skipped, ", failed, " failed.",
    )
    return summary


def run_split_tool(
    plex: PlexServer,
    mode: str = "albums-by-folder",
    section: str = None,
    artist: str = None,
    album: str = None,
    apply: bool = False,
    input_fn=None,
) -> SplitSummary:
    """Scans the music section for split candidates, then reviews them.

    The full scan finishes (all candidates queued) before the first prompt.
    An unknown `mode` raises ValueError before touching `plex`.
    """
    if mode not in SPLIT_MODES:
        raise ValueError(f"Unknown split mode: {mode}")

    spec = SPLIT_MODES[mode]
    music_section = resolve_music_section(plex, section)
    candidates = spec.find(music_section, artist=artist, album=album)
    return review_and_split(candidates, input_fn=input_fn, apply=apply, mode=spec, music_section=music_section)


_EPILOG = """\
Disc folders: a folder named like "CD1", "CD 2", "Disc 2", "Disk3", "[Disc 1]"
or "CD1 - Title" counts as part of its parent album folder, and "Album (Disc 1)"
/ "Album - CD2" count as "Album". Volumes and parts ("Vol. 1", "Part 2") are not
treated as discs, since they are usually separate releases.

Splitting: the default is a dry run that only lists candidates. With --apply
each album is still confirmed one by one (Enter means no). Plex decides how a
split divides an album; the result may not match these folders, and a later
scan or match may combine the albums again. After each split the tool re-reads
the tracks and reports where they ended up.

Colors: output is colored on a terminal; use --no-color or set NO_COLOR to turn it off.
"""


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find Plex music albums whose tracks come from more than one album folder "
            "and split them after confirming each one."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-m", "--mode",
        choices=sorted(SPLIT_MODES),
        default="albums-by-folder",
        help=(
            "What to look for: 'albums-by-folder' (albums whose tracks span several album "
            "folders, ignoring CD/Disc subfolders). Default albums-by-folder."
        ),
    )
    parser.add_argument(
        "-s", "--section",
        default=None,
        help="Music library section name to read from. Defaults to the server's only music library.",
    )
    parser.add_argument(
        "--artist",
        default=None,
        help="Limit the scan to albums whose album artist contains this string (case-insensitive).",
    )
    parser.add_argument(
        "--album",
        default=None,
        help="Limit the scan to albums whose title contains this string (case-insensitive).",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "-d", "--dry-run",
        action="store_true",
        help="List split candidates without prompting or splitting anything (the default).",
    )
    action.add_argument(
        "--apply",
        action="store_true",
        help="Prompt for each candidate and split the ones you confirm.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help=style.NO_COLOR_HELP,
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
    style.configure(no_color=args.no_color)

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
        summary = run_split_tool(
            plex,
            mode=args.mode,
            section=args.section,
            artist=args.artist,
            album=args.album,
            apply=args.apply,
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
        NotImplementedError,
    ) as exc:
        style.eprint(style.error("Error:"), f" {exc}")
        return 1

    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
