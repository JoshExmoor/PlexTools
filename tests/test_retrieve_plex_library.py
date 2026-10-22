import json
from unittest.mock import MagicMock

import pytest
import requests
from plexapi.exceptions import NotFound
from plexapi.library import MusicSection

import retrieve_plex_library as rl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tag(name):
    tag = MagicMock()
    tag.tag = name
    return tag


def make_guid(gid):
    guid = MagicMock()
    guid.id = gid
    return guid


def make_part(
    file_path,
    size=1000,
    container="flac",
    bitrate=900,
    duration=180000,
    audio_codec="flac",
    audio_channels=2,
    stream=None,
):
    part = MagicMock()
    part.file = file_path
    part.size = size
    part.container = container
    part.duration = duration

    media = MagicMock()
    media.container = container
    media.bitrate = bitrate
    media.duration = duration
    media.audioCodec = audio_codec
    media.audioChannels = audio_channels
    part._parent.return_value = media

    part.audioStreams.return_value = [stream] if stream else []
    return part, media


def make_audio_stream(
    codec="flac",
    channels=2,
    sampling_rate=44100,
    bit_depth=16,
    bitrate_mode="vbr",
    layout="stereo",
):
    stream = MagicMock()
    stream.codec = codec
    stream.channels = channels
    stream.samplingRate = sampling_rate
    stream.bitDepth = bit_depth
    stream.bitrateMode = bitrate_mode
    stream.audioChannelLayout = layout
    return stream


def make_track(
    title="Song",
    artist="Artist",
    album="Album",
    *,
    parts=None,
    genres=None,
    moods=None,
    collections=None,
    labels=None,
    guids=None,
    user_rating=None,
    view_count=0,
    skip_count=0,
    year=2020,
    track_number=1,
    disc_number=1,
    original_title=None,
):
    track = MagicMock()
    track.title = title
    track.titleSort = title
    track.index = track_number
    track.parentIndex = disc_number
    track.ratingKey = 100
    track.guid = "plex://track/abc"
    track.grandparentTitle = artist
    track.originalTitle = original_title
    track.parentTitle = album
    track.year = year
    track.grandparentRatingKey = 10
    track.parentRatingKey = 50
    track.userRating = user_rating
    track.rating = None
    track.audienceRating = None
    track.ratingCount = None
    track.viewCount = view_count
    track.skipCount = skip_count
    track.lastViewedAt = None
    track.lastRatedAt = None
    track.addedAt = None
    track.updatedAt = None
    track.genres = genres or []
    track.moods = moods or []
    track.collections = collections or []
    track.labels = labels or []
    track.guids = guids or []

    # Build the media -> parts structure from supplied (part, media) pairs.
    if parts is None:
        stream = make_audio_stream()
        part, media = make_part("/music/song.flac", stream=stream)
        parts = [(part, media)]

    media_objs = []
    for part, media in parts:
        media.parts = [part]
        media_objs.append(media)
    track.media = media_objs
    return track


def make_plex(tracks, as_music=True):
    section = MagicMock(spec=MusicSection) if as_music else MagicMock()

    def fake_search(filters=None, container_start=0, container_size=None, maxresults=None):
        end = None if container_size is None else container_start + container_size
        return tracks[container_start:end]

    section.searchTracks.side_effect = fake_search
    plex = MagicMock()
    plex.library.section.return_value = section
    # Default (no --section) path resolves via sections(); expose this section
    # as the server's single music library when it is one.
    plex.library.sections.return_value = [section] if as_music else []
    return plex, section


# ---------------------------------------------------------------------------
# iter_tracks() - paged fetch
# ---------------------------------------------------------------------------

def test_iter_tracks_pages_through_all(monkeypatch):
    monkeypatch.setattr(rl, "_TRACK_PAGE_SIZE", 200)
    tracks = [make_track(f"S{i}") for i in range(450)]
    plex, section = make_plex(tracks)

    got = list(rl.iter_tracks(section))

    assert len(got) == 450
    offsets = [c.kwargs["container_start"] for c in section.searchTracks.call_args_list]
    assert offsets == [0, 200, 400]


def test_iter_tracks_handles_exact_multiple(monkeypatch):
    monkeypatch.setattr(rl, "_TRACK_PAGE_SIZE", 200)
    tracks = [make_track(f"S{i}") for i in range(400)]
    plex, section = make_plex(tracks)

    got = list(rl.iter_tracks(section))

    assert len(got) == 400
    offsets = [c.kwargs["container_start"] for c in section.searchTracks.call_args_list]
    assert offsets == [0, 200, 400]


