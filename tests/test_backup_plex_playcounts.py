import json
from unittest.mock import MagicMock

import pytest
import requests
from plexapi.exceptions import NotFound
from plexapi.library import MusicSection

import backup_plex_playcounts as bp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_track(title, artist, album, rating, playcount, file_paths):
    track = MagicMock()
    track.title = title
    track.grandparentTitle = artist
    track.parentTitle = album
    track.userRating = rating
    track.viewCount = playcount
    parts = []
    for file_path in file_paths:
        part = MagicMock()
        part.file = file_path
        parts.append(part)
    track.iterParts.return_value = parts
    return track


def make_plex(tracks, as_music=True):
    section = MagicMock(spec=MusicSection) if as_music else MagicMock()
    section.searchTracks.return_value = tracks
    plex = MagicMock()
    plex.library.section.return_value = section
    return plex, section


# ---------------------------------------------------------------------------
# retrieve_plex_playcounts()
# ---------------------------------------------------------------------------

def test_retrieve_playcounts_returns_expected_dict_shape():
    tracks = [make_track("Song A", "Artist 1", "Album X", 8.0, 5, ["/music/a.mp3"])]
    plex, _ = make_plex(tracks)

    result = bp.retrieve_plex_playcounts(plex, section="Music")

    assert result == [
        {
            "path": "/music/a.mp3",
            "artist": "Artist 1",
            "album": "Album X",
            "title": "Song A",
            "rating": 8.0,
            "playcount": 5,
        }
    ]


def test_retrieve_playcounts_reads_requested_section():
    plex, _ = make_plex([])

    bp.retrieve_plex_playcounts(plex, section="My Music")

    plex.library.section.assert_called_once_with("My Music")


def test_retrieve_playcounts_filters_by_viewcount_gte_1():
    tracks = [make_track("Song", "A", "B", None, 1, ["/x.mp3"])]
    plex, section = make_plex(tracks)

    bp.retrieve_plex_playcounts(plex)

    section.searchTracks.assert_called_once_with(viewCount__gte=1)


def test_retrieve_playcounts_defaults_section_to_music():
    plex, _ = make_plex([])

    bp.retrieve_plex_playcounts(plex)

    plex.library.section.assert_called_once_with("Music")


def test_retrieve_playcounts_rejects_non_music_section():
    plex, _ = make_plex([], as_music=False)

    with pytest.raises(ValueError, match="is not a music library"):
        bp.retrieve_plex_playcounts(plex, section="Movies")


def test_retrieve_playcounts_handles_unrated_track():
    tracks = [make_track("Song", "A", "B", None, 3, ["/x.mp3"])]
    plex, _ = make_plex(tracks)

    result = bp.retrieve_plex_playcounts(plex)

    assert result[0]["rating"] is None
    assert result[0]["playcount"] == 3


def test_retrieve_playcounts_track_without_file_path_gets_null_path():
    tracks = [make_track("No File", "A", "B", 6.0, 2, [])]
    plex, _ = make_plex(tracks)

    result = bp.retrieve_plex_playcounts(plex)

    assert result[0]["path"] is None


def test_retrieve_playcounts_uses_first_part_for_multi_part_track():
    tracks = [make_track("Multi", "A", "B", 4.0, 1, ["/music/disc1.mp3", "/music/disc2.mp3"])]
    plex, _ = make_plex(tracks)

    result = bp.retrieve_plex_playcounts(plex)

    assert result[0]["path"] == "/music/disc1.mp3"


def test_retrieve_playcounts_empty_when_nothing_played():
    plex, _ = make_plex([])

    assert bp.retrieve_plex_playcounts(plex) == []


# ---------------------------------------------------------------------------
# CLI: parse_args()
# ---------------------------------------------------------------------------

def test_parse_args_requires_output():
    with pytest.raises(SystemExit):
        bp.parse_args(["--url", "http://localhost:32400"])


def test_parse_args_requires_url_unless_credential_file_given():
    with pytest.raises(SystemExit):
        bp.parse_args(["--output", "out.json"])


