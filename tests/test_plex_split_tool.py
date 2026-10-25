import re
from datetime import datetime
from unittest.mock import MagicMock

import pytest
import requests
from plexapi.exceptions import NotFound, PlexApiException
from plexapi.library import MusicSection

import plex_split_tool as ps
import plex_style as style


def render_all(items):
    """Renders describe_candidate() renderables the way review_and_split() prints them."""
    return "".join(style.render(item) for item in items)


def _plain(value):
    return value.plain if hasattr(value, "plain") else str(value)


def table_rows(table):
    """Returns the table's rows as tuples of plain cell text."""
    columns = [list(column.cells) for column in table.columns]
    return [tuple(_plain(column[i]) for column in columns) for i in range(table.row_count)]

# Never a real server: every main() test patches PlexServer and the credential lookup.
FAKE_URL = "http://plex.invalid:32400"
URL_ARGS = ["-l", FAKE_URL, "-u", "joe", "-p", "pw"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_track(album_key, title, artist, paths, rating_key, parent_year=None):
    """A track mock with only the attributes the tool reads set explicitly."""
    track = MagicMock()
    track.parentRatingKey = album_key
    track.parentTitle = title
    track.grandparentTitle = artist
    track.parentYear = parent_year
    track.ratingKey = rating_key
    track.media = [MagicMock(parts=[MagicMock(file=p) for p in paths])]
    return track


def album_tracks(album_key, title, folders, per_folder=2, artist="Smashing Pumpkins", start_key=None):
    """Tracks of one album spread over `folders` (Windows-style paths), `per_folder` each."""
    start_key = album_key * 1000 if start_key is None else start_key
    tracks = []
    for folder in folders:
        for _ in range(per_folder):
            key = start_key + len(tracks)
            tracks.append(make_track(album_key, title, artist, [f"{folder}\\{key}.mp3"], key))
    return tracks


def make_album_obj(title, rating_key, year=None, originally=None, artist="Smashing Pumpkins"):
    album = MagicMock()
    album.title = title
    album.parentTitle = artist
    album.ratingKey = rating_key
    album.year = year
    album.originallyAvailableAt = originally
    album.split = MagicMock()
    return album


def _paged(items):
    # Signature matches what the tool may pass; any other kwarg (e.g. libtype) raises TypeError.
    def fake_search(filters=None, container_start=0, container_size=None, maxresults=None):
        end = None if container_size is None else container_start + container_size
        return items[container_start:end]

    return fake_search


def make_section(tracks, albums_by_key=None, after_split=None):
    """A MusicSection mock: paged searchTracks, fetchItem by key, fetchItems by key list.

    `after_split` maps track ratingKey -> (album ratingKey, album title) as Plex
    reports it after a split; keys missing from it are not found.
    """
    albums_by_key = albums_by_key or {}
    section = MagicMock(spec=MusicSection)
    section.searchTracks.side_effect = _paged(tracks)

    def fetch_item(key):
        if key not in albums_by_key:
            raise NotFound(f"no album {key}")
        value = albums_by_key[key]
        if isinstance(value, BaseException):
            raise value
        return value

    section.fetchItem.side_effect = fetch_item

    def fetch_items(keys):
        result = []
        for key in keys:
            if after_split is not None and key in after_split:
                album_key, title = after_split[key]
                result.append(make_track(album_key, title, "x", [], key))
        return result

    section.fetchItems.side_effect = fetch_items
    return section


def make_plex(section):
    plex = MagicMock()
    plex.library.section.return_value = section
    plex.library.sections.return_value = [section]
    return plex


def scripted_input(answers):
    """Returns an input() stand-in that replays `answers` and records prompts."""
    remaining = list(answers)

    def fake_input(prompt=""):
        fake_input.prompts.append(prompt)
        answer = remaining.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    fake_input.prompts = []
    return fake_input


def fake_creds(url, username, password):
    return {"url": url, "token": "tok"}


SP = "E:\\MP3\\Smashing Pumpkins"


def siamese_section(after_split=None):
    """One wrongly combined album (two release folders) and one clean multi-disc album."""
    tracks = (
        album_tracks(100, "Siamese Dream", [f"{SP}\\Siamese Dream", f"{SP}\\Siamese Dream (Reissue)"])
        + album_tracks(200, "Mellon Collie", [f"{SP}\\Mellon Collie\\CD1", f"{SP}\\Mellon Collie\\CD2"])
    )
    albums = {100: make_album_obj("Siamese Dream", 100, year=1993), 200: make_album_obj("Mellon Collie", 200)}
    return make_section(tracks, albums, after_split), albums


# ---------------------------------------------------------------------------
# Disc patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "CD1", "cd1", "CD 1", "CD-2", "CD_3", "Disc1", "Disc 2", "DISC 02", "Disk 3", "disc01", "Disc.2",
    "CD1 - Title", "Disc 2 (Bonus)", "[Disc 1]", "(CD 2)", "Disc 1 of 2",
])
def test_disc_folder_pattern_positives(name):
    assert ps.DISC_FOLDER_PATTERN.match(name)


