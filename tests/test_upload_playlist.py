from unittest.mock import MagicMock, patch

import pytest
import requests
from plexapi.exceptions import BadRequest, NotFound
from plexapi.library import MusicSection

import upload_playlist as up


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_track(file_paths, rating_key=1):
    track = MagicMock()
    track.ratingKey = rating_key
    parts = []
    for file_path in file_paths:
        part = MagicMock()
        part.file = file_path
        parts.append(part)
    track.iterParts.return_value = parts
    return track


def make_music_section(section_tracks, title="Music", uuid="section-uuid", updated_at="2026-01-01 00:00:00"):
    """A mocked MusicSection whose searchTracks returns the given tracks.

    searchTracks is used both by detect_path_style and reconcile_playlist, so a
    single return value standing in for 'all tracks in the section' is fine.
    totalViewSize returns a real int (reconcile_playlist uses it for the tqdm
    progress bar total AND the cache fingerprint); a MagicMock there would break
    tqdm's numeric math. uuid/updatedAt back the cache validity check.
    """
    section = MagicMock(spec=MusicSection)
    section.title = title
    section.uuid = uuid
    section.updatedAt = updated_at

    # Honor container_start/container_size so iter_section_tracks (used by
    # reconcile_playlist) pages correctly even if a fixture grows past the page
    # size. detect_path_style calls searchTracks(maxresults=...) with no
    # container_start, which slices from 0 - also fine.
    def fake_search(container_start=0, container_size=None, maxresults=None):
        end = None if container_size is None else container_start + container_size
        return section_tracks[container_start:end]

    section.searchTracks.side_effect = fake_search
    section.totalViewSize.return_value = len(section_tracks)
    return section


def make_plex_resolving(tracks):
    """A plex mock whose fetchItems("/library/metadata/k1,k2,...") resolves back
    to the given Track mocks - mirrors how reconcile_playlist re-fetches the
    tracks it adds (it builds a /library/metadata/<keys> string, not a list).
    """
    by_key = {int(t.ratingKey): t for t in tracks}

    def fake_fetch(ekey):
        keys_part = ekey.rsplit("/", 1)[-1]  # "/library/metadata/1,2" -> "1,2"
        return [by_key[int(k)] for k in keys_part.split(",")]

    plex = MagicMock()
    plex.fetchItems.side_effect = fake_fetch
    # Default: no playlists on the server (so warn_if_duplicate_playlists, called
    # at the end of upload_playlist, sees nothing to warn about). Tests that
    # exercise duplicates override plex.playlists.return_value explicitly.
    plex.playlists.return_value = []
    return plex


@pytest.fixture(autouse=True)
def _isolate_track_cache(tmp_path, monkeypatch):
    """Redirect the on-disk section cache to a per-test temp dir so tests never
    read or write the real ~/.cache/plextools, and never leak state to each
    other.
    """
    cache_dir = tmp_path / "_cache"
    monkeypatch.setattr(up, "_cache_dir", lambda: str(cache_dir))


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
# iter_section_tracks() - manual pagination
# ---------------------------------------------------------------------------

def _paged_section(tracks):
    """A section whose searchTracks honors container_start/container_size, so
    iter_section_tracks pages through it the way a real server would.

    (Note: a plain return_value mock would hand back the SAME full list on every
    call and make iter_section_tracks loop; tests must use real paging like this
    for any list at/above the page size.)
    """
    section = MagicMock(spec=MusicSection)

    def fake_search(container_start, container_size, maxresults):
        return tracks[container_start:container_start + container_size]

    section.searchTracks.side_effect = fake_search
    return section


def test_iter_section_tracks_pages_through_all(monkeypatch):
    monkeypatch.setattr(up, "_TRACK_PAGE_SIZE", 200)
    tracks = [make_track([f"/m/{i}.mp3"]) for i in range(450)]
    section = _paged_section(tracks)

    got = list(up.iter_section_tracks(section))

    assert len(got) == 450
    offsets = [c.kwargs["container_start"] for c in section.searchTracks.call_args_list]
    assert offsets == [0, 200, 400]  # partial 3rd page (50) stops the loop


