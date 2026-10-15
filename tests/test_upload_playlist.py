from unittest.mock import MagicMock, patch

import pytest
import requests
from plexapi.exceptions import BadRequest, NotFound
from plexapi.library import MusicSection

import upload_playlist as up


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_track(file_paths):
    track = MagicMock()
    parts = []
    for file_path in file_paths:
        part = MagicMock()
        part.file = file_path
        parts.append(part)
    track.iterParts.return_value = parts
    return track


def make_music_section(section_tracks, title="Music"):
    """A mocked MusicSection whose searchTracks returns the given tracks.

    searchTracks is used both by detect_path_style and reconcile_playlist, so a
    single return value standing in for 'all tracks in the section' is fine.
    """
    section = MagicMock(spec=MusicSection)
    section.title = title
    section.searchTracks.return_value = section_tracks
    return section


def write_m3u(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


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
# correct_path_for_server()
# ---------------------------------------------------------------------------

def test_correct_path_windows_uppercases_drive_and_uses_backslashes():
    assert up.correct_path_for_server("e:\\Music\\a.mp3", "windows") == "E:\\Music\\a.mp3"
    assert up.correct_path_for_server("e:/Music/a.mp3", "windows") == "E:\\Music\\a.mp3"


def test_correct_path_posix_preserves_case_and_slashes():
    assert up.correct_path_for_server("/music/A.mp3", "posix") == "/music/A.mp3"
    assert up.correct_path_for_server("/Music/song.mp3", "posix") == "/Music/song.mp3"


# ---------------------------------------------------------------------------
# normalize_path_for_compare()
# ---------------------------------------------------------------------------

def test_normalize_windows_is_case_insensitive_and_separator_agnostic():
    a = up.normalize_path_for_compare("E:\\Music\\A.mp3", "windows")
    b = up.normalize_path_for_compare("e:/music/a.mp3", "windows")
    assert a == b


def test_normalize_posix_is_case_sensitive():
    a = up.normalize_path_for_compare("/music/A.mp3", "posix")
    b = up.normalize_path_for_compare("/music/a.mp3", "posix")
    assert a != b


# ---------------------------------------------------------------------------
# detect_path_style()
# ---------------------------------------------------------------------------

def test_detect_path_style_windows():
    section = make_music_section(
        [make_track(["E:\\Music\\a.mp3"]), make_track(["E:\\Music\\b.mp3"])]
    )
    assert up.detect_path_style(section) == "windows"


def test_detect_path_style_posix():
    section = make_music_section(
        [make_track(["/music/a.mp3"]), make_track(["/music/b.mp3"])]
    )
    assert up.detect_path_style(section) == "posix"


def test_detect_path_style_falls_back_to_posix_when_no_samples():
    section = make_music_section([])
    assert up.detect_path_style(section) == "posix"


# ---------------------------------------------------------------------------
# write_normalized_m3u()
# ---------------------------------------------------------------------------

def test_write_normalized_m3u_corrects_paths_and_preserves_directives(tmp_path):
    src = write_m3u(
        tmp_path / "list.m3u",
        ["#EXTM3U", "#EXTINF:-1,Song A", "e:\\Music\\a.mp3", "e:/Music/b.mp3"],
    )
    dst = tmp_path / ".normalized_list.m3u"

    paths = up.write_normalized_m3u(src, str(dst), "windows")

    assert paths == ["E:\\Music\\a.mp3", "E:\\Music\\b.mp3"]
    content = dst.read_text(encoding="utf-8")
    assert "#EXTM3U" in content
    assert "#EXTINF:-1,Song A" in content
    assert "E:\\Music\\a.mp3" in content


def test_write_normalized_m3u_posix_leaves_paths_untouched(tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["#EXTM3U", "/music/A.mp3", "/music/b.mp3"])
    dst = tmp_path / ".normalized_list.m3u"

    paths = up.write_normalized_m3u(src, str(dst), "posix")

    assert paths == ["/music/A.mp3", "/music/b.mp3"]


# ---------------------------------------------------------------------------
# reconcile_playlist()
# ---------------------------------------------------------------------------

def test_reconcile_adds_missing_tracks_and_reports(tmp_path):
    t_a = make_track(["E:\\Music\\a.mp3"])
    t_b = make_track(["E:\\Music\\b.mp3"])
    t_c = make_track(["E:\\Music\\c.mp3"])
    section = make_music_section([t_a, t_b, t_c])

    playlist = MagicMock()
    playlist.items.return_value = [t_a]  # only A present after import

    expected = ["e:\\music\\a.mp3", "E:/Music/b.mp3", "E:\\Music\\missing.mp3"]
    report = up.reconcile_playlist(MagicMock(), playlist, section, expected, "windows")

    assert report["already_present"] == 1
    assert report["added"] == ["E:/Music/b.mp3"]
    assert report["unresolvable"] == ["E:\\Music\\missing.mp3"]
    playlist.addItems.assert_called_once_with([t_b])


def test_reconcile_no_additions_when_all_present():
    t_a = make_track(["/music/a.mp3"])
    section = make_music_section([t_a])
    playlist = MagicMock()
    playlist.items.return_value = [t_a]

    report = up.reconcile_playlist(MagicMock(), playlist, section, ["/music/a.mp3"], "posix")

    assert report["added"] == []
    assert report["already_present"] == 1
    playlist.addItems.assert_not_called()


def test_reconcile_dedupes_repeated_expected_paths():
    t_a = make_track(["E:\\Music\\a.mp3"])
    section = make_music_section([t_a])
    playlist = MagicMock()
    playlist.items.return_value = []  # nothing matched on import

    # same path twice (differing case) should only add once
    expected = ["E:\\Music\\a.mp3", "e:\\music\\a.mp3"]
    report = up.reconcile_playlist(MagicMock(), playlist, section, expected, "windows")

    assert report["added"] == ["E:\\Music\\a.mp3"]
    playlist.addItems.assert_called_once_with([t_a])


# ---------------------------------------------------------------------------
# delete_existing_playlist()
# ---------------------------------------------------------------------------

def test_delete_existing_playlist_deletes_when_present():
    existing = MagicMock()
    plex = MagicMock()
    plex.playlist.return_value = existing

    assert up.delete_existing_playlist(plex, "My List") is True
    existing.delete.assert_called_once()


def test_delete_existing_playlist_returns_false_when_absent():
    plex = MagicMock()
    plex.playlist.side_effect = NotFound("nope")

    assert up.delete_existing_playlist(plex, "Missing") is False


# ---------------------------------------------------------------------------
# upload_playlist()
# ---------------------------------------------------------------------------

def test_upload_playlist_replace_deletes_existing_before_create(tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])
    t_a = make_track(["E:\\Music\\a.mp3"])
    section = make_music_section([t_a])
    plex = MagicMock()
    plex.library.section.return_value = section
    old_playlist = MagicMock()
    plex.playlist.return_value = old_playlist

    created = MagicMock()
    created.items.return_value = [t_a]
    order = []
    old_playlist.delete.side_effect = lambda: order.append("delete")

    def fake_create(server, title, section, m3ufilepath):
        order.append("create")
        return created

    with patch.object(up.Playlist, "create", side_effect=fake_create):
        up.upload_playlist(plex, src, "My List", section="Music", replace=True)

    assert order == ["delete", "create"]
    old_playlist.delete.assert_called_once()


