import json
from unittest.mock import MagicMock

import pytest
import requests
from plexapi.exceptions import BadRequest, NotFound

import push_plex_ratings_by_playlist as push_mod
from push_plex_ratings_by_playlist import stars_to_rating, update_playlist_ratings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_track(title, user_rating, rating_key=None):
    track = MagicMock()
    track.title = title
    track.userRating = user_rating
    track.ratingKey = rating_key or title
    return track


def make_plex(tracks, playlist_name="MyList"):
    playlist = MagicMock()
    playlist.items.return_value = tracks
    plex = MagicMock()
    plex.playlist.return_value = playlist
    return plex


# ---------------------------------------------------------------------------
# stars_to_rating()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stars,expected_rating",
    [
        (0, 0),
        (0.5, 1),
        (1, 2),
        (2.5, 5),
        (4, 8),
        (4.5, 9),
        (5, 10),
    ],
)
def test_stars_to_rating_valid_values(stars, expected_rating):
    assert stars_to_rating(stars) == expected_rating


@pytest.mark.parametrize("bad_stars", [-1, 5.1, 6, 10])
def test_stars_to_rating_rejects_out_of_range(bad_stars):
    with pytest.raises(ValueError, match="between"):
        stars_to_rating(bad_stars)


@pytest.mark.parametrize("bad_stars", [0.25, 1.3, 4.9, 3.1])
def test_stars_to_rating_rejects_non_half_step(bad_stars):
    with pytest.raises(ValueError, match="steps of"):
        stars_to_rating(bad_stars)


def test_stars_to_rating_rejects_non_numeric():
    with pytest.raises(ValueError, match="must be a number"):
        stars_to_rating("four")


# ---------------------------------------------------------------------------
# update_playlist_ratings() - core behavior
# ---------------------------------------------------------------------------

def test_update_playlist_ratings_rates_every_track_when_none_rated():
    tracks = [make_track("a", None), make_track("b", None)]
    plex = make_plex(tracks)

    result = update_playlist_ratings(plex, "MyList", 5.0)

    tracks[0].rate.assert_called_once_with(10)
    tracks[1].rate.assert_called_once_with(10)
    assert result == {"total": 2, "updated": 2, "skipped": 0, "failed": []}


def test_update_playlist_ratings_looks_up_playlist_by_name():
    tracks = [make_track("a", None)]
    plex = make_plex(tracks)

    update_playlist_ratings(plex, "Road Trip", 3.0)

    plex.playlist.assert_called_once_with("Road Trip")


def test_update_playlist_ratings_propagates_playlist_not_found():
    plex = MagicMock()
    plex.playlist.side_effect = NotFound('Unable to find playlist with title "Nope".')

    with pytest.raises(NotFound):
        update_playlist_ratings(plex, "Nope", 3.0)


def test_update_playlist_ratings_validates_rating_before_touching_playlist():
    plex = MagicMock()

    with pytest.raises(ValueError):
        update_playlist_ratings(plex, "MyList", 7.25)

    plex.playlist.assert_not_called()


# ---------------------------------------------------------------------------
# skip-already-at-target default / --force-ratings
# ---------------------------------------------------------------------------

def test_default_skips_tracks_already_at_target_rating():
    tracks = [make_track("a", 8.0), make_track("b", 6.0), make_track("c", None)]
    plex = make_plex(tracks)

    result = update_playlist_ratings(plex, "MyList", 4.0)  # 4 stars = rating 8

    tracks[0].rate.assert_not_called()
    tracks[1].rate.assert_called_once_with(8)
    tracks[2].rate.assert_called_once_with(8)
    assert result == {"total": 3, "updated": 2, "skipped": 1, "failed": []}


def test_force_ratings_updates_everything_regardless_of_current_rating():
    tracks = [make_track("a", 8.0), make_track("b", 8.0)]
    plex = make_plex(tracks)

    result = update_playlist_ratings(plex, "MyList", 4.0, force_ratings=True)

    tracks[0].rate.assert_called_once_with(8)
    tracks[1].rate.assert_called_once_with(8)
    assert result == {"total": 2, "updated": 2, "skipped": 0, "failed": []}