def test_iter_section_tracks_handles_exact_multiple(monkeypatch):
    monkeypatch.setattr(up, "_TRACK_PAGE_SIZE", 200)
    tracks = [make_track([f"/m/{i}.mp3"]) for i in range(400)]
    section = _paged_section(tracks)

    got = list(up.iter_section_tracks(section))

    assert len(got) == 400
    offsets = [c.kwargs["container_start"] for c in section.searchTracks.call_args_list]
    assert offsets == [0, 200, 400]  # empty 3rd page detects the end


def test_iter_section_tracks_empty_section():
    section = MagicMock(spec=MusicSection)
    section.searchTracks.return_value = []

    assert list(up.iter_section_tracks(section)) == []


def test_iter_section_tracks_requests_pages_with_maxresults(monkeypatch):
    monkeypatch.setattr(up, "_TRACK_PAGE_SIZE", 200)
    tracks = [make_track([f"/m/{i}.mp3"]) for i in range(10)]
    section = _paged_section(tracks)

    list(up.iter_section_tracks(section))

    call = section.searchTracks.call_args_list[0]
    assert call.kwargs["container_size"] == 200
    assert call.kwargs["maxresults"] == 200


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


def test_detect_path_style_does_not_pass_libtype_to_search_tracks():
    # Regression: searchTracks() already hardcodes libtype='track' internally,
    # so passing libtype="track" ourselves makes the real plexapi raise
    # "got multiple values for keyword argument 'libtype'". Ensure we don't.
    section = make_music_section([make_track(["/music/a.mp3"])])
    up.detect_path_style(section)
    for call in section.searchTracks.call_args_list:
        assert "libtype" not in call.kwargs, f"searchTracks called with libtype: {call}"


def test_reconcile_does_not_pass_libtype_to_search_tracks():
    section = make_music_section([make_track(["/music/a.mp3"])])
    playlist = MagicMock()
    playlist.items.return_value = []
    up.reconcile_playlist(MagicMock(), playlist, section, ["/music/a.mp3"], "posix")
    for call in section.searchTracks.call_args_list:
        assert "libtype" not in call.kwargs, f"searchTracks called with libtype: {call}"


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
    t_a = make_track(["E:\\Music\\a.mp3"], rating_key=1)
    t_b = make_track(["E:\\Music\\b.mp3"], rating_key=2)
    t_c = make_track(["E:\\Music\\c.mp3"], rating_key=3)
    section = make_music_section([t_a, t_b, t_c])
    plex = make_plex_resolving([t_a, t_b, t_c])

    playlist = MagicMock()
    playlist.items.return_value = [t_a]  # only A present after import

    expected = ["e:\\music\\a.mp3", "E:/Music/b.mp3", "E:\\Music\\missing.mp3"]
    report = up.reconcile_playlist(plex, playlist, section, expected, "windows")

    assert report["already_present"] == 1
    assert report["added"] == ["E:/Music/b.mp3"]
    assert report["unresolvable"] == ["E:\\Music\\missing.mp3"]
    plex.fetchItems.assert_called_once_with("/library/metadata/2")  # only track b
    playlist.addItems.assert_called_once_with([t_b])


def test_reconcile_resolves_adds_via_metadata_key_string(tmp_path):
    # Regression: reconcile must fetch the tracks-to-add using a
    # /library/metadata/<k1,k2,...> STRING, not a list of ints. The list form is
    # version-dependent and raises "Failed to parse: <baseurl>[...]" on older
    # plexapi. Assert the exact string key with multiple comma-joined ids.
    t_a = make_track(["/music/a.mp3"], rating_key=10)
    t_b = make_track(["/music/b.mp3"], rating_key=20)
    section = make_music_section([t_a, t_b])
    plex = make_plex_resolving([t_a, t_b])
    playlist = MagicMock()
    playlist.items.return_value = []  # nothing matched -> both get added

    up.reconcile_playlist(plex, playlist, section, ["/music/a.mp3", "/music/b.mp3"], "posix")

    key_arg = plex.fetchItems.call_args.args[0]
    assert isinstance(key_arg, str)
    assert key_arg == "/library/metadata/10,20"