def test_upload_playlist_replace_false_does_not_delete(tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])
    t_a = make_track(["E:\\Music\\a.mp3"])
    section = make_music_section([t_a])
    plex = MagicMock()
    plex.library.section.return_value = section
    old_playlist = MagicMock()
    plex.playlist.return_value = old_playlist

    created = MagicMock()
    created.items.return_value = [t_a]

    with patch.object(up.Playlist, "create", return_value=created):
        up.upload_playlist(plex, src, "My List", section="Music", replace=False)

    old_playlist.delete.assert_not_called()


def test_upload_playlist_replace_with_no_existing_playlist_is_fine(tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])
    t_a = make_track(["E:\\Music\\a.mp3"])
    section = make_music_section([t_a])
    plex = MagicMock()
    plex.library.section.return_value = section
    plex.playlist.side_effect = NotFound("none")

    created = MagicMock()
    created.items.return_value = [t_a]

    with patch.object(up.Playlist, "create", return_value=created):
        result = up.upload_playlist(plex, src, "My List", section="Music", replace=True)

    assert result is created


def test_upload_playlist_imports_normalized_temp_and_cleans_up(tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["#EXTM3U", "e:\\Music\\a.mp3"])
    t_a = make_track(["E:\\Music\\a.mp3"])
    section = make_music_section([t_a])
    plex = MagicMock()
    plex.library.section.return_value = section

    created = MagicMock()
    created.items.return_value = [t_a]
    seen = {}

    def fake_create(server, title, section, m3ufilepath):
        seen["path"] = m3ufilepath
        seen["exists_during"] = __import__("os").path.exists(m3ufilepath)
        seen["content"] = open(m3ufilepath, encoding="utf-8").read()
        return created

    with patch.object(up.Playlist, "create", side_effect=fake_create):
        result = up.upload_playlist(plex, src, "My List", section="Music")

    assert result is created
    # Imported a temp normalized file, not the original, and it had the fixed path
    assert seen["path"] != src
    assert seen["exists_during"] is True
    assert "E:\\Music\\a.mp3" in seen["content"]
    # Temp file is gone afterward
    import os
    assert not os.path.exists(seen["path"])