def test_parse_args_allows_credential_file_without_url():
    args = bp.parse_args(["--output", "out.json", "--credential-file", "creds.json"])
    assert args.credential_file == "creds.json"
    assert args.url is None


def test_parse_args_section_defaults_to_music():
    args = bp.parse_args(["--output", "out.json", "--url", "http://localhost:32400"])
    assert args.section == "Music"


def test_parse_args_short_flags():
    args = bp.parse_args(
        ["-o", "out.json", "-s", "Tunes", "-l", "http://x:32400", "-u", "joe", "-p", "pw", "-S", "c.json"]
    )
    assert args.output == "out.json"
    assert args.section == "Tunes"
    assert args.url == "http://x:32400"
    assert args.username == "joe"
    assert args.password == "pw"
    assert args.save_creds == "c.json"


# ---------------------------------------------------------------------------
# CLI: main()
# ---------------------------------------------------------------------------

def test_main_writes_json_and_returns_0(monkeypatch, tmp_path):
    tracks = [
        make_track("Song A", "Artist 1", "Album X", 8.0, 5, ["/music/a.mp3"]),
        make_track("Song B", "Artist 2", "Album Y", None, 1, ["/music/b.flac"]),
    ]
    plex, _ = make_plex(tracks)
    out = tmp_path / "playcounts.json"

    monkeypatch.setattr(
        bp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(bp, "PlexServer", lambda url, token: plex)

    exit_code = bp.main(
        ["-o", str(out), "-l", "http://localhost:32400", "-u", "joe", "-p", "pw"]
    )

    assert exit_code == 0
    data = json.loads(out.read_text())
    assert len(data) == 2
    assert data[0]["title"] == "Song A"
    assert data[1]["rating"] is None


def test_main_creates_parent_directories(monkeypatch, tmp_path):
    tracks = [make_track("Song", "A", "B", 4.0, 1, ["/x.mp3"])]
    plex, _ = make_plex(tracks)
    out = tmp_path / "nested" / "dir" / "playcounts.json"

    monkeypatch.setattr(
        bp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(bp, "PlexServer", lambda url, token: plex)

    exit_code = bp.main(["-o", str(out), "-l", "http://localhost:32400", "-u", "joe", "-p", "pw"])

    assert exit_code == 0
    assert out.exists()


def test_main_skips_login_with_credential_file(monkeypatch, tmp_path):
    from get_plex_credentials import save_plex_credentials

    creds_path = tmp_path / "creds.json"
    save_plex_credentials({"url": "http://localhost:32400", "token": "cached-tok"}, str(creds_path))

    tracks = [make_track("Song", "A", "B", 4.0, 1, ["/x.mp3"])]
    plex, _ = make_plex(tracks)
    out = tmp_path / "playcounts.json"

    def fail_if_called(*args, **kwargs):
        pytest.fail("get_plex_credentials should not be called when --credential-file is given")

    monkeypatch.setattr(bp, "get_plex_credentials", fail_if_called)
    monkeypatch.setattr(bp, "PlexServer", lambda url, token: plex)

    exit_code = bp.main(["-o", str(out), "-c", str(creds_path)])

    assert exit_code == 0
    assert out.exists()


def test_main_returns_1_on_section_not_found(monkeypatch, tmp_path):
    plex = MagicMock()
    plex.library.section.side_effect = NotFound('Section not found')
    out = tmp_path / "playcounts.json"

    monkeypatch.setattr(
        bp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(bp, "PlexServer", lambda url, token: plex)

    exit_code = bp.main(["-o", str(out), "-l", "http://localhost:32400", "-u", "joe", "-p", "pw"])

    assert exit_code == 1
    assert not out.exists()


def test_main_returns_1_on_connection_error(monkeypatch, tmp_path):
    out = tmp_path / "playcounts.json"

    def fake_plex_server(url, token):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(
        bp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(bp, "PlexServer", fake_plex_server)

    exit_code = bp.main(["-o", str(out), "-l", "http://localhost:32400", "-u", "joe", "-p", "pw"])

    assert exit_code == 1
