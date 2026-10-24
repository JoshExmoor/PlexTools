from datetime import datetime
from unittest.mock import MagicMock

import pytest
import requests
from plexapi.exceptions import NotFound, PlexApiException
from plexapi.library import MusicSection

import plex_merge_tool as pm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_artist(title, rating_key):
    artist = MagicMock()
    artist.title = title
    artist.ratingKey = rating_key
    artist.merge = MagicMock()
    return artist


def make_album(
    title,
    artist_key,
    artist_title=None,
    rating_key=None,
    year=None,
    originally=None,
    leaf_count=None,
    paths=None,
):
    album = MagicMock()
    album.title = title
    album.parentRatingKey = artist_key
    album.parentTitle = artist_title
    album.ratingKey = rating_key
    album.year = year
    album.originallyAvailableAt = originally
    album.leafCount = leaf_count
    album.merge = MagicMock()
    tracks = []
    for path in paths or []:
        track = MagicMock()
        track.iterParts.return_value = [MagicMock(file=path)]
        tracks.append(track)
    album.tracks.return_value = tracks
    return album


def sp_album(title, rating_key, tracks=13, year=None, paths=None, artist="Smashing Pumpkins", artist_key=1):
    """A Smashing Pumpkins album mock with every attribute album mode reads."""
    if paths is None:
        paths = [f"E:\\MP3\\Smashing Pumpkins\\{title}\\{i:02}.mp3" for i in range(tracks)]
    return make_album(
        title, artist_key, artist_title=artist, rating_key=rating_key,
        year=year, leaf_count=tracks, paths=paths,
    )


def album_entry(title, key, artist="Smashing Pumpkins", artist_key=1, tracks=13, year=None, dirs=None):
    return pm.MergeEntry(
        MagicMock(), title, key,
        track_count=tracks, artist=artist, artist_key=artist_key, year=year, directories=dirs,
    )


def _paged(items):
    def fake_search(filters=None, container_start=0, container_size=None, maxresults=None):
        end = None if container_size is None else container_start + container_size
        return items[container_start:end]

    return fake_search


def make_plex(artists, albums, as_music=True):
    section = MagicMock(spec=MusicSection) if as_music else MagicMock()
    section.searchArtists.side_effect = _paged(artists)
    section.searchAlbums.side_effect = _paged(albums)
    plex = MagicMock()
    plex.library.section.return_value = section
    plex.library.sections.return_value = [section] if as_music else []
    return plex, section


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


def entry(title, key, albums=0):
    return pm.MergeEntry(MagicMock(), title, key, albums)


def pomeroy_plex():
    artists = [make_artist("Ken Pomeroy", 777239), make_artist("Ken Pomeroy", 858664)]
    albums = [make_album(f"A{i}", 777239) for i in range(5)] + [make_album(f"B{i}", 858664) for i in range(2)]
    return make_plex(artists, albums), artists


def fake_creds(url, username, password):
    return {"url": url, "token": "tok"}


URL_ARGS = ["-l", "http://localhost:32400", "-u", "joe", "-p", "pw"]


# ---------------------------------------------------------------------------
# normalize_name()
# ---------------------------------------------------------------------------

def test_normalize_drops_leading_the():
    assert pm.normalize_name("The Smashing Pumpkins") == pm.normalize_name("Smashing Pumpkins")
    assert pm.normalize_name("Smashing Pumpkins") == "smashing pumpkins"


def test_normalize_ampersand_and_accents():
    assert pm.normalize_name("Simon & Garfunkel") == pm.normalize_name("Simon and Garfunkel")
    assert pm.normalize_name("Beyoncé") == pm.normalize_name("BEYONCE")


def test_normalize_punctuation_and_whitespace():
    assert pm.normalize_name("  AC/DC ") == "ac dc"
    assert pm.normalize_name("Guns N' Roses") == "guns n roses"


def test_normalize_the_the_keeps_the():
    assert pm.normalize_name("The The") == "the"


def test_normalize_punctuation_only_names_stay_distinct():
    a, b = pm.normalize_name("!!!"), pm.normalize_name("???")
    assert a and b
    assert a != b


# ---------------------------------------------------------------------------
# group_candidates() / choose_target()
# ---------------------------------------------------------------------------

def test_group_exact_normalized_match():
    groups = pm.group_candidates([entry("The Smashing Pumpkins", 1), entry("Smashing Pumpkins", 2)])
    assert len(groups) == 1
    assert groups[0].match == "exact"
    assert {groups[0].target.rating_key} | {o.rating_key for o in groups[0].others} == {1, 2}


def test_group_fuzzy_near_match():
    entries = [entry("Smashing Pumpkins", 1), entry("Smashing Pumkins", 2)]
    groups = pm.group_candidates(entries)
    assert len(groups) == 1
    assert groups[0].match == "similar"


def test_group_no_fuzzy_skips_near_match():
    entries = [entry("Smashing Pumpkins", 1), entry("Smashing Pumkins", 2)]
    assert pm.group_candidates(entries, fuzzy=False) == []


@pytest.mark.parametrize("a, b", [("Prince", "Princess"), ("Bush", "Rush")])
def test_group_default_threshold_keeps_distinct_names_apart(a, b):
    assert pm.group_candidates([entry(a, 1), entry(b, 2)]) == []


def test_group_threshold_one_disables_near_matches():
    entries = [entry("Smashing Pumpkins", 1), entry("Smashing Pumkins", 2)]
    assert pm.group_candidates(entries, threshold=1.0) == []


def test_group_same_rating_key_is_not_a_candidate():
    assert pm.group_candidates([entry("Ken Pomeroy", 5), entry("Ken Pomeroy", 5)]) == []


def test_group_unrelated_names():
    assert pm.group_candidates([entry("Radiohead", 1), entry("Björk", 2), entry("Low", 3)]) == []


def test_group_three_entries_one_group():
    groups = pm.group_candidates([entry("Low", 1, 1), entry("Low", 2, 3), entry("LOW", 3, 2)])
    assert len(groups) == 1
    assert groups[0].target.rating_key == 2
    assert [o.rating_key for o in groups[0].others] == [3, 1]


def test_choose_target_most_albums_wins():
    target, others = pm.choose_target([entry("A", 1, 2), entry("A", 2, 5)])
    assert target.rating_key == 2
    assert [o.rating_key for o in others] == [1]