@pytest.mark.parametrize("name", [
    "Discography", "CD Collection", "Disco Hits", "CDs", "Discs 1-2", "CD1A", "Disc 1000", "ACDC 2",
    "Album CD1", "Vol. 1", "Volume 2", "Part 1", "Siamese Dream",
])
def test_disc_folder_pattern_negatives(name):
    assert not ps.DISC_FOLDER_PATTERN.match(name)


@pytest.mark.parametrize("name, title", [
    ("Album (Disc 1)", "Album"),
    ("Album - CD2", "Album"),
    ("Album CD 2", "Album"),
    ("Album [Disc 1]", "Album"),
    ("Album_Disc_2", "Album"),
    ("Mellon Collie (Disc 1)", "Mellon Collie"),
])
def test_disc_suffix_pattern_positives(name, title):
    match = ps.DISC_SUFFIX_PATTERN.match(name)
    assert match and match["title"].rstrip(" ._-") == title


@pytest.mark.parametrize("name", ["Disco 2", "AlbumCD1", "Album Disc 2 (Bonus)", "Album Vol. 2", "Siamese Dream"])
def test_disc_suffix_pattern_negatives(name):
    assert not ps.DISC_SUFFIX_PATTERN.match(name)


# ---------------------------------------------------------------------------
# album_root()
# ---------------------------------------------------------------------------

def roots_of(*folders):
    return {ps.album_root(f)[0] for f in folders}


def test_album_root_disc_subfolders_collapse():
    assert len(roots_of("E:\\M\\A\\CD1", "E:\\M\\A\\CD2")) == 1
    assert ps.album_root("E:\\M\\A\\CD1")[1] == "E:\\M\\A"


def test_album_root_root_plus_disc_folder_collapse():
    assert len(roots_of("E:\\M\\A", "E:\\M\\A\\Disc 2")) == 1


def test_album_root_title_disc_suffix_siblings_collapse():
    assert len(roots_of("E:\\M\\X (Disc 1)", "E:\\M\\X (Disc 2)", "E:\\M\\X")) == 1
    # The display is the original folder, since the stripped path may not exist.
    assert ps.album_root("E:\\M\\X (Disc 1)")[1] == "E:\\M\\X (Disc 1)"


def test_album_root_different_albums_stay_apart():
    assert len(roots_of("E:\\M\\Siamese Dream", "E:\\M\\Siamese Dream (Reissue)")) == 2
    assert len(roots_of("E:\\M\\Vol. 1", "E:\\M\\Vol. 2")) == 2


def test_album_root_windows_case_and_separators():
    assert len(roots_of("E:\\MP3\\A\\CD1", "e:/mp3/a/cd2")) == 1
    assert len(roots_of("E:\\MP3\\A", "e:/mp3/a/")) == 1


def test_album_root_posix_is_case_sensitive():
    assert len(roots_of("/m/A", "/m/a")) == 2
    assert len(roots_of("/m/A/CD1", "/m/A/CD2")) == 1


def test_album_root_drive_root_stays_itself():
    assert ps.album_root("E:\\") == (ps.folder_key("E:\\"), "E:\\")


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def test_scan_groups_by_album_in_one_pass_and_fetches_only_candidates():
    section, albums = siamese_section()

    candidates = ps.find_albums_by_folder(section)

    assert [c.rating_key for c in candidates] == [100]
    assert section.searchTracks.call_count == 1
    section.fetchItem.assert_called_once_with(100)
    for album in albums.values():
        album.tracks.assert_not_called()
    candidate = candidates[0]
    assert candidate.obj is albums[100]
    assert candidate.track_count == 4
    assert candidate.year == 1993
    assert [r.display for r in candidate.roots] == [f"{SP}\\Siamese Dream", f"{SP}\\Siamese Dream (Reissue)"]
    assert [r.track_count for r in candidate.roots] == [2, 2]


def test_scan_fetch_happens_after_whole_scan():
    section, _ = siamese_section()
    order = []
    search = section.searchTracks.side_effect
    fetch = section.fetchItem.side_effect

    def tracked_search(**kwargs):
        order.append("search")
        return search(**kwargs)

    def tracked_fetch(key):
        order.append("fetch")
        return fetch(key)

    section.searchTracks.side_effect = tracked_search
    section.fetchItem.side_effect = tracked_fetch

    ps.find_albums_by_folder(section)
    assert order == ["search", "fetch"]


def test_scan_pagination_and_filters(monkeypatch):
    monkeypatch.setattr(ps, "_PAGE_SIZE", 2)
    tracks = album_tracks(1, "A", ["E:\\M\\A"], per_folder=5)
    section = make_section(tracks)

    albums, skipped = ps.scan_album_tracks(section, artist="Pumpkins", album="Dream")

    assert skipped == 0
    assert albums[1].track_count == 5
    calls = section.searchTracks.call_args_list
    assert [c.kwargs["container_start"] for c in calls] == [0, 2, 4]
    for call in calls:
        assert call.kwargs["filters"] == {"artist.title__icontains": "Pumpkins", "album.title__icontains": "Dream"}
        assert "libtype" not in call.kwargs