def test_upload_playlist_reconciles_by_default(tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3", "e:\\Music\\b.mp3"])
    t_a = make_track(["E:\\Music\\a.mp3"])
    t_b = make_track(["E:\\Music\\b.mp3"])
    section = make_music_section([t_a, t_b])
    plex = MagicMock()
    plex.library.section.return_value = section

    created = MagicMock()
    created.items.return_value = [t_a]  # import only matched A

    with patch.object(up.Playlist, "create", return_value=created):
        up.upload_playlist(plex, src, "My List", section="Music")

    created.addItems.assert_called_once_with([t_b])


def test_upload_playlist_reconcile_false_skips_backfill(tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3", "e:\\Music\\b.mp3"])
    t_a = make_track(["E:\\Music\\a.mp3"])
    t_b = make_track(["E:\\Music\\b.mp3"])
    section = make_music_section([t_a, t_b])
    plex = MagicMock()
    plex.library.section.return_value = section

    created = MagicMock()
    created.items.return_value = [t_a]

    with patch.object(up.Playlist, "create", return_value=created):
        up.upload_playlist(plex, src, "My List", section="Music", reconcile=False)

    created.addItems.assert_not_called()


def test_upload_playlist_rejects_non_music_section():
    not_music = MagicMock()
    not_music.title = "Movies"
    plex = MagicMock()
    plex.library.section.return_value = not_music

    with pytest.raises(ValueError, match="is not a music library"):
        up.upload_playlist(plex, "/x.m3u", "Title", section="Movies")


def test_upload_playlist_wraps_bad_request_with_helpful_note(tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])
    section = make_music_section([make_track(["E:\\Music\\a.mp3"])])
    plex = MagicMock()
    plex.library.section.return_value = section

    with patch.object(up.Playlist, "create", side_effect=BadRequest("Failed to import m3u file.")):
        with pytest.raises(BadRequest, match="readable by the Plex server"):
            up.upload_playlist(plex, src, "Title", section="Music")


def test_upload_playlist_cleans_up_temp_even_on_import_failure(tmp_path):
    import os

    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])
    section = make_music_section([make_track(["E:\\Music\\a.mp3"])])
    plex = MagicMock()
    plex.library.section.return_value = section

    with patch.object(up.Playlist, "create", side_effect=BadRequest("boom")):
        with pytest.raises(BadRequest):
            up.upload_playlist(plex, src, "Title", section="Music")

    # No stray .normalized_* file left behind in the source directory
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".normalized_")]
    assert leftovers == []


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


def test_parse_args_no_reconcile_defaults_false_and_sets_true():
    default = up.parse_args(
        ["--m3u-path", "x.m3u", "--title", "Test", "--url", "http://localhost:32400"]
    )
    assert default.no_reconcile is False

    on = up.parse_args(
        ["--m3u-path", "x.m3u", "--title", "Test", "--url", "http://localhost:32400", "-R"]
    )
    assert on.no_reconcile is True


def test_parse_args_replace_defaults_false_and_sets_true():
    default = up.parse_args(
        ["--m3u-path", "x.m3u", "--title", "Test", "--url", "http://localhost:32400"]
    )
    assert default.replace is False

    on = up.parse_args(
        ["--m3u-path", "x.m3u", "--title", "Test", "--url", "http://localhost:32400", "-x"]
    )
    assert on.replace is True


# ---------------------------------------------------------------------------
# CLI: main()
# ---------------------------------------------------------------------------

def test_main_success_returns_0(monkeypatch, tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])
    t_a = make_track(["E:\\Music\\a.mp3"])
    section = make_music_section([t_a])
    plex = MagicMock()
    plex.library.section.return_value = section

    created = MagicMock()
    created.items.return_value = [t_a]

    monkeypatch.setattr(
        up, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(up, "PlexServer", lambda url, token: plex)

    with patch.object(up.Playlist, "create", return_value=created):
        exit_code = up.main(
            [
                "--m3u-path", src,
                "--title", "Test",
                "--section", "Music",
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

    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])
    t_a = make_track(["E:\\Music\\a.mp3"])
    section = make_music_section([t_a])
    plex = MagicMock()
    plex.library.section.return_value = section
    created = MagicMock()
    created.items.return_value = [t_a]

    def fail_if_called(*args, **kwargs):
        pytest.fail("get_plex_credentials should not be called when --credential-file is given")

    monkeypatch.setattr(up, "get_plex_credentials", fail_if_called)
    monkeypatch.setattr(up, "PlexServer", lambda url, token: plex)

    with patch.object(up.Playlist, "create", return_value=created):
        exit_code = up.main(
            ["--m3u-path", src, "--title", "Test", "--section", "Music", "--credential-file", str(creds_path)]
        )

    assert exit_code == 0


def test_main_returns_1_on_bad_request(monkeypatch, tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])
    section = make_music_section([make_track(["E:\\Music\\a.mp3"])])
    plex = MagicMock()
    plex.library.section.return_value = section

    monkeypatch.setattr(
        up, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(up, "PlexServer", lambda url, token: plex)

    with patch.object(up.Playlist, "create", side_effect=BadRequest("nope")):
        exit_code = up.main(
            [
                "--m3u-path", src,
                "--title", "Test",
                "--section", "Music",
                "--url", "http://localhost:32400",
                "--username", "joe",
                "--password", "pw",
            ]
        )

    assert exit_code == 1


def test_main_returns_1_on_connection_error(monkeypatch, tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])

    def fake_plex_server(url, token):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(
        up, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(up, "PlexServer", fake_plex_server)

    exit_code = up.main(
        [
            "--m3u-path", src,
            "--title", "Test",
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
        ]
    )

    assert exit_code == 1
