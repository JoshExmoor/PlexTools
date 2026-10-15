import csv
import io
import json
from unittest.mock import MagicMock

import pytest
import requests
from plexapi.exceptions import NotFound

import list_playlists as lp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_playlist(rating_key, title, playlist_type="audio", smart=False, items=10, tracks=None):
    pl = MagicMock()
    pl.ratingKey = rating_key
    pl.title = title
    pl.playlistType = playlist_type
    pl.smart = smart
    pl.leafCount = items
    pl.duration = items * 200000
    pl.addedAt = "2026-01-01 00:00:00"
    pl.updatedAt = "2026-01-02 00:00:00"
    pl.guid = f"guid-{rating_key}"
    pl.key = f"/playlist/{rating_key}"
    pl.items.return_value = tracks or []
    return pl


def make_track(file_path, title, rating_key=0):
    track = MagicMock()
    track.title = title
    track.ratingKey = rating_key
    if file_path is None:
        track.iterParts.return_value = []
    else:
        part = MagicMock()
        part.file = file_path
        track.iterParts.return_value = [part]
    return track


def make_plex(playlists):
    plex = MagicMock()
    plex.playlists.return_value = playlists
    return plex


# ---------------------------------------------------------------------------
# list_playlists() filters
# ---------------------------------------------------------------------------

def test_list_all_sorted_by_title():
    plex = make_plex([make_playlist(2, "Beta"), make_playlist(1, "alpha")])
    rows = lp.list_playlists(plex)
    assert [r["title"] for r in rows] == ["alpha", "Beta"]  # case-insensitive sort


def test_title_filter_is_case_insensitive():
    plex = make_plex([make_playlist(1, "zMB4Stars"), make_playlist(2, "zmb4stars"), make_playlist(3, "Other")])
    rows = lp.list_playlists(plex, title="ZMB4STARS")
    assert {r["ratingKey"] for r in rows} == {"1", "2"}


def test_title_contains_filter():
    plex = make_plex([make_playlist(1, "Summer Hits"), make_playlist(2, "Winter"), make_playlist(3, "hit list")])
    rows = lp.list_playlists(plex, title_contains="hit")
    assert {r["title"] for r in rows} == {"Summer Hits", "hit list"}


def test_type_filter():
    plex = make_plex([make_playlist(1, "A", playlist_type="audio"), make_playlist(2, "V", playlist_type="video")])
    rows = lp.list_playlists(plex, playlist_type="audio")
    assert [r["title"] for r in rows] == ["A"]


def test_smart_filter():
    plex = make_plex([make_playlist(1, "Reg", smart=False), make_playlist(2, "Smart", smart=True)])
    assert [r["title"] for r in lp.list_playlists(plex, smart=True)] == ["Smart"]
    assert [r["title"] for r in lp.list_playlists(plex, smart=False)] == ["Reg"]


# ---------------------------------------------------------------------------
# find_duplicate_titles() - case-insensitive
# ---------------------------------------------------------------------------

def test_find_duplicate_titles_case_insensitive():
    rows = [
        lp._playlist_metadata(make_playlist(1, "Dup")),
        lp._playlist_metadata(make_playlist(2, "dup")),
        lp._playlist_metadata(make_playlist(3, "Unique")),
    ]
    dupes = lp.find_duplicate_titles(rows)
    assert len(dupes) == 1
    (entries,) = dupes.values()
    assert {e["ratingKey"] for e in entries} == {"1", "2"}


def test_find_duplicate_titles_none_when_all_unique():
    rows = [lp._playlist_metadata(make_playlist(1, "A")), lp._playlist_metadata(make_playlist(2, "B"))]
    assert lp.find_duplicate_titles(rows) == {}


# ---------------------------------------------------------------------------
# diff_playlists()
# ---------------------------------------------------------------------------

def test_diff_playlists_reports_differences():
    tracks_a = [make_track("/m/a.mp3", "A"), make_track("/m/b.mp3", "B")]
    tracks_b = [make_track("/m/b.mp3", "B"), make_track("/m/c.mp3", "C")]
    pl_a = make_playlist(10, "List", tracks=tracks_a)
    pl_b = make_playlist(20, "List", tracks=tracks_b)
    plex = make_plex([pl_a, pl_b])

    result = lp.diff_playlists(plex, 10, 20)

    assert result["in_both"] == 1  # /m/b.mp3
    assert result["only_in_a"] == ["A"]
    assert result["only_in_b"] == ["C"]