def test_choose_target_tie_goes_to_lower_rating_key():
    target, _ = pm.choose_target([entry("A", 9, 3), entry("A", 4, 3)])
    assert target.rating_key == 4


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def test_iter_artists_pages_through_all(monkeypatch):
    monkeypatch.setattr(pm, "_PAGE_SIZE", 200)
    artists = [make_artist(f"A{i}", i) for i in range(450)]
    _, section = make_plex(artists, [])

    got = list(pm.iter_artists(section))

    assert len(got) == 450
    offsets = [c.kwargs["container_start"] for c in section.searchArtists.call_args_list]
    assert offsets == [0, 200, 400]


def test_iter_artists_filters_and_no_libtype():
    _, section = make_plex([make_artist("Ken Pomeroy", 1)], [])

    list(pm.iter_artists(section, artist="pomeroy"))

    call = section.searchArtists.call_args_list[0]
    assert call.kwargs["filters"] == {"title__icontains": "pomeroy"}
    assert "libtype" not in call.kwargs


def test_iter_artists_no_filter_by_default():
    _, section = make_plex([make_artist("X", 1)], [])
    list(pm.iter_artists(section))
    assert section.searchArtists.call_args_list[0].kwargs["filters"] == {}


def test_count_albums_by_artist():
    albums = [make_album("a", 1), make_album("b", 1), make_album("c", 2), make_album("d", None)]
    _, section = make_plex([], albums)

    counts = pm.count_albums_by_artist(section, artist="ken")

    assert counts == {1: 2, 2: 1}
    call = section.searchAlbums.call_args_list[0]
    assert call.kwargs["filters"] == {"artist.title__icontains": "ken"}
    assert "libtype" not in call.kwargs


def test_scan_artists_skips_partial_artist():
    class PartialArtist:
        title = "No Key"

        def __getattr__(self, name):
            raise AttributeError(f"'Artist' object has no attribute '{name}'")

    artists = [make_artist("Ken Pomeroy", 1), PartialArtist()]
    _, section = make_plex(artists, [make_album("a", 1)])

    entries = pm.scan_artists(section)

    assert [(e.title, e.rating_key, e.album_count) for e in entries] == [("Ken Pomeroy", 1, 1)]


def test_find_artist_candidates_pomeroy():
    (plex, section), _ = pomeroy_plex()

    groups = pm.find_artist_candidates(section)

    assert len(groups) == 1
    assert groups[0].target.rating_key == 777239
    assert groups[0].target.album_count == 5
    assert [(o.rating_key, o.album_count) for o in groups[0].others] == [(858664, 2)]


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

def test_format_prompt_exact_wording():
    prompt = pm.format_prompt(entry("Ken Pomeroy", 777239, 5), entry("Ken Pomeroy", 858664, 2))
    assert prompt == "Would you like to merge Ken Pomeroy (5 albums) with Ken Pomeroy (2 albums)? "


@pytest.mark.parametrize("answer", ["y", "yes", "Y", " YES "])
def test_ask_yes_no_yes(answer):
    assert pm.ask_yes_no("? ", scripted_input([answer])) is True


@pytest.mark.parametrize("answer", ["n", "no", "", "NO"])
def test_ask_yes_no_no(answer):
    assert pm.ask_yes_no("? ", scripted_input([answer])) is False


def test_ask_yes_no_reprompts_on_invalid(capsys):
    fake = scripted_input(["maybe", "y"])
    assert pm.ask_yes_no("? ", fake) is True
    assert len(fake.prompts) == 2
    assert "Please answer y or n." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# review_and_merge()
# ---------------------------------------------------------------------------

def pomeroy_group():
    target, other = entry("Ken Pomeroy", 777239, 5), entry("Ken Pomeroy", 858664, 2)
    return pm.CandidateGroup(target, [other], "exact"), target, other


def test_review_yes_merges_into_target():
    group, target, other = pomeroy_group()

    summary = pm.review_and_merge([group], input_fn=scripted_input(["y"]))

    target.obj.merge.assert_called_once_with([858664])
    other.obj.merge.assert_not_called()
    assert (summary.merged, summary.skipped, summary.failed) == (1, 0, 0)


def test_review_no_skips():
    group, target, _ = pomeroy_group()

    summary = pm.review_and_merge([group], input_fn=scripted_input(["n"]))

    target.obj.merge.assert_not_called()
    assert (summary.merged, summary.skipped, summary.failed) == (0, 1, 0)


def test_review_three_member_group_pairwise():
    target, a, b = entry("Low", 1, 3), entry("Low", 2, 2), entry("Low", 3, 1)
    group = pm.CandidateGroup(target, [a, b], "exact")
    fake = scripted_input(["y", "n"])

    summary = pm.review_and_merge([group], input_fn=fake)

    target.obj.merge.assert_called_once_with([2])
    assert fake.prompts == [
        "Would you like to merge Low (3 albums) with Low (2 albums)? ",
        "Would you like to merge Low (5 albums) with Low (1 albums)? ",
    ]
    assert (summary.merged, summary.skipped) == (1, 1)


def test_review_merge_failure_continues(capsys):
    group1, target1, _ = pomeroy_group()
    target1.obj.merge.side_effect = PlexApiException("boom")
    target2, other2 = entry("Low", 1, 1), entry("Low", 2, 1)
    group2 = pm.CandidateGroup(target2, [other2], "exact")

    summary = pm.review_and_merge([group1, group2], input_fn=scripted_input(["y", "y"]))

    target2.obj.merge.assert_called_once_with([2])
    assert (summary.merged, summary.skipped, summary.failed) == (1, 0, 1)
    assert "Error: boom" in capsys.readouterr().err


def test_review_eof_stops_and_counts_remaining_skipped():
    group1, target1, _ = pomeroy_group()
    target2, other2 = entry("Low", 1, 1), entry("Low", 2, 1)
    group2 = pm.CandidateGroup(target2, [other2], "exact")

    summary = pm.review_and_merge([group1, group2], input_fn=scripted_input([EOFError()]))

    target1.obj.merge.assert_not_called()
    target2.obj.merge.assert_not_called()
    assert (summary.merged, summary.skipped, summary.failed) == (0, 2, 0)


