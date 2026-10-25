"""Shared helpers for the Plex music library tools (merge, split).

These are small, type-agnostic helpers: reading attributes off partial plexapi
objects, comparing server folder paths that may come from a Windows or a POSIX
Plex server, reading an album's year, and asking a yes/no question. Keeping
them here lets plex_merge_tool.py and plex_split_tool.py behave the same way.
"""

import ntpath
import posixpath
import re

import plex_style as style

# Most folders listed per album before collapsing into "... and N more".
MAX_FOLDERS_SHOWN = 5


def attr(obj, name):
    """Returns getattr(obj, name), or None if the attribute is absent.

    Items returned from a paginated search come back as "partial" objects:
    Plex's list view omits attributes that aren't set, and plexapi's
    auto-reload is disabled on searched items, so a missing attribute raises
    AttributeError instead of yielding None. Reading through this helper
    treats "absent" as None.
    """
    return getattr(obj, name, None)


def album_year(album):
    """Returns the album's year, else its release date's year, else None."""
    year = attr(album, "year")
    if year:
        return year
    released = attr(album, "originallyAvailableAt")
    if released is not None:
        return attr(released, "year")
    return None


DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


def is_windows_path(path: str) -> bool:
    """True for Windows-style server paths (a backslash or a leading drive letter like "E:")."""
    return "\\" in path or bool(DRIVE_LETTER.match(path))


def server_dirname(path: str) -> str:
    """Returns the directory of a path as reported by the Plex server.

    The server's OS may differ from this machine's, so Windows-style paths
    (see is_windows_path) use ntpath and everything else uses posixpath.
    """
    return ntpath.dirname(path) if is_windows_path(path) else posixpath.dirname(path)


def folder_key(path: str) -> str:
    """Returns the comparison key for a server folder path.

    Windows-style paths are case-insensitive and accept either separator, so
    they go through ntpath.normcase (lowercase, "/" -> "\\"). POSIX paths are
    case-sensitive and compared as-is. Trailing separators are ignored.
    """
    if is_windows_path(path):
        return ntpath.normcase(path).rstrip("\\") or path
    return path.rstrip("/") or path


def dedupe_folders(directories) -> list:
    """Returns the distinct folders (by folder_key), sorted.

    The first-seen original string is kept for each key, so folders are
    displayed exactly as Plex reports them.
    """
    seen = {}
    for directory in directories:
        seen.setdefault(folder_key(directory), directory)
    return sorted(seen.values())


def same_folders(a, b) -> bool:
    """True when two folder lists name the same folders (see folder_key)."""
    return {folder_key(d) for d in a} == {folder_key(d) for d in b}


def shorten_path(path: str, max_width: int, ellipsis: str = "\u2026") -> str:
    """Returns `path` cut to at most `max_width` characters by replacing middle folders with `ellipsis`.

    The anchor (drive letter, UNC share or POSIX root) and the last folder
    are kept; then whole folders are added back alternately from the left
    (root side) and from the right (parent folders) while they fit, so the
    result is cut at separators, e.g. "E:\\MP3\\…\\Siamese Dream". When even
    that does not fit (or the path has a single component) the characters in
    the middle are cut, keeping more of the end. Widths are counted with
    len(), so wide (e.g. CJK) characters may still overflow; tables fold such
    values instead of hiding them.
    """
    if max_width <= 0:
        return ""
    if len(path) <= max_width:
        return path

    if is_windows_path(path):
        sep = "\\" if "\\" in path else "/"
        drive, rest = ntpath.splitdrive(path)
        anchor = drive + (sep if rest[:1] in ("\\", "/") else "")
        parts = [p for p in re.split(r"[\\/]+", rest) if p]
    else:
        sep = "/"
        anchor = "/" if path.startswith("/") else ""
        parts = [p for p in re.split(r"/+", path) if p]

    def build(head, tail):
        return anchor + "".join(c + sep for c in head) + ellipsis + sep + sep.join(tail)

    if len(parts) >= 2 and len(build([], parts[-1:])) <= max_width:
        left, right = 0, len(parts) - 1  # next head index; tail starts at `right`
        head, tail = [], [parts[-1]]
        grow_head = grow_tail = True
        while grow_head or grow_tail:
            if left >= right - 1:
                break  # only the cut-out part is left
            if grow_head:
                if len(build(head + [parts[left]], tail)) <= max_width:
                    head.append(parts[left])
                    left += 1
                else:
                    grow_head = False
            if left >= right - 1:
                break
            if grow_tail:
                if len(build(head, [parts[right - 1]] + tail)) <= max_width:
                    tail.insert(0, parts[right - 1])
                    right -= 1
                else:
                    grow_tail = False
        return build(head, tail)

    keep = max_width - len(ellipsis)
    if keep <= 0:
        return ellipsis[:max_width]
    left = keep // 3
    right = keep - left
    return path[:left] + ellipsis + path[len(path) - right:]


# Answer hint at the end of every yes/no question (Enter means no).
YES_NO_HINT = "[y/N]"


def question(template: str, **values) -> "style.Text":
    """Returns a yes/no question: the filled template (see plex_style.fill) then " [y/N] "."""
    return style.line(style.fill(template, **values), f" {YES_NO_HINT} ")


def ask_yes_no(prompt, input_fn=None) -> bool:
    """Asks `prompt` (a str or a rich Text) until the answer is yes or no; Enter alone means no.

    `input_fn` defaults to the built-in input(), looked up at call time, and
    always receives a str (see plex_style.prompt_str). EOFError and
    KeyboardInterrupt propagate to the caller.
    """
    input_fn = input_fn or input
    while True:
        answer = input_fn(style.prompt_str(prompt)).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        style.say(style.warn("Please answer y or n."))
