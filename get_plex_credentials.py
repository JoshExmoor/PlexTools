import argparse
import json
import os
import platform
import subprocess
import sys
from getpass import getpass

from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from plexapi.exceptions import PlexApiException, Unauthorized


def get_plex_credentials(url: str, username: str = None, password: str = None) -> dict:
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
    try:
        PlexServer(url, token)
    except Unauthorized as exc:
        raise Unauthorized(
            f"Authenticated with Plex, but token was rejected by server at {url}."
        ) from exc
    except PlexApiException as exc:
        raise RuntimeError(f"Failed to connect to Plex server at {url}: {exc}") from exc

    plex_credentials["token"] = token

    return plex_credentials


def _restrict_file_permissions(location: str) -> None:
    """Best-effort lockdown of a credential file to the owning user only.

    The credentials file contains a Plex auth token, so it should not be
    world/group readable. POSIX permissions apply directly; Windows has no
    chmod equivalent, so icacls is used instead. Failures are non-fatal,
    since the file was still written successfully.
    """
    try:
        if platform.system() == "Windows":
            subprocess.run(
                [
                    "icacls",
                    location,
                    "/inheritance:r",
                    "/grant:r",
                    f"{os.environ.get('USERNAME', '')}:F",
                ],
                check=True,
                capture_output=True,
            )
        else:
            os.chmod(location, 0o600)
    except (subprocess.SubprocessError, OSError) as exc:
        print(
            f"Warning: could not restrict permissions on {location}: {exc}",
            file=sys.stderr,
        )


def save_plex_credentials(credentials: dict, location: str) -> None:
    """Saves Plex credentials (url/token) to a JSON file cache at location."""
    directory = os.path.dirname(os.path.abspath(location))
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(location, "w", encoding="utf-8") as credentials_file:
        json.dump(credentials, credentials_file, indent=2)

    _restrict_file_permissions(location)


def retrieve_plex_credentials(location: str) -> dict:
    """Opens the JSON file cache at location and returns the credentials."""
    if not os.path.isfile(location):
        raise FileNotFoundError(f"No credential file found at {location}")

    with open(location, "r", encoding="utf-8") as credentials_file:
        try:
            credentials = json.load(credentials_file)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Credential file at {location} is not valid JSON.") from exc

    if "url" not in credentials or "token" not in credentials:
        raise ValueError(
            f"Credential file at {location} is missing required 'url'/'token' keys."
        )

    return credentials


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authenticate with Plex and retrieve a server auth token.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "Base URL of the Plex server, e.g. http://localhost:32400. "
            "Required unless --credential-file is given."
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
            "Path to a JSON credential file previously saved with "
            "--save-to. If given, login is skipped and credentials are "
            "loaded from this file instead."
        ),
    )
    parser.add_argument(
        "--save-to",
        default=None,
        help="Path to save the retrieved credentials to as a JSON file cache.",
    )
    args = parser.parse_args(argv)

    if not args.credential_file and not args.url:
        parser.error("--url is required unless --credential-file is given.")

    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        if args.credential_file:
            plex_credentials = retrieve_plex_credentials(args.credential_file)
        else:
            plex_credentials = get_plex_credentials(
                url=args.url,
                username=args.username,
                password=args.password,
            )

        if args.save_to:
            save_plex_credentials(plex_credentials, args.save_to)
    except (Unauthorized, PlexApiException, RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"url:   {plex_credentials['url']}")
    print(f"token: {plex_credentials['token']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