def test_review_dry_run_never_prompts_or_merges(capsys):
    group, target, _ = pomeroy_group()

    def no_input(prompt=""):
        pytest.fail("input should not be called in dry-run mode")

    summary = pm.review_and_merge([group], input_fn=no_input, dry_run=True)

    target.obj.merge.assert_not_called()
    assert (summary.merged, summary.skipped, summary.failed) == (0, 0, 0)
    out = capsys.readouterr().out
    assert "Dry run: 1 merge(s) would be offered." in out
    assert "Keep ratingKey 777239" in out


def test_review_no_groups():
    summary = pm.review_and_merge([], input_fn=scripted_input([]))
    assert (summary.merged, summary.skipped, summary.failed) == (0, 0, 0)


# ---------------------------------------------------------------------------
# run_merge_tool()
# ---------------------------------------------------------------------------

def test_run_merge_tool_queues_full_scan_before_prompting():
    (plex, section), artists = pomeroy_plex()

    def checking_input(prompt=""):
        # Both scans must be complete and nothing merged when the first prompt arrives.
        assert section.searchArtists.call_count >= 1
        assert section.searchAlbums.call_count >= 1
        for artist in artists:
            artist.merge.assert_not_called()
        checking_input.calls += 1
        return "y"

    checking_input.calls = 0

    summary = pm.run_merge_tool(plex, input_fn=checking_input)

    assert checking_input.calls == 1
    artists[0].merge.assert_called_once_with([858664])
    assert summary.merged == 1


def test_run_merge_tool_albums_mode_runs():
    albums = [sp_album("Siamese Dream", 100), sp_album("Siamese Dream", 200)]
    plex, section = make_plex([], albums)

    summary = pm.run_merge_tool(plex, mode="albums", dry_run=True)

    assert (summary.merged, summary.skipped, summary.failed) == (0, 0, 0)
    assert section.searchAlbums.call_count >= 1
    section.searchArtists.assert_not_called()


def test_run_merge_tool_unknown_mode():
    plex = MagicMock()
    with pytest.raises(ValueError, match="Unknown merge mode"):
        pm.run_merge_tool(plex, mode="tracks")
    plex.library.section.assert_not_called()


def test_run_merge_tool_album_filter_rejected_in_artist_mode():
    plex = MagicMock()
    with pytest.raises(ValueError, match="--album is only supported"):
        pm.run_merge_tool(plex, mode="artists", album="x")
    plex.library.section.assert_not_called()
    plex.library.sections.assert_not_called()


def test_run_merge_tool_uses_named_section():
    (plex, _), _ = pomeroy_plex()

    pm.run_merge_tool(plex, section="Music", dry_run=True)

    plex.library.section.assert_called_once_with("Music")


# ---------------------------------------------------------------------------
# CLI: parse_args()
# ---------------------------------------------------------------------------

def test_parse_args_requires_url_unless_credential_file():
    with pytest.raises(SystemExit):
        pm.parse_args([])
    args = pm.parse_args(["-c", "creds.json"])
    assert args.credential_file == "creds.json"


def test_parse_args_defaults():
    args = pm.parse_args(["-l", "http://x:32400"])
    assert args.merge == "artists"
    assert args.threshold == 0.9
    assert args.no_fuzzy is False
    assert args.dry_run is False
    assert args.artist is None
    assert args.album is None
    assert args.section is None


@pytest.mark.parametrize("value", ["0", "1.5", "-0.2"])
def test_parse_args_rejects_bad_threshold(value):
    with pytest.raises(SystemExit):
        pm.parse_args(["-l", "http://x:32400", "--threshold", value])


def test_parse_args_rejects_album_with_artist_mode():
    with pytest.raises(SystemExit):
        pm.parse_args(["-l", "http://x:32400", "--album", "x"])


def test_parse_args_accepts_album_with_album_mode():
    args = pm.parse_args(["-l", "http://x:32400", "--merge", "albums", "--album", "x"])
    assert args.merge == "albums"
    assert args.album == "x"


# ---------------------------------------------------------------------------
# CLI: main()
# ---------------------------------------------------------------------------