def test_reconcile_still_works_when_totalviewsize_fails(tmp_path):
    # The tqdm progress-bar total is best-effort: if totalViewSize raises (or
    # returns something non-numeric), reconciliation must still complete.
    t_a = make_track(["/music/a.mp3"], rating_key=1)
    t_b = make_track(["/music/b.mp3"], rating_key=2)
    section = make_music_section([t_a, t_b])
    section.totalViewSize.side_effect = Exception("count unavailable")
    plex = make_plex_resolving([t_a, t_b])
    playlist = MagicMock()
    playlist.items.return_value = []

    report = up.reconcile_playlist(
        plex, playlist, section, ["/music/a.mp3", "/music/b.mp3"], "posix"
    )

    assert sorted(report["added"]) == ["/music/a.mp3", "/music/b.mp3"]


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
    t_a = make_track(["E:\\Music\\a.mp3"], rating_key=1)
    section = make_music_section([t_a])
    plex = make_plex_resolving([t_a])
    playlist = MagicMock()
    playlist.items.return_value = []  # nothing matched on import

    # same path twice (differing case) should only add once
    expected = ["E:\\Music\\a.mp3", "e:\\music\\a.mp3"]
    report = up.reconcile_playlist(plex, playlist, section, expected, "windows")

    assert report["added"] == ["E:\\Music\\a.mp3"]
    playlist.addItems.assert_called_once_with([t_a])


# ---------------------------------------------------------------------------
# track cache: save_track_cache / load_track_cache
# ---------------------------------------------------------------------------

def test_real_cache_dir_resolves_without_import_error(monkeypatch):
    # Regression: _cache_dir uses pathlib.Path, which must be imported in the
    # module (a missing import only surfaces at runtime, and the autouse fixture
    # patches _cache_dir away so other tests never exercise the real one). Undo
    # that patch and call the genuine implementation.
    monkeypatch.undo()  # remove the autouse _cache_dir patch for this test
    cache_dir = up._cache_dir()
    assert isinstance(cache_dir, str)
    assert cache_dir.endswith("plextools")
    assert up._cache_path("abc").endswith("section_abc.json")


def test_save_then_load_track_cache_round_trip():
    section = make_music_section([make_track(["/music/a.mp3"], rating_key=5)])
    mapping = {"/music/a.mp3": 5}

    up.save_track_cache(section, mapping, "posix")
    loaded = up.load_track_cache(section)

    assert loaded is not None
    assert loaded["tracks"] == mapping
    assert loaded["style"] == "posix"


def test_load_track_cache_returns_none_when_absent():
    section = make_music_section([make_track(["/music/a.mp3"])], uuid="never-cached")
    assert up.load_track_cache(section) is None


def test_load_track_cache_invalidated_by_updated_at_change():
    tracks = [make_track(["/music/a.mp3"], rating_key=5)]
    section = make_music_section(tracks, updated_at="2026-01-01 00:00:00")
    up.save_track_cache(section, {"/music/a.mp3": 5}, "posix")

    # Same section/uuid/count, but the library was updated since caching.
    section.updatedAt = "2026-02-02 00:00:00"
    assert up.load_track_cache(section) is None


def test_load_track_cache_invalidated_by_track_count_change():
    tracks = [make_track(["/music/a.mp3"], rating_key=5)]
    section = make_music_section(tracks)
    up.save_track_cache(section, {"/music/a.mp3": 5}, "posix")

    # A track was added/removed since caching (count no longer matches).
    section.totalViewSize.return_value = 999
    assert up.load_track_cache(section) is None