def test_diff_playlists_uses_path_identity_not_title():
    # Same song title but different files -> treated as different tracks.
    tracks_a = [make_track("/m/x.mp3", "Song")]
    tracks_b = [make_track("/m/y.mp3", "Song")]
    plex = make_plex([make_playlist(1, "L", tracks=tracks_a), make_playlist(2, "L", tracks=tracks_b)])

    result = lp.diff_playlists(plex, 1, 2)

    assert result["in_both"] == 0
    assert result["only_in_a"] == ["Song"]
    assert result["only_in_b"] == ["Song"]


def test_diff_playlists_raises_for_unknown_rating_key():
    plex = make_plex([make_playlist(1, "L")])
    with pytest.raises(ValueError, match="No playlist"):
        lp.diff_playlists(plex, 1, 999)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def test_render_json_is_parseable():
    rows = [lp._playlist_metadata(make_playlist(1, "A"))]
    out = lp._render(rows, "json")
    assert json.loads(out)[0]["title"] == "A"


def test_render_csv_has_header_and_row():
    rows = [lp._playlist_metadata(make_playlist(1, "A"))]
    out = lp._render(rows, "csv")
    reader = list(csv.DictReader(io.StringIO(out)))
    assert reader[0]["title"] == "A"
    assert reader[0]["ratingKey"] == "1"


def test_render_table_includes_title_and_key():
    rows = [lp._playlist_metadata(make_playlist(5, "My List"))]
    out = lp._render(rows, "table")
    assert "My List" in out
    assert "ratingKey=5" in out


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

def test_parse_args_requires_url_or_credential_file():
    with pytest.raises(SystemExit):
        lp.parse_args([])


def test_parse_args_smart_flags_mutually_exclusive():
    with pytest.raises(SystemExit):
        lp.parse_args(["--url", "http://x:32400", "--smart", "--no-smart"])


def test_parse_args_defaults():
    args = lp.parse_args(["--url", "http://x:32400"])
    assert args.format == "table"
    assert args.smart is None
    assert args.duplicates_only is False
    assert args.diff is None


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def test_main_lists_and_warns_on_duplicates(monkeypatch, capsys):
    plex = make_plex([make_playlist(1, "Dup"), make_playlist(2, "Dup"), make_playlist(3, "Solo")])
    monkeypatch.setattr(lp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "t"})
    monkeypatch.setattr(lp, "PlexServer", lambda url, token: plex)

    exit_code = lp.main(["--url", "http://x:32400", "--username", "u", "--password", "p"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Dup" in captured.out
    assert "DUPLICATE TITLES DETECTED" in captured.err


def test_main_duplicates_only_filters_output(monkeypatch, capsys):
    plex = make_plex([make_playlist(1, "Dup"), make_playlist(2, "Dup"), make_playlist(3, "Solo")])
    monkeypatch.setattr(lp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "t"})
    monkeypatch.setattr(lp, "PlexServer", lambda url, token: plex)

    exit_code = lp.main(["--url", "http://x:32400", "-u", "u", "-p", "p", "--duplicates-only"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Dup" in out
    assert "Solo" not in out


def test_main_diff_mode(monkeypatch, capsys):
    tracks_a = [make_track("/m/a.mp3", "A")]
    tracks_b = [make_track("/m/a.mp3", "A"), make_track("/m/b.mp3", "B")]
    plex = make_plex([make_playlist(10, "L", tracks=tracks_a), make_playlist(20, "L", tracks=tracks_b)])
    monkeypatch.setattr(lp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "t"})
    monkeypatch.setattr(lp, "PlexServer", lambda url, token: plex)

    exit_code = lp.main(["--url", "http://x:32400", "-u", "u", "-p", "p", "--diff", "10", "20"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Only in B (1)" in out
    assert "B" in out


def test_main_returns_1_on_connection_error(monkeypatch):
    def boom(url, token):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(lp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "t"})
    monkeypatch.setattr(lp, "PlexServer", boom)

    exit_code = lp.main(["--url", "http://x:32400", "-u", "u", "-p", "p"])
    assert exit_code == 1
