# PlexTools

A growing collection of command-line tools built on [python-plexapi](https://python-plexapi.readthedocs.io/) for managing a Plex Media Server.

## Setup

This project uses a Python virtual environment at `.venv`.

```powershell
# Create the venv (first time only)
python -m venv .venv

# Install dependencies
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

All example commands below assume you run them with the venv's Python:

```powershell
.\.venv\Scripts\python.exe <script>.py ...
```

## Shared authentication

Every tool that needs to talk to a Plex server authenticates through **`get_plex_credentials.py`**. New scripts should import and reuse it rather than re-implementing login.

### `get_plex_credentials(url, username=None, password=None) -> dict`

Logs in to Plex via `plexapi.myplex.MyPlexAccount`, retrieves an auth token, and validates it against the target server at `url`. Returns `{"url": ..., "token": ...}`.

- If `username`/`password` are omitted, you're prompted for them on the console (password input is hidden via `getpass`).
- Raises `plexapi.exceptions.Unauthorized` on bad credentials or a rejected token, `RuntimeError` on other connection failures.

### `save_plex_credentials(credentials, location)` / `retrieve_plex_credentials(location)`

Cache a credentials dict to a JSON file, or load one back. `save_plex_credentials` locks the file down to the current user only (`chmod 600` on POSIX, `icacls` on Windows) since the file contains a live auth token.

### CLI

`get_plex_credentials.py` can also be run directly to fetch (and optionally cache) a token:

```powershell
# Interactive login, prints url/token
.\.venv\Scripts\python.exe get_plex_credentials.py --url http://localhost:32400

# Login and cache the result for reuse by other scripts
.\.venv\Scripts\python.exe get_plex_credentials.py --url http://localhost:32400 --save-to creds.json

# Skip login entirely using a cached file
.\.venv\Scripts\python.exe get_plex_credentials.py --credential-file creds.json
```

### Convention for new scripts

Every script in this project that needs a token follows the same CLI shape, so credentials never need to be typed twice:

| Flag | Purpose |
|---|---|
| `--url` | Base URL of the Plex server. Required unless `--credential-file` is given. |
| `--username` / `--password` | Optional; prompted if omitted. |
| `--credential-file PATH` | Skip live login; load a cached `url`/`token` from this JSON file instead. |
| `--save-to PATH` | Cache the retrieved credentials to this JSON file for later `--credential-file` use. |

**Security note:** credential cache files contain a live Plex auth token. Treat them like a password — don't commit them to source control, and prefer `--credential-file` + interactive login over passing `--password` on the command line (it can leak into shell history and process listings).

## Tools

### `push_plex_ratings_by_playlist.py`

Sets a star rating on every track in a Plex playlist.

```powershell
.\.venv\Scripts\python.exe push_plex_ratings_by_playlist.py `
  --credential-file creds.json `
  --playlist "Favorites" `
  --stars 4.5
```

Key behavior:
- **Stars, not raw Plex rating.** `--stars` takes 0-5 in half-star steps, matching what Plex displays. Internally this is converted to Plex's native 0-10 integer scale (5 stars = 10, 4.5 = 9, etc.).
- **Skips tracks already at the target rating by default.** Pass `--force-ratings` to update every track regardless of its current rating.
- **`--dry-run`** reports what would change without calling the Plex API to rate anything.
- **`--backup-ratings-to PATH`** (optional) writes each track's current rating to a JSON file before making any changes, so you can manually restore if needed.
- Shows a live progress bar (`tqdm`) with a running "Updated N of M ratings" description.
- Prints a summary line when done, e.g. `Playlist "Favorites" job run. 20/23 songs updated to 4.5 stars (rating 9). 3 already at target rating were skipped.`

Full options:

| Flag | Purpose |
|---|---|
| `--playlist` | Title of the playlist to update (required). |
| `--stars` | Target rating, 0-5 in 0.5 steps (required). |
| `--force-ratings` | Update every track, even ones already at the target rating. |
| `--dry-run` | Preview changes without writing any ratings. |
| `--backup-ratings-to PATH` | Save current ratings to JSON before updating. |

(Plus the shared auth flags above: `--url`, `--username`, `--password`, `--credential-file`, `--save-to`.)

### `upload_playlist.py`

Uploads an m3u playlist file to Plex as a new playlist, in a music library.

```powershell
.\.venv\Scripts\python.exe upload_playlist.py `
  --credential-file creds.json `
  --m3u-path "D:\Music\Playlists\RoadTrip.m3u" `
  --title "Road Trip"
```

**Important:** m3u import is a server-side operation in Plex — `--m3u-path` must be a path the *Plex Media Server process itself* can read (its local disk, or a volume it has mounted), not necessarily this machine. If this script runs somewhere other than the Plex server host, the path needs to resolve on the server's filesystem (e.g. a shared/mounted path), or the import will fail with a `BadRequest`.

Key behavior:
- Only works against **music** library sections (a PlexAPI/Plex Media Server limitation on m3u import).
- If `--section` is omitted, the tool uses the server's only music library section. If there's more than one, it raises an error asking you to specify `--section` explicitly.
- Re-uploading the same m3u path overwrites any playlist previously created from that file (this is Plex server behavior, not something this tool controls).

Full options:

| Flag | Purpose |
|---|---|
| `--m3u-path` | Path to the m3u file, as seen by the Plex server (required). |
| `--title` | Title to give the created playlist in Plex (required). |
| `--section` | Music library section name to import into. Defaults to the server's only music library. |

(Plus the shared auth flags above: `--url`, `--username`, `--password`, `--credential-file`, `--save-to`.)

### `retrieve_playlist.py`

Retrieves a Plex playlist by name and saves it as a local m3u file.

```powershell
.\.venv\Scripts\python.exe retrieve_playlist.py `
  --credential-file creds.json `
  --playlist "Road Trip" `
  --save-path "D:\Backups\RoadTrip.m3u"
```

If `--save-path` is omitted, the playlist is saved as `<playlist name>.m3u` in the current directory (unsafe filename characters are replaced with `_`).

Key behavior:
- plexapi has no built-in m3u export, so this builds the m3u file itself from each track's underlying file path(s) (`track.iterParts()` → `part.file`), with one `#EXTINF`/path pair per track.
- Those file paths are as seen by the **Plex server**, same caveat as `upload_playlist.py` in reverse: if this script runs elsewhere, the m3u's paths may not resolve locally unless that filesystem is shared/mounted. The m3u is still correct as a backup of the playlist's track list and titles.
- Tracks with no resolvable file path (e.g. missing media) are skipped and reported, rather than failing the whole export.

Full options:

| Flag | Purpose |
|---|---|
| `--playlist` | Title of the Plex playlist to retrieve (required). |
| `--save-path` | Path to save the m3u file to. Defaults to `<playlist name>.m3u` in the current directory. |

(Plus the shared auth flags above: `--url`, `--username`, `--password`, `--credential-file`, `--save-to`.)

## Testing

Tests are written with [pytest](https://docs.pytest.org/) and live under `tests/`. They run entirely offline — no real Plex server or plex.tv login is contacted; all `plexapi` interaction is mocked.

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Run a single file or a verbose run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_push_plex_ratings_by_playlist.py -v
```

## Project layout

```
PlexTools/
├── get_plex_credentials.py            # Shared Plex auth: login, token cache save/load
├── push_plex_ratings_by_playlist.py   # Bulk-set star ratings for a playlist's tracks
├── upload_playlist.py                 # Import an m3u file into Plex as a new playlist
├── retrieve_playlist.py               # Export a Plex playlist to a local m3u file
├── tests/
│   ├── test_get_plex_credentials.py
│   ├── test_push_plex_ratings_by_playlist.py
│   ├── test_upload_playlist.py
│   └── test_retrieve_playlist.py
├── conftest.py                        # Makes project root importable for tests
├── requirements.txt
└── README.md
```
