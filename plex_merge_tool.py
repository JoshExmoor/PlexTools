"""Find and merge Plex music artists (or albums) that were split into several entries.

Plex sometimes files one artist under more than one artist entry, each with
its own ratingKey. For example, an export of "Ken Pomeroy" tracks showed two
different ``artistRatingKey`` values (777239 with 5 albums, 858664 with 2
albums) for the same artist. This tool scans a music library, finds artists
with identical or very similar names but different ratingKeys, and offers to
merge them one at a time.

How it works:

1. Scan. Every artist in scope (the whole section, or only names containing
   ``--artist``) is read, then one paged album scan counts albums per artist.
   Candidate groups are built only after the whole scan has finished; nothing
   is merged during the scan.
2. Review. Each queued candidate is shown one at a time with the prompt
   ``Would you like to merge [artist1] (## albums) with [artist2] (## albums)?``
   Answer ``y``/``yes`` to merge, ``n``/``no`` or just Enter to skip (the
   default is no). The live server is only modified after a "yes".
3. Summary. The number of merged, skipped, and failed merges is printed.

Design decisions:

- Merge direction: in each group the entry with the most albums survives;
  ties go to the lower (older) ratingKey. The survivor is always ``[artist1]``
  in the prompt, and the other entry is folded into it via plexapi's
  ``merge()`` on the survivor (``survivor.merge([other.ratingKey])``). A
  context line before each prompt spells out which ratingKey is kept.
- Groups of 3 or more: each extra entry is offered as its own pairwise prompt
  into the same survivor ("merge T with A?", then "merge T with B?"). You can
  reject one bad match without losing the rest, and one failed merge doesn't
  affect the others. After a successful merge the survivor's displayed album
  count includes the merged albums, so later prompts stay accurate.
- Name matching: names are normalized (case, accents, punctuation, "&"/"+" to
  "and", a leading "The") so "The Smashing Pumpkins" and "Smashing Pumpkins"
  match exactly. Fuzzy matching (difflib similarity on the normalized names)
  is on by default with a threshold of 0.90 (``--threshold``), which catches
  typos like "Smashing Pumkins" but keeps pairs like "Prince"/"Princess"
  apart. Fuzzy-only groups are labelled "similar name match" so you know to
  look closer. ``--no-fuzzy`` limits matching to normalized-identical names.
  Fuzzy links are transitive (A~B and B~C put A, B, C in one group); that is
  acceptable because every pair is confirmed individually.
- ``--dry-run`` scans and lists every candidate group and the merges that
  would be offered, without prompting or changing anything.

Album mode (``--merge albums``):

- Same flow: one paged album scan (the whole section, or scoped by
  ``--album`` on the album title and/or ``--artist`` on the album artist,
  both case-insensitive substrings), then one confirmation per pair, then a
  summary.
- Matching is strict. Two albums are candidates only when their album artist
  names are the same after the artist normalization above (so albums under a
  still-split artist, e.g. "The Smashing Pumpkins" vs "Smashing Pumpkins",
  are found) AND their titles are the same after light normalization only:
  case, accents, whitespace runs, and curly vs straight quotes. Parenthetical
  or bracketed qualifiers, punctuation and a leading "The" are never ignored,
  so "Siamese Dream" and "Siamese Dream (Reissue)" are never proposed.
  There is no fuzzy matching for albums; ``--threshold`` and ``--no-fuzzy``
  only affect artist mode.
- The prompt shows the artist, year and track count of each album:
  ``Would you like to merge [Album] by [Artist] (1993, 13 tracks) with
  [Album] by [Artist] (2011, 13 tracks)?``. The year is the album's year,
  else the year of its release date, else "unknown year".
- After the scan, tracks are read for candidate albums only, to find the
  folders their files live in. Every pair gets a folder status line: "both
  albums are in the same folder: ..." (or "use the same N folders:" for
  multi-folder albums such as CD1/CD2), "these albums are in different
  folders", or "could not confirm whether these albums share a folder" (tracks
  unreadable or no track files). The last two also list the Keep and
  Merge-in folders. Windows paths compare case- and separator-insensitively;
  POSIX paths are case-sensitive; partial overlap counts as different.
- The question line ends with a matching tag: ``[same folder]``,
  ``[same folders]``, ``[different folders]`` or ``[folders unknown]``. In a
  group of 3+, the tag reflects the survivor's folders after earlier merges.
  ``--dry-run`` prints the status line (it never prints the question line).
- A note is printed when both years are known and differ, and when the
  albums sit under different artist entries.
- Survivor: the album with the most tracks; ties go to the lower ratingKey.
"""