def test_iter_tracks_empty_section():
    plex, section = make_plex([])
    assert list(rl.iter_tracks(section)) == []


def test_iter_tracks_no_filters_by_default():
    plex, section = make_plex([make_track()])

    list(rl.iter_tracks(section))

    for call in section.searchTracks.call_args_list:
        assert call.kwargs.get("filters") == {}


def test_iter_tracks_applies_search_artist_album_filters():
    plex, section = make_plex([make_track()])

    list(rl.iter_tracks(section, search="love", artist="beatles", album="abbey"))

    first = section.searchTracks.call_args_list[0].kwargs["filters"]
    assert first == {
        "title__icontains": "love",
        "artist.title__icontains": "beatles",
        "album.title__icontains": "abbey",
    }


def test_iter_tracks_does_not_pass_libtype():
    # searchTracks hardcodes libtype='track'; passing it would raise.
    plex, section = make_plex([make_track()])

    list(rl.iter_tracks(section))

    for call in section.searchTracks.call_args_list:
        assert "libtype" not in call.kwargs


# ---------------------------------------------------------------------------
# retrieve_plex_library() - record shape
# ---------------------------------------------------------------------------

def test_record_has_flat_scalar_top_level_fields():
    stream = make_audio_stream(sampling_rate=48000, bit_depth=24)
    part, media = make_part("/music/a.flac", size=5000, bitrate=1100, stream=stream)
    track = make_track(
        "Song A", "Artist 1", "Album X",
        parts=[(part, media)],
        user_rating=8.0, view_count=5, skip_count=1, year=1999,
    )
    plex, _ = make_plex([track])

    result = rl.retrieve_plex_library(plex)

    assert len(result) == 1
    rec = result[0]
    assert rec["title"] == "Song A"
    assert rec["artist"] == "Artist 1"
    assert rec["album"] == "Album X"
    assert rec["year"] == 1999
    assert rec["userRating"] == 8.0
    assert rec["playCount"] == 5
    assert rec["skipCount"] == 1
    # Primary file fields flattened to top level (CSV-ready).
    assert rec["path"] == "/music/a.flac"
    assert rec["fileSize"] == 5000
    assert rec["bitrate"] == 1100
    assert rec["samplingRate"] == 48000
    assert rec["bitDepth"] == 24


def test_record_tag_fields_are_string_lists():
    track = make_track(
        genres=[make_tag("Rock"), make_tag("Indie")],
        moods=[make_tag("Energetic")],
        collections=[make_tag("Favorites")],
        labels=[make_tag("Label A")],
        guids=[make_guid("mbid://123")],
    )
    plex, _ = make_plex([track])

    rec = rl.retrieve_plex_library(plex)[0]

    assert rec["genres"] == ["Rock", "Indie"]
    assert rec["moods"] == ["Energetic"]
    assert rec["collections"] == ["Favorites"]
    assert rec["labels"] == ["Label A"]
    assert rec["guids"] == ["mbid://123"]


def test_record_captures_all_files_for_multi_part_track():
    s1 = make_audio_stream()
    s2 = make_audio_stream()
    p1, m1 = make_part("/music/disc1.flac", stream=s1)
    p2, m2 = make_part("/music/disc2.flac", stream=s2)
    track = make_track(parts=[(p1, m1), (p2, m2)])
    plex, _ = make_plex([track])

    rec = rl.retrieve_plex_library(plex)[0]

    assert rec["path"] == "/music/disc1.flac"  # primary = first
    assert len(rec["files"]) == 2
    assert [f["path"] for f in rec["files"]] == ["/music/disc1.flac", "/music/disc2.flac"]


