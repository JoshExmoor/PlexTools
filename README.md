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

**Path normalization (handles case/separator mismatches).** The m3u import matches each path against the library *server-side*, and on a Windows server a lower-case drive letter (`e:\` vs `E:\`) or forward slashes can make a line silently fail to match. Before importing, the tool samples a few existing tracks to detect the server's path style, writes a normalized copy of the m3u alongside the original (`.normalized_<name>.m3u`, deleted afterward), and imports that:
- **Windows server:** drive letters are upper-cased and separators switched to `\`. Matching is case-insensitive.
- **Linux/POSIX server:** paths are left as-is — POSIX filesystems are case-sensitive, so rewriting case would risk matching the wrong file. Matching is case-sensitive.

**Reconciliation (recovers silently-dropped tracks).** Plex's m3u import gives no per-track feedback and silently skips lines it can't match. After importing, the tool compares the resulting playlist against the m3u's track list, and for any expected track that's missing but *does* exist in the library, adds it via the playlist API. Tracks genuinely not found in the library are reported on stderr. This runs by default; pass `--no-reconcile`/`-R` to skip it. The reconciliation step fetches all tracks in the section once (paged, with a progress bar) to build a path lookup, which is the slowest part on a large library.

**Library track cache.** To avoid re-fetching the whole library on every run, reconciliation caches the section's `path → track` index to `~/.cache/plextools/section_<uuid>.json`. On the next run it reuses the cache only if the section's `uuid`, `updatedAt`, and track count all still match the server — so a cache that's gone stale (tracks added/removed, or the library rescanned) is detected and refreshed automatically. Playlist create/modify/delete does **not** bump a music section's `updatedAt` (verified against a live server), so running this tool repeatedly doesn't invalidate its own cache. Pass `--no-cache`/`-C` to force a fresh fetch and ignore any existing cache.

**How Plex identifies the playlist (and why duplicates can appear).** Plex keys an m3u-imported playlist by the **m3u file path**, *not* by the playlist title — re-importing from the same path overwrites that playlist. This tool imports a normalized temp copy written to a stable path per source m3u (`.normalized_<name>.m3u` next to the original), so repeatedly uploading the same m3u overwrites the same playlist as expected. However, if the m3u path ever changes — you rename/move the m3u, or you upgrade the tool in a way that changes the temp filename — Plex no longer recognizes it as the same import and creates a **new** playlist with the same title, leaving same-named duplicates behind.

**Duplicate detection and cleanup.** After uploading (and reconciling), the tool checks for other playlists sharing the title (case-insensitive). If it finds any, it prints a warning listing each duplicate's metadata (ratingKey, item count, added/updated times) so you can decide what to do. To clean them up, re-run with `--remove-duplicates`/`-D`, which deletes every same-named playlist *except* the one just uploaded (identified by its ratingKey). This is **off by default and destructive** — a same-named playlist might be an unrelated or smart playlist you want to keep, so review the warning's list before using it. (The `list_playlists.py` helper prints the same metadata for all playlists if you want to inspect first.)

The helper functions (`detect_path_style`, `write_normalized_m3u`, `reconcile_playlist`, `find_playlists_by_title`, `warn_if_duplicate_playlists`, `remove_duplicate_playlists`, `iter_section_tracks`, `load_track_cache`, `save_track_cache`) are importable for reuse in other scripts.

Full options:

| Flag | Short | Purpose |
|---|---|---|
| `--m3u-path` | `-i` | Path to the m3u file, as seen by the Plex server (required). |
| `--title` | `-t` | Title to give the created playlist in Plex (required). |
| `--section` | `-s` | Music library section name to import into. Defaults to the server's only music library. |
| `--no-reconcile` | `-R` | Skip the post-import back-fill of tracks the import silently dropped. |
| `--remove-duplicates` | `-D` | After uploading, delete other playlists sharing this title, keeping only the just-uploaded one (destructive). |
| `--no-cache` | `-C` | Ignore the local library track cache and re-fetch the section fresh. |

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

### `backup_plex_playcounts.py`

Backs up the play count (and rating) of every played song in a music library to a JSON file.

```powershell
.\.venv\Scripts\python.exe backup_plex_playcounts.py `
  -c creds.json `
  -s "Music" `
  -o "D:\Backups\playcounts.json"
```

Retrieves every track in the section with a play count of 1 or more and writes a JSON array, one object per song, each with `path` (file location as seen by the Plex server), `artist`, `album`, `title`, `rating` (user rating on Plex's 0-10 scale, `null` if unrated), and `playcount`.

Key behavior:
- `--output`/`-o` is **required** — the script always writes to a file you specify.
- `--section`/`-s` defaults to `"Music"`. Must be a music library; the script errors if the named section isn't one.
- Only songs with `playcount >= 1` are included (unplayed tracks are skipped).
- `path` is the file location as the **Plex server** sees it (same caveat as the playlist tools: it may not resolve locally if this script runs on a different machine than the server). Tracks with no resolvable file path are still backed up, with `path` set to `null`, and the count of such tracks is reported on stderr.
- Note: Plex itself only lets you *reset* a play count or increment it by one play at a time (there's no "set play count to N" in the Plex API), so this backup is best paired with a restore approach that accounts for that limitation.

Full options:

| Flag | Short | Purpose |
|---|---|---|
| `--output` | `-o` | Path to write the play count backup JSON to (required). |
| `--section` | `-s` | Music library section name to read from (default: `Music`). |

(Plus the shared auth flags above: `--url`/`-l`, `--username`/`-u`, `--password`/`-p`, `--credential-file`/`-c`, `--save-creds`/`-S`.)

### `list_playlists.py`

Lists, filters, and compares Plex playlists. Read-only — it never modifies or deletes anything, so it's safe to run freely (use `upload_playlist.py --remove-duplicates` for cleanup).

```powershell
# All playlists, flagging any duplicate titles
.\.venv\Scripts\python.exe list_playlists.py -c creds.json

# Only playlists whose title is shared by more than one playlist
.\.venv\Scripts\python.exe list_playlists.py -c creds.json --duplicates-only

# Compare the track contents of two playlists (e.g. duplicates) by ratingKey
.\.venv\Scripts\python.exe list_playlists.py -c creds.json --diff 856096 856787
```

Key behavior:
- **Filters:** `--title`/`-t` (exact, case-insensitive), `--title-contains` (substring, case-insensitive), `--type audio|video|photo`, and `--smart`/`--no-smart`.
- **Duplicate detection** is case-insensitive and consistent with `upload_playlist.py` — same-titled playlists (which Plex can create because it keys m3u imports by path, not title) are flagged with each one's metadata on stderr. `--duplicates-only`/`-d` narrows the listing to just those.
- **`--diff RATINGKEY_A RATINGKEY_B`** compares two playlists' track contents (by file path, so the same song in different files is treated as distinct) and reports what's only in A, only in B, and in both — the quickest way to see *how* two duplicates differ.
- **Output formats** via `--format`/`-f`: `table` (default, human-readable), `json`, or `csv` (handy for spreadsheet comparison). `--output`/`-o` also writes the rendered output to a file. In `json`/`csv` mode the duplicate warning goes to stderr so it doesn't corrupt machine-readable stdout.

Full options:

| Flag | Short | Purpose |
|---|---|---|
| `--title` | `-t` | Only list playlists with this exact title (case-insensitive). |
| `--title-contains` | | Only list playlists whose title contains this substring. |
| `--type` | | Filter by playlist type: `audio`, `video`, or `photo`. |
| `--smart` / `--no-smart` | | Only smart, or only regular, playlists. |
| `--duplicates-only` | `-d` | Only show playlists whose title is shared by more than one. |
| `--diff` | | Compare two playlists' track contents by ratingKey. |
| `--format` | `-f` | Output format: `table` (default), `json`, or `csv`. |
| `--output` | `-o` | Also write the rendered output to this file. |

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
├── backup_plex_playcounts.py          # Back up play counts of all played songs to JSON
├── list_playlists.py                  # List, filter, and compare (diff) Plex playlists
├── tests/
│   ├── test_get_plex_credentials.py
│   ├── test_push_plex_ratings_by_playlist.py
│   ├── test_upload_playlist.py
│   ├── test_retrieve_playlist.py
│   ├── test_backup_plex_playcounts.py
│   └── test_list_playlists.py
├── conftest.py                        # Makes project root importable for tests
├── requirements.txt
└── README.md
```