def test_scan_no_filters_by_default():
    section = make_section([])
    ps.scan_album_tracks(section)
    assert section.searchTracks.call_args.kwargs["filters"] == {}


def test_scan_pathless_and_keyless_tracks(capsys):
    tracks = album_tracks(1, "A", ["E:\\M\\A"])
    tracks.append(make_track(1, "A", "X", [], 99))  # no file path
    tracks.append(make_track(None, "B", "X", ["E:\\M\\B\\1.mp3"], 98))  # no album key
    section = make_section(tracks, {1: make_album_obj("A", 1)})

    albums, skipped = ps.scan_album_tracks(section)
    assert skipped == 1
    assert albums[1].track_count == 3
    assert albums[1].pathless_tracks == 1
    assert len(albums[1].roots) == 1

    # Only one real folder: not a candidate.
    assert ps.find_albums_by_folder(section) == []
    assert "Skipped 1 track(s) with no album ratingKey." in capsys.readouterr().err


def test_scan_multi_part_track_counts_once_per_root():
    track = make_track(1, "A", "X", ["E:\\M\\A\\1.mp3", "E:\\M\\A\\CD2\\1.mp3", "E:\\M\\B\\1.mp3"], 10)
    albums, _ = ps.scan_album_tracks(make_section([track]))
    roots = albums[1].roots
    assert [(r.display, r.track_count, len(r.folders)) for r in roots] == [
        ("E:\\M\\A", 1, 2), ("E:\\M\\B", 1, 1),
    ]


def test_scan_missing_titles_fall_back_and_fill_from_album():
    tracks = [make_track(5, None, None, ["E:\\M\\A\\1.mp3"], 1), make_track(5, None, None, ["E:\\M\\B\\1.mp3"], 2)]
    section = make_section(tracks, {5: make_album_obj("Real Title", 5, artist="Real Artist")})
    candidate = ps.find_albums_by_folder(section)[0]
    assert (candidate.title, candidate.artist) == ("Real Title", "Real Artist")


def test_scan_year_from_release_date_and_scan_fallback():
    tracks = album_tracks(5, "A", ["E:\\M\\A", "E:\\M\\B"])
    section = make_section(tracks, {5: make_album_obj("A", 5, originally=datetime(2011, 5, 1))})
    assert ps.find_albums_by_folder(section)[0].year == 2011


def test_candidates_sorted_by_artist_then_title():
    tracks = (
        album_tracks(1, "Zed", ["E:\\A", "E:\\B"], artist="beta")
        + album_tracks(2, "Alpha", ["E:\\C", "E:\\D"], artist="Beta")
        + album_tracks(3, "Mid", ["E:\\E", "E:\\F"], artist="Alpha")
    )
    section = make_section(tracks, {k: make_album_obj("t", k) for k in (1, 2, 3)})
    assert [c.rating_key for c in ps.find_albums_by_folder(section)] == [3, 2, 1]


# ---------------------------------------------------------------------------
# Context and prompt text
# ---------------------------------------------------------------------------

def candidate_with_roots(roots, year=1993, tracks=26, **kwargs):
    return ps.SplitCandidate(
        rating_key=12345, title="Siamese Dream", artist="Smashing Pumpkins",
        year=year, track_count=tracks, roots=roots, obj=MagicMock(), **kwargs,
    )


def test_describe_and_prompt_text():
    candidate = candidate_with_roots([
        ps.AlbumRoot("a", "E:\\SP\\Siamese Dream", 13, ["E:\\SP\\Siamese Dream"]),
        ps.AlbumRoot("b", "E:\\SP\\Mellon Collie", 28, ["E:\\SP\\Mellon Collie\\CD1", "E:\\SP\\Mellon Collie\\CD2"]),
        ps.AlbumRoot("c", "E:\\SP\\Single", 1, ["E:\\SP\\Single"]),
    ], tracks=42)
    items = ps.describe_candidate(candidate)
    assert len(items) == 1
    table = items[0].renderable
    assert [c.header for c in table.columns] == ["Folder", "Tracks", "Disc folders"]
    assert table_rows(table) == [
        ("E:\\SP\\Siamese Dream", "13", ""),
        ("E:\\SP\\Mellon Collie", "28", "2"),
        ("E:\\SP\\Single", "1", ""),
    ]
    assert ps.format_split_prompt(candidate).plain == 'Split "Siamese Dream" (42 tracks across 3 folders)? [y/N] '
    header = ps.format_candidate_header(1, 2, candidate)
    assert header.plain == '[1/2] "Siamese Dream" by Smashing Pumpkins  (ratingKey 12345, 1993)'


def test_describe_caps_roots_and_notes():
    roots = [ps.AlbumRoot(str(i), f"E:\\F{i}", 1, [f"E:\\F{i}"]) for i in range(ps.MAX_FOLDERS_SHOWN + 2)]
    candidate = candidate_with_roots(roots, year=None, pathless_tracks=2, load_error="boom")
    items = ps.describe_candidate(candidate)
    rows = table_rows(items[0].renderable)
    assert len(rows) == ps.MAX_FOLDERS_SHOWN + 1
    assert rows[-1] == ("... and 2 more", "", "")
    assert [_plain(item) for item in items[1:]] == [
        "  Note: 2 track(s) have no file path and were not checked.",
        "  Note: could not load this album from Plex: boom",
    ]
    assert "unknown year" in ps.format_candidate_header(1, 1, candidate).plain


