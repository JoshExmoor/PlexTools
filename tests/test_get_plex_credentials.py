import json

import pytest
import requests
from plexapi.exceptions import PlexApiException, Unauthorized

import get_plex_credentials as gpc


# ---------------------------------------------------------------------------
# get_plex_credentials()
# ---------------------------------------------------------------------------

class FakeAccount:
    def __init__(self, token="fake-token-123"):
        self.authenticationToken = token


def test_get_plex_credentials_uses_provided_username_and_password(monkeypatch, tmp_path):
    """Should not prompt when username/password are both supplied."""
    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("abc123"))
    monkeypatch.setattr(gpc, "PlexServer", lambda url, token: object())
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("should not prompt for username"))
    monkeypatch.setattr(gpc, "getpass", lambda *_: pytest.fail("should not prompt for password"))

    result = gpc.get_plex_credentials(
        "http://localhost:32400",
        username="joe",
        password="secret",
        cache_location=str(tmp_path / "creds.json"),
    )

    assert result == {"url": "http://localhost:32400", "token": "abc123"}


def test_get_plex_credentials_prompts_for_missing_username_and_password(monkeypatch, tmp_path):
    prompted = {"username": False, "password": False}

    def fake_input(prompt):
        prompted["username"] = True
        return "joe"

    def fake_getpass(prompt):
        prompted["password"] = True
        return "secret"

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(gpc, "getpass", fake_getpass)
    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("tok"))
    monkeypatch.setattr(gpc, "PlexServer", lambda url, token: object())

    result = gpc.get_plex_credentials("http://localhost:32400", cache_location=str(tmp_path / "creds.json"))

    assert prompted == {"username": True, "password": True}
    assert result["token"] == "tok"


def test_get_plex_credentials_raises_unauthorized_on_bad_login(monkeypatch, tmp_path):
    def fake_account(u, p):
        raise Unauthorized("bad creds")

    monkeypatch.setattr(gpc, "MyPlexAccount", fake_account)

    with pytest.raises(Unauthorized):
        gpc.get_plex_credentials(
            "http://localhost:32400",
            username="joe",
            password="wrong",
            cache_location=str(tmp_path / "creds.json"),
        )


def test_get_plex_credentials_wraps_other_myplex_errors(monkeypatch, tmp_path):
    def fake_account(u, p):
        raise PlexApiException("network blip")

    monkeypatch.setattr(gpc, "MyPlexAccount", fake_account)

    with pytest.raises(RuntimeError, match="Failed to authenticate with Plex"):
        gpc.get_plex_credentials(
            "http://localhost:32400", username="joe", password="pw", cache_location=str(tmp_path / "creds.json")
        )


def test_get_plex_credentials_raises_unauthorized_when_server_rejects_token(monkeypatch, tmp_path):
    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("tok"))

    def fake_plex_server(url, token):
        raise Unauthorized("server rejected token")

    monkeypatch.setattr(gpc, "PlexServer", fake_plex_server)

    with pytest.raises(Unauthorized, match="rejected by server"):
        gpc.get_plex_credentials(
            "http://localhost:32400", username="joe", password="pw", cache_location=str(tmp_path / "creds.json")
        )


def test_get_plex_credentials_wraps_other_server_connection_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("tok"))

    def fake_plex_server(url, token):
        raise PlexApiException("timed out")

    monkeypatch.setattr(gpc, "PlexServer", fake_plex_server)

    with pytest.raises(RuntimeError, match="Failed to connect to Plex server"):
        gpc.get_plex_credentials(
            "http://localhost:32400", username="joe", password="pw", cache_location=str(tmp_path / "creds.json")
        )


def test_get_plex_credentials_wraps_connection_error_from_server_validation(monkeypatch, tmp_path):
    """A plain network failure (e.g. unreachable host) isn't a PlexApiException,
    but should still be wrapped into a RuntimeError rather than propagating raw."""
    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("tok"))

    def fake_plex_server(url, token):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(gpc, "PlexServer", fake_plex_server)

    with pytest.raises(RuntimeError, match="Failed to connect to Plex server"):
        gpc.get_plex_credentials(
            "http://localhost:32400", username="joe", password="pw", cache_location=str(tmp_path / "creds.json")
        )