def test_all_tracks_already_at_target_updates_nothing():
    tracks = [make_track("a", 8.0), make_track("b", 8.0)]
    plex = make_plex(tracks)

    result = update_playlist_ratings(plex, "MyList", 4.0)

    tracks[0].rate.assert_not_called()
    tracks[1].rate.assert_not_called()
    assert result == {"total": 2, "updated": 0, "skipped": 2, "failed": []}


def test_empty_playlist_is_handled_without_error():
    plex = make_plex([])

    result = update_playlist_ratings(plex, "EmptyList", 4.0)

    assert result == {"total": 0, "updated": 0, "skipped": 0, "failed": []}


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_makes_no_rate_calls_but_reports_would_be_updated():
    tracks = [make_track("a", 6.0), make_track("b", None)]
    plex = make_plex(tracks)

    result = update_playlist_ratings(plex, "MyList", 4.0, dry_run=True)

    tracks[0].rate.assert_not_called()
    tracks[1].rate.assert_not_called()
    assert result == {"total": 2, "updated": 2, "skipped": 0, "failed": []}


def test_dry_run_prints_dry_run_prefix(capsys):
    tracks = [make_track("a", None)]
    plex = make_plex(tracks)

    update_playlist_ratings(plex, "MyList", 4.0, dry_run=True)

    captured = capsys.readouterr()
    assert "[DRY RUN]" in captured.out


def test_non_dry_run_does_not_print_dry_run_prefix(capsys):
    tracks = [make_track("a", None)]
    plex = make_plex(tracks)

    update_playlist_ratings(plex, "MyList", 4.0, dry_run=False)

    captured = capsys.readouterr()
    assert "[DRY RUN]" not in captured.out


# ---------------------------------------------------------------------------
# partial failure handling
# ---------------------------------------------------------------------------

def test_partial_failure_is_reported_and_does_not_abort_remaining_tracks():
    tracks = [make_track("a", None), make_track("b", None), make_track("c", None)]
    tracks[1].rate.side_effect = BadRequest("server rejected rating")
    plex = make_plex(tracks)

    result = update_playlist_ratings(plex, "MyList", 5.0)

    assert result["total"] == 3
    assert result["updated"] == 2
    assert result["failed"] == [("b", "server rejected rating")]
    tracks[2].rate.assert_called_once_with(10)


def test_failures_are_printed_to_stderr(capsys):
    tracks = [make_track("a", None)]
    tracks[0].rate.side_effect = BadRequest("nope")
    plex = make_plex(tracks)

    update_playlist_ratings(plex, "MyList", 5.0)

    captured = capsys.readouterr()
    assert "a" in captured.err
    assert "nope" in captured.err


# ---------------------------------------------------------------------------
# --backup-ratings-to
# ---------------------------------------------------------------------------

def test_backup_ratings_to_writes_json_snapshot_before_changes(tmp_path):
    tracks = [make_track("a", 6.0, rating_key=101), make_track("b", None, rating_key=102)]
    plex = make_plex(tracks)
    backup_path = tmp_path / "backup.json"

    update_playlist_ratings(plex, "MyList", 4.0, backup_ratings_to=str(backup_path))

    data = json.loads(backup_path.read_text())
    assert data == {
        "101": {"title": "a", "userRating": 6.0},
        "102": {"title": "b", "userRating": None},
    }


def test_backup_ratings_to_creates_parent_directories(tmp_path):
    tracks = [make_track("a", None, rating_key=1)]
    plex = make_plex(tracks)
    backup_path = tmp_path / "nested" / "dir" / "backup.json"

    update_playlist_ratings(plex, "MyList", 4.0, backup_ratings_to=str(backup_path))

    assert backup_path.exists()


def test_no_backup_file_written_when_flag_omitted(tmp_path):
    tracks = [make_track("a", None)]
    plex = make_plex(tracks)

    update_playlist_ratings(plex, "MyList", 4.0)

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# CLI: parse_args()
# ---------------------------------------------------------------------------

def test_parse_args_requires_playlist_and_stars():
    with pytest.raises(SystemExit):
        push_mod.parse_args(["--url", "http://localhost:32400"])


def test_parse_args_requires_url_unless_credential_file_given():
    with pytest.raises(SystemExit):
        push_mod.parse_args(["--playlist", "MyList", "--stars", "4"])