def test_prompt_single_track():
    candidate = candidate_with_roots([ps.AlbumRoot("a", "A"), ps.AlbumRoot("b", "B")], tracks=1)
    assert "(1 track across 2 folders)" in ps.format_split_prompt(candidate)


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def test_queue_before_prompt_and_yes_splits(capsys):
    after = {k: (100, "Siamese Dream") for k in (100000, 100001)}
    after.update({k: (300, "Siamese Dream (Reissue)") for k in (100002, 100003)})
    section, albums = siamese_section(after)
    events = []
    search = section.searchTracks.side_effect

    def tracked_search(**kwargs):
        events.append("search")
        return search(**kwargs)

    section.searchTracks.side_effect = tracked_search
    fake = scripted_input(["y"])

    def recording_input(prompt=""):
        events.append("input")
        return fake(prompt)

    summary = ps.run_split_tool(make_plex(section), apply=True, input_fn=recording_input)

    assert events == ["search", "input"]
    albums[100].split.assert_called_once_with()
    albums[200].split.assert_not_called()
    assert fake.prompts == ['Split "Siamese Dream" (4 tracks across 2 folders)? [y/N] ']
    assert (summary.split, summary.unchanged, summary.skipped, summary.failed) == (1, 0, 0, 0)
    out = capsys.readouterr().out
    assert "Found 1 album(s) whose tracks span more than one album folder." in out
    assert f"Note: {ps.SPLIT_CAVEAT}" in out
    assert '[1/1] "Siamese Dream" by Smashing Pumpkins  (ratingKey 100, ' in out
    assert "Split requested. Plex now lists these tracks under:" in out
    lines = out.splitlines()
    assert any(line.startswith("  | 100 ") and "| Siamese Dream " in line and "| original |" in line for line in lines)
    assert any(line.startswith("  | 300 ") and "| Siamese Dream (Reissue) |" in line for line in lines)
    assert "Summary: 1 split, 0 unchanged, 0 skipped, 0 failed." in out


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_review_no_and_enter_skip(answer):
    section, albums = siamese_section()
    summary = ps.run_split_tool(make_plex(section), apply=True, input_fn=scripted_input([answer]))
    albums[100].split.assert_not_called()
    section.fetchItems.assert_not_called()
    assert (summary.split, summary.skipped) == (0, 1)


@pytest.mark.parametrize("args", [{}, {"apply": False}])
def test_dry_run_never_prompts_or_splits(capsys, args):
    section, albums = siamese_section()

    def no_input(prompt=""):
        pytest.fail("input should not be called in dry-run mode")

    summary = ps.run_split_tool(make_plex(section), input_fn=no_input, **args)

    albums[100].split.assert_not_called()
    section.fetchItems.assert_not_called()
    assert (summary.split, summary.skipped, summary.failed) == (0, 0, 0)
    out = capsys.readouterr().out
    assert any(
        line.startswith("  | E:\\MP3\\Smashing Pumpkins\\Siamese Dream ") and "|      2 |" in line
        for line in out.splitlines()
    )
    assert "Dry run: 1 album(s) would be offered for splitting. Nothing was changed. Use --apply to split." in out
    assert ps.SPLIT_CAVEAT not in out
    assert "[y/N]" not in out


def two_candidates():
    tracks = (
        album_tracks(1, "A", ["E:\\M\\A", "E:\\M\\A2"], artist="Art")
        + album_tracks(2, "B", ["E:\\M\\B", "E:\\M\\B2"], artist="Art")
    )
    albums = {1: make_album_obj("A", 1), 2: make_album_obj("B", 2)}
    return make_section(tracks, albums), albums


@pytest.mark.parametrize("error", [PlexApiException("boom"), requests.exceptions.ConnectionError("refused")])
def test_split_failure_continues(capsys, error):
    section, albums = two_candidates()
    albums[1].split.side_effect = error
    fake = scripted_input(["y", "y"])

    summary = ps.run_split_tool(make_plex(section), apply=True, input_fn=fake)

    assert len(fake.prompts) == 2
    albums[2].split.assert_called_once_with()
    assert (summary.split, summary.unchanged, summary.failed) == (1, 0, 1)
    assert "Error:" in capsys.readouterr().err


def test_load_failure_listed_and_counted_failed(capsys):
    section, albums = two_candidates()
    albums[1] = requests.exceptions.ConnectionError("down")
    fake = scripted_input(["n"])

    summary = ps.run_split_tool(make_plex(section), apply=True, input_fn=fake)

    assert len(fake.prompts) == 1 and fake.prompts[0].startswith('Split "B"')
    captured = capsys.readouterr()
    assert "Note: could not load this album from Plex: down" in captured.out
    assert "Cannot split: album could not be loaded." in captured.out
    assert 'Warning: could not load album "A"' in captured.err
    assert (summary.failed, summary.skipped) == (1, 1)