def test_load_track_cache_invalidated_by_uuid_mismatch():
    section_a = make_music_section([make_track(["/music/a.mp3"], rating_key=5)], uuid="uuid-a")
    up.save_track_cache(section_a, {"/music/a.mp3": 5}, "posix")

    # A different section (different uuid) must not read section-a's cache.
    section_b = make_music_section([make_track(["/music/a.mp3"], rating_key=5)], uuid="uuid-b")
    assert up.load_track_cache(section_b) is None


def test_reconcile_uses_cache_on_hit_without_paging():
    t_a = make_track(["/music/a.mp3"], rating_key=1)
    t_b = make_track(["/music/b.mp3"], rating_key=2)
    section = make_music_section([t_a, t_b])
    plex = make_plex_resolving([t_a, t_b])

    # First run populates the cache (a real fetch happens here).
    pl1 = MagicMock()
    pl1.items.return_value = []
    up.reconcile_playlist(plex, pl1, section, ["/music/a.mp3", "/music/b.mp3"], "posix")

    # Second run: make paging explode so the test fails if the cache is NOT used.
    section.searchTracks.side_effect = AssertionError("cache hit must not page the library")
    pl2 = MagicMock()
    pl2.items.return_value = [t_a]
    report = up.reconcile_playlist(plex, pl2, section, ["/music/a.mp3", "/music/b.mp3"], "posix")

    assert report["added"] == ["/music/b.mp3"]


def test_reconcile_no_cache_always_fetches():
    t_a = make_track(["/music/a.mp3"], rating_key=1)
    section = make_music_section([t_a])
    plex = make_plex_resolving([t_a])

    # Pre-populate a valid cache.
    up.save_track_cache(section, {"/music/a.mp3": 1}, "posix")

    # use_cache=False must ignore it and page the library.
    paged = []
    original = section.searchTracks.side_effect

    def tracking(container_start=0, container_size=None, maxresults=None):
        paged.append(container_start)
        return original(container_start=container_start, container_size=container_size, maxresults=maxresults)

    section.searchTracks.side_effect = tracking
    playlist = MagicMock()
    playlist.items.return_value = []
    up.reconcile_playlist(plex, playlist, section, ["/music/a.mp3"], "posix", use_cache=False)

    assert paged, "use_cache=False should have paged the library"


# ---------------------------------------------------------------------------
# duplicate playlist handling
# ---------------------------------------------------------------------------

def make_playlist(rating_key, title):
    pl = MagicMock()
    pl.ratingKey = rating_key
    pl.title = title
    pl.leafCount = 10
    pl.addedAt = "2026-01-01 00:00:00"
    pl.updatedAt = "2026-01-01 00:00:00"
    return pl


def test_find_playlists_by_title_is_case_insensitive():
    p1 = make_playlist(1, "zMB4Stars")
    p2 = make_playlist(2, "zmb4stars")  # same name, different case
    p3 = make_playlist(3, "Other")
    plex = MagicMock()
    plex.playlists.return_value = [p1, p2, p3]

    matches = up.find_playlists_by_title(plex, "ZMB4STARS")

    assert {p.ratingKey for p in matches} == {1, 2}


def test_remove_duplicate_playlists_keeps_only_the_kept_one():
    keep = make_playlist(100, "zMB4Stars")
    dup1 = make_playlist(101, "zMB4Stars")
    dup2 = make_playlist(102, "ZMB4STARS")  # case-insensitive match
    other = make_playlist(200, "Different")
    plex = MagicMock()
    plex.playlists.return_value = [keep, dup1, dup2, other]

    removed = up.remove_duplicate_playlists(plex, "zMB4Stars", keep=keep)

    assert set(removed) == {101, 102}
    keep.delete.assert_not_called()
    dup1.delete.assert_called_once()
    dup2.delete.assert_called_once()
    other.delete.assert_not_called()