def test_parse_args_allows_credential_file_without_url():
    args = push_mod.parse_args(
        ["--playlist", "MyList", "--stars", "4", "--credential-file", "creds.json"]
    )
    assert args.credential_file == "creds.json"
    assert args.url is None


def test_parse_args_flags_default_to_false():
    args = push_mod.parse_args(
        ["--playlist", "MyList", "--stars", "4", "--url", "http://localhost:32400"]
    )
    assert args.force_ratings is False
    assert args.dry_run is False
    assert args.backup_ratings_to is None


def test_parse_args_sets_flags_when_given():
    args = push_mod.parse_args(
        [
            "--playlist", "MyList",
            "--stars", "4",
            "--url", "http://localhost:32400",
            "--force-ratings",
            "--dry-run",
            "--backup-ratings-to", "backup.json",
        ]
    )
    assert args.force_ratings is True
    assert args.dry_run is True
    assert args.backup_ratings_to == "backup.json"


# ---------------------------------------------------------------------------
# CLI: main()
# ---------------------------------------------------------------------------

def test_main_success_returns_0(monkeypatch):
    tracks = [make_track("a", None)]
    plex = make_plex(tracks)

    monkeypatch.setattr(
        push_mod,
        "get_plex_credentials",
        lambda url, username, password: {"url": url, "token": "tok"},
    )
    monkeypatch.setattr(push_mod, "PlexServer", lambda url, token: plex)

    exit_code = push_mod.main(
        [
            "--playlist", "MyList",
            "--stars", "5",
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
        ]
    )

    assert exit_code == 0
    tracks[0].rate.assert_called_once_with(10)


def test_main_skips_login_with_credential_file(monkeypatch, tmp_path):
    from get_plex_credentials import save_plex_credentials

    creds_path = tmp_path / "creds.json"
    save_plex_credentials({"url": "http://localhost:32400", "token": "cached-tok"}, str(creds_path))

    tracks = [make_track("a", None)]
    plex = make_plex(tracks)

    def fail_if_called(*args, **kwargs):
        pytest.fail("get_plex_credentials should not be called when --credential-file is given")

    monkeypatch.setattr(push_mod, "get_plex_credentials", fail_if_called)
    monkeypatch.setattr(push_mod, "PlexServer", lambda url, token: plex)

    exit_code = push_mod.main(
        ["--playlist", "MyList", "--stars", "5", "--credential-file", str(creds_path)]
    )

    assert exit_code == 0
    tracks[0].rate.assert_called_once_with(10)


def test_main_returns_1_on_playlist_not_found(monkeypatch):
    plex = MagicMock()
    plex.playlist.side_effect = NotFound('Unable to find playlist with title "Nope".')

    monkeypatch.setattr(
        push_mod,
        "get_plex_credentials",
        lambda url, username, password: {"url": url, "token": "tok"},
    )
    monkeypatch.setattr(push_mod, "PlexServer", lambda url, token: plex)

    exit_code = push_mod.main(
        [
            "--playlist", "Nope",
            "--stars", "5",
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
        ]
    )

    assert exit_code == 1


def test_main_returns_1_on_connection_error(monkeypatch):
    """A plain network failure constructing PlexServer isn't a PlexApiException,
    but main() should still exit 1 with a clean message instead of a raw traceback."""
    def fake_plex_server(url, token):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(
        push_mod,
        "get_plex_credentials",
        lambda url, username, password: {"url": url, "token": "tok"},
    )
    monkeypatch.setattr(push_mod, "PlexServer", fake_plex_server)

    exit_code = push_mod.main(
        [
            "--playlist", "MyList",
            "--stars", "5",
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
        ]
    )

    assert exit_code == 1


def test_main_returns_1_on_invalid_rating(monkeypatch):
    monkeypatch.setattr(
        push_mod,
        "get_plex_credentials",
        lambda url, username, password: {"url": url, "token": "tok"},
    )
    monkeypatch.setattr(push_mod, "PlexServer", lambda url, token: MagicMock())

    exit_code = push_mod.main(
        [
            "--playlist", "MyList",
            "--stars", "99",
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
        ]
    )

    assert exit_code == 1