import argparse
import difflib
import re
import sys
import unicodedata
from dataclasses import dataclass, field

import requests
from plexapi.exceptions import NotFound, PlexApiException, Unauthorized
from plexapi.library import MusicSection
from plexapi.server import PlexServer
from tqdm import tqdm

from get_plex_credentials import get_plex_credentials, retrieve_plex_credentials, save_plex_credentials
from plex_common import (  # noqa: F401 - re-exported under the names this module always used
    DRIVE_LETTER as _DRIVE_LETTER,
    MAX_FOLDERS_SHOWN as _MAX_FOLDERS_SHOWN,
    album_year as _album_year,
    ask_yes_no,
    attr as _attr,
    dedupe_folders as _dedupe_folders,
    folder_key as _folder_key,
    is_windows_path as _is_windows_path,
    same_folders as _same_folders,
    server_dirname as _server_dirname,
)
from plex_sections import resolve_music_section

# Number of items to request per page when scanning artists/albums.
_PAGE_SIZE = 200

# Default similarity (0-1) for treating two normalized names as the same.
DEFAULT_THRESHOLD = 0.90


@dataclass
class MergeEntry:
    """One Plex item that could take part in a merge (an artist or an album)."""

    obj: object  # the plexapi object; must support .merge(ratingKeys)
    title: str
    rating_key: int
    album_count: int = 0
    # Album-mode fields.
    track_count: int = 0
    artist: str = None
    artist_key: int = None
    year: int = None
    directories: list = None  # None = not fetched / unknown


@dataclass
class CandidateGroup:
    """A set of entries that look like the same item.

    `target` is the surviving entry; `others` are offered for merging into it,
    in order. `match` is "exact" (all names identical after normalization) or
    "similar" (at least one fuzzy link).
    """

    target: MergeEntry
    others: list = field(default_factory=list)
    match: str = "exact"


@dataclass
class MergeSummary:
    """Counts of what happened during the review."""

    merged: int = 0
    skipped: int = 0
    failed: int = 0