def test_dry_run_offer_count_excludes_unloaded(capsys):
    section, albums = two_candidates()
    albums[1] = NotFound("gone")
    ps.run_split_tool(make_plex(section))
    assert "Dry run: 1 album(s) would be offered" in capsys.readouterr().out


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt()])
def test_eof_or_ctrl_c_stops_review(capsys, error):
    section, albums = two_candidates()
    summary = ps.run_split_tool(make_plex(section), apply=True, input_fn=scripted_input([error]))
    for album in albums.values():
        album.split.assert_not_called()
    assert (summary.split, summary.skipped, summary.failed) == (0, 2, 0)
    out = capsys.readouterr().out
    assert "Stopping review." in out
    assert '[2/2]' not in out
    assert "Summary: 0 split, 0 unchanged, 2 skipped, 0 failed." in out


def test_no_candidates():
    summary = ps.review_and_split([], input_fn=scripted_input([]), apply=True)
    assert (summary.split, summary.skipped, summary.failed) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Post-split report
# ---------------------------------------------------------------------------

def report_candidate(keys):
    return ps.SplitCandidate(rating_key=100, title="T", artist="A", track_keys=list(keys), obj=MagicMock())


def test_report_unchanged(capsys):
    section = make_section([], after_split={1: (100, "T"), 2: (100, "T")})
    assert ps.report_split(section, report_candidate([1, 2])) == "unchanged"
    assert "Plex did not divide this album: all 2 tracks are still under ratingKey 100." in capsys.readouterr().out


def test_report_missing_tracks_note(capsys):
    section = make_section([], after_split={1: (100, "T"), 2: (200, "T2")})
    assert ps.report_split(section, report_candidate([1, 2, 3])) == "split"
    out = capsys.readouterr().out
    assert "  Note: 1 track(s) could not be found after the split." in out
    assert "re-run this tool" in out


def test_report_none_found(capsys):
    section = make_section([], after_split={})
    assert ps.report_split(section, report_candidate([1, 2])) == "split"
    assert "None of the album's 2 tracks could be found yet." in capsys.readouterr().out


def test_report_chunks_track_keys():
    keys = list(range(1, 251))
    section = make_section([], after_split={k: (100 if k < 200 else 300, "T") for k in keys})
    assert ps.report_split(section, report_candidate(keys)) == "split"
    sizes = [len(c.args[0]) for c in section.fetchItems.call_args_list]
    assert sizes == [100, 100, 50]


def test_report_failure_is_warning_and_counts_split(capsys):
    section, albums = siamese_section()
    section.fetchItems.side_effect = requests.exceptions.ConnectionError("lost")
    summary = ps.run_split_tool(make_plex(section), apply=True, input_fn=scripted_input(["y"]))
    albums[100].split.assert_called_once_with()
    assert (summary.split, summary.failed) == (1, 0)
    assert "Warning: split requested, but the tracks could not be re-read: lost" in capsys.readouterr().err


def test_review_counts_unchanged():
    keys = [100000, 100001, 100002, 100003]
    section, _ = siamese_section(after_split={k: (100, "Siamese Dream") for k in keys})
    summary = ps.run_split_tool(make_plex(section), apply=True, input_fn=scripted_input(["y"]))
    assert (summary.split, summary.unchanged) == (0, 1)


# ---------------------------------------------------------------------------
# run_split_tool()
# ---------------------------------------------------------------------------

def test_run_split_tool_unknown_mode():
    plex = MagicMock()
    with pytest.raises(ValueError, match="Unknown split mode"):
        ps.run_split_tool(plex, mode="tracks")
    plex.library.section.assert_not_called()
    plex.library.sections.assert_not_called()


def test_run_split_tool_uses_named_section():
    section, _ = siamese_section()
    plex = make_plex(section)
    ps.run_split_tool(plex, section="Music")
    plex.library.section.assert_called_once_with("Music")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_parse_args_defaults():
    args = ps.parse_args(["-l", FAKE_URL])
    assert args.mode == "albums-by-folder"
    assert args.apply is False
    assert args.dry_run is False
    assert (args.artist, args.album, args.section) == (None, None, None)


def test_parse_args_apply_and_filters():
    args = ps.parse_args(["-l", FAKE_URL, "--apply", "--artist", "x", "--album", "y"])
    assert args.apply is True
    assert (args.artist, args.album) == ("x", "y")


def test_parse_args_dry_run_and_apply_conflict():
    with pytest.raises(SystemExit) as exc:
        ps.parse_args(["-l", FAKE_URL, "-d", "--apply"])
    assert exc.value.code == 2


def test_parse_args_requires_url_unless_credential_file():
    with pytest.raises(SystemExit):
        ps.parse_args([])
    assert ps.parse_args(["-c", "creds.json"]).credential_file == "creds.json"


def test_parse_args_rejects_unknown_mode():
    with pytest.raises(SystemExit):
        ps.parse_args(["-l", FAKE_URL, "--mode", "tracks"])


