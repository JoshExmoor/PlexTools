import os
from unittest.mock import MagicMock

import pytest
import requests
from plexapi.exceptions import NotFound

import retrieve_playlist as rp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_track(title, file_paths):
    track = MagicMock()
    track.title = title
    parts = []
    for file_path in file_paths:
        part = MagicMock()
        part.file = file_path
        parts.append(part)
    track.iterParts.return_value = parts
    return track


def make_plex(tracks):
    playlist = MagicMock()
    playlist.items.return_value = tracks
    plex = MagicMock()
    plex.playlist.return_value = playlist
    return plex


# ---------------------------------------------------------------------------
# _default_save_path()
# ---------------------------------------------------------------------------

def test_default_save_path_uses_playlist_name_in_cwd():
    path = rp._default_save_path("My Playlist")
    assert path == os.path.join(os.getcwd(), "My Playlist.m3u")


def test_default_save_path_sanitizes_unsafe_characters():
    path = rp._default_save_path("My:Playlist/Name?")
    basename = os.path.basename(path)
    assert ":" not in basename
    assert "?" not in basename


def test_default_save_path_falls_back_when_name_is_empty():
    path = rp._default_save_path("")
    assert os.path.basename(path) == "playlist.m3u"


def test_default_save_path_replaces_unsafe_characters_rather_than_stripping():
    # Characters outside [alnum, space, ._-] become underscores, not removed outright.
    path = rp._default_save_path(":::")
    assert os.path.basename(path) == "___.m3u"


# ---------------------------------------------------------------------------
# retrieve_playlist()
# ---------------------------------------------------------------------------

def test_retrieve_playlist_writes_m3u_with_header_and_track_paths(tmp_path):
    tracks = [make_track("Song A", ["/music/a.mp3"]), make_track("Song B", ["/music/b.flac"])]
    plex = make_plex(tracks)
    save_path = tmp_path / "out.m3u"

    result_path = rp.retrieve_playlist(plex, "My Playlist", save_path=str(save_path))

    assert result_path == str(save_path)
    content = save_path.read_text()
    assert content.startswith("#EXTM3U\n")
    assert "/music/a.mp3" in content
    assert "/music/b.flac" in content
    assert "#EXTINF:-1,Song A" in content
    assert "#EXTINF:-1,Song B" in content


def test_retrieve_playlist_looks_up_playlist_by_name():
    tracks = [make_track("Song", ["/music/song.mp3"])]
    plex = make_plex(tracks)

    rp.retrieve_playlist(plex, "Road Trip", save_path="unused.m3u")

    plex.playlist.assert_called_once_with("Road Trip")
    os.remove("unused.m3u")