def normalize_name(name: str) -> str:
    """Returns a comparison key for a name.

    Folds case and accents, treats "&" and "+" as "and", drops apostrophes,
    turns other punctuation into spaces, collapses whitespace, and drops a
    leading "The " (unless that would leave nothing, so "The The" -> "the").
    Punctuation-only names (e.g. "!!!") fall back to their casefolded form so
    they don't all collide on an empty key.
    """
    original = name or ""
    text = unicodedata.normalize("NFKD", original)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = text.replace("&", " and ").replace("+", " and ")
    text = text.replace("'", "").replace("\u2019", "")
    text = re.sub(r"[^\w\s]|_", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    without_the = re.sub(r"^the\s+", "", text)
    if without_the:
        text = without_the
    if not text:
        return original.casefold().strip()
    return text


_SINGLE_QUOTES = dict.fromkeys(map(ord, "\u2018\u2019\u201a\u201b\u2032"), "'")
_DOUBLE_QUOTES = dict.fromkeys(map(ord, "\u201c\u201d\u201e\u201f\u2033"), '"')


def normalize_album_title(title: str) -> str:
    """Returns a strict comparison key for an album title.

    Only folds case and accents, maps curly quotes/apostrophes to straight
    ones, and collapses whitespace. Parentheses, brackets and their contents,
    "&", dashes, other punctuation, and a leading "The" are all kept, so
    "Siamese Dream" and "Siamese Dream (Reissue)" stay distinct.
    """
    original = title or ""
    text = unicodedata.normalize("NFKD", original)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = text.translate(_SINGLE_QUOTES).translate(_DOUBLE_QUOTES)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return original.casefold().strip()
    return text


def album_match_key(entry: "MergeEntry") -> tuple:
    """Returns the album grouping key: (normalized album artist, normalized title)."""
    return normalize_name(entry.artist), normalize_album_title(entry.title)


def _album_size(entry: "MergeEntry") -> int:
    return entry.track_count


def choose_target(entries: list, size_fn=None) -> tuple:
    """Returns (target, others) for a group of entries.

    The entry with the largest `size_fn(entry)` survives (default: most
    albums); ties go to the lower (older) ratingKey. `others` keeps the same
    ordering (largest first).
    """
    size_fn = size_fn or (lambda e: e.album_count)
    ordered = sorted(entries, key=lambda e: (-size_fn(e), e.rating_key))
    return ordered[0], ordered[1:]


def _find(parent: dict, key: str) -> str:
    while parent[key] != key:
        parent[key] = parent[parent[key]]
        key = parent[key]
    return key


def group_candidates(
    entries: list,
    threshold: float = DEFAULT_THRESHOLD,
    fuzzy: bool = True,
    key_fn=None,
    size_fn=None,
) -> list:
    """Groups entries whose names look the same into CandidateGroups.

    Entries are de-duplicated by ratingKey first, then bucketed by
    `key_fn(entry)` (default: normalize_name(entry.title)). With `fuzzy` on,
    buckets whose keys have a difflib similarity ratio >= `threshold` are
    joined (transitively, via union-find); fuzzy needs string keys. Only
    groups with 2+ distinct ratingKeys are returned, sorted by the target's
    title. `size_fn` is passed to choose_target() to pick the survivor.

    The fuzzy pass compares distinct normalized keys only, sorted by length,
    and skips pairs whose lengths alone rule out reaching the threshold.
    """
    key_fn = key_fn or (lambda e: normalize_name(e.title))
    unique = {}
    for entry in entries:
        if entry.rating_key not in unique:
            unique[entry.rating_key] = entry

    buckets = {}
    for entry in unique.values():
        buckets.setdefault(key_fn(entry), []).append(entry)

    parent = {key: key for key in buckets}
    if fuzzy:
        keys = sorted(buckets, key=lambda k: (len(k), k))
        for i, a in enumerate(tqdm(keys, desc="Comparing names", unit=" names")):
            for b in keys[i + 1:]:
                la, lb = len(a), len(b)
                # Max possible ratio given the lengths; keys are sorted by
                # length, so once this falls below threshold it stays below.
                if la + lb == 0 or 2 * min(la, lb) / (la + lb) < threshold:
                    break
                matcher = difflib.SequenceMatcher(None, a, b)
                if (
                    matcher.real_quick_ratio() >= threshold
                    and matcher.quick_ratio() >= threshold
                    and matcher.ratio() >= threshold
                ):
                    ra, rb = _find(parent, a), _find(parent, b)
                    if ra != rb:
                        parent[rb] = ra

    unions = {}
    for key in buckets:
        unions.setdefault(_find(parent, key), []).append(key)

    groups = []
    for keys_in_union in unions.values():
        members = [entry for key in keys_in_union for entry in buckets[key]]
        if len(members) < 2:
            continue
        target, others = choose_target(members, size_fn)
        match = "exact" if len(keys_in_union) == 1 else "similar"
        groups.append(CandidateGroup(target=target, others=others, match=match))

    groups.sort(key=lambda g: (g.target.title.casefold(), g.target.rating_key))
    return groups


def _iter_paged(search_fn, filters: dict, page_size: int = None):
    """Yields every result of `search_fn`, one page at a time.

    Same pagination loop as retrieve_plex_library.iter_tracks: requests
    `page_size` items per call and stops on an empty or short page.
    """
    page_size = page_size or _PAGE_SIZE
    offset = 0
    while True:
        page = search_fn(
            filters=filters,
            container_start=offset,
            container_size=page_size,
            maxresults=page_size,
        )
        if not page:
            break
        for item in page:
            yield item
        if len(page) < page_size:
            break  # last (partial) page - no need for another round-trip
        offset += len(page)


def iter_artists(music_section: MusicSection, artist: str = None, page_size: int = None):
    """Yields every artist in the section, optionally limited to names containing `artist`."""
    filters = {"title__icontains": artist} if artist else {}
    return _iter_paged(music_section.searchArtists, filters, page_size)


def count_albums_by_artist(music_section: MusicSection, artist: str = None, page_size: int = None) -> dict:
    """Returns {artist ratingKey: album count} from one paged album scan.

    Plex artists carry no album count, and Artist.albums() costs one request
    per artist. Paging through albums once and counting by each album's
    parentRatingKey (its album artist) is far cheaper on a large library.
    """
    filters = {"artist.title__icontains": artist} if artist else {}
    counts = {}
    for album in tqdm(
        _iter_paged(music_section.searchAlbums, filters, page_size),
        desc="Counting albums",
        unit=" albums",
    ):
        key = _attr(album, "parentRatingKey")
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def scan_artists(music_section: MusicSection, artist: str = None) -> list:
    """Returns a MergeEntry for every artist in scope, with album counts filled in.

    Artists missing a title or ratingKey (partial-object gaps) are skipped.
    """
    entries = []
    for obj in tqdm(iter_artists(music_section, artist=artist), desc="Scanning artists", unit=" artists"):
        title = _attr(obj, "title")
        rating_key = _attr(obj, "ratingKey")
        if not title or rating_key is None:
            continue
        entries.append(MergeEntry(obj=obj, title=title, rating_key=rating_key))

    counts = count_albums_by_artist(music_section, artist=artist)
    for entry in entries:
        entry.album_count = counts.get(entry.rating_key, 0)
    return entries


def find_artist_candidates(
    music_section: MusicSection,
    artist: str = None,
    threshold: float = DEFAULT_THRESHOLD,
    fuzzy: bool = True,
) -> list:
    """Scans the section's artists and returns the full list of candidate groups."""
    return group_candidates(scan_artists(music_section, artist=artist), threshold=threshold, fuzzy=fuzzy)


def iter_albums(music_section: MusicSection, artist: str = None, album: str = None, page_size: int = None):
    """Yields every album in the section, optionally filtered.

    `album` limits to titles containing it; `artist` limits to album artists
    whose name contains it. Both are case-insensitive and may be combined.
    """
    filters = {}
    if album:
        filters["title__icontains"] = album
    if artist:
        filters["artist.title__icontains"] = artist
    return _iter_paged(music_section.searchAlbums, filters, page_size)


def scan_albums(music_section: MusicSection, artist: str = None, album: str = None) -> list:
    """Returns a MergeEntry for every album in scope.

    Albums missing a title, ratingKey or album artist name (partial-object
    gaps) are skipped, since their artist can't be confirmed.
    """
    entries = []
    for obj in tqdm(
        iter_albums(music_section, artist=artist, album=album),
        desc="Scanning albums",
        unit=" albums",
    ):
        title = _attr(obj, "title")
        rating_key = _attr(obj, "ratingKey")
        artist_title = _attr(obj, "parentTitle")
        if not title or rating_key is None or not artist_title:
            continue
        entries.append(MergeEntry(
            obj=obj,
            title=title,
            rating_key=rating_key,
            track_count=_attr(obj, "leafCount") or 0,
            artist=artist_title,
            artist_key=_attr(obj, "parentRatingKey"),
            year=_album_year(obj),
        ))
    return entries


def album_directories(album_obj) -> tuple:
    """Returns (sorted distinct folders of the album's track files, track count).

    One request per album (Album.tracks()), so only call this for candidates.
    """
    tracks = album_obj.tracks()
    directories = []
    for track in tracks:
        for part in track.iterParts():
            path = _attr(part, "file")
            if path:
                directories.append(_server_dirname(path))
    return _dedupe_folders(directories), len(tracks)


def _hydrate_album_groups(groups: list) -> list:
    """Reads folders (and missing track counts) for every candidate album.

    Any error while reading one album's tracks or folders (Plex/network
    errors, but also XML parse errors or partial track/part objects) is
    reported as a warning and leaves its folders unknown; the other albums
    are still read. Only Exception is caught, so Ctrl+C still stops the run.
    The survivor of each group is re-picked afterwards in case a track count
    was filled in from the fetched tracks.
    """
    entries = [e for g in groups for e in [g.target, *g.others]]
    for entry in tqdm(entries, desc="Reading album folders", unit=" albums"):
        try:
            directories, count = album_directories(entry.obj)
        except Exception as exc:  # noqa: BLE001 - one bad album must not abort the review
            print(f'Warning: could not read tracks for "{entry.title}": {exc}', file=sys.stderr)
            continue
        entry.directories = directories
        if not entry.track_count:
            entry.track_count = count

    for group in groups:
        group.target, group.others = choose_target([group.target, *group.others], _album_size)
    groups.sort(key=lambda g: (g.target.title.casefold(), g.target.rating_key))
    return groups


def find_album_candidates(music_section: MusicSection, artist: str = None, album: str = None) -> list:
    """Scans the section's albums and returns the full list of candidate groups.

    Grouping is exact on album_match_key (never fuzzy). Tracks are read only
    for albums in a candidate group, after the whole scan has finished.
    """
    entries = scan_albums(music_section, artist=artist, album=album)
    groups = group_candidates(entries, fuzzy=False, key_fn=album_match_key, size_fn=_album_size)
    return _hydrate_album_groups(groups)


def format_prompt(target: MergeEntry, other: MergeEntry) -> str:
    """Returns the confirmation prompt for merging `other` into `target`."""
    return (
        f"Would you like to merge {target.title} ({target.album_count} albums) "
        f"with {other.title} ({other.album_count} albums)? "
    )


def merge_entry(target: MergeEntry, other: MergeEntry) -> None:
    """Merges `other` into `target` on the Plex server."""
    target.obj.merge([other.rating_key])


def _describe_pair(target: MergeEntry, other: MergeEntry) -> str:
    return (
        f'  Keep ratingKey {target.rating_key} "{target.title}" ({target.album_count} albums); '
        f'merge in ratingKey {other.rating_key} "{other.title}" ({other.album_count} albums)'
    )


def _absorb_artist(target: MergeEntry, other: MergeEntry) -> None:
    target.album_count += other.album_count


def _year_text(entry: MergeEntry) -> str:
    return str(entry.year) if entry.year else "unknown year"


def _tracks_text(count: int) -> str:
    return "1 track" if count == 1 else f"{count} tracks"


def _album_label(entry: MergeEntry) -> str:
    return f"{entry.title} by {entry.artist} ({_year_text(entry)}, {_tracks_text(entry.track_count)})"


# Folder status of an album pair (see folder_status()).
SAME_FOLDER = "same_folder"
SAME_FOLDERS = "same_folders"
DIFFERENT_FOLDERS = "different"
FOLDERS_UNKNOWN = "unknown"

# Tag appended to the album question line for each folder status.
_FOLDER_TAGS = {
    SAME_FOLDER: "[same folder]",
    SAME_FOLDERS: "[same folders]",
    DIFFERENT_FOLDERS: "[different folders]",
    FOLDERS_UNKNOWN: "[folders unknown]",
}


def folder_status(target: MergeEntry, other: MergeEntry) -> str:
    """Classifies where two albums' track files live.

    Returns FOLDERS_UNKNOWN when either album's folders are unknown (None) or
    empty (no track files), SAME_FOLDER / SAME_FOLDERS when both name the
    same set of one / several folders (compared with _folder_key), and
    DIFFERENT_FOLDERS otherwise, including partial overlap.
    """
    if not target.directories or not other.directories:
        return FOLDERS_UNKNOWN
    if not _same_folders(target.directories, other.directories):
        return DIFFERENT_FOLDERS
    if len({_folder_key(d) for d in target.directories}) == 1:
        return SAME_FOLDER
    return SAME_FOLDERS


def format_album_prompt(target: MergeEntry, other: MergeEntry) -> str:
    """Returns the confirmation prompt for merging album `other` into `target`.

    Ends with a tag for the pair's current folder status, e.g. "[same folder]".
    """
    tag = _FOLDER_TAGS[folder_status(target, other)]
    return f"Would you like to merge {_album_label(target)} with {_album_label(other)}? {tag} "


def _folder_list_lines(directories) -> list:
    lines = [f"    {d}" for d in directories[:_MAX_FOLDERS_SHOWN]]
    if len(directories) > _MAX_FOLDERS_SHOWN:
        lines.append(f"    ... and {len(directories) - _MAX_FOLDERS_SHOWN} more")
    return lines


def _folder_lines(role: str, directories) -> list:
    if directories is None:
        return [f"  {role} folder: unknown (tracks could not be read)"]
    if not directories:
        return [f"  {role} folder: unknown (no track files found)"]
    if len(directories) == 1:
        return [f"  {role} folder: {directories[0]}"]
    return [f"  {role} folders ({len(directories)}):", *_folder_list_lines(directories)]


def _folder_status_lines(target: MergeEntry, other: MergeEntry) -> list:
    status = folder_status(target, other)
    if status == SAME_FOLDER:
        return [f"  Folders: both albums are in the same folder: {target.directories[0]}"]
    if status == SAME_FOLDERS:
        folders = _dedupe_folders(target.directories)
        return [f"  Folders: both albums use the same {len(folders)} folders:", *_folder_list_lines(folders)]
    if status == DIFFERENT_FOLDERS:
        lines = ["  Note: these albums are in different folders"]
    else:
        lines = ["  Note: could not confirm whether these albums share a folder"]
    return lines + _folder_lines("Keep", target.directories) + _folder_lines("Merge-in", other.directories)


def describe_album_pair(target: MergeEntry, other: MergeEntry) -> list:
    """Returns the context lines printed before an album prompt (and in dry run)."""
    lines = [
        f'  Keep ratingKey {target.rating_key} "{target.title}" by {target.artist} '
        f"({_year_text(target)}, {_tracks_text(target.track_count)}); "
        f'merge in ratingKey {other.rating_key} "{other.title}" by {other.artist} '
        f"({_year_text(other)}, {_tracks_text(other.track_count)})"
    ]
    if target.year and other.year and target.year != other.year:
        lines.append(
            f"  Note: release years differ ({target.year} vs {other.year}) - "
            "these may be different releases."
        )
    if target.artist_key != other.artist_key:
        lines.append(
            "  Note: these albums are under different artist entries "
            f"(ratingKey {target.artist_key} vs {other.artist_key}); "
            "consider running --merge artists first."
        )
    lines += _folder_status_lines(target, other)
    return lines


def _absorb_album(target: MergeEntry, other: MergeEntry) -> None:
    target.track_count += other.track_count
    if target.directories is not None and other.directories is not None:
        target.directories = _dedupe_folders([*target.directories, *other.directories])


@dataclass(frozen=True)
class MergeMode:
    """Per-mode hooks used by run_merge_tool() and review_and_merge()."""

    find: object  # (music_section, artist, album, threshold, fuzzy) -> list[CandidateGroup]
    format_prompt: object  # (target, other) -> str
    describe_pair: object  # (target, other) -> list[str] context lines
    absorb: object  # (target, other) -> None, updates the survivor after a merge


def _find_artists(music_section, artist=None, album=None, threshold=DEFAULT_THRESHOLD, fuzzy=True):
    return find_artist_candidates(music_section, artist=artist, threshold=threshold, fuzzy=fuzzy)


def _find_albums(music_section, artist=None, album=None, threshold=DEFAULT_THRESHOLD, fuzzy=True):
    # Album mode never uses fuzzy matching; threshold/fuzzy are ignored.
    return find_album_candidates(music_section, artist=artist, album=album)


ARTIST_MODE = MergeMode(
    find=_find_artists,
    format_prompt=format_prompt,
    describe_pair=lambda t, o: [_describe_pair(t, o)],
    absorb=_absorb_artist,
)

ALBUM_MODE = MergeMode(
    find=_find_albums,
    format_prompt=format_album_prompt,
    describe_pair=describe_album_pair,
    absorb=_absorb_album,
)


def review_and_merge(groups: list, input_fn=None, dry_run: bool = False, mode: MergeMode = None) -> MergeSummary:
    """Presents each queued candidate pair and merges the ones confirmed.

    Groups of 3+ are offered pairwise into the same target. A failed merge is
    reported and counted, and the review continues. EOF or Ctrl+C at a prompt
    stops the review; that pair and all remaining ones count as skipped. With
    `dry_run`, every pair is listed but nothing is prompted or merged. `mode`
    supplies the prompt/context/update hooks (default: artist mode).
    """
    mode = mode or ARTIST_MODE
    summary = MergeSummary()
    print(f"Found {len(groups)} candidate group(s).")
    if not groups:
        return summary

    total_pairs = sum(len(g.others) for g in groups)
    handled = 0
    stopped = False

    for index, group in enumerate(groups, start=1):
        if stopped:
            break
        print(f'[{index}/{len(groups)}] "{group.target.title}" - {group.match} name match')
        for other in group.others:
            for line in mode.describe_pair(group.target, other):
                print(line)
            if dry_run:
                continue
            try:
                confirmed = ask_yes_no(mode.format_prompt(group.target, other), input_fn)
            except (EOFError, KeyboardInterrupt):
                print("\nStopping review.")
                summary.skipped += total_pairs - handled
                stopped = True
                break
            handled += 1
            if not confirmed:
                summary.skipped += 1
                continue
            try:
                merge_entry(group.target, other)
            except (PlexApiException, requests.exceptions.RequestException) as exc:
                print(f"Error: {exc}", file=sys.stderr)
                summary.failed += 1
                continue
            summary.merged += 1
            mode.absorb(group.target, other)
            print("Merged.")

    if dry_run:
        print(f"Dry run: {total_pairs} merge(s) would be offered. Nothing was changed.")
        return summary

    print(f"Summary: {summary.merged} merged, {summary.skipped} skipped, {summary.failed} failed.")
    return summary


# Merge modes by --merge value.
MERGE_MODES = {"artists": ARTIST_MODE, "albums": ALBUM_MODE}


def run_merge_tool(
    plex: PlexServer,
    mode: str = "artists",
    section: str = None,
    artist: str = None,
    album: str = None,
    threshold: float = DEFAULT_THRESHOLD,
    fuzzy: bool = True,
    dry_run: bool = False,
    input_fn=None,
) -> MergeSummary:
    """Scans the music section for duplicates, then reviews and merges them.

    The full scan finishes (all candidates queued) before the first prompt.
    An unknown `mode`, or `album` given with mode "artists", raises
    ValueError before touching `plex`. `threshold` and `fuzzy` only apply to
    artist mode.
    """
    if mode not in MERGE_MODES:
        raise ValueError(f"Unknown merge mode: {mode}")
    if album and mode != "albums":
        raise ValueError("--album is only supported with --merge albums.")

    spec = MERGE_MODES[mode]
    music_section = resolve_music_section(plex, section)
    groups = spec.find(music_section, artist=artist, album=album, threshold=threshold, fuzzy=fuzzy)
    return review_and_merge(groups, input_fn=input_fn, dry_run=dry_run, mode=spec)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find Plex music artists or albums that were split into several "
            "entries (different ratingKeys) and merge them after confirming "
            "each one."
        ),
    )
    parser.add_argument(
        "-m", "--merge",
        choices=["artists", "albums"],
        default="artists",
        help=(
            "What to merge: 'artists' (identical or very similar names) or 'albums' "
            "(same album artist and the same title after light normalization; "
            "qualifiers like '(Reissue)' or '[Live]' are never ignored). Default artists."
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
        help=(
            "Limit the scan to artists whose name contains this string (case-insensitive). "
            "With --merge albums, limits to albums whose album artist contains it."
        ),
    )
    parser.add_argument(
        "--album",
        default=None,
        help=(
            "Limit the scan to albums whose title contains this string (case-insensitive). "
            "Only used with --merge albums."
        ),
    )
    parser.add_argument(
        "-t", "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=(
            f"Similarity (0-1) for near-match names. Higher is stricter. Default {DEFAULT_THRESHOLD:.2f}. "
            "Artist mode only; album mode always requires matching titles."
        ),
    )
    parser.add_argument(
        "--no-fuzzy",
        action="store_true",
        help=(
            "Only group names that are identical after normalization. "
            "Artist mode only (album mode never uses fuzzy matching)."
        ),
    )
    parser.add_argument(
        "-d", "--dry-run",
        action="store_true",
        help="List merge candidates without prompting or merging anything.",
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
    if not 0 < args.threshold <= 1:
        parser.error("--threshold must be greater than 0 and at most 1.")
    if args.album and args.merge != "albums":
        parser.error("--album is only supported with --merge albums.")

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
        summary = run_merge_tool(
            plex,
            mode=args.merge,
            section=args.section,
            artist=args.artist,
            album=args.album,
            threshold=args.threshold,
            fuzzy=not args.no_fuzzy,
            dry_run=args.dry_run,
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
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