def test_get_plex_credentials_disables_cache_when_auto_cache_false(monkeypatch, tmp_path):
    cache_location = tmp_path / "creds.json"
    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("tok"))
    monkeypatch.setattr(gpc, "PlexServer", lambda url, token: object())

    gpc.get_plex_credentials(
        "http://localhost:32400",
        username="joe",
        password="pw",
        auto_cache=False,
        cache_location=str(cache_location),
    )

    assert not cache_location.exists()


def test_get_plex_credentials_saves_to_cache_after_fresh_login(monkeypatch, tmp_path):
    cache_location = tmp_path / "creds.json"
    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("fresh-tok"))
    monkeypatch.setattr(gpc, "PlexServer", lambda url, token: object())

    gpc.get_plex_credentials(
        "http://localhost:32400",
        username="joe",
        password="pw",
        cache_location=str(cache_location),
    )

    assert json.loads(cache_location.read_text()) == {"http://localhost:32400": {"token": "fresh-tok"}}


def test_get_plex_credentials_uses_valid_cached_token_without_prompting(monkeypatch, tmp_path):
    cache_location = tmp_path / "creds.json"
    gpc.save_plex_credentials({"url": "http://localhost:32400", "token": "cached-tok"}, str(cache_location))

    def fail_if_called(*args, **kwargs):
        pytest.fail("MyPlexAccount should not be called when a valid cached token exists")

    monkeypatch.setattr(gpc, "MyPlexAccount", fail_if_called)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("should not prompt"))
    monkeypatch.setattr(gpc, "PlexServer", lambda url, token: object())

    result = gpc.get_plex_credentials("http://localhost:32400", cache_location=str(cache_location))

    assert result == {"url": "http://localhost:32400", "token": "cached-tok"}


def test_get_plex_credentials_falls_back_to_login_when_cached_token_rejected(monkeypatch, tmp_path):
    cache_location = tmp_path / "creds.json"
    gpc.save_plex_credentials({"url": "http://localhost:32400", "token": "stale-tok"}, str(cache_location))

    def flaky_plex_server(url, token):
        if token == "stale-tok":
            raise Unauthorized("rejected")
        return object()

    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("rotated-tok"))
    monkeypatch.setattr(gpc, "PlexServer", flaky_plex_server)

    result = gpc.get_plex_credentials(
        "http://localhost:32400", username="joe", password="pw", cache_location=str(cache_location)
    )

    assert result == {"url": "http://localhost:32400", "token": "rotated-tok"}
    assert json.loads(cache_location.read_text())["http://localhost:32400"]["token"] == "rotated-tok"


def test_get_plex_credentials_falls_back_to_login_when_url_not_cached(monkeypatch, tmp_path):
    cache_location = tmp_path / "creds.json"
    gpc.save_plex_credentials({"url": "http://other-server:32400", "token": "other-tok"}, str(cache_location))

    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("new-tok"))
    monkeypatch.setattr(gpc, "PlexServer", lambda url, token: object())

    result = gpc.get_plex_credentials(
        "http://localhost:32400", username="joe", password="pw", cache_location=str(cache_location)
    )

    assert result == {"url": "http://localhost:32400", "token": "new-tok"}
    # The other server's cached entry should still be there, untouched.
    data = json.loads(cache_location.read_text())
    assert data["http://other-server:32400"]["token"] == "other-tok"
    assert data["http://localhost:32400"]["token"] == "new-tok"


# ---------------------------------------------------------------------------
# save_plex_credentials() / retrieve_plex_credentials() - multi-server cache
# ---------------------------------------------------------------------------

def test_save_and_retrieve_round_trip_single_server(tmp_path):
    creds = {"url": "http://localhost:32400", "token": "abc123"}
    location = tmp_path / "creds.json"

    gpc.save_plex_credentials(creds, str(location))
    loaded = gpc.retrieve_plex_credentials(str(location))

    assert loaded == creds


def test_save_plex_credentials_stores_multiple_servers_keyed_by_url(tmp_path):
    location = tmp_path / "creds.json"

    gpc.save_plex_credentials({"url": "http://server1:32400", "token": "tok1"}, str(location))
    gpc.save_plex_credentials({"url": "http://server2:32400", "token": "tok2"}, str(location))

    on_disk = json.loads(location.read_text())
    assert on_disk == {
        "http://server1:32400": {"token": "tok1"},
        "http://server2:32400": {"token": "tok2"},
    }