def test_warn_if_duplicate_playlists_warns_on_multiple(capsys):
    p1 = make_playlist(1, "Dup")
    p2 = make_playlist(2, "Dup")
    plex = MagicMock()
    plex.playlists.return_value = [p1, p2]

    matches = up.warn_if_duplicate_playlists(plex, "Dup")

    assert len(matches) == 2
    err = capsys.readouterr().err
    assert "2 playlists named" in err
    assert "--remove-duplicates" in err


def test_warn_if_duplicate_playlists_silent_on_single(capsys):
    p1 = make_playlist(1, "Unique")
    plex = MagicMock()
    plex.playlists.return_value = [p1]

    matches = up.warn_if_duplicate_playlists(plex, "Unique")

    assert len(matches) == 1
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# upload_playlist()
# ---------------------------------------------------------------------------

def test_upload_playlist_warns_when_duplicates_exist(tmp_path, capsys):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])
    t_a = make_track(["E:\\Music\\a.mp3"], rating_key=1)
    section = make_music_section([t_a])
    plex = make_plex_resolving([t_a])
    plex.library.section.return_value = section

    created = make_playlist(500, "My List")
    created.items.return_value = [t_a]
    other = make_playlist(501, "My List")  # a pre-existing duplicate
    plex.playlists.return_value = [created, other]

    with patch.object(up.Playlist, "create", return_value=created):
        up.upload_playlist(plex, src, "My List", section="Music")

    err = capsys.readouterr().err
    assert "2 playlists named" in err
    # Default (no remove_duplicates): nothing is deleted.
    other.delete.assert_not_called()
    created.delete.assert_not_called()


def test_upload_playlist_removes_duplicates_when_requested(tmp_path):
    src = write_m3u(tmp_path / "list.m3u", ["e:\\Music\\a.mp3"])
    t_a = make_track(["E:\\Music\\a.mp3"], rating_key=1)
    section = make_music_section([t_a])
    plex = make_plex_resolving([t_a])
    plex.library.section.return_value = section

    created = make_playlist(500, "My List")
    created.items.return_value = [t_a]
    dup = make_playlist(501, "My List")
    plex.playlists.return_value = [created, dup]

    with patch.object(up.Playlist, "create", return_value=created):
        up.upload_playlist(plex, src, "My List", section="Music", remove_duplicates=True)

    dup.delete.assert_called_once()  # the other same-named playlist is removed
    created.delete.assert_not_called()  # the fresh upload is kept


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
    t_a = make_track(["E:\\Music\\a.mp3"], rating_key=1)
    t_b = make_track(["E:\\Music\\b.mp3"], rating_key=2)
    section = make_music_section([t_a, t_b])
    plex = make_plex_resolving([t_a, t_b])
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


def test_parse_args_remove_duplicates_defaults_false_and_sets_true():
    default = up.parse_args(
        ["--m3u-path", "x.m3u", "--title", "Test", "--url", "http://localhost:32400"]
    )
    assert default.remove_duplicates is False

    on = up.parse_args(
        ["--m3u-path", "x.m3u", "--title", "Test", "--url", "http://localhost:32400", "-D"]
    )
    assert on.remove_duplicates is True


def test_parse_args_no_longer_has_replace():
    # --replace was removed (it did nothing different from default). Passing it
    # should now be an unrecognized-argument error.
    with pytest.raises(SystemExit):
        up.parse_args(
            ["--m3u-path", "x.m3u", "--title", "Test", "--url", "http://localhost:32400", "--replace"]
        )


def test_parse_args_no_cache_defaults_false_and_sets_true():
    default = up.parse_args(
        ["--m3u-path", "x.m3u", "--title", "Test", "--url", "http://localhost:32400"]
    )
    assert default.no_cache is False

    on = up.parse_args(
        ["--m3u-path", "x.m3u", "--title", "Test", "--url", "http://localhost:32400", "-C"]
    )
    assert on.no_cache is True


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