def test_record_handles_partial_track_missing_attributes():
    # Reproduces the real failure: tracks from a paginated search come back
    # "partial" and raise AttributeError (not None) for attributes Plex omitted
    # from the list view, e.g. `rating`. The record must treat those as None.
    #
    # A MagicMock auto-creates any attribute, so it can't model "absent". Use a
    # plain object that mirrors a partial Track: defined attributes return
    # values, and the ones Plex left out raise AttributeError on access.
    missing = {"rating", "audienceRating", "ratingCount", "originalTitle"}

    class PartialTrack:
        def __init__(self):
            self.title = "Song"
            self.titleSort = "Song"
            self.index = 1
            self.parentIndex = 1
            self.ratingKey = 100
            self.guid = "plex://track/abc"
            self.grandparentTitle = "Artist"
            self.parentTitle = "Album"
            self.year = 2020
            self.grandparentRatingKey = 10
            self.parentRatingKey = 50
            self.userRating = None
            self.viewCount = 3
            self.skipCount = 0
            self.lastViewedAt = None
            self.lastRatedAt = None
            self.addedAt = None
            self.updatedAt = None
            self.genres = []
            self.moods = []
            self.collections = []
            self.labels = []
            self.guids = []
            self.media = []

        def __getattr__(self, name):
            # Reached only for attributes not set in __init__ - i.e. the ones
            # Plex omitted. Emulate PlexPartialObject by raising.
            if name in missing:
                raise AttributeError(f"'Track' object has no attribute '{name}'")
            raise AttributeError(name)

    rec = rl._track_record(PartialTrack())

    assert rec["rating"] is None
    assert rec["audienceRating"] is None
    assert rec["ratingCount"] is None
    assert rec["trackArtist"] is None  # from originalTitle
    assert rec["title"] == "Song"
    assert rec["playCount"] == 3


def test_attr_returns_none_for_missing_attribute():
    class Partial:
        @property
        def rating(self):
            raise AttributeError("'Track' object has no attribute 'rating'")

    assert rl._attr(Partial(), "rating") is None


def test_attr_returns_value_when_present():
    class Obj:
        rating = 7.5

    assert rl._attr(Obj(), "rating") == 7.5


def test_record_track_without_files_gets_null_path():
    track = make_track(parts=[])
    plex, _ = make_plex([track])

    rec = rl.retrieve_plex_library(plex)[0]

    assert rec["path"] is None
    assert rec["files"] == []


def test_record_handles_part_without_audio_stream():
    part, media = make_part("/music/x.mp3", stream=None)
    track = make_track(parts=[(part, media)])
    plex, _ = make_plex([track])

    rec = rl.retrieve_plex_library(plex)[0]

    assert rec["path"] == "/music/x.mp3"
    assert rec["samplingRate"] is None
    assert rec["bitDepth"] is None
    assert rec["audioCodec"] == "flac"  # still from Media


def test_retrieve_library_reads_requested_section():
    plex, _ = make_plex([])

    rl.retrieve_plex_library(plex, section="My Music")

    plex.library.section.assert_called_once_with("My Music")


def test_retrieve_library_defaults_to_servers_only_music_section():
    # With no --section, resolution falls back to the server's single music
    # library via sections(), not a hardcoded "Music" lookup.
    plex, _ = make_plex([])

    rl.retrieve_plex_library(plex)

    plex.library.section.assert_not_called()
    plex.library.sections.assert_called_once()


def test_retrieve_library_rejects_non_music_section():
    plex, _ = make_plex([], as_music=False)

    with pytest.raises(ValueError, match="is not a music library"):
        rl.retrieve_plex_library(plex, section="Movies")


def test_retrieve_library_empty_section_returns_empty_list():
    plex, _ = make_plex([])

    assert rl.retrieve_plex_library(plex) == []


def test_retrieve_library_result_is_json_serializable():
    track = make_track(genres=[make_tag("Rock")])
    plex, _ = make_plex([track])

    result = rl.retrieve_plex_library(plex)

    # Must not raise (datetimes are ISO strings, tags are plain lists).
    json.dumps(result)


# ---------------------------------------------------------------------------
# Helpers: _iso / _tag_values / _guid_values
# ---------------------------------------------------------------------------

def test_iso_converts_datetime():
    import datetime

    dt = datetime.datetime(2020, 1, 2, 3, 4, 5)
    assert rl._iso(dt) == "2020-01-02T03:04:05"


def test_iso_passes_through_none():
    assert rl._iso(None) is None


def test_tag_values_skips_missing_names():
    good = make_tag("Rock")
    bad = MagicMock()
    bad.tag = None
    assert rl._tag_values([good, bad]) == ["Rock"]


def test_tag_values_empty():
    assert rl._tag_values(None) == []
    assert rl._tag_values([]) == []


# ---------------------------------------------------------------------------
# CLI: parse_args()
# ---------------------------------------------------------------------------

def test_parse_args_requires_output():
    with pytest.raises(SystemExit):
        rl.parse_args(["--url", "http://localhost:32400"])


def test_parse_args_requires_url_unless_credential_file_given():
    with pytest.raises(SystemExit):
        rl.parse_args(["--output", "out.json"])


