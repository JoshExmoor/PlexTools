"""Terminal colors for the interactive Plex tools (plex_merge_tool, plex_split_tool).

Small wrappers that color one piece of display text so names, years, counts,
paths and warnings stand out in prompts and context lines:

- artist() cyan, album() bold, year() yellow (normal, never bright),
  count() green, path() and key() dim (always behind a text label, so color
  is never the only way to tell them apart).
- warn() yellow, error() red, ok() green; tag(text, status) picks one of
  those three for a status tag such as "[same folder]".

Color is off until configure() turns it on (each tool calls it once in
main()), so importing code and tests see plain text. configure() decides
separately for stdout and stderr (wrappers take ``err=True`` for text printed
to stderr). For each stream, the first matching rule wins:

1. ``--no-color`` (configure(no_color=True)) -> off
2. ``NO_COLOR`` set to a non-empty value -> off (see https://no-color.org)
3. ``FORCE_COLOR`` set to a non-empty value other than "0" -> on
4. ``TERM=dumb`` -> off
5. the stream is not a terminal (piped, redirected, cron) -> off
6. otherwise -> on

The codes are plain ANSI SGR sequences, which Linux and macOS terminals
understand natively. On Windows, colorama's ``just_fix_windows_console()`` is
called once (only when color would be on) so Windows Terminal, PowerShell and
the legacy console all render them; if colorama is missing or fails, color
stays off unless FORCE_COLOR is set. colorama is optional.

Rules for callers: every wrapper returns ``CODE + text + RESET``, so never
pass wrapped text into another wrapper (compose by concatenation instead),
and never store wrapped text or use it in comparisons; wrap only where the
text is printed. Prompts built from these pieces end in plain text, so what
the user types is never colored. input_prompt() additionally marks the codes
for GNU readline (Linux/macOS) so line editing measures the prompt correctly.
"""

import os
import re
import sys

RESET = "\x1b[0m"

# SGR parameter for each display role.
STYLES = {
    "artist": "36",  # cyan
    "album": "1",  # bold
    "year": "33",  # yellow (normal intensity: readable on light backgrounds)
    "count": "32",  # green
    "path": "2",  # dim
    "key": "2",  # dim
    "warn": "33",  # yellow
    "error": "31",  # red
    "ok": "32",  # green
}

_TAG_STATUSES = ("ok", "warn", "error")

# --help text for the tools' --no-color flag.
NO_COLOR_HELP = (
    "Disable colored output. Color is also off when output is not a terminal, when NO_COLOR "
    "is set, or when TERM=dumb; set FORCE_COLOR=1 to force it on."
)

_SGR = re.compile(r"\x1b\[[0-9;]*m")

_IS_WINDOWS = os.name == "nt"

_enabled = {"out": False, "err": False}
_console_fixed = False


def _forced(env) -> bool:
    value = env.get("FORCE_COLOR", "")
    return bool(value) and value != "0"


def _stream_on(stream, no_color: bool, env) -> bool:
    """Returns whether color should be on for one stream (see the module docstring)."""
    if no_color:
        return False
    if env.get("NO_COLOR"):
        return False
    if _forced(env):
        return True
    if env.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except Exception:  # noqa: BLE001 - a closed or odd stream just means no color
        return False


def _fix_windows_console() -> bool:
    """Enables ANSI codes on the Windows console via colorama, once. False if unavailable."""
    global _console_fixed
    if _console_fixed:
        return True
    try:
        import colorama

        colorama.just_fix_windows_console()
    except Exception:  # noqa: BLE001 - colorama missing or failing means no color
        return False
    _console_fixed = True
    return True


def configure(no_color: bool = False, env=None, stdout=None, stderr=None) -> bool:
    """Decides whether stdout and stderr get color; returns the stdout decision.

    `env` defaults to os.environ and the streams to sys.stdout/sys.stderr,
    looked up at call time.
    """
    env = os.environ if env is None else env
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    out = _stream_on(stdout, no_color, env)
    err = _stream_on(stderr, no_color, env)
    if (out or err) and _IS_WINDOWS and not _fix_windows_console() and not _forced(env):
        out = err = False
    _enabled["out"], _enabled["err"] = out, err
    return out


def reset() -> None:
    """Turns color off on both streams."""
    _enabled["out"] = _enabled["err"] = False


def enable(out: bool = True, err: bool = True) -> None:
    """Sets the color flags directly, skipping detection (for tests and samples)."""
    _enabled["out"], _enabled["err"] = out, err


def is_enabled(err: bool = False) -> bool:
    return _enabled["err" if err else "out"]


def paint(role: str, text, err: bool = False) -> str:
    """Returns str(text) wrapped in the role's color, or unchanged when color is off or text is empty."""
    text = str(text)
    if not text or not is_enabled(err):
        return text
    return f"\x1b[{STYLES[role]}m{text}{RESET}"


def artist(text, err: bool = False) -> str:
    return paint("artist", text, err)


def album(text, err: bool = False) -> str:
    return paint("album", text, err)


def year(text, err: bool = False) -> str:
    return paint("year", text, err)


def count(text, err: bool = False) -> str:
    return paint("count", text, err)


def path(text, err: bool = False) -> str:
    return paint("path", text, err)


def key(text, err: bool = False) -> str:
    return paint("key", text, err)


def warn(text, err: bool = False) -> str:
    return paint("warn", text, err)


def error(text, err: bool = False) -> str:
    return paint("error", text, err)


def ok(text, err: bool = False) -> str:
    return paint("ok", text, err)


def tag(text, status: str, err: bool = False) -> str:
    """Colors a status tag: `status` is "ok" (green), "warn" (yellow) or "error" (red)."""
    if status not in _TAG_STATUSES:
        raise ValueError(f"Unknown tag status: {status}")
    return paint(status, text, err)


def strip(text: str) -> str:
    """Removes color codes (and readline markers) from text."""
    return _SGR.sub("", text).replace("\x01", "").replace("\x02", "")


def input_prompt(text: str) -> str:
    """Returns a prompt for input(); marks color codes for GNU readline when it is loaded."""
    if not _enabled["out"] or "readline" not in sys.modules:
        return text
    return _SGR.sub(lambda m: f"\x01{m.group(0)}\x02", text)
