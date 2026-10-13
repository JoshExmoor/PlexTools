import argparse
import json
import os
import sys
from getpass import getpass
from pathlib import Path

import requests
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from plexapi.exceptions import PlexApiException, Unauthorized

# Default credential cache location: ~/.creds/plex_creds.json on POSIX, and
# the equivalent under the user's home directory on Windows
# (e.g. C:\Users\<you>\.creds\plex_creds.json). Path.home() resolves correctly
# on both platforms.
DEFAULT_CREDENTIAL_FILE = str(Path.home() / ".creds" / "plex_creds.json")


def _validate_token(url: str, token: str) -> None:
    """Raises Unauthorized/RuntimeError if `token` does not work against `url`."""
    try:
        PlexServer(url, token)
    except Unauthorized as exc:
        raise Unauthorized(
            f"Authenticated with Plex, but token was rejected by server at {url}."
        ) from exc
    except (PlexApiException, requests.exceptions.RequestException) as exc:
        raise RuntimeError(f"Failed to connect to Plex server at {url}: {exc}") from exc


def get_plex_credentials(
    url: str,
    username: str = None,
    password: str = None,
    auto_cache: bool = True,
    cache_location: str = None,
) -> dict:
    """Returns {"url": url, "token": token} for the given Plex server.

    If auto_cache is True (default), this first checks cache_location (default
    ~/.creds/plex_creds.json) for a previously saved token for `url` and
    validates it against the server. If that token is present and still
    valid, it's returned immediately with no login prompt. Otherwise (no
    cached token, or the server rejects it), this falls back to the normal
    username/password login flow below, and on success saves the new token
    back into cache_location for next time.
    """
    cache_location = cache_location or DEFAULT_CREDENTIAL_FILE

    if auto_cache:
        try:
            cached = retrieve_plex_credentials(cache_location, url=url)
            _validate_token(url, cached["token"])
            return cached
        except (FileNotFoundError, ValueError, KeyError):
            pass  # No usable cached entry for this url; fall through to login.
        except (Unauthorized, RuntimeError):
            pass  # Cached token exists but was rejected; fall through to login.

    plex_credentials = {
        "url": url,
        "token": None,
    }

    # If username and password are not passed, ask the user to enter them via console input:
    if not username:
        username = input("Plex username: ")
    if not password:
        password = getpass("Plex password: ")

    # Once you have credentials, use PlexAPI to attempt to login to the plex instance
    # and retrieve the token and then return it below.
    try:
        account = MyPlexAccount(username, password)
    except Unauthorized as exc:
        raise Unauthorized("Invalid Plex username or password.") from exc
    except PlexApiException as exc:
        raise RuntimeError(f"Failed to authenticate with Plex: {exc}") from exc

    token = account.authenticationToken

    # Validate the token actually works against the target server/url.
    _validate_token(url, token)

    plex_credentials["token"] = token

    if auto_cache:
        save_plex_credentials(plex_credentials, cache_location)

    return plex_credentials


def save_plex_credentials(credentials: dict, location: str = None) -> None:
    """Saves {"url", "token"} credentials into the JSON cache file at location.

    The cache file holds credentials for multiple servers, keyed by url:
        {"<url>": {"token": "<token>"}, "<url2>": {"token": "<token2>"}}
    This reads any existing cache, adds/replaces only the entry for
    credentials["url"], and writes the merged result back - other servers'
    saved credentials are preserved.

    location defaults to ~/.creds/plex_creds.json (DEFAULT_CREDENTIAL_FILE).
    """
    location = location or DEFAULT_CREDENTIAL_FILE
    url = credentials.get("url")
    token = credentials.get("token")
    if not url or not token:
        raise ValueError("credentials must include a non-empty 'url' and 'token'.")

    directory = os.path.dirname(os.path.abspath(location))
    if directory:
        os.makedirs(directory, exist_ok=True)

    all_credentials = _load_credential_store(location)
    all_credentials[url] = {"token": token}

    with open(location, "w", encoding="utf-8") as credentials_file:
        json.dump(all_credentials, credentials_file, indent=2)