def patch_server(monkeypatch, plex):
    monkeypatch.setattr(ps, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(ps, "PlexServer", lambda url, token: plex)


@pytest.mark.parametrize("extra", [[], ["-d"]])
def test_main_dry_run_returns_0(monkeypatch, extra):
    section, albums = siamese_section()
    patch_server(monkeypatch, make_plex(section))

    def no_input(prompt=""):
        pytest.fail("input should not be called in dry-run mode")

    monkeypatch.setattr("builtins.input", no_input)

    assert ps.main(URL_ARGS + extra) == 0
    albums[100].split.assert_not_called()


def test_main_apply_yes_splits(monkeypatch):
    section, albums = siamese_section()
    patch_server(monkeypatch, make_plex(section))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    assert ps.main(URL_ARGS + ["--apply", "--artist", "Pumpkins"]) == 0
    albums[100].split.assert_called_once_with()
    assert section.searchTracks.call_args.kwargs["filters"] == {"artist.title__icontains": "Pumpkins"}


def test_main_returns_1_when_split_fails(monkeypatch):
    section, albums = siamese_section()
    albums[100].split.side_effect = requests.exceptions.ConnectionError("refused")
    patch_server(monkeypatch, make_plex(section))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    assert ps.main(URL_ARGS + ["--apply"]) == 1


def test_main_returns_1_on_section_not_found(monkeypatch):
    plex = MagicMock()
    plex.library.section.side_effect = NotFound("Section not found")
    patch_server(monkeypatch, plex)

    assert ps.main(URL_ARGS + ["--section", "Nope"]) == 1


def test_main_returns_1_on_connection_error(monkeypatch):
    def fake_plex_server(url, token):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(ps, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(ps, "PlexServer", fake_plex_server)

    assert ps.main(URL_ARGS) == 1


def test_main_skips_login_with_credential_file(monkeypatch, tmp_path):
    from get_plex_credentials import save_plex_credentials

    creds_path = tmp_path / "creds.json"
    save_plex_credentials({"url": FAKE_URL, "token": "cached-tok"}, str(creds_path))
    section, _ = siamese_section()

    def fail_if_called(*args, **kwargs):
        pytest.fail("get_plex_credentials should not be called when --credential-file is given")

    monkeypatch.setattr(ps, "get_plex_credentials", fail_if_called)
    monkeypatch.setattr(ps, "PlexServer", lambda url, token: make_plex(section))

    assert ps.main(["-c", str(creds_path)]) == 0


# ---------------------------------------------------------------------------
# Colors (display only)
# ---------------------------------------------------------------------------

import plex_style  # noqa: E402

CYAN, BOLD, YELLOW, GREEN, DIM, RED, RESET = (
    "\x1b[36m", "\x1b[1m", "\x1b[33m", "\x1b[32m", "\x1b[2m", "\x1b[31m", "\x1b[0m",
)


_BORDERS = re.compile(r"[\s|+\-\u2500-\u257f]+")


def words(text):
    """Text without color codes or table borders (ASCII or Unicode), for comparing color on/off output."""
    return _BORDERS.sub(" ", plex_style.strip(text)).split()


def _render_value(value):
    return render_all(value) if isinstance(value, list) else plex_style.prompt_str(value)


def plain_and_colored(fn, *args):
    """Returns (fn(*args) rendered with color off, fn(*args) rendered with color on)."""
    plex_style.reset()
    plain = _render_value(fn(*args))
    plex_style.enable()
    return plain, _render_value(fn(*args))


def sample_candidate(**kwargs):
    return candidate_with_roots([
        ps.AlbumRoot("a", "E:\\SP\\Siamese Dream", 13, ["E:\\SP\\Siamese Dream"]),
        ps.AlbumRoot("b", "E:\\SP\\Mellon", 13, ["E:\\SP\\Mellon\\CD1", "E:\\SP\\Mellon\\CD2"]),
    ], **kwargs)


def test_color_split_prompt():
    plain, colored = plain_and_colored(ps.format_split_prompt, sample_candidate())
    assert plain == 'Split "Siamese Dream" (26 tracks across 2 folders)? [y/N] '
    assert f'Split "{BOLD}Siamese Dream{RESET}"' in colored
    assert f"({GREEN}26 tracks{RESET} across {GREEN}2 folders{RESET})? " in colored
    assert colored.endswith(f"{RESET})? [y/N] ")
    assert plex_style.strip(colored) == plain


def test_color_describe_candidate():
    plain, colored = plain_and_colored(ps.describe_candidate, sample_candidate(pathless_tracks=2, load_error="boom"))
    assert f"{DIM}E:\\SP\\Mellon" in colored
    assert re.search(r"\x1b\[32m *13\x1b\[0m", colored)  # right-justified inside the style
    assert "\u256d" in colored and "\u256d" not in plain
    assert f"  {YELLOW}Note:{RESET} {GREEN}2{RESET} track(s)" in colored
    assert f"  {RED}Note:{RESET} could not load this album from Plex: boom" in colored
    assert words(colored) == words(plain)


def test_color_candidate_header():
    plain, colored = plain_and_colored(ps.format_candidate_header, 1, 2, sample_candidate())
    assert plain == '[1/2] "Siamese Dream" by Smashing Pumpkins  (ratingKey 12345, 1993)'
    assert colored == (
        f'[1/2] "{BOLD}Siamese Dream{RESET}" by {CYAN}Smashing Pumpkins{RESET}  '
        f"(ratingKey {DIM}12345{RESET}, {YELLOW}1993{RESET})"
    )


def test_color_root_display_stays_raw():
    candidate = sample_candidate()
    plex_style.enable()
    ps.describe_candidate(candidate)
    assert [r.display for r in candidate.roots] == ["E:\\SP\\Siamese Dream", "E:\\SP\\Mellon"]


def report_out(capsys, color, section_fn, keys):
    if color:
        plex_style.enable()
    else:
        plex_style.reset()
    result = ps.report_split(section_fn(), report_candidate(keys))
    return result, capsys.readouterr().out


@pytest.mark.parametrize("after, keys, expected", [
    ({1: (100, "T"), 2: (200, "T2")}, [1, 2, 3], f"{GREEN}Split requested. Plex now lists these tracks under:{RESET}"),
    ({1: (100, "T"), 2: (100, "T")}, [1, 2], f"{YELLOW}Plex did not divide this album:{RESET}"),
    ({}, [1, 2], f"{YELLOW}Split requested. None of the album's 2 tracks could be found yet.{RESET}"),
])
def test_color_report_split(capsys, after, keys, expected):
    section_fn = lambda: make_section([], after_split=after)  # noqa: E731
    plain_result, plain = report_out(capsys, False, section_fn, keys)
    result, colored = report_out(capsys, True, section_fn, keys)

    assert result == plain_result
    assert expected in colored
    assert words(colored) == words(plain)


def test_color_report_split_original_tag_and_missing_note(capsys):
    section_fn = lambda: make_section([], after_split={1: (100, "T"), 2: (200, "T2")})  # noqa: E731
    _, colored = report_out(capsys, True, section_fn, [1, 2, 3])
    row = next(line for line in colored.splitlines() if f"{DIM}100" in line)
    assert f"{BOLD}T" in row and re.search(r"\x1b\[32m *1\x1b\[0m", row) and f"{DIM}original" in row
    assert f"  {YELLOW}Note:{RESET} {GREEN}1{RESET} track(s) could not be found after the split." in colored


def run_split_review(capsys, color, answers):
    if color:
        plex_style.enable()
    else:
        plex_style.reset()
    after = {k: (100, "Siamese Dream") for k in (100000, 100001)}
    after.update({k: (300, "Siamese Dream (Reissue)") for k in (100002, 100003)})
    section, _ = siamese_section(after_split=after)
    fake = scripted_input(answers)
    summary = ps.run_split_tool(make_plex(section), apply=True, input_fn=fake)
    return summary, fake.prompts, capsys.readouterr().out


def test_color_review_and_split(capsys):
    plain_summary, plain_prompts, plain_out = run_split_review(capsys, False, ["y"])
    summary, prompts, out = run_split_review(capsys, True, ["y"])

    assert summary == plain_summary
    assert f"{YELLOW}Note: {ps.SPLIT_CAVEAT}{RESET}" in out
    assert f'"{BOLD}Siamese Dream{RESET}" by {CYAN}Smashing Pumpkins{RESET}' in out
    assert f"Summary: {GREEN}1{RESET} split" in out
    assert "\x1b" in prompts[0]
    assert [plex_style.strip(p) for p in prompts] == plain_prompts
    assert words(out) == words(plain_out)


def test_color_summary_unchanged_count_is_yellow(capsys):
    plex_style.enable()
    section, _ = siamese_section(after_split={k: (100, "Siamese Dream") for k in range(100000, 100004)})
    ps.run_split_tool(make_plex(section), apply=True, input_fn=scripted_input(["y"]))
    assert f"{YELLOW}1{RESET} unchanged" in capsys.readouterr().out


def test_color_cannot_split_is_red(capsys):
    plex_style.enable()
    section, albums = two_candidates()
    albums[1] = requests.exceptions.ConnectionError("down")
    ps.run_split_tool(make_plex(section), apply=True, input_fn=scripted_input(["n"]))
    out = capsys.readouterr().out
    assert f"{RED}Cannot split: album could not be loaded.{RESET}" in out
    assert f"{RED}1{RESET} failed." in out


@pytest.mark.parametrize("argv, expected", [([], False), (["--no-color"], True)])
def test_parse_args_no_color(argv, expected):
    assert ps.parse_args(URL_ARGS + argv).no_color is expected


def test_help_mentions_no_color(capsys):
    with pytest.raises(SystemExit):
        ps.parse_args(["--help"])
    out = capsys.readouterr().out
    assert "--no-color" in out
    assert "NO_COLOR" in out


@pytest.fixture
def mocked_main(monkeypatch):
    """Patches login and the server (never a real Plex), and keeps colorama out of it."""
    section, _ = siamese_section()
    monkeypatch.setattr(ps, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(ps, "PlexServer", lambda url, token: make_plex(section))
    monkeypatch.setattr(plex_style, "_IS_WINDOWS", False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)


@pytest.mark.parametrize("extra, env, colored", [
    ([], {"FORCE_COLOR": "1"}, True),
    (["--no-color"], {"FORCE_COLOR": "1"}, False),
    ([], {"FORCE_COLOR": "1", "NO_COLOR": "1"}, False),
    ([], {"FORCE_COLOR": "1", "TERM": "dumb"}, True),  # FORCE_COLOR beats TERM=dumb
    ([], {"TERM": "dumb"}, False),
    ([], {}, False),  # captured stdout is not a terminal
])
def test_main_color_resolution(mocked_main, monkeypatch, capsys, extra, env, colored):
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert ps.main(URL_ARGS + extra) == 0

    out = capsys.readouterr().out
    assert ("\x1b[" in out) is colored
    assert "Dry run: 1 album(s)" in plex_style.strip(out)
    # Unicode borders only with color; ASCII borders otherwise.
    assert ("\u256d" in out) is colored
    assert ("+---" in out) is not colored


# ---------------------------------------------------------------------------
# Tables and questions (display only)
# ---------------------------------------------------------------------------

LONG_ROOT = "E:\\MP3\\Rock\\Alternative\\Smashing Pumpkins\\Studio Albums\\Deluxe Editions\\1993 - Siamese Dream"


def test_candidate_table_long_path_is_middle_truncated_to_width():
    candidate = candidate_with_roots([
        ps.AlbumRoot("a", LONG_ROOT, 13, [LONG_ROOT]),
        ps.AlbumRoot("b", "E:\\MP3\\Mellon", 28, ["E:\\MP3\\Mellon\\CD1", "E:\\MP3\\Mellon\\CD2"]),
    ])
    plex_style.reset(width=60)
    table = ps.candidate_table(candidate)
    rows = table_rows(table)
    folder_width = ps._folder_width(60)
    assert rows[0] == (ps.shorten_path(LONG_ROOT, folder_width, "..."), "13", "")
    assert rows[0][0].startswith("E:\\") and rows[0][0].endswith("\\1993 - Siamese Dream") and "\\...\\" in rows[0][0]
    assert len(rows[0][0]) <= folder_width
    assert rows[1] == ("E:\\MP3\\Mellon", "28", "2")
    rendered = plex_style.render(plex_style.indent(table))
    assert all(len(line) <= 60 for line in rendered.splitlines())
    assert candidate.roots[0].display == LONG_ROOT  # display data is never changed


def test_candidate_table_explicit_width_and_right_justified_counts():
    candidate = candidate_with_roots([ps.AlbumRoot("a", LONG_ROOT, 13, [LONG_ROOT])])
    table = ps.candidate_table(candidate, width=200)
    assert table_rows(table)[0][0] == LONG_ROOT
    assert [c.justify for c in table.columns] == ["left", "right", "right"]


def test_split_report_table_rows():
    table = ps.split_report_table([(100, "Siamese Dream", 2), (300, "Siamese Dream (Reissue)", 2)], 100)
    assert [c.header for c in table.columns] == ["ratingKey", "Title", "Tracks", "Note"]
    assert table_rows(table) == [
        ("100", "Siamese Dream", "2", "original"),
        ("300", "Siamese Dream (Reissue)", "2", ""),
    ]


def test_report_split_prints_table_in_original_first_order(capsys):
    section = make_section([], after_split={1: (300, "B"), 2: (100, "T"), 3: (200, "C")})
    assert ps.report_split(section, report_candidate([1, 2, 3])) == "split"
    out = capsys.readouterr().out
    keys = [line.split("|")[1].strip() for line in out.splitlines() if line.startswith("  | ") and "ratingKey" not in line]
    assert keys == ["100", "200", "300"]
    assert "| original |" in out


def test_split_question_constant():
    assert ps.SPLIT_QUESTION == 'Split "{title}" ({tracks} across {folders})?'


@pytest.mark.parametrize("color", [False, True])
def test_bracket_titles_render_literally(color, capsys):
    if color:
        plex_style.enable()
    candidate = ps.SplitCandidate(
        rating_key=7, title="Gish [Deluxe]", artist="[Live] Band", year=1991, track_count=2,
        roots=[ps.AlbumRoot("a", "E:\\[x]", 1, ["E:\\[x]"]), ps.AlbumRoot("b", "E:\\[y]", 1, ["E:\\[y]"])],
        obj=MagicMock(),
    )
    fake = scripted_input(["n"])

    ps.review_and_split([candidate], input_fn=fake, apply=True)

    out = plex_style.strip(capsys.readouterr().out)
    assert '[1/1] "Gish [Deluxe]" by [Live] Band  (ratingKey 7, 1991)' in out
    assert "E:\\[x]" in out and "E:\\[y]" in out
    assert plex_style.strip(fake.prompts[0]) == 'Split "Gish [Deluxe]" (2 tracks across 2 folders)? [y/N] '