def test_main_dry_run_returns_0(monkeypatch):
    (plex, _), artists = pomeroy_plex()
    monkeypatch.setattr(pm, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(pm, "PlexServer", lambda url, token: plex)

    def no_input(prompt=""):
        pytest.fail("input should not be called in dry-run mode")

    monkeypatch.setattr("builtins.input", no_input)

    assert pm.main(URL_ARGS + ["--dry-run"]) == 0
    for artist in artists:
        artist.merge.assert_not_called()


def test_main_interactive_yes_merges(monkeypatch):
    (plex, _), artists = pomeroy_plex()
    monkeypatch.setattr(pm, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(pm, "PlexServer", lambda url, token: plex)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    assert pm.main(URL_ARGS + ["--artist", "Pomeroy"]) == 0
    artists[0].merge.assert_called_once_with([858664])


def test_main_returns_1_when_merge_fails(monkeypatch):
    (plex, _), artists = pomeroy_plex()
    artists[0].merge.side_effect = requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(pm, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(pm, "PlexServer", lambda url, token: plex)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    assert pm.main(URL_ARGS) == 1


def test_main_returns_1_on_section_not_found(monkeypatch):
    plex = MagicMock()
    plex.library.section.side_effect = NotFound("Section not found")
    monkeypatch.setattr(pm, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(pm, "PlexServer", lambda url, token: plex)

    assert pm.main(URL_ARGS + ["--section", "Nope"]) == 1


def test_main_returns_1_on_connection_error(monkeypatch):
    def fake_plex_server(url, token):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(pm, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(pm, "PlexServer", fake_plex_server)

    assert pm.main(URL_ARGS) == 1


def test_main_skips_login_with_credential_file(monkeypatch, tmp_path):
    from get_plex_credentials import save_plex_credentials

    creds_path = tmp_path / "creds.json"
    save_plex_credentials({"url": "http://localhost:32400", "token": "cached-tok"}, str(creds_path))
    (plex, _), _ = pomeroy_plex()

    def fail_if_called(*args, **kwargs):
        pytest.fail("get_plex_credentials should not be called when --credential-file is given")

    monkeypatch.setattr(pm, "get_plex_credentials", fail_if_called)
    monkeypatch.setattr(pm, "PlexServer", lambda url, token: plex)

    assert pm.main(["-c", str(creds_path), "--dry-run"]) == 0


# ---------------------------------------------------------------------------
# Album mode: normalize_album_title()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a, b", [
    ("Siamese Dream", "SIAMESE DREAM"),
    ("Café Tacvba", "CAFE TACVBA"),
    ("Siamese   Dream ", " Siamese Dream"),
    ("Don't Look Back", "Don\u2019t Look Back"),
    ('"Heroes"', "\u201cHeroes\u201d"),
])
def test_normalize_album_title_light_equalities(a, b):
    assert pm.normalize_album_title(a) == pm.normalize_album_title(b)


@pytest.mark.parametrize("variant", [
    "Siamese Dream (Reissue)",
    "Siamese Dream (Deluxe)",
    "Siamese Dream [Live]",
    "Siamese Dream (Disc 2)",
    "Siamese Dream (Remastered)",
])
def test_normalize_album_title_keeps_qualifiers(variant):
    assert pm.normalize_album_title(variant) != pm.normalize_album_title("Siamese Dream")


def test_normalize_album_title_keeps_leading_the():
    assert pm.normalize_album_title("The Wall") != pm.normalize_album_title("Wall")


def test_normalize_album_title_empty():
    assert pm.normalize_album_title("") == ""
    assert pm.normalize_album_title(None) == ""


# ---------------------------------------------------------------------------
# Album mode: matching
# ---------------------------------------------------------------------------

def test_album_reissue_is_never_grouped():
    albums = [sp_album("Siamese Dream", 100), sp_album("Siamese Dream (Reissue)", 200)]
    _, section = make_plex([], albums)
    assert pm.find_album_candidates(section) == []


def test_album_reissue_not_grouped_even_with_loose_fuzzy(capsys):
    albums = [sp_album("Siamese Dream", 100), sp_album("Siamese Dream (Reissue)", 200)]
    plex, _ = make_plex([], albums)

    summary = pm.run_merge_tool(plex, mode="albums", threshold=0.5, fuzzy=True, dry_run=True)

    assert "Found 0 candidate group(s)." in capsys.readouterr().out
    assert (summary.merged, summary.skipped, summary.failed) == (0, 0, 0)


def test_album_case_whitespace_apostrophe_variants_grouped():
    albums = [sp_album("Don't Look Back", 100), sp_album("don\u2019t  look back", 200)]
    _, section = make_plex([], albums)

    groups = pm.find_album_candidates(section)

    assert len(groups) == 1
    assert {groups[0].target.rating_key, groups[0].others[0].rating_key} == {100, 200}


def test_album_same_title_different_artists_not_grouped():
    albums = [
        sp_album("Greatest Hits", 100, artist="Artist A", artist_key=1),
        sp_album("Greatest Hits", 200, artist="Artist B", artist_key=2),
    ]
    _, section = make_plex([], albums)
    assert pm.find_album_candidates(section) == []


def test_album_split_artist_grouped_with_note():
    albums = [
        sp_album("Siamese Dream", 100, artist="The Smashing Pumpkins", artist_key=777239),
        sp_album("Siamese Dream", 200, artist="Smashing Pumpkins", artist_key=858664),
    ]
    _, section = make_plex([], albums)

    groups = pm.find_album_candidates(section)

    assert len(groups) == 1
    lines = pm.describe_album_pair(groups[0].target, groups[0].others[0])
    assert any(
        "different artist entries (ratingKey 777239 vs 858664)" in line and "--merge artists" in line
        for line in lines
    )


def test_album_same_rating_key_not_a_candidate():
    albums = [sp_album("Siamese Dream", 100), sp_album("Siamese Dream", 100)]
    _, section = make_plex([], albums)
    assert pm.find_album_candidates(section) == []


def test_scan_albums_skips_partial_album():
    class PartialAlbum:
        title = "Siamese Dream"
        ratingKey = 300

        def __getattr__(self, name):
            raise AttributeError(f"'Album' object has no attribute '{name}'")

    albums = [sp_album("Siamese Dream", 100), PartialAlbum()]
    _, section = make_plex([], albums)

    entries = pm.scan_albums(section)

    assert [e.rating_key for e in entries] == [100]
    assert pm.find_album_candidates(section) == []


# ---------------------------------------------------------------------------
# Album mode: survivor
# ---------------------------------------------------------------------------

def test_album_survivor_most_tracks_wins():
    albums = [sp_album("Siamese Dream", 100, tracks=10), sp_album("Siamese Dream", 200, tracks=13)]
    _, section = make_plex([], albums)

    group = pm.find_album_candidates(section)[0]

    assert group.target.rating_key == 200
    assert [o.rating_key for o in group.others] == [100]


def test_album_survivor_tie_goes_to_lower_rating_key():
    albums = [sp_album("Siamese Dream", 200), sp_album("Siamese Dream", 100)]
    _, section = make_plex([], albums)
    assert pm.find_album_candidates(section)[0].target.rating_key == 100


def test_album_missing_leaf_count_filled_from_tracks():
    no_count = sp_album("Siamese Dream", 200, tracks=3)
    no_count.leafCount = None
    albums = [sp_album("Siamese Dream", 100, tracks=2), no_count]
    _, section = make_plex([], albums)

    group = pm.find_album_candidates(section)[0]

    assert group.target.rating_key == 200
    assert group.target.track_count == 3


# ---------------------------------------------------------------------------
# Album mode: prompt and notes
# ---------------------------------------------------------------------------

def test_album_prompt_exact_wording():
    target = album_entry(
        "Siamese Dream", 100, artist="The Smashing Pumpkins", tracks=13, year=1993, dirs=["E:\\A"],
    )
    other = album_entry("Siamese Dream", 200, artist="Smashing Pumpkins", tracks=13, year=2011, dirs=["E:\\B"])
    assert pm.format_album_prompt(target, other) == (
        "Would you like to merge Siamese Dream by The Smashing Pumpkins (1993, 13 tracks) "
        "with Siamese Dream by Smashing Pumpkins (2011, 13 tracks)? [different folders] "
    )


def test_album_year_falls_back_to_release_date_then_unknown():
    dated = make_album("A", 1, artist_title="X", rating_key=1, originally=datetime(2011, 5, 1), leaf_count=1)
    undated = make_album("A", 1, artist_title="X", rating_key=2, leaf_count=1)
    _, section = make_plex([], [dated, undated])

    entries = pm.scan_albums(section)

    assert entries[0].year == 2011
    assert entries[1].year is None
    prompt = pm.format_album_prompt(entries[0], entries[1])
    assert prompt == (
        "Would you like to merge A by X (2011, 1 track) with A by X (unknown year, 1 track)? [folders unknown] "
    )


def test_album_year_field_preferred_over_release_date():
    album = make_album("A", 1, artist_title="X", rating_key=1, year=1993, originally=datetime(2011, 5, 1))
    _, section = make_plex([], [album])
    assert pm.scan_albums(section)[0].year == 1993


@pytest.mark.parametrize("year_a, year_b, expected", [
    (1993, 2011, True),
    (1993, 1993, False),
    (1993, None, False),
    (None, None, False),
])
def test_album_years_differ_note(year_a, year_b, expected):
    lines = pm.describe_album_pair(
        album_entry("Siamese Dream", 100, year=year_a, dirs=["E:\\a"]),
        album_entry("Siamese Dream", 200, year=year_b, dirs=["E:\\a"]),
    )
    note = "  Note: release years differ (1993 vs 2011) - these may be different releases."
    assert (note in lines) is expected
    assert any("release years differ" in line for line in lines) is expected


def test_album_same_artist_entry_has_no_artist_note():
    lines = pm.describe_album_pair(
        album_entry("Siamese Dream", 100, dirs=["E:\\a"]),
        album_entry("Siamese Dream", 200, dirs=["E:\\a"]),
    )
    assert not any("different artist entries" in line for line in lines)


# ---------------------------------------------------------------------------
# Album mode: folders
# ---------------------------------------------------------------------------

def test_server_dirname_windows_and_posix():
    assert pm._server_dirname("E:\\MP3\\A\\01.mp3") == "E:\\MP3\\A"
    assert pm._server_dirname("/music/a/01.mp3") == "/music/a"


def test_album_folders_shown_when_different_windows_paths():
    albums = [
        sp_album("Siamese Dream", 100, tracks=1, paths=["E:\\MP3\\A\\01.mp3"]),
        sp_album("Siamese Dream", 200, tracks=1, paths=["E:\\MP3\\B\\01.mp3"]),
    ]
    _, section = make_plex([], albums)

    group = pm.find_album_candidates(section)[0]
    lines = pm.describe_album_pair(group.target, group.others[0])

    assert "  Keep folder: E:\\MP3\\A" in lines
    assert "  Merge-in folder: E:\\MP3\\B" in lines


def test_album_identical_folder_shows_same_folder_line_only():
    albums = [
        sp_album("Siamese Dream", 100, tracks=1, paths=["E:\\MP3\\A\\01.mp3"]),
        sp_album("Siamese Dream", 200, tracks=1, paths=["E:\\MP3\\A\\02.mp3"]),
    ]
    _, section = make_plex([], albums)

    group = pm.find_album_candidates(section)[0]
    lines = pm.describe_album_pair(group.target, group.others[0])

    assert lines[-1] == "  Folders: both albums are in the same folder: E:\\MP3\\A"
    assert not any("Keep folder" in line or "Merge-in folder" in line for line in lines)


def test_album_folders_posix_paths():
    albums = [
        sp_album("Siamese Dream", 100, tracks=1, paths=["/music/a/01.mp3"]),
        sp_album("Siamese Dream", 200, tracks=1, paths=["/music/b/01.mp3"]),
    ]
    _, section = make_plex([], albums)

    group = pm.find_album_candidates(section)[0]

    assert group.target.directories == ["/music/a"]
    assert group.others[0].directories == ["/music/b"]


def test_album_multi_folder_album_lists_every_folder():
    paths = ["E:\\MP3\\Ken Pomeroy\\Misc\\01.mp3", "E:\\MP3\\Ken Pomeroy\\2021 Christmas Lights In April\\02.mp3"]
    albums = [
        sp_album("Cruel Joke", 100, tracks=2, paths=paths),
        sp_album("Cruel Joke", 200, tracks=1, paths=["E:\\MP3\\Ken Pomeroy\\Misc\\01.mp3"]),
    ]
    _, section = make_plex([], albums)

    group = pm.find_album_candidates(section)[0]
    lines = pm.describe_album_pair(group.target, group.others[0])

    assert lines[-4:] == [
        "  Keep folders (2):",
        "    E:\\MP3\\Ken Pomeroy\\2021 Christmas Lights In April",
        "    E:\\MP3\\Ken Pomeroy\\Misc",
        "  Merge-in folder: E:\\MP3\\Ken Pomeroy\\Misc",
    ]


def test_album_folders_capped():
    dirs = [f"E:\\MP3\\D{i}" for i in range(7)]
    lines = pm.describe_album_pair(
        album_entry("A", 100, dirs=dirs),
        album_entry("A", 200, dirs=["E:\\MP3\\Other"]),
    )
    assert "  Keep folders (7):" in lines
    assert "    E:\\MP3\\D4" in lines
    assert "    E:\\MP3\\D5" not in lines
    assert "    ... and 2 more" in lines


def test_album_tracks_error_warns_and_review_continues(capsys):
    broken = sp_album("Siamese Dream", 200, tracks=1)
    broken.tracks.side_effect = PlexApiException("nope")
    albums = [sp_album("Siamese Dream", 100, tracks=1), broken]
    plex, _ = make_plex([], albums)

    summary = pm.run_merge_tool(plex, mode="albums", input_fn=scripted_input(["n"]))

    captured = capsys.readouterr()
    assert 'Warning: could not read tracks for "Siamese Dream": nope' in captured.err
    assert "  Merge-in folder: unknown (tracks could not be read)" in captured.out
    assert summary.skipped == 1


@pytest.mark.parametrize("error", [ValueError("bad xml"), AttributeError("no file"), TypeError("bad path")])
def test_album_unexpected_tracks_error_does_not_abort(capsys, error):
    broken = sp_album("Siamese Dream", 200, tracks=1)
    broken.tracks.side_effect = error
    albums = [
        sp_album("Siamese Dream", 100, tracks=1),
        broken,
        sp_album("Gish", 300, tracks=1),
        sp_album("Gish", 400, tracks=1),
    ]
    plex, _ = make_plex([], albums)
    fake = scripted_input(["y", "y"])

    summary = pm.run_merge_tool(plex, mode="albums", input_fn=fake)

    captured = capsys.readouterr()
    assert f'Warning: could not read tracks for "Siamese Dream": {error}' in captured.err
    assert "  Merge-in folder: unknown (tracks could not be read)" in captured.out
    assert len(fake.prompts) == 2
    albums[0].merge.assert_called_once_with([200])
    albums[2].merge.assert_called_once_with([400])
    assert (summary.merged, summary.skipped, summary.failed) == (2, 0, 0)


def test_album_bad_part_path_warns_and_continues(capsys):
    broken = sp_album("Siamese Dream", 200, tracks=1)
    broken.tracks.return_value[0].iterParts.return_value = [MagicMock(file=12345)]
    albums = [sp_album("Siamese Dream", 100, tracks=1), broken]
    _, section = make_plex([], albums)

    group = pm.find_album_candidates(section)[0]

    assert 'Warning: could not read tracks for "Siamese Dream"' in capsys.readouterr().err
    assert group.others[0].directories is None
    assert group.target.directories == ["E:\\MP3\\Smashing Pumpkins\\Siamese Dream"]


def test_album_part_missing_file_attribute_is_skipped():
    class PartialPart:
        pass

    album = sp_album("Siamese Dream", 100, tracks=1)
    album.tracks.return_value[0].iterParts.return_value = [PartialPart()]

    assert pm.album_directories(album) == ([], 1)


def test_album_keyboard_interrupt_reading_tracks_propagates():
    # Ctrl+C during the scan (before any prompt) stops the run, as it does
    # anywhere else in the scan; only Exception is tolerated per album.
    broken = sp_album("Siamese Dream", 200, tracks=1)
    broken.tracks.side_effect = KeyboardInterrupt
    albums = [sp_album("Siamese Dream", 100, tracks=1), broken]
    plex, _ = make_plex([], albums)
    fake = scripted_input([])

    with pytest.raises(KeyboardInterrupt):
        pm.run_merge_tool(plex, mode="albums", input_fn=fake)
    assert fake.prompts == []
    albums[0].merge.assert_not_called()


@pytest.mark.parametrize("other_path", ["e:\\mp3\\foo\\01.mp3", "E:/MP3/Foo/01.mp3"])
def test_album_windows_folders_differing_by_case_or_separator_match(other_path):
    albums = [
        sp_album("Siamese Dream", 100, tracks=1, paths=["E:\\MP3\\Foo\\01.mp3"]),
        sp_album("Siamese Dream", 200, tracks=1, paths=[other_path]),
    ]
    _, section = make_plex([], albums)

    group = pm.find_album_candidates(section)[0]
    lines = pm.describe_album_pair(group.target, group.others[0])

    assert lines[-1] == "  Folders: both albums are in the same folder: E:\\MP3\\Foo"
    assert pm.folder_status(group.target, group.others[0]) == pm.SAME_FOLDER


def test_album_windows_trailing_separator_ignored():
    lines = pm.describe_album_pair(
        album_entry("A", 100, dirs=["E:\\MP3\\Foo\\"]),
        album_entry("A", 200, dirs=["e:/mp3/foo"]),
    )
    assert lines[-1] == "  Folders: both albums are in the same folder: E:\\MP3\\Foo\\"


def test_album_different_windows_folders_show_original_strings():
    albums = [
        sp_album("Siamese Dream", 100, tracks=1, paths=["E:\\MP3\\Foo\\01.mp3"]),
        sp_album("Siamese Dream", 200, tracks=1, paths=["e:/mp3/Bar/01.mp3"]),
    ]
    _, section = make_plex([], albums)

    group = pm.find_album_candidates(section)[0]
    lines = pm.describe_album_pair(group.target, group.others[0])

    assert lines[-2:] == [
        "  Keep folder: E:\\MP3\\Foo",
        "  Merge-in folder: e:/mp3/Bar",
    ]


def test_album_dedupes_case_variant_folders_keeping_first_seen():
    album = sp_album(
        "Siamese Dream", 100, tracks=2,
        paths=["E:\\MP3\\Ken Pomeroy\\Misc\\01.mp3", "e:\\mp3\\ken pomeroy\\misc\\02.mp3"],
    )

    assert pm.album_directories(album) == (["E:\\MP3\\Ken Pomeroy\\Misc"], 2)


def test_album_posix_folders_differing_by_case_stay_different():
    albums = [
        sp_album("Siamese Dream", 100, tracks=1, paths=["/music/Foo/01.mp3"]),
        sp_album("Siamese Dream", 200, tracks=1, paths=["/music/foo/01.mp3"]),
    ]
    _, section = make_plex([], albums)

    group = pm.find_album_candidates(section)[0]
    lines = pm.describe_album_pair(group.target, group.others[0])

    assert lines[-2:] == ["  Keep folder: /music/Foo", "  Merge-in folder: /music/foo"]


def test_album_absorb_does_not_create_case_duplicate_folders():
    target = album_entry("Low", 1, artist="X", tracks=5, dirs=["E:\\MP3\\Low"])
    other = album_entry("Low", 2, artist="X", tracks=3, dirs=["e:/mp3/low", "E:\\MP3\\Low Extras"])

    pm._absorb_album(target, other)

    assert target.directories == ["E:\\MP3\\Low", "E:\\MP3\\Low Extras"]
    assert target.track_count == 8


def test_album_tracks_only_fetched_for_candidates_after_scan(monkeypatch):
    monkeypatch.setattr(pm, "_PAGE_SIZE", 2)
    a = sp_album("Siamese Dream", 100, tracks=1)
    b = sp_album("Siamese Dream", 200, tracks=1)
    others = [sp_album(f"Other {i}", 300 + i, tracks=1) for i in range(3)]
    _, section = make_plex([], [a, others[0], others[1], b, others[2]])

    def checking_tracks():
        # 5 albums, page size 2: pages of 2, 2, 1 must all be served first.
        assert section.searchAlbums.call_count == 3
        return []

    a.tracks.side_effect = checking_tracks
    b.tracks.side_effect = checking_tracks

    groups = pm.find_album_candidates(section)

    assert len(groups) == 1
    assert a.tracks.call_count == 1
    assert b.tracks.call_count == 1
    for album in others:
        album.tracks.assert_not_called()


# ---------------------------------------------------------------------------
# Album mode: review
# ---------------------------------------------------------------------------

def test_album_review_yes_merges_into_survivor():
    albums = [sp_album("Siamese Dream", 100, tracks=13), sp_album("Siamese Dream", 200, tracks=10)]
    plex, _ = make_plex([], albums)

    summary = pm.run_merge_tool(plex, mode="albums", input_fn=scripted_input(["y"]))

    albums[0].merge.assert_called_once_with([200])
    albums[1].merge.assert_not_called()
    assert (summary.merged, summary.skipped, summary.failed) == (1, 0, 0)


def test_album_review_no_skips():
    albums = [sp_album("Siamese Dream", 100), sp_album("Siamese Dream", 200)]
    plex, _ = make_plex([], albums)

    summary = pm.run_merge_tool(plex, mode="albums", input_fn=scripted_input(["n"]))

    albums[0].merge.assert_not_called()
    assert (summary.merged, summary.skipped, summary.failed) == (0, 1, 0)


def test_album_review_three_member_group_updates_track_count():
    target = album_entry("Low", 1, artist="X", tracks=5, year=1990, dirs=["E:\\a"])
    a = album_entry("Low", 2, artist="X", tracks=3, year=1990, dirs=["E:\\b"])
    b = album_entry("Low", 3, artist="X", tracks=1, year=1990, dirs=["E:\\b"])
    group = pm.CandidateGroup(target, [a, b], "exact")
    fake = scripted_input(["y", "n"])

    summary = pm.review_and_merge([group], input_fn=fake, mode=pm.ALBUM_MODE)

    target.obj.merge.assert_called_once_with([2])
    assert fake.prompts == [
        "Would you like to merge Low by X (1990, 5 tracks) with Low by X (1990, 3 tracks)? [different folders] ",
        "Would you like to merge Low by X (1990, 8 tracks) with Low by X (1990, 1 track)? [different folders] ",
    ]
    assert target.directories == ["E:\\a", "E:\\b"]
    assert (summary.merged, summary.skipped) == (1, 1)


def test_album_dry_run_shows_year_and_folders(capsys):
    albums = [
        sp_album("Siamese Dream", 100, year=1993, tracks=1, paths=["E:\\MP3\\A\\01.mp3"]),
        sp_album("Siamese Dream", 200, year=2011, tracks=1, paths=["E:\\MP3\\B\\01.mp3"]),
    ]
    plex, _ = make_plex([], albums)

    def no_input(prompt=""):
        pytest.fail("input should not be called in dry-run mode")

    pm.run_merge_tool(plex, mode="albums", dry_run=True, input_fn=no_input)

    for album in albums:
        album.merge.assert_not_called()
    out = capsys.readouterr().out
    assert "(1993, 1 track)" in out
    assert "(2011, 1 track)" in out
    assert "release years differ (1993 vs 2011)" in out
    assert "  Note: these albums are in different folders" in out
    assert "  Keep folder: E:\\MP3\\A" in out
    assert "  Merge-in folder: E:\\MP3\\B" in out
    assert "Dry run: 1 merge(s) would be offered." in out


def test_album_dry_run_shows_same_folder_status(capsys):
    albums = [
        sp_album("Siamese Dream", 100, tracks=1, paths=["E:\\MP3\\A\\01.mp3"]),
        sp_album("Siamese Dream", 200, tracks=1, paths=["E:\\MP3\\A\\02.mp3"]),
    ]
    plex, _ = make_plex([], albums)

    def no_input(prompt=""):
        pytest.fail("input should not be called in dry-run mode")

    pm.run_merge_tool(plex, mode="albums", dry_run=True, input_fn=no_input)

    out = capsys.readouterr().out
    assert "  Folders: both albums are in the same folder: E:\\MP3\\A" in out
    assert "Keep folder" not in out


# ---------------------------------------------------------------------------
# Album mode: folder status and prompt tag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dirs_a, dirs_b, expected", [
    (["E:\\A"], ["E:\\A"], pm.SAME_FOLDER),
    (["E:\\A\\CD1", "E:\\A\\CD2"], ["E:\\A\\CD1", "E:\\A\\CD2"], pm.SAME_FOLDERS),
    (["E:\\A"], ["E:\\B"], pm.DIFFERENT_FOLDERS),
    (["E:\\A", "E:\\B"], ["E:\\A"], pm.DIFFERENT_FOLDERS),
    (["E:\\A"], None, pm.FOLDERS_UNKNOWN),
    (None, ["E:\\A"], pm.FOLDERS_UNKNOWN),
    ([], ["E:\\A"], pm.FOLDERS_UNKNOWN),
    (["E:\\A"], [], pm.FOLDERS_UNKNOWN),
    (["E:\\MP3\\Foo"], ["e:/mp3/foo"], pm.SAME_FOLDER),
    (["E:\\MP3\\Foo\\CD1", "E:\\MP3\\Foo\\CD2"], ["e:/mp3/foo/cd2", "e:\\MP3\\FOO\\cd1"], pm.SAME_FOLDERS),
    (["/music/Foo"], ["/music/foo"], pm.DIFFERENT_FOLDERS),
    (["/music/Foo"], ["/music/Foo"], pm.SAME_FOLDER),
])
def test_folder_status(dirs_a, dirs_b, expected):
    status = pm.folder_status(album_entry("A", 1, dirs=dirs_a), album_entry("A", 2, dirs=dirs_b))
    assert status == expected


def test_describe_same_folders_lists_target_folders_without_keep_lists():
    lines = pm.describe_album_pair(
        album_entry("A", 1, dirs=["E:\\A\\CD1", "E:\\A\\CD2"]),
        album_entry("A", 2, dirs=["e:/a/cd1", "e:/a/cd2"]),
    )
    assert lines[-3:] == [
        "  Folders: both albums use the same 2 folders:",
        "    E:\\A\\CD1",
        "    E:\\A\\CD2",
    ]
    assert not any("Keep folder" in line or "Merge-in folder" in line for line in lines)


def test_describe_same_folders_capped():
    dirs = [f"E:\\A\\D{i}" for i in range(7)]
    lines = pm.describe_album_pair(album_entry("A", 1, dirs=dirs), album_entry("A", 2, dirs=list(dirs)))
    assert "  Folders: both albums use the same 7 folders:" in lines
    assert "    E:\\A\\D4" in lines
    assert "    E:\\A\\D5" not in lines
    assert lines[-1] == "    ... and 2 more"


def test_describe_different_folders_note_and_lists():
    lines = pm.describe_album_pair(
        album_entry("A", 1, dirs=["E:\\A", "E:\\B"]),
        album_entry("A", 2, dirs=["E:\\A"]),
    )
    assert lines[-5:] == [
        "  Note: these albums are in different folders",
        "  Keep folders (2):",
        "    E:\\A",
        "    E:\\B",
        "  Merge-in folder: E:\\A",
    ]


@pytest.mark.parametrize("dirs_b, merge_in_line", [
    (None, "  Merge-in folder: unknown (tracks could not be read)"),
    ([], "  Merge-in folder: unknown (no track files found)"),
])
def test_describe_unknown_folders_note_and_lists(dirs_b, merge_in_line):
    lines = pm.describe_album_pair(album_entry("A", 1, dirs=["E:\\A"]), album_entry("A", 2, dirs=dirs_b))
    assert lines[-3:] == [
        "  Note: could not confirm whether these albums share a folder",
        "  Keep folder: E:\\A",
        merge_in_line,
    ]


@pytest.mark.parametrize("dirs_a, dirs_b, tag", [
    (["E:\\A"], ["e:/a"], "[same folder]"),
    (["E:\\A\\CD1", "E:\\A\\CD2"], ["E:\\A\\CD1", "E:\\A\\CD2"], "[same folders]"),
    (["E:\\A"], ["E:\\B"], "[different folders]"),
    (["E:\\A"], None, "[folders unknown]"),
    (["E:\\A"], [], "[folders unknown]"),
])
def test_album_prompt_ends_with_folder_tag(dirs_a, dirs_b, tag):
    prompt = pm.format_album_prompt(
        album_entry("A", 1, artist="X", tracks=2, year=1993, dirs=dirs_a),
        album_entry("A", 2, artist="X", tracks=2, year=1993, dirs=dirs_b),
    )
    assert prompt == f"Would you like to merge A by X (1993, 2 tracks) with A by X (1993, 2 tracks)? {tag} "


def test_album_three_member_group_tag_reflects_absorbed_folders():
    target = album_entry("Low", 1, artist="X", tracks=5, year=1990, dirs=["E:\\a"])
    a = album_entry("Low", 2, artist="X", tracks=3, year=1990, dirs=["E:\\b"])
    b = album_entry("Low", 3, artist="X", tracks=1, year=1990, dirs=["E:\\a", "E:\\b"])
    group = pm.CandidateGroup(target, [a, b], "exact")
    fake = scripted_input(["y", "n"])

    pm.review_and_merge([group], input_fn=fake, mode=pm.ALBUM_MODE)

    assert fake.prompts[0].endswith("? [different folders] ")
    assert fake.prompts[1].endswith("? [same folders] ")


# ---------------------------------------------------------------------------
# Album mode: filters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, expected", [
    ({"album": "dream"}, {"title__icontains": "dream"}),
    ({"artist": "pumpkins"}, {"artist.title__icontains": "pumpkins"}),
    (
        {"album": "dream", "artist": "pumpkins"},
        {"title__icontains": "dream", "artist.title__icontains": "pumpkins"},
    ),
    ({}, {}),
])
def test_iter_albums_filters_and_no_libtype(kwargs, expected):
    _, section = make_plex([], [sp_album("Siamese Dream", 100)])

    list(pm.iter_albums(section, **kwargs))

    call = section.searchAlbums.call_args_list[0]
    assert call.kwargs["filters"] == expected
    assert "libtype" not in call.kwargs


