"""Terminal colors and tables for the interactive Plex tools (plex_merge_tool, plex_split_tool).

Built on rich (https://github.com/Textualize/rich). Display text is passed
around as rich ``Text`` objects that carry a display role, and tables are rich
``Table`` objects; both are printed through one stdout console and one stderr
console owned by this module:

- artist() cyan, album() bold, year() yellow (normal, never bright),
  count() green, path() and key() dim (always behind a label or a column
  header, so color is never the only way to tell them apart).
- warn() yellow, error() red, ok() green; tag(text, status) picks one of
  those three for a status tag such as "[same folder]".
- line()/fill() assemble a one-line Text from plain strings and styled
  pieces; new_table() builds a table with this module's border style.

Color is off until configure() turns it on (each tool calls it once in
main()), so importing code and tests see plain text. configure() decides
separately for stdout and stderr. For each stream, the first matching rule
wins:

1. ``--no-color`` (configure(no_color=True)) -> off
2. ``NO_COLOR`` set to a non-empty value -> off (see https://no-color.org)
3. ``FORCE_COLOR`` set to a non-empty value other than "0" -> on
4. ``TERM=dumb`` -> off
5. the stream is not a terminal (piped, redirected, cron) -> off
6. otherwise -> on

With color on, tables use rounded Unicode borders. With color off, no escape
codes are written at all and tables use plain ASCII borders (``+ - |``) and
"..." instead of "…", so redirected output is safe for any file encoding
(e.g. cp1252).

Windows: rich renders color either with ANSI (VT) codes or, on old consoles
without VT support, through its own Win32 legacy console renderer, so colorama
is not needed here (tqdm still uses it). rich only *detects* VT mode, though,
so when color would be on this module first switches the console handles to
VT mode with ``SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING)`` via ctypes
(Windows 10 1511+, Windows Terminal, PowerShell 7); rich then uses full ANSI
color instead of the 16-color legacy renderer. If that fails, color stays on
and rich's legacy renderer is used. Run ``python plex_style.py`` to see what
rich detected on this machine.

Rules for callers: user data (titles, names, paths) only enters output as
Text (consoles never parse markup or emoji codes), so a title like
"Gish [Deluxe]" is shown literally. Never store styled Text or rendered
strings in data or use them in comparisons; style only where it is printed.
Prompts built by prompt_str() end in plain text, so what the user types is
never colored. input_prompt() additionally marks the codes for GNU readline
(Linux/macOS) so line editing measures the prompt correctly.
"""

import os
import re
import string
import sys

from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# rich style for each display role.
STYLES = {
    "artist": "cyan",
    "album": "bold",
    "year": "yellow",  # normal intensity: readable on light backgrounds
    "count": "green",
    "path": "dim",
    "key": "dim",
    "warn": "yellow",
    "error": "red",
    "ok": "green",
    "label": "bold",  # table row labels and column headers
}

# Every console carries this theme (rich raises on unknown style names).
THEME = Theme(STYLES, inherit=True)

_TAG_STATUSES = ("ok", "warn", "error")

# --help text for the tools' --no-color flag.
NO_COLOR_HELP = (
    "Disable colored output. Color is also off when output is not a terminal, when NO_COLOR "
    "is set, or when TERM=dumb; set FORCE_COLOR=1 to force it on."
)

_SGR = re.compile(r"\x1b\[[0-9;]*m")

_IS_WINDOWS = os.name == "nt"

_enabled = {"out": False, "err": False}
# Fixed console width (None = detect from the terminal).
_width = None
# Windows VT mode: "" (not attempted), "enabled" or "unavailable".
_vt_state = ""

_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def make_console(
    *,
    color: bool,
    file=None,
    stderr: bool = False,
    width: int = None,
    env=None,
    legacy_windows=False,
    color_system: str = None,
) -> Console:
    """Returns a console with this module's theme and settings.

    `file` None writes to whatever sys.stdout (or sys.stderr with `stderr`)
    is at write time. With `color` the console always emits styles
    (`color_system` defaults to "standard"); without it no escape codes are
    ever written. Markup, emoji codes and automatic highlighting are off.
    """
    common = dict(
        file=file,
        stderr=stderr,
        width=width,
        theme=THEME,
        highlight=False,
        markup=False,
        emoji=False,
        safe_box=True,
        _environ=os.environ if env is None else env,
    )
    if color:
        return Console(
            color_system=color_system or "standard",
            force_terminal=True,
            no_color=False,
            legacy_windows=legacy_windows,
            **common,
        )
    return Console(color_system=None, force_terminal=False, no_color=True, legacy_windows=False, **common)


def _plain_consoles(width=None) -> dict:
    return {
        "out": make_console(color=False, width=width),
        "err": make_console(color=False, stderr=True, width=width),
    }


_consoles = _plain_consoles()


def _forced(env) -> bool:
    value = env.get("FORCE_COLOR", "")
    return bool(value) and value != "0"


def _is_tty(stream) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except Exception:  # noqa: BLE001 - a closed or odd stream just means no color
        return False


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
    return _is_tty(stream)


