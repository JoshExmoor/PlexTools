# PlexTools

A growing collection of command-line tools built on [python-plexapi](https://python-plexapi.readthedocs.io/) for managing a Plex Media Server.

## Setup

This project uses a Python virtual environment at `.venv`. **Every command below — installing dependencies and running scripts — must use that same environment.** Mixing a venv install with a global-Python run (or vice versa) is the most common source of confusing "but it says it's installed!" errors with this project; see the troubleshooting note at the end of this section if you hit that.

```powershell
# Create the venv (first time only)
python -m venv .venv

# Activate it in your current shell (do this every time you open a new terminal for this project)
.\.venv\Scripts\Activate.ps1

# Install dependencies - now that the venv is active, bare `python`/`pip` are safe to use
pip install -r requirements.txt
```

Once activated, your prompt is prefixed with `(.venv)` and plain `python`/`pip` commands in that shell stay inside the venv - including running the tools:

```powershell
python push_plex_ratings_by_playlist.py ...
```

If you'd rather not activate (e.g. a one-off command, or a script calling into this project), use the venv's Python explicitly instead - this works from any shell state and is what the rest of this README uses in its examples:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe <script>.py ...
```

**Don't mix the two approaches within one install.** If you ever see a package reported as "already installed" by `pip` but a script still fails with `ModuleNotFoundError`, it means the install and the run used two different Python interpreters. Confirm which one is active:

```powershell
# Shows the interpreter actually running - compare this to .venv's path
python -c "import sys; print(sys.executable)"

# Shows what's installed in the venv specifically, regardless of shell state
.\.venv\Scripts\python.exe -m pip list
```

If the venv is missing a package that `requirements.txt` lists, reinstall explicitly against it rather than guessing which `pip` is active:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Shared authentication

Every tool that needs to talk to a Plex server authenticates through **`get_plex_credentials.py`**. New scripts should import and reuse it rather than re-implementing login.

### `get_plex_credentials(url, username=None, password=None, auto_cache=True, cache_location=None) -> dict`

Logs in to Plex via `plexapi.myplex.MyPlexAccount`, retrieves an auth token, and validates it against the target server at `url`. Returns `{"url": ..., "token": ...}`.

- **Automatic credential cache (default behavior).** Before prompting for anything, this checks the default cache file (`~/.creds/plex_creds.json`, or `cache_location` if given) for a previously saved token for `url` and validates it against the server. If it's still valid, it's returned immediately — no login, no prompt. If there's no cached entry for `url`, or the server rejects it, this falls through to a normal login and **saves the new token back into the cache** for next time. Pass `auto_cache=False` to disable this entirely (always do a fresh login, never read or write the cache).
- If `username`/`password` are omitted (and no valid cached token was found), you're prompted for them on the console (password input is hidden via `getpass`).
- Raises `plexapi.exceptions.Unauthorized` on bad credentials or a rejected token, `RuntimeError` on other connection failures.

### `save_plex_credentials(credentials, location=None)` / `retrieve_plex_credentials(location=None, url=None)`

The cache file holds credentials for **multiple servers**, keyed by url:

```json
{
  "http://server1:32400": { "token": "..." },
  "http://server2:32400": { "token": "..." }
}
```

- `save_plex_credentials` reads any existing cache at `location`, adds or replaces only the entry for `credentials["url"]`, and writes the merged result back — other servers' saved credentials are untouched. `location` defaults to `~/.creds/plex_creds.json`.
- `retrieve_plex_credentials` returns `{"url": ..., "token": ...}` for one server. Pass `url` to pick a specific server; if omitted, the file must contain exactly one saved server (raises `ValueError` if there's more than one, asking the caller to specify `url`).
- Credential files are no longer locked down to restrictive permissions — an earlier version did this on Windows via `icacls`, but that made the file inaccessible to the very process that needed to read it back. Treat the cache file like any other file containing a secret: don't commit it, don't share it, but it isn't artificially locked down either.

### CLI

`get_plex_credentials.py` can also be run directly to fetch (and optionally cache) a token:

```powershell
# Interactive login; checks/updates the default cache (~/.creds/plex_creds.json) automatically
.\.venv\Scripts\python.exe get_plex_credentials.py -l http://localhost:32400

# Force a fresh login, bypassing the automatic cache check/save
.\.venv\Scripts\python.exe get_plex_credentials.py -l http://localhost:32400 -n

# Also cache the result to a specific file (in addition to the default cache)
.\.venv\Scripts\python.exe get_plex_credentials.py -l http://localhost:32400 -S creds.json

# Skip login entirely using a cached file (single-server file, or combine with --url for multi-server)
.\.venv\Scripts\python.exe get_plex_credentials.py -c creds.json
.\.venv\Scripts\python.exe get_plex_credentials.py -c creds.json -l http://server2:32400
```

### Convention for new scripts

Every script in this project that needs a token follows the same CLI shape (long form plus a short alias), so credentials never need to be typed twice:

| Flag | Short | Purpose |
|---|---|---|
| `--url` | `-l` | Base URL of the Plex server. Required unless `--credential-file` is given. |
| `--username` | `-u` | Optional; prompted if omitted and no valid cached token is found. |
| `--password` | `-p` | Optional; prompted (hidden) if omitted and no valid cached token is found. |
| `--credential-file PATH` | `-c` | Skip live login; load cached credentials from this JSON file instead. Combine with `--url`/`-l` to pick one server out of a file with multiple saved servers. |
| `--save-creds PATH` | `-S` | Also cache the retrieved credentials to this JSON file (in addition to the automatic default cache). |

Scripts that call `get_plex_credentials()` directly (rather than going through its CLI) get the automatic default-cache check/save for free, since `auto_cache=True` is the default.

**Security note:** credential cache files contain a live Plex auth token. Treat them like a password — don't commit them to source control, and prefer the automatic cache / interactive login over passing `--password` on the command line (it can leak into shell history and process listings).

## Tools

### `push_plex_ratings_by_playlist.py`

Sets a star rating on every track in a Plex playlist.

```powershell
.\.venv\Scripts\python.exe push_plex_ratings_by_playlist.py `
  -c creds.json `
  -m "Favorites" `
  -r 4.5
```

Key behavior:
- **Stars, not raw Plex rating.** `--stars`/`-r` takes 0-5 in half-star steps, matching what Plex displays. Internally this is converted to Plex's native 0-10 integer scale (5 stars = 10, 4.5 = 9, etc.).
- **Skips tracks already at the target rating by default.** Pass `--force-ratings`/`-f` to update every track regardless of its current rating.
- **`--dry-run`/`-d`** reports what would change without calling the Plex API to rate anything.
- **`--backup-ratings-to PATH`/`-b`** (optional) writes each track's current rating to a JSON file before making any changes, so you can manually restore if needed.
- Shows a live progress bar (`tqdm`) with a running "Updated N of M ratings" description.
- Prints a summary line when done, e.g. `Playlist "Favorites" job run. 20/23 songs updated to 4.5 stars (rating 9). 3 already at target rating were skipped.`

Full options:

| Flag | Short | Purpose |
|---|---|---|
| `--playlist` | `-m` | Title of the playlist to update (required). |
| `--stars` | `-r` | Target rating, 0-5 in 0.5 steps (required). |
| `--force-ratings` | `-f` | Update every track, even ones already at the target rating. |
| `--dry-run` | `-d` | Preview changes without writing any ratings. |
| `--backup-ratings-to PATH` | `-b` | Save current ratings to JSON before updating. |

(Plus the shared auth flags above: `--url`/`-l`, `--username`/`-u`, `--password`/`-p`, `--credential-file`/`-c`, `--save-creds`/`-S`.)

### `upload_playlist.py`

Uploads an m3u playlist file to Plex as a new playlist, in a music library.

```powershell
.\.venv\Scripts\python.exe upload_playlist.py `
  -c creds.json `
  -i "D:\Music\Playlists\RoadTrip.m3u" `
  -t "Road Trip"
```

**Important:** m3u import is a server-side operation in Plex — `--m3u-path`/`-i` must be a path the *Plex Media Server process itself* can read (its local disk, or a volume it has mounted), not necessarily this machine. If this script runs somewhere other than the Plex server host, the path needs to resolve on the server's filesystem (e.g. a shared/mounted path), or the import will fail with a `BadRequest`.

Key behavior:
- Only works against **music** library sections (a PlexAPI/Plex Media Server limitation on m3u import).
- If `--section`/`-s` is omitted, the tool uses the server's only music library section. If there's more than one, it raises an error asking you to specify `--section` explicitly.
- Re-uploading the same m3u path overwrites any playlist previously created from that file (this is Plex server behavior, not something this tool controls).

Full options:

| Flag | Short | Purpose |
|---|---|---|
| `--m3u-path` | `-i` | Path to the m3u file, as seen by the Plex server (required). |
| `--title` | `-t` | Title to give the created playlist in Plex (required). |
| `--section` | `-s` | Music library section name to import into. Defaults to the server's only music library. |

(Plus the shared auth flags above: `--url`/`-l`, `--username`/`-u`, `--password`/`-p`, `--credential-file`/`-c`, `--save-creds`/`-S`.)

### `retrieve_playlist.py`

Retrieves a Plex playlist by name and saves it as a local m3u file.

```powershell
.\.venv\Scripts\python.exe retrieve_playlist.py `
  -c creds.json `
  -m "Road Trip" `
  -o "D:\Backups\RoadTrip.m3u"
```

If `--save-path`/`-o` is omitted, the playlist is saved as `<playlist name>.m3u` in the current directory (unsafe filename characters are replaced with `_`).

Key behavior:
- plexapi has no built-in m3u export, so this builds the m3u file itself from each track's underlying file path(s) (`track.iterParts()` → `part.file`), with one `#EXTINF`/path pair per track.
- Those file paths are as seen by the **Plex server**, same caveat as `upload_playlist.py` in reverse: if this script runs elsewhere, the m3u's paths may not resolve locally unless that filesystem is shared/mounted. The m3u is still correct as a backup of the playlist's track list and titles.
- Tracks with no resolvable file path (e.g. missing media) are skipped and reported, rather than failing the whole export.

Full options:

| Flag | Short | Purpose |
|---|---|---|
| `--playlist` | `-m` | Title of the Plex playlist to retrieve (required). |
| `--save-path` | `-o` | Path to save the m3u file to. Defaults to `<playlist name>.m3u` in the current directory. |

(Plus the shared auth flags above: `--url`/`-l`, `--username`/`-u`, `--password`/`-p`, `--credential-file`/`-c`, `--save-creds`/`-S`.)

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
