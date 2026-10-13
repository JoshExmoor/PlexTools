import json
import platform

import pytest
from plexapi.exceptions import PlexApiException, Unauthorized

import get_plex_credentials as gpc


# ---------------------------------------------------------------------------
# get_plex_credentials()
# ---------------------------------------------------------------------------

class FakeAccount:
    def __init__(self, token="fake-token-123"):
        self.authenticationToken = token


def test_get_plex_credentials_uses_provided_username_and_password(monkeypatch):
    """Should not prompt when username/password are both supplied."""
    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("abc123"))
    monkeypatch.setattr(gpc, "PlexServer", lambda url, token: object())
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("should not prompt for username"))
    monkeypatch.setattr(gpc, "getpass", lambda *_: pytest.fail("should not prompt for password"))

    result = gpc.get_plex_credentials("http://localhost:32400", username="joe", password="secret")

    assert result == {"url": "http://localhost:32400", "token": "abc123"}


def test_get_plex_credentials_prompts_for_missing_username_and_password(monkeypatch):
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

    result = gpc.get_plex_credentials("http://localhost:32400")

    assert prompted == {"username": True, "password": True}
    assert result["token"] == "tok"


def test_get_plex_credentials_raises_unauthorized_on_bad_login(monkeypatch):
    def fake_account(u, p):
        raise Unauthorized("bad creds")

    monkeypatch.setattr(gpc, "MyPlexAccount", fake_account)

    with pytest.raises(Unauthorized):
        gpc.get_plex_credentials("http://localhost:32400", username="joe", password="wrong")


def test_get_plex_credentials_wraps_other_myplex_errors(monkeypatch):
    def fake_account(u, p):
        raise PlexApiException("network blip")

    monkeypatch.setattr(gpc, "MyPlexAccount", fake_account)

    with pytest.raises(RuntimeError, match="Failed to authenticate with Plex"):
        gpc.get_plex_credentials("http://localhost:32400", username="joe", password="pw")


def test_get_plex_credentials_raises_unauthorized_when_server_rejects_token(monkeypatch):
    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("tok"))

    def fake_plex_server(url, token):
        raise Unauthorized("server rejected token")

    monkeypatch.setattr(gpc, "PlexServer", fake_plex_server)

    with pytest.raises(Unauthorized, match="rejected by server"):
        gpc.get_plex_credentials("http://localhost:32400", username="joe", password="pw")


def test_get_plex_credentials_wraps_other_server_connection_errors(monkeypatch):
    monkeypatch.setattr(gpc, "MyPlexAccount", lambda u, p: FakeAccount("tok"))

    def fake_plex_server(url, token):
        raise PlexApiException("timed out")

    monkeypatch.setattr(gpc, "PlexServer", fake_plex_server)

    with pytest.raises(RuntimeError, match="Failed to connect to Plex server"):
        gpc.get_plex_credentials("http://localhost:32400", username="joe", password="pw")


# ---------------------------------------------------------------------------
# save_plex_credentials() / retrieve_plex_credentials()
# ---------------------------------------------------------------------------

def test_save_and_retrieve_round_trip(tmp_path):
    creds = {"url": "http://localhost:32400", "token": "abc123"}
    location = tmp_path / "creds.json"

    gpc.save_plex_credentials(creds, str(location))
    loaded = gpc.retrieve_plex_credentials(str(location))

    assert loaded == creds


def test_save_plex_credentials_creates_parent_directories(tmp_path):
    creds = {"url": "http://localhost:32400", "token": "abc123"}
    location = tmp_path / "nested" / "dir" / "creds.json"

    gpc.save_plex_credentials(creds, str(location))

    assert location.exists()
    assert json.loads(location.read_text()) == creds


def test_save_plex_credentials_writes_readable_json(tmp_path):
    creds = {"url": "http://localhost:32400", "token": "abc123"}
    location = tmp_path / "creds.json"

    gpc.save_plex_credentials(creds, str(location))

    with open(location, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk == creds


@pytest.mark.skipif(platform.system() == "Windows", reason="POSIX chmod semantics only apply on POSIX")
def test_save_plex_credentials_restricts_permissions_on_posix(tmp_path):
    import stat

    creds = {"url": "http://localhost:32400", "token": "abc123"}
    location = tmp_path / "creds.json"

    gpc.save_plex_credentials(creds, str(location))

    mode = stat.S_IMODE(location.stat().st_mode)
    assert mode == 0o600


def test_retrieve_plex_credentials_missing_file_raises(tmp_path):
    missing = tmp_path / "does_not_exist.json"

    with pytest.raises(FileNotFoundError):
        gpc.retrieve_plex_credentials(str(missing))


def test_retrieve_plex_credentials_invalid_json_raises(tmp_path):
    location = tmp_path / "bad.json"
    location.write_text("not valid json {{{")

    with pytest.raises(ValueError, match="not valid JSON"):
        gpc.retrieve_plex_credentials(str(location))


def test_retrieve_plex_credentials_missing_keys_raises(tmp_path):
    location = tmp_path / "incomplete.json"
    location.write_text(json.dumps({"url": "http://localhost:32400"}))

    with pytest.raises(ValueError, match="missing required"):
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
            "--save-to", "out.json",
        ]
    )
    assert args.url == "http://localhost:32400"
    assert args.username == "joe"
    assert args.password == "secret"
    assert args.save_to == "out.json"


def test_main_success_prints_url_and_token(monkeypatch, capsys):
    monkeypatch.setattr(
        gpc,
        "get_plex_credentials",
        lambda url, username, password: {"url": url, "token": "tok123"},
    )

    exit_code = gpc.main(["--url", "http://localhost:32400", "--username", "joe", "--password", "pw"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "tok123" in captured.out
    assert "http://localhost:32400" in captured.out


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


def test_main_returns_1_and_prints_error_on_unauthorized(monkeypatch, capsys):
    def fake_get_plex_credentials(url, username, password):
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


def test_main_saves_credentials_when_save_to_given(monkeypatch, tmp_path):
    save_location = tmp_path / "saved.json"
    monkeypatch.setattr(
        gpc,
        "get_plex_credentials",
        lambda url, username, password: {"url": url, "token": "tok456"},
    )

    exit_code = gpc.main(
        [
            "--url", "http://localhost:32400",
            "--username", "joe",
            "--password", "pw",
            "--save-to", str(save_location),
        ]
    )

    assert exit_code == 0
    assert json.loads(save_location.read_text()) == {"url": "http://localhost:32400", "token": "tok456"}