def _load_credential_store(location: str) -> dict:
    """Loads the raw multi-server {url: {token}} store, or {} if absent/empty."""
    if not os.path.isfile(location):
        return {}

    with open(location, "r", encoding="utf-8") as credentials_file:
        raw = credentials_file.read().strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Credential file at {location} is not valid JSON.") from exc


def retrieve_plex_credentials(location: str = None, url: str = None) -> dict:
    """Returns {"url": url, "token": token} for one server from the cache file.

    location defaults to ~/.creds/plex_creds.json (DEFAULT_CREDENTIAL_FILE).

    If `url` is given, returns that server's entry (raises KeyError if it's
    not in the file). If `url` is omitted, the file must contain exactly one
    server's credentials - that single entry is returned, matching the
    original single-server --credential-file behavior. If it contains more
    than one, a ValueError asks the caller to specify which url to use.
    """
    location = location or DEFAULT_CREDENTIAL_FILE
    if not os.path.isfile(location):
        raise FileNotFoundError(f"No credential file found at {location}")

    all_credentials = _load_credential_store(location)

    if url is not None:
        if url not in all_credentials:
            raise KeyError(f"No saved credentials for {url} in {location}")
        entry = all_credentials[url]
        if "token" not in entry:
            raise ValueError(f"Credential entry for {url} in {location} is missing a 'token'.")
        return {"url": url, "token": entry["token"]}

    if not all_credentials:
        raise ValueError(f"Credential file at {location} has no saved servers.")
    if len(all_credentials) > 1:
        urls = ", ".join(all_credentials.keys())
        raise ValueError(
            f"Credential file at {location} has credentials for multiple servers "
            f"({urls}). Specify which url to use."
        )

    (only_url, entry), = all_credentials.items()
    if "token" not in entry:
        raise ValueError(f"Credential entry for {only_url} in {location} is missing a 'token'.")
    return {"url": only_url, "token": entry["token"]}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authenticate with Plex and retrieve a server auth token.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "Base URL of the Plex server, e.g. http://localhost:32400. "
            "Required unless --credential-file is given (without --url, the "
            "credential file must contain exactly one saved server)."
        ),
    )
    parser.add_argument(
        "--username",
        default=None,
        help="Plex username. Prompted via console input if omitted.",
    )
    parser.add_argument(
        "--password",
        default=None,
        help=(
            "Plex password. Prompted via a hidden console prompt if omitted. "
            "NOTE: passing this on the command line may expose it in shell "
            "history and process listings; prefer omitting it."
        ),
    )
    parser.add_argument(
        "--credential-file",
        default=None,
        help=(
            "Path to a JSON credential file previously saved with --save-to "
            "(or the default cache). If given, login is skipped and "
            "credentials are loaded from this file instead. Combine with "
            "--url to pick one server out of a file with multiple saved "
            "servers."
        ),
    )
    parser.add_argument(
        "--save-to",
        default=None,
        help="Path to save the retrieved credentials to as a JSON file cache.",
    )
    parser.add_argument(
        "--no-auto-cache",
        action="store_true",
        help=(
            "Disable automatically checking/saving credentials in the "
            f"default cache file ({DEFAULT_CREDENTIAL_FILE}) during login."
        ),
    )
    args = parser.parse_args(argv)

    if not args.credential_file and not args.url:
        parser.error("--url is required unless --credential-file is given.")

    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        if args.credential_file:
            plex_credentials = retrieve_plex_credentials(args.credential_file, url=args.url)
        else:
            plex_credentials = get_plex_credentials(
                url=args.url,
                username=args.username,
                password=args.password,
                auto_cache=not args.no_auto_cache,
            )

        if args.save_to:
            save_plex_credentials(plex_credentials, args.save_to)
    except (
        Unauthorized,
        PlexApiException,
        requests.exceptions.RequestException,
        RuntimeError,
        FileNotFoundError,
        KeyError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"url:   {plex_credentials['url']}")
    print(f"token: {plex_credentials['token']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