def test_retrieve_playlist_uses_default_path_when_not_given(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tracks = [make_track("Song", ["/music/song.mp3"])]
    plex = make_plex(tracks)

    result_path = rp.retrieve_playlist(plex, "Chill Vibes")

    assert result_path == os.path.join(str(tmp_path), "Chill Vibes.m3u")
    assert os.path.exists(result_path)


def test_retrieve_playlist_propagates_playlist_not_found():
    plex = MagicMock()
    plex.playlist.side_effect = NotFound('Unable to find playlist with title "Nope".')

    with pytest.raises(NotFound):
        rp.retrieve_playlist(plex, "Nope")


def test_retrieve_playlist_skips_tracks_with_no_file_path(tmp_path):
    tracks = [make_track("Good", ["/music/good.mp3"]), make_track("NoFile", [])]
    plex = make_plex(tracks)
    save_path = tmp_path / "mixed.m3u"

    rp.retrieve_playlist(plex, "Mixed", save_path=str(save_path))

    content = save_path.read_text()
    assert "good.mp3" in content
    assert "NoFile" not in content


def test_retrieve_playlist_skipped_tracks_printed_to_stderr(tmp_path, capsys):
    tracks = [make_track("NoFile", [])]
    plex = make_plex(tracks)
    save_path = tmp_path / "mixed.m3u"

    rp.retrieve_playlist(plex, "Mixed", save_path=str(save_path))

    captured = capsys.readouterr()
    assert "skipped" in captured.err


def test_retrieve_playlist_creates_parent_directories(tmp_path):
    tracks = [make_track("Song", ["/music/song.mp3"])]
    plex = make_plex(tracks)
    save_path = tmp_path / "nested" / "dir" / "list.m3u"

    rp.retrieve_playlist(plex, "Nested", save_path=str(save_path))

    assert save_path.exists()


def test_retrieve_playlist_handles_multi_part_tracks(tmp_path):
    tracks = [make_track("Multi", ["/music/disc1.mp3", "/music/disc2.mp3"])]
    plex = make_plex(tracks)
    save_path = tmp_path / "multi.m3u"

    rp.retrieve_playlist(plex, "Multi", save_path=str(save_path))

    content = save_path.read_text()
    assert "/music/disc1.mp3" in content
    assert "/music/disc2.mp3" in content


# ---------------------------------------------------------------------------
# CLI: parse_args()
# ---------------------------------------------------------------------------

def test_parse_args_requires_playlist():
    with pytest.raises(SystemExit):
        rp.parse_args(["--url", "http://localhost:32400"])


def test_parse_args_requires_url_unless_credential_file_given():
    with pytest.raises(SystemExit):
        rp.parse_args(["--playlist", "MyList"])


def test_parse_args_allows_credential_file_without_url():
    args = rp.parse_args(["--playlist", "MyList", "--credential-file", "creds.json"])
    assert args.credential_file == "creds.json"
    assert args.url is None


def test_parse_args_save_path_defaults_to_none():
    args = rp.parse_args(["--playlist", "MyList", "--url", "http://localhost:32400"])
    assert args.save_path is None


# ---------------------------------------------------------------------------
# CLI: main()
# ---------------------------------------------------------------------------

def test_main_success_returns_0(monkeypatch, tmp_path):
    tracks = [make_track("Song", ["/music/song.mp3"])]
    plex = make_plex(tracks)
    save_path = tmp_path / "out.m3u"

    monkeypatch.setattr(
        rp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(rp, "PlexServer", lambda url, token: plex)

    exit_code = rp.main(
        [
            "--playlist", "MyList",
            "--save-path", str(save_path),
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
        ]
    )

    assert exit_code == 0
    assert save_path.exists()


def test_main_skips_login_with_credential_file(monkeypatch, tmp_path):
    from get_plex_credentials import save_plex_credentials

    creds_path = tmp_path / "creds.json"
    save_plex_credentials({"url": "http://localhost:32400", "token": "cached-tok"}, str(creds_path))

    tracks = [make_track("Song", ["/music/song.mp3"])]
    plex = make_plex(tracks)
    save_path = tmp_path / "out.m3u"

    def fail_if_called(*args, **kwargs):
        pytest.fail("get_plex_credentials should not be called when --credential-file is given")

    monkeypatch.setattr(rp, "get_plex_credentials", fail_if_called)
    monkeypatch.setattr(rp, "PlexServer", lambda url, token: plex)

    exit_code = rp.main(
        ["--playlist", "MyList", "--save-path", str(save_path), "--credential-file", str(creds_path)]
    )

    assert exit_code == 0


def test_main_returns_1_on_playlist_not_found(monkeypatch):
    plex = MagicMock()
    plex.playlist.side_effect = NotFound('Unable to find playlist with title "Nope".')

    monkeypatch.setattr(
        rp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(rp, "PlexServer", lambda url, token: plex)

    exit_code = rp.main(
        [
            "--playlist", "Nope",
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
        ]
    )

    assert exit_code == 1


def test_main_returns_1_on_connection_error(monkeypatch):
    def fake_plex_server(url, token):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(
        rp, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(rp, "PlexServer", fake_plex_server)

    exit_code = rp.main(
        [
            "--playlist", "MyList",
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
        ]
    )

    assert exit_code == 1