def test_parse_args_allows_credential_file_without_url():
    args = rl.parse_args(["--output", "out.json", "--credential-file", "creds.json"])
    assert args.credential_file == "creds.json"
    assert args.url is None


def test_parse_args_scope_filters_default_none():
    args = rl.parse_args(["--output", "out.json", "--url", "http://localhost:32400"])
    assert args.search is None
    assert args.artist is None
    assert args.album is None


def test_parse_args_accepts_scope_filters():
    args = rl.parse_args(
        ["-o", "out.json", "-l", "http://x:32400",
         "--search", "love", "--artist", "beatles", "--album", "abbey"]
    )
    assert args.search == "love"
    assert args.artist == "beatles"
    assert args.album == "abbey"


# ---------------------------------------------------------------------------
# CLI: main()
# ---------------------------------------------------------------------------

def test_main_writes_json_and_returns_0(monkeypatch, tmp_path):
    tracks = [make_track("Song A"), make_track("Song B", user_rating=None)]
    plex, _ = make_plex(tracks)
    out = tmp_path / "library.json"

    monkeypatch.setattr(
        rl, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(rl, "PlexServer", lambda url, token: plex)

    exit_code = rl.main(["-o", str(out), "-l", "http://localhost:32400", "-u", "joe", "-p", "pw"])

    assert exit_code == 0
    data = json.loads(out.read_text())
    assert len(data) == 2
    assert data[0]["title"] == "Song A"


def test_main_passes_scope_filters_through(monkeypatch, tmp_path):
    plex, section = make_plex([make_track()])
    out = tmp_path / "library.json"

    monkeypatch.setattr(
        rl, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(rl, "PlexServer", lambda url, token: plex)

    exit_code = rl.main(
        ["-o", str(out), "-l", "http://localhost:32400", "-u", "joe", "-p", "pw",
         "--search", "love", "--artist", "beatles", "--album", "abbey"]
    )

    assert exit_code == 0
    first = section.searchTracks.call_args_list[0].kwargs["filters"]
    assert first == {
        "title__icontains": "love",
        "artist.title__icontains": "beatles",
        "album.title__icontains": "abbey",
    }


def test_main_creates_parent_directories(monkeypatch, tmp_path):
    plex, _ = make_plex([make_track()])
    out = tmp_path / "nested" / "dir" / "library.json"

    monkeypatch.setattr(
        rl, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(rl, "PlexServer", lambda url, token: plex)

    exit_code = rl.main(["-o", str(out), "-l", "http://localhost:32400", "-u", "joe", "-p", "pw"])

    assert exit_code == 0
    assert out.exists()


def test_main_skips_login_with_credential_file(monkeypatch, tmp_path):
    from get_plex_credentials import save_plex_credentials

    creds_path = tmp_path / "creds.json"
    save_plex_credentials({"url": "http://localhost:32400", "token": "cached-tok"}, str(creds_path))

    plex, _ = make_plex([make_track()])
    out = tmp_path / "library.json"

    def fail_if_called(*args, **kwargs):
        pytest.fail("get_plex_credentials should not be called when --credential-file is given")

    monkeypatch.setattr(rl, "get_plex_credentials", fail_if_called)
    monkeypatch.setattr(rl, "PlexServer", lambda url, token: plex)

    exit_code = rl.main(["-o", str(out), "-c", str(creds_path)])

    assert exit_code == 0
    assert out.exists()


def test_main_returns_1_on_section_not_found(monkeypatch, tmp_path):
    plex = MagicMock()
    plex.library.section.side_effect = NotFound("Section not found")
    out = tmp_path / "library.json"

    monkeypatch.setattr(
        rl, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(rl, "PlexServer", lambda url, token: plex)

    exit_code = rl.main(["-o", str(out), "-l", "http://localhost:32400", "-u", "joe", "-p", "pw"])

    assert exit_code == 1
    assert not out.exists()


def test_main_returns_1_on_connection_error(monkeypatch, tmp_path):
    out = tmp_path / "library.json"

    def fake_plex_server(url, token):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(
        rl, "get_plex_credentials", lambda url, username, password: {"url": url, "token": "tok"}
    )
    monkeypatch.setattr(rl, "PlexServer", fake_plex_server)

    exit_code = rl.main(["-o", str(out), "-l", "http://localhost:32400", "-u", "joe", "-p", "pw"])

    assert exit_code == 1