def _enable_windows_vt() -> bool:
    """Turns on ANSI (VT) processing for the stdout/stderr console handles via ctypes.

    True if at least one handle is a console that now has (or already had) VT
    mode. Windows only; never called elsewhere.
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        enabled = False
        for std_handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(wintypes.DWORD(std_handle & 0xFFFFFFFF))
            mode = wintypes.DWORD()
            if not handle or not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue  # not a console (redirected) or no handle
            if mode.value & _ENABLE_VIRTUAL_TERMINAL_PROCESSING:
                enabled = True
            elif kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING):
                enabled = True
        return enabled
    except Exception:  # noqa: BLE001 - no ctypes/kernel32 just means rich's legacy renderer
        return False


def _setup_windows_vt() -> None:
    """Tries to enable VT mode once; success is cached, failure is retried next time."""
    global _vt_state
    if _vt_state == "enabled":
        return
    try:
        ok_ = _enable_windows_vt()
    except Exception:  # noqa: BLE001 - a failure only means rich's legacy renderer
        ok_ = False
    _vt_state = "enabled" if ok_ else "unavailable"


def _build_console(color: bool, stream, stderr: bool, env, width) -> Console:
    if not color:
        return make_console(color=False, stderr=stderr, width=width, env=env)
    # Let rich pick its Win32 legacy renderer for a real Windows console without VT. Once VT is
    # enabled, or for a forced non-TTY stream, rich writes ANSI codes instead of console API calls
    # (rich checks VT on the stdout handle only, which is wrong for stderr when stdout is redirected).
    legacy = None if _IS_WINDOWS and _is_tty(stream) and _vt_state != "enabled" else False
    console_ = make_console(
        color=True, stderr=stderr, width=width, env=env, legacy_windows=legacy, color_system="auto"
    )
    if console_.color_system is None:
        # rich's auto detection says no color for TERM=dumb, but FORCE_COLOR beats TERM=dumb here.
        console_ = make_console(
            color=True, stderr=stderr, width=width, env=env, legacy_windows=legacy, color_system="standard"
        )
    return console_


def configure(no_color: bool = False, env=None, stdout=None, stderr=None, width: int = None) -> bool:
    """Decides whether stdout and stderr get color; returns the stdout decision.

    `env` defaults to os.environ and the streams to sys.stdout/sys.stderr,
    looked up at call time; the streams are only used for detection (output
    always goes to the current sys.stdout/sys.stderr). `width` None means
    the terminal width (80 when it cannot be detected).
    """
    global _width
    env = os.environ if env is None else env
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    out = _stream_on(stdout, no_color, env)
    err = _stream_on(stderr, no_color, env)
    if (out or err) and _IS_WINDOWS:
        _setup_windows_vt()
    _width = width
    _enabled["out"], _enabled["err"] = out, err
    _consoles["out"] = _build_console(out, stdout, False, env, width)
    _consoles["err"] = _build_console(err, stderr, True, env, width)
    return out


def reset(width: int = None) -> None:
    """Turns color off on both streams (plain consoles). `width` None keeps the current width."""
    global _width
    if width is not None:
        _width = width
    _enabled["out"] = _enabled["err"] = False
    _consoles.update(_plain_consoles(_width))


def enable(out: bool = True, err: bool = True, width: int = None) -> None:
    """Sets color directly, skipping detection (for tests and samples). `width` None keeps the current width."""
    global _width
    if width is not None:
        _width = width
    _enabled["out"], _enabled["err"] = out, err
    _consoles["out"] = make_console(color=out, width=_width)
    _consoles["err"] = make_console(color=err, stderr=True, width=_width)


def is_enabled(err: bool = False) -> bool:
    return _enabled["err" if err else "out"]


def console(err: bool = False) -> Console:
    """Returns the stdout (or stderr) console."""
    return _consoles["err" if err else "out"]


def width() -> int:
    """Returns the stdout console width."""
    return console().width


def role(name: str, value) -> Text:
    """Returns str(value) as Text styled with a display role (see STYLES)."""
    if name not in STYLES:
        raise ValueError(f"Unknown display role: {name}")
    return Text(str(value), style=name)


def artist(value) -> Text:
    return role("artist", value)


def album(value) -> Text:
    return role("album", value)


def year(value) -> Text:
    return role("year", value)


def count(value) -> Text:
    return role("count", value)


def path(value) -> Text:
    return role("path", value)


def key(value) -> Text:
    return role("key", value)


def warn(value) -> Text:
    return role("warn", value)


def error(value) -> Text:
    return role("error", value)


def ok(value) -> Text:
    return role("ok", value)


def tag(text, status: str) -> Text:
    """Colors a status tag: `status` is "ok" (green), "warn" (yellow) or "error" (red)."""
    if status not in _TAG_STATUSES:
        raise ValueError(f"Unknown tag status: {status}")
    return role(status, text)


def line(*parts) -> Text:
    """Joins parts into one Text: Text parts keep their style, anything else is plain str()."""
    result = Text()
    for part in parts:
        result.append(part if isinstance(part, Text) else str(part))
    return result


def fill(template: str, **values) -> Text:
    """Fills a str.format-style template: literal text is plain, each {name} is its value.

    Text values keep their style; anything else is plain str(). Format specs
    and conversions are not supported.
    """
    result = Text()
    for literal, name, _spec, _conversion in string.Formatter().parse(template):
        if literal:
            result.append(literal)
        if name is not None:
            value = values[name]
            result.append(value if isinstance(value, Text) else str(value))
    return result


def ellipsis() -> str:
    """Returns "…" when stdout is colored and UTF-8, else the ASCII "..."."""
    encoding = (console().encoding or "").lower()
    return "\u2026" if is_enabled() and encoding.startswith("utf") else "..."


def table_box():
    """Returns the table border style: rounded Unicode with color, plain ASCII without."""
    return box.ROUNDED if is_enabled() else box.ASCII


def new_table(*headers, **kwargs) -> Table:
    """Returns a Table with this module's border style and one column per header.

    A header is a column title, or (title, dict of add_column() kwargs).
    Columns fold long values onto more lines instead of cutting them.
    """
    options = dict(box=table_box(), header_style="label", show_edge=True, pad_edge=True, expand=False)
    options.update(kwargs)
    table = Table(**options)
    for header in headers:
        title, column = (header, {}) if isinstance(header, str) else header
        table.add_column(title, **{"overflow": "fold", **column})
    return table


def indent(renderable, n: int = 2) -> Padding:
    """Indents a renderable by `n` spaces."""
    return Padding(renderable, (0, 0, 0, n), expand=False)


def _print(console_, renderable, end: str = "\n") -> None:
    # One-line Text is never re-wrapped; tables are laid out to the console width.
    console_.print(renderable, soft_wrap=isinstance(renderable, (Text, str)), end=end)


def out(*renderables) -> None:
    """Prints each renderable on stdout."""
    for renderable in renderables:
        _print(console(), renderable)


def say(*parts) -> None:
    """Prints one line on stdout (see line())."""
    out(line(*parts))


def eprint(*parts) -> None:
    """Prints one line on stderr (see line())."""
    _print(console(err=True), line(*parts))


def blank() -> None:
    """Prints an empty line on stdout."""
    console().print()


def render(renderable) -> str:
    """Returns what printing `renderable` on stdout would write (for tests and samples)."""
    console_ = console()
    with console_.capture() as capture:
        _print(console_, renderable)
    return capture.get()


def prompt_str(prompt) -> str:
    """Returns the string to pass to input() for a str or Text prompt.

    Text is rendered with the stdout console's colors when color is on (and
    the console understands ANSI codes), else as plain text.
    """
    if isinstance(prompt, Text):
        console_ = console()
        if is_enabled() and not console_.legacy_windows:
            with console_.capture() as capture:
                _print(console_, prompt, end="")
            prompt = capture.get()
        else:
            prompt = prompt.plain
    return input_prompt(str(prompt))


def strip(text: str) -> str:
    """Removes color codes (and readline markers) from text."""
    return _SGR.sub("", text).replace("\x01", "").replace("\x02", "")


def input_prompt(text: str) -> str:
    """Returns a prompt for input(); marks color codes for GNU readline when it is loaded."""
    if not _enabled["out"] or "readline" not in sys.modules:
        return text
    return _SGR.sub(lambda m: f"\x01{m.group(0)}\x02", text)


def _console_info(console_) -> str:
    return (
        f"(color system: {console_.color_system}, terminal: {console_.is_terminal}, "
        f"legacy Windows: {console_.legacy_windows}, encoding: {console_.encoding}, width: {console_.width})"
    )


def diagnose() -> None:
    """Prints why color is on or off on this machine (run ``python plex_style.py``)."""
    try:
        from importlib.metadata import version

        rich_version = version("rich")
    except Exception:  # noqa: BLE001 - report any lookup failure as unknown
        rich_version = "unknown version"

    print(f"Python:            {sys.executable}")
    print(f"rich:              {rich_version}")
    print(f"stdout is a TTY:   {_is_tty(sys.stdout)}")
    print(f"stderr is a TTY:   {_is_tty(sys.stderr)}")
    for name in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        value = os.environ.get(name)
        print(f"{name + ':':<19}{'(unset)' if value is None else repr(value)}")
    configure()
    if not _IS_WINDOWS:
        vt = "not needed (not Windows)"
    elif _vt_state == "enabled":
        vt = "enabled"
    elif _vt_state == "unavailable":
        vt = "unavailable (rich uses its legacy Windows renderer)"
    else:
        vt = "not attempted (color off)"
    print(f"Windows VT mode:   {vt}")
    print(f"Color on stdout:   {is_enabled()} {_console_info(console())}")
    print(f"Color on stderr:   {is_enabled(err=True)} {_console_info(console(err=True))}")
    for name in STYLES:
        say(f"  {name:<7} ", role(name, f"Sample text for {name}"))
    table = new_table("Role", "Sample")
    table.add_row("album", album("Siamese Dream"))
    table.add_row("path", path("E:\\MP3\\Smashing Pumpkins\\Siamese Dream"))
    out(indent(table))


if __name__ == "__main__":
    diagnose()