def test_save_plex_credentials_replaces_only_matching_url(tmp_path):
    location = tmp_path / "creds.json"
    gpc.save_plex_credentials({"url": "http://server1:32400", "token": "tok1"}, str(location))
    gpc.save_plex_credentials({"url": "http://server2:32400", "token": "tok2"}, str(location))

    gpc.save_plex_credentials({"url": "http://server1:32400", "token": "tok1-updated"}, str(location))

    on_disk = json.loads(location.read_text())
    assert on_disk["http://server1:32400"]["token"] == "tok1-updated"
    assert on_disk["http://server2:32400"]["token"] == "tok2"


def test_save_plex_credentials_creates_parent_directories(tmp_path):
    creds = {"url": "http://localhost:32400", "token": "abc123"}
    location = tmp_path / "nested" / "dir" / "creds.json"

    gpc.save_plex_credentials(creds, str(location))

    assert location.exists()
    assert json.loads(location.read_text()) == {"http://localhost:32400": {"token": "abc123"}}


def test_save_plex_credentials_requires_url_and_token(tmp_path):
    location = tmp_path / "creds.json"

    with pytest.raises(ValueError):
        gpc.save_plex_credentials({"url": "http://localhost:32400", "token": None}, str(location))

    with pytest.raises(ValueError):
        gpc.save_plex_credentials({"url": "", "token": "tok"}, str(location))


def test_save_plex_credentials_defaults_to_default_credential_file(monkeypatch, tmp_path):
    default_location = tmp_path / "default_creds.json"
    monkeypatch.setattr(gpc, "DEFAULT_CREDENTIAL_FILE", str(default_location))

    gpc.save_plex_credentials({"url": "http://localhost:32400", "token": "tok"})

    assert default_location.exists()


def test_retrieve_plex_credentials_by_url(tmp_path):
    location = tmp_path / "creds.json"
    gpc.save_plex_credentials({"url": "http://server1:32400", "token": "tok1"}, str(location))
    gpc.save_plex_credentials({"url": "http://server2:32400", "token": "tok2"}, str(location))

    result = gpc.retrieve_plex_credentials(str(location), url="http://server2:32400")

    assert result == {"url": "http://server2:32400", "token": "tok2"}


def test_retrieve_plex_credentials_unknown_url_raises_key_error(tmp_path):
    location = tmp_path / "creds.json"
    gpc.save_plex_credentials({"url": "http://server1:32400", "token": "tok1"}, str(location))

    with pytest.raises(KeyError):
        gpc.retrieve_plex_credentials(str(location), url="http://unknown:32400")


def test_retrieve_plex_credentials_without_url_requires_single_entry(tmp_path):
    location = tmp_path / "creds.json"
    gpc.save_plex_credentials({"url": "http://server1:32400", "token": "tok1"}, str(location))
    gpc.save_plex_credentials({"url": "http://server2:32400", "token": "tok2"}, str(location))

    with pytest.raises(ValueError, match="multiple servers"):
        gpc.retrieve_plex_credentials(str(location))


def test_retrieve_plex_credentials_missing_file_raises(tmp_path):
    missing = tmp_path / "does_not_exist.json"

    with pytest.raises(FileNotFoundError):
        gpc.retrieve_plex_credentials(str(missing))


def test_retrieve_plex_credentials_invalid_json_raises(tmp_path):
    location = tmp_path / "bad.json"
    location.write_text("not valid json {{{")

    with pytest.raises(ValueError, match="not valid JSON"):
        gpc.retrieve_plex_credentials(str(location))


def test_retrieve_plex_credentials_empty_store_raises(tmp_path):
    location = tmp_path / "empty.json"
    location.write_text("{}")

    with pytest.raises(ValueError, match="no saved servers"):
        gpc.retrieve_plex_credentials(str(location))


def test_retrieve_plex_credentials_entry_missing_token_raises(tmp_path):
    location = tmp_path / "incomplete.json"
    location.write_text(json.dumps({"http://localhost:32400": {}}))

    with pytest.raises(ValueError, match="missing a 'token'"):
        gpc.retrieve_plex_credentials(str(location))