# ---------------------------------------------------------------------------
# Album mode: CLI
# ---------------------------------------------------------------------------

def test_parse_args_album_mode_with_artist_and_album():
    args = pm.parse_args(["-l", "http://x:32400", "--merge", "albums", "--artist", "x", "--album", "y"])
    assert (args.merge, args.artist, args.album) == ("albums", "x", "y")


def album_plex():
    albums = [sp_album("Siamese Dream", 100, tracks=13), sp_album("Siamese Dream", 200, tracks=10)]
    return make_plex([], albums), albums


def test_main_albums_dry_run_returns_0(monkeypatch):
    (plex, _), albums = album_plex()
    monkeypatch.setattr(pm, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(pm, "PlexServer", lambda url, token: plex)

    def no_input(prompt=""):
        pytest.fail("input should not be called in dry-run mode")

    monkeypatch.setattr("builtins.input", no_input)

    assert pm.main(URL_ARGS + ["--merge", "albums", "--dry-run"]) == 0
    for album in albums:
        album.merge.assert_not_called()


def test_main_albums_interactive_yes_merges(monkeypatch):
    (plex, section), albums = album_plex()
    monkeypatch.setattr(pm, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(pm, "PlexServer", lambda url, token: plex)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    assert pm.main(URL_ARGS + ["--merge", "albums", "--album", "Siamese"]) == 0
    albums[0].merge.assert_called_once_with([200])
    assert section.searchAlbums.call_args_list[0].kwargs["filters"] == {"title__icontains": "Siamese"}


def test_main_albums_returns_1_when_merge_fails(monkeypatch):
    (plex, _), albums = album_plex()
    albums[0].merge.side_effect = requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(pm, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(pm, "PlexServer", lambda url, token: plex)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    assert pm.main(URL_ARGS + ["--merge", "albums"]) == 1


def test_main_albums_returns_1_on_section_not_found(monkeypatch):
    plex = MagicMock()
    plex.library.section.side_effect = NotFound("Section not found")
    monkeypatch.setattr(pm, "get_plex_credentials", fake_creds)
    monkeypatch.setattr(pm, "PlexServer", lambda url, token: plex)

    assert pm.main(URL_ARGS + ["--merge", "albums", "--section", "Nope"]) == 1
