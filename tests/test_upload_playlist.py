from unittest.mock import MagicMock, patch

import pytest
import requests
from plexapi.exceptions import BadRequest, NotFound
from plexapi.library import MusicSection

import upload_playlist as up


# ---------------------------------------------------------------------------
# _default_music_section()
# ---------------------------------------------------------------------------

def test_default_music_section_returns_only_music_section():
    music_section = MagicMock(spec=MusicSection)
    plex = MagicMock()
    plex.library.sections.return_value = [music_section]

    assert up._default_music_section(plex) is music_section


def test_default_music_section_raises_when_none_found():
    plex = MagicMock()
    plex.library.sections.return_value = []

    with pytest.raises(ValueError, match="No music library section"):
        up._default_music_section(plex)


def test_default_music_section_raises_when_ambiguous():
    m1 = MagicMock(spec=MusicSection)
    m1.title = "Music A"
    m2 = MagicMock(spec=MusicSection)
    m2.title = "Music B"
    plex = MagicMock()
    plex.library.sections.return_value = [m1, m2]

    with pytest.raises(ValueError, match="Multiple music library sections"):
        up._default_music_section(plex)


def test_default_music_section_ignores_non_music_sections():
    music_section = MagicMock(spec=MusicSection)
    non_music = MagicMock()  # not spec'd as MusicSection
    plex = MagicMock()
    plex.library.sections.return_value = [non_music, music_section]

    assert up._default_music_section(plex) is music_section


# ---------------------------------------------------------------------------
# upload_playlist()
# ---------------------------------------------------------------------------

def test_upload_playlist_uses_default_section_when_not_specified():
    music_section = MagicMock(spec=MusicSection)
    music_section.title = "Music"
    plex = MagicMock()
    plex.library.sections.return_value = [music_section]

    fake_playlist = MagicMock()
    fake_playlist.items.return_value = [1, 2, 3]

    with patch.object(up.Playlist, "create", return_value=fake_playlist) as mock_create:
        result = up.upload_playlist(plex, "/mnt/music/list.m3u", "My Playlist")

    mock_create.assert_called_once_with(
        plex, title="My Playlist", section=music_section, m3ufilepath="/mnt/music/list.m3u"
    )
    assert result is fake_playlist


def test_upload_playlist_uses_explicit_section_when_given():
    music_section = MagicMock(spec=MusicSection)
    music_section.title = "Music"
    plex = MagicMock()
    plex.library.section.return_value = music_section

    fake_playlist = MagicMock()
    fake_playlist.items.return_value = []

    with patch.object(up.Playlist, "create", return_value=fake_playlist):
        up.upload_playlist(plex, "/mnt/music/list.m3u", "My Playlist", section="Music")

    plex.library.section.assert_called_once_with("Music")
    plex.library.sections.assert_not_called()


def test_upload_playlist_rejects_non_music_section():
    not_music = MagicMock()
    not_music.title = "Movies"
    plex = MagicMock()
    plex.library.section.return_value = not_music

    with pytest.raises(ValueError, match="is not a music library"):
        up.upload_playlist(plex, "/x.m3u", "Title", section="Movies")


def test_upload_playlist_wraps_bad_request_with_helpful_note():
    music_section = MagicMock(spec=MusicSection)
    music_section.title = "Music"
    plex = MagicMock()
    plex.library.sections.return_value = [music_section]

    with patch.object(up.Playlist, "create", side_effect=BadRequest("Failed to import m3u file.")):
        with pytest.raises(BadRequest, match="readable by the Plex server"):
            up.upload_playlist(plex, "/local/only/path.m3u", "Title")


# ---------------------------------------------------------------------------
# CLI: parse_args()
# ---------------------------------------------------------------------------

def test_parse_args_requires_m3u_path_and_title():
    with pytest.raises(SystemExit):
        up.parse_args(["--url", "http://localhost:32400"])


def test_parse_args_requires_url_unless_credential_file_given():
    with pytest.raises(SystemExit):
        up.parse_args(["--m3u-path", "x.m3u", "--title", "Test"])


def test_parse_args_allows_credential_file_without_url():
    args = up.parse_args(
        ["--m3u-path", "x.m3u", "--title", "Test", "--credential-file", "creds.json"]
    )
    assert args.credential_file == "creds.json"
    assert args.url is None


def test_parse_args_section_defaults_to_none():
    args = up.parse_args(
        ["--m3u-path", "x.m3u", "--title", "Test", "--url", "http://localhost:32400"]
    )
    assert args.section is None


# ---------------------------------------------------------------------------
# CLI: main()
# ---------------------------------------------------------------------------

def test_main_success_returns_0(monkeypatch):
    music_section = MagicMock(spec=MusicSection)
    music_section.title = "Music"
    plex = MagicMock()
    plex.library.sections.return_value = [music_section]

    fake_playlist = MagicMock()
    fake_playlist.items.return_value = [1]

    monkeypatch.setattr(
        up, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(up, "PlexServer", lambda url, token: plex)

    with patch.object(up.Playlist, "create", return_value=fake_playlist):
        exit_code = up.main(
            [
                "--m3u-path", "/mnt/music/list.m3u",
                "--title", "Test",
                "--url", "http://localhost:32400",
                "--username", "joe",
                "--password", "pw",
            ]
        )

    assert exit_code == 0


def test_main_skips_login_with_credential_file(monkeypatch, tmp_path):
    from get_plex_credentials import save_plex_credentials

    creds_path = tmp_path / "creds.json"
    save_plex_credentials({"url": "http://localhost:32400", "token": "cached-tok"}, str(creds_path))

    music_section = MagicMock(spec=MusicSection)
    music_section.title = "Music"
    plex = MagicMock()
    plex.library.sections.return_value = [music_section]
    fake_playlist = MagicMock()
    fake_playlist.items.return_value = []

    def fail_if_called(*args, **kwargs):
        pytest.fail("get_plex_credentials should not be called when --credential-file is given")

    monkeypatch.setattr(up, "get_plex_credentials", fail_if_called)
    monkeypatch.setattr(up, "PlexServer", lambda url, token: plex)

    with patch.object(up.Playlist, "create", return_value=fake_playlist):
        exit_code = up.main(
            ["--m3u-path", "/x.m3u", "--title", "Test", "--credential-file", str(creds_path)]
        )

    assert exit_code == 0


def test_main_returns_1_on_bad_request(monkeypatch):
    music_section = MagicMock(spec=MusicSection)
    music_section.title = "Music"
    plex = MagicMock()
    plex.library.sections.return_value = [music_section]

    monkeypatch.setattr(
        up, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(up, "PlexServer", lambda url, token: plex)

    with patch.object(up.Playlist, "create", side_effect=BadRequest("nope")):
        exit_code = up.main(
            [
                "--m3u-path", "/x.m3u",
                "--title", "Test",
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
        up, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(up, "PlexServer", fake_plex_server)

    exit_code = up.main(
        [
            "--m3u-path", "/x.m3u",
            "--title", "Test",
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
        ]
    )

    assert exit_code == 1