# ---------------------------------------------------------------------------
# CLI: parse_args() / main()
# ---------------------------------------------------------------------------

def test_parse_args_requires_url_unless_credential_file_given():
    with pytest.raises(SystemExit):
        gpc.parse_args([])


def test_parse_args_allows_credential_file_without_url():
    args = gpc.parse_args(["--credential-file", "creds.json"])
    assert args.credential_file == "creds.json"
    assert args.url is None


def test_parse_args_parses_all_options():
    args = gpc.parse_args(
        [
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "secret",
            "--save-creds", "out.json",
            "--no-auto-cache",
        ]
    )
    assert args.url == "http://localhost:32400"
    assert args.username == "joe"
    assert args.password == "secret"
    assert args.save_creds == "out.json"
    assert args.no_auto_cache is True


def test_parse_args_no_auto_cache_defaults_to_false():
    args = gpc.parse_args(["--url", "http://localhost:32400"])
    assert args.no_auto_cache is False


def test_main_success_prints_url_and_token(monkeypatch, capsys):
    monkeypatch.setattr(
        gpc,
        "get_plex_credentials",
        lambda url, username, password, auto_cache: {"url": url, "token": "tok123"},
    )

    exit_code = gpc.main(["--url", "http://localhost:32400", "--username", "joe", "--password", "pw"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "tok123" in captured.out
    assert "http://localhost:32400" in captured.out


def test_main_passes_no_auto_cache_through(monkeypatch):
    captured_kwargs = {}

    def fake_get_plex_credentials(url, username, password, auto_cache):
        captured_kwargs["auto_cache"] = auto_cache
        return {"url": url, "token": "tok"}

    monkeypatch.setattr(gpc, "get_plex_credentials", fake_get_plex_credentials)

    gpc.main(
        [
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
            "--no-auto-cache",
        ]
    )

    assert captured_kwargs["auto_cache"] is False


def test_main_skips_login_with_credential_file(monkeypatch, tmp_path, capsys):
    location = tmp_path / "creds.json"
    gpc.save_plex_credentials({"url": "http://localhost:32400", "token": "cached-tok"}, str(location))

    def fail_if_called(*args, **kwargs):
        pytest.fail("get_plex_credentials should not be called when --credential-file is given")

    monkeypatch.setattr(gpc, "get_plex_credentials", fail_if_called)

    exit_code = gpc.main(["--credential-file", str(location)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "cached-tok" in captured.out


def test_main_credential_file_with_url_picks_matching_server(monkeypatch, tmp_path, capsys):
    location = tmp_path / "creds.json"
    gpc.save_plex_credentials({"url": "http://server1:32400", "token": "tok1"}, str(location))
    gpc.save_plex_credentials({"url": "http://server2:32400", "token": "tok2"}, str(location))

    exit_code = gpc.main(["--credential-file", str(location), "--url", "http://server2:32400"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "tok2" in captured.out


def test_main_returns_1_and_prints_error_on_unauthorized(monkeypatch, capsys):
    def fake_get_plex_credentials(url, username, password, auto_cache):
        raise Unauthorized("nope")

    monkeypatch.setattr(gpc, "get_plex_credentials", fake_get_plex_credentials)

    exit_code = gpc.main(["--url", "http://localhost:32400", "--username", "joe", "--password", "pw"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error" in captured.err


def test_main_returns_1_on_missing_credential_file(capsys, tmp_path):
    missing = tmp_path / "nope.json"

    exit_code = gpc.main(["--credential-file", str(missing)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error" in captured.err


def test_main_saves_credentials_when_save_creds_given(monkeypatch, tmp_path):
    save_location = tmp_path / "saved.json"
    monkeypatch.setattr(
        gpc,
        "get_plex_credentials",
        lambda url, username, password, auto_cache: {"url": url, "token": "tok456"},
    )

    exit_code = gpc.main(
        [
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
            "--save-creds", str(save_location),
        ]
    )

    assert exit_code == 0
    assert json.loads(save_location.read_text()) == {"http://localhost:32400": {"token": "tok456"}}
