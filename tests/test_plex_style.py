import io
import sys

import pytest
from rich.text import Text

import plex_style


class FakeTTY:
    """A stream stand-in whose isatty() returns a fixed answer (or raises)."""

    def __init__(self, tty=True, raises=False):
        self.tty = tty
        self.raises = raises

    def isatty(self):
        if self.raises:
            raise ValueError("closed")
        return self.tty


WRAPPERS = ["artist", "album", "year", "count", "path", "key", "warn", "error", "ok"]

# SGR codes rich writes for each role with the "standard" color system.
CODES = {
    "artist": "\x1b[36m", "album": "\x1b[1m", "year": "\x1b[33m", "count": "\x1b[32m", "path": "\x1b[2m",
    "key": "\x1b[2m", "warn": "\x1b[33m", "error": "\x1b[31m", "ok": "\x1b[32m",
}

BOX_CHARS = set("╭╮╰╯│─┬┴┼├┤")


@pytest.fixture
def posix(monkeypatch):
    """Pretend not to be on Windows so configure() never touches the Windows console."""
    monkeypatch.setattr(plex_style, "_IS_WINDOWS", False)


def configure(**kwargs):
    kwargs.setdefault("stdout", FakeTTY(True))
    kwargs.setdefault("stderr", FakeTTY(True))
    kwargs.setdefault("env", {})
    return plex_style.configure(**kwargs)


def sample_table():
    table = plex_style.new_table("", "Keep", "Merge in")
    table.add_row("Title", plex_style.album("Siamese Dream"), plex_style.album("Siamese Dream"))
    table.add_row("Year", plex_style.year(1993), plex_style.year(2011))
    return table


# ---------------------------------------------------------------------------
# Roles, Text helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", WRAPPERS)
def test_wrappers_return_text_with_role_style(name):
    text = getattr(plex_style, name)(1993)
    assert isinstance(text, Text)
    assert text.plain == "1993"
    assert text.style == name


@pytest.mark.parametrize("name", WRAPPERS)
def test_off_by_default_renders_plain(name):
    assert plex_style.render(getattr(plex_style, name)("Siamese Dream")) == "Siamese Dream\n"


@pytest.mark.parametrize("name", WRAPPERS)
def test_enabled_renders_role_code_and_reset(name):
    plex_style.enable()
    assert plex_style.render(getattr(plex_style, name)("x")) == f"{CODES[name]}x\x1b[0m\n"


def test_scheme_and_theme():
    assert plex_style.STYLES == {
        "artist": "cyan", "album": "bold", "year": "yellow", "count": "green", "path": "dim",
        "key": "dim", "warn": "yellow", "error": "red", "ok": "green", "label": "bold",
    }
    assert not any("bright" in value for value in plex_style.STYLES.values())
    for name in plex_style.STYLES:
        assert name in plex_style.THEME.styles


def test_role_unknown_name_raises():
    with pytest.raises(ValueError):
        plex_style.role("blue", "x")


def test_streams_are_independent(capsys):
    plex_style.enable(out=True, err=False)
    plex_style.say(plex_style.warn("w"))
    plex_style.eprint(plex_style.warn("w"))
    captured = capsys.readouterr()
    assert captured.out == "\x1b[33mw\x1b[0m\n"
    assert captured.err == "w\n"


@pytest.mark.parametrize("status, code", [("ok", "32"), ("warn", "33"), ("error", "31")])
def test_tag_colors(status, code):
    plex_style.enable()
    assert plex_style.render(plex_style.tag("[same folder]", status)) == f"\x1b[{code}m[same folder]\x1b[0m\n"


def test_tag_bad_status_raises():
    with pytest.raises(ValueError):
        plex_style.tag("[x]", "blue")


def test_line_keeps_text_styles_and_plain_parts():
    text = plex_style.line("Found ", plex_style.count(3), " group(s), ", 5)
    assert text.plain == "Found 3 group(s), 5"
    plex_style.enable()
    assert plex_style.render(text) == "Found \x1b[32m3\x1b[0m group(s), 5\n"


def test_fill_keeps_text_styles_and_literal_text():
    text = plex_style.fill('Merge "{a}" [x] into {b}?', a=plex_style.album("[Live]"), b=7)
    assert text.plain == 'Merge "[Live]" [x] into 7?'
    plex_style.enable()
    assert plex_style.render(text) == 'Merge "\x1b[1m[Live]\x1b[0m" [x] into 7?\n'


def test_strip_removes_codes_and_readline_markers():
    assert plex_style.strip("merge \x1b[36mA\x1b[0m (\x1b[32m5 albums\x1b[0m)? ") == "merge A (5 albums)? "
    assert plex_style.strip("\x01\x1b[36m\x02A\x01\x1b[0m\x02") == "A"


@pytest.mark.parametrize("title", ["[Live]", "Gish [Deluxe]", "[bold]x[/bold]", ":smile:", "[red]"])
@pytest.mark.parametrize("color", [False, True])
def test_bracket_and_emoji_titles_render_literally(title, color):
    if color:
        plex_style.enable()
    table = plex_style.new_table("Title")
    table.add_row(plex_style.album(title))
    assert title in plex_style.strip(plex_style.render(table))
    assert plex_style.strip(plex_style.render(plex_style.line('"', plex_style.album(title), '"'))) == f'"{title}"\n'


# ---------------------------------------------------------------------------
# Tables, ellipsis, render, prompt_str
# ---------------------------------------------------------------------------

def test_color_off_table_is_ascii_without_codes():
    rendered = plex_style.render(plex_style.indent(sample_table()))
    assert "\x1b" not in rendered
    assert not BOX_CHARS & set(rendered)
    assert "+" in rendered and "|" in rendered and "-" in rendered
    rendered.encode("ascii")
    assert rendered.splitlines()[0].startswith("  +")


def test_color_on_table_uses_unicode_box_and_styles():
    plex_style.enable()
    rendered = plex_style.render(sample_table())
    assert "╭" in rendered and "│" in rendered
    assert "\x1b[1mSiamese Dream\x1b[0m" in rendered
    assert "\x1b[33m1993" in rendered  # cell padding shares the cell's style


def test_table_box_follows_stdout_color():
    from rich import box

    assert plex_style.table_box() is box.ASCII
    plex_style.enable()
    assert plex_style.table_box() is box.ROUNDED


def test_new_table_header_kwargs_and_fold():
    table = plex_style.new_table("Folder", ("Tracks", {"justify": "right"}))
    assert [c.header for c in table.columns] == ["Folder", "Tracks"]
    assert table.columns[1].justify == "right"
    assert all(c.overflow == "fold" for c in table.columns)


def test_table_fits_console_width():
    plex_style.reset(width=40)
    table = plex_style.new_table("Folder")
    table.add_row(plex_style.path("x" * 100))
    rendered = plex_style.render(table)
    assert all(len(line) <= 40 for line in rendered.splitlines())
    assert "".join(rendered.split("|")).count("x") == 100  # folded, nothing hidden


def test_ellipsis_ascii_when_off_unicode_when_on():
    assert plex_style.ellipsis() == "..."
    plex_style.enable()
    if (plex_style.console().encoding or "").lower().startswith("utf"):
        assert plex_style.ellipsis() == "\u2026"


def test_width_is_console_width():
    assert plex_style.width() == 120
    plex_style.enable(width=90)
    assert plex_style.width() == 90


def test_prompt_str_plain_when_off():
    prompt = plex_style.line("Merge ", plex_style.artist("A"), "? [y/N] ")
    assert plex_style.prompt_str(prompt) == "Merge A? [y/N] "
    assert plex_style.prompt_str("plain? ") == "plain? "


def test_prompt_str_colored_ends_plain_when_on():
    plex_style.enable()
    prompt = plex_style.line("Merge ", plex_style.artist("A"), "? [y/N] ")
    rendered = plex_style.prompt_str(prompt)
    assert rendered == "Merge \x1b[36mA\x1b[0m? [y/N] "
    last_code = rendered.rindex("\x1b[")
    assert rendered[last_code:].startswith("\x1b[0m")
    assert rendered.endswith("[y/N] ")


def test_prompt_str_plain_on_legacy_windows_console(monkeypatch):
    plex_style.enable()
    monkeypatch.setattr(plex_style.console(), "legacy_windows", True)
    prompt = plex_style.line("Merge ", plex_style.artist("A"), "? [y/N] ")
    assert plex_style.prompt_str(prompt) == "Merge A? [y/N] "


def test_out_and_blank_write_to_current_stdout(capsys):
    plex_style.out(plex_style.line("a"), sample_table())
    plex_style.blank()
    out = capsys.readouterr().out
    assert out.startswith("a\n+")
    assert out.endswith("+\n\n")


def test_long_line_is_never_wrapped():
    plex_style.reset(width=20)
    text = plex_style.line("word " * 10)
    assert plex_style.render(text) == "word " * 10 + "\n"


def test_redirect_to_cp1252_file_is_safe():
    import plex_merge_tool as pm
    import plex_split_tool as ps

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    console = plex_style.make_console(color=False, file=stream, width=80)
    target = pm.MergeEntry(
        obj=None, title="Siamese Dream", rating_key=1, track_count=13, artist="Smashing Pumpkins",
        artist_key=5, year=1993, directories=["E:\\MP3\\Rock\\Smashing Pumpkins\\Some Very Long Folder\\1993 - Siamese Dream"],
    )
    other = pm.MergeEntry(
        obj=None, title="Siamese Dream", rating_key=2, track_count=13, artist="Smashing Pumpkins",
        artist_key=5, year=2011, directories=["E:\\MP3\\Rock\\Smashing Pumpkins\\Another Long Folder Name\\2011 - Siamese Dream"],
    )
    candidate = ps.SplitCandidate(
        rating_key=3, title="Gish", artist="Smashing Pumpkins", year=1991, track_count=26,
        roots=[ps.AlbumRoot(key="k", display="/music/" + "deep/" * 30 + "Gish", track_count=13, folders=["a", "b"])],
    )
    for item in [*pm.describe_album_pair(target, other, width=80), *ps.describe_candidate(candidate, width=80),
                 ps.split_report_table([(3, "Gish", 13), (4, "Gish (Reissue)", 13)], 3)]:
        console.print(item)
    console.print(pm.format_album_prompt(target, other))
    stream.flush()
    data = raw.getvalue()
    text = data.decode("ascii")
    assert "\x1b" not in text
    assert "..." in text


def test_make_console_color_flag():
    plain = plex_style.make_console(color=False, width=50)
    colored = plex_style.make_console(color=True, width=50)
    assert plain.color_system is None and plain.no_color
    assert colored.color_system == "standard" and colored.is_terminal
    assert plain.width == colored.width == 50


# ---------------------------------------------------------------------------
# configure(): detection and precedence
# ---------------------------------------------------------------------------

def test_tty_turns_color_on(posix):
    assert configure() is True
    assert plex_style.is_enabled() and plex_style.is_enabled(err=True)


@pytest.mark.parametrize("env", [{"NO_COLOR": "1"}, {"NO_COLOR": "anything"}, {"TERM": "dumb"}])
def test_env_turns_color_off(posix, env):
    assert configure(env=env) is False
    assert not plex_style.is_enabled(err=True)


def test_empty_no_color_is_ignored(posix):
    assert configure(env={"NO_COLOR": ""}) is True


@pytest.mark.parametrize("stdout", [FakeTTY(False), FakeTTY(raises=True), object()])
def test_non_tty_stdout_turns_color_off(posix, stdout):
    assert configure(stdout=stdout) is False


def test_missing_stream_turns_color_off(posix, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    assert configure(stdout=None) is False


def test_no_color_flag_wins_over_force_color(posix):
    assert configure(no_color=True, env={"FORCE_COLOR": "1"}) is False
    assert not plex_style.is_enabled(err=True)


def test_no_color_env_wins_over_force_color(posix):
    assert configure(env={"NO_COLOR": "1", "FORCE_COLOR": "1"}) is False


def test_force_color_turns_on_without_tty_and_beats_term_dumb(posix):
    assert configure(stdout=FakeTTY(False), stderr=FakeTTY(False), env={"FORCE_COLOR": "1", "TERM": "dumb"})
    assert plex_style.is_enabled(err=True)


def test_force_color_zero_is_ignored(posix):
    assert configure(stdout=FakeTTY(False), env={"FORCE_COLOR": "0"}) is False


def test_stdout_tty_stderr_redirected(posix):
    configure(stdout=FakeTTY(True), stderr=FakeTTY(False))
    assert plex_style.is_enabled() is True
    assert plex_style.is_enabled(err=True) is False


def test_configure_can_turn_color_back_off(posix):
    configure()
    configure(stdout=FakeTTY(False), stderr=FakeTTY(False))
    assert not plex_style.is_enabled() and not plex_style.is_enabled(err=True)


def test_reset_turns_both_off():
    plex_style.enable()
    plex_style.reset()
    assert not plex_style.is_enabled() and not plex_style.is_enabled(err=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"env": {"NO_COLOR": "1"}},
        {"no_color": True},
        {"env": {"TERM": "dumb"}},
        {"stdout": FakeTTY(False), "stderr": FakeTTY(False)},
    ],
    ids=["NO_COLOR", "--no-color", "TERM=dumb", "non-TTY"],
)
def test_color_off_consoles_have_no_color_system(posix, kwargs):
    configure(width=80, **kwargs)
    assert plex_style.console().color_system is None
    assert plex_style.console(err=True).color_system is None
    rendered = plex_style.render(plex_style.indent(sample_table()))
    assert "\x1b" not in rendered and not BOX_CHARS & set(rendered)


def test_force_color_with_term_dumb_still_has_a_color_system(posix):
    configure(stdout=FakeTTY(False), stderr=FakeTTY(False), env={"FORCE_COLOR": "1", "TERM": "dumb"}, width=80)
    assert plex_style.console().color_system is not None
    assert "\x1b[1m" in plex_style.render(plex_style.album("x"))


def test_configure_on_writes_colors_and_unicode_box(posix, capsys):
    configure(width=80)
    plex_style.out(sample_table())
    out = capsys.readouterr().out
    assert "╭" in out and "\x1b[" in out


def test_configure_width_none_detects(posix):
    configure(env={"COLUMNS": "77"})
    assert plex_style.width() == 77


# ---------------------------------------------------------------------------
# configure(): Windows VT mode
# ---------------------------------------------------------------------------

class Recorder:
    """A stand-in for _enable_windows_vt that records calls and returns (or raises) a fixed result."""

    def __init__(self, result=True, raises=None):
        self.result = result
        self.raises = raises
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture
def windows(monkeypatch):
    """Pretend to be on Windows with VT enabling succeeding (override per test)."""
    monkeypatch.setattr(plex_style, "_IS_WINDOWS", True)
    vt = Recorder(True)
    monkeypatch.setattr(plex_style, "_enable_windows_vt", vt)
    return vt


def test_windows_enables_vt_once_when_color_on(windows):
    assert configure(stdout=FakeTTY(False), stderr=FakeTTY(False), env={"FORCE_COLOR": "1"}) is True
    assert configure(stdout=FakeTTY(False), stderr=FakeTTY(False), env={"FORCE_COLOR": "1"}) is True
    assert windows.calls == 1
    assert plex_style._vt_state == "enabled"


def test_windows_no_vt_attempt_when_color_off(windows):
    configure(stdout=FakeTTY(False), stderr=FakeTTY(False))
    configure(no_color=True)
    assert windows.calls == 0
    assert plex_style._vt_state == ""


def test_windows_vt_failure_keeps_color_on_and_is_retried(windows):
    windows.result = False
    env = {"FORCE_COLOR": "1"}
    assert configure(stdout=FakeTTY(False), stderr=FakeTTY(False), env=env) is True
    assert plex_style.is_enabled(err=True)
    assert plex_style._vt_state == "unavailable"
    windows.result = True
    assert configure(stdout=FakeTTY(False), stderr=FakeTTY(False), env=env) is True
    assert windows.calls == 2
    assert plex_style._vt_state == "enabled"


def test_windows_vt_exception_is_swallowed(windows):
    windows.raises = OSError("no kernel32")
    assert configure(stdout=FakeTTY(False), stderr=FakeTTY(False), env={"FORCE_COLOR": "1"}) is True
    assert plex_style._vt_state == "unavailable"


def test_windows_forced_non_tty_stream_uses_ansi(windows):
    windows.result = False
    configure(stdout=FakeTTY(False), stderr=FakeTTY(False), env={"FORCE_COLOR": "1"}, width=80)
    assert plex_style.console().legacy_windows is False
    assert "\x1b[1m" in plex_style.render(plex_style.album("x"))


def test_non_windows_never_attempts_vt(posix, monkeypatch):
    vt = Recorder()
    monkeypatch.setattr(plex_style, "_enable_windows_vt", vt)
    assert configure() is True
    assert configure(stdout=FakeTTY(False), stderr=FakeTTY(False)) is False
    assert vt.calls == 0
    assert plex_style._vt_state == ""


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows failure path")
def test_windows_vt_fails_quietly_off_windows():
    assert plex_style._enable_windows_vt() is False


def test_colorama_code_is_gone():
    for name in ("_colorama_just_fix", "_colorama_init", "_fix_windows_console", "_console_method", "paint"):
        assert not hasattr(plex_style, name)


# ---------------------------------------------------------------------------
# diagnose()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("is_windows", [True, False])
def test_diagnose_runs(monkeypatch, capsys, is_windows):
    monkeypatch.setattr(plex_style, "_IS_WINDOWS", is_windows)
    monkeypatch.setattr(plex_style, "_enable_windows_vt", Recorder(False))
    plex_style.diagnose()
    out = capsys.readouterr().out
    assert sys.executable in out
    assert "rich:" in out
    assert "Windows VT mode:" in out
    assert "color system:" in out
    if not is_windows:
        assert "not needed (not Windows)" in out
    for role in plex_style.STYLES:
        assert f"Sample text for {role}" in out
    assert "Siamese Dream" in out


def test_diagnose_reports_vt_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(plex_style, "_IS_WINDOWS", True)
    monkeypatch.setattr(plex_style, "_enable_windows_vt", Recorder(False))
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    plex_style.diagnose()
    assert "unavailable (rich uses its legacy Windows renderer)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# input_prompt()
# ---------------------------------------------------------------------------

def test_input_prompt_unchanged_when_off(monkeypatch):
    monkeypatch.setitem(sys.modules, "readline", object())
    assert plex_style.input_prompt("Merge? ") == "Merge? "


def test_input_prompt_marks_codes_for_readline(monkeypatch):
    monkeypatch.setitem(sys.modules, "readline", object())
    plex_style.enable()
    prompt = "Merge \x1b[36mA\x1b[0m? "
    marked = plex_style.input_prompt(prompt)
    assert marked == "Merge \x01\x1b[36m\x02A\x01\x1b[0m\x02? "
    assert plex_style.strip(marked) == "Merge A? "


def test_input_prompt_unchanged_without_readline(monkeypatch):
    monkeypatch.delitem(sys.modules, "readline", raising=False)
    plex_style.enable()
    prompt = "Merge \x1b[36mA\x1b[0m? "
    assert plex_style.input_prompt(prompt) == prompt


def test_prompt_str_marks_codes_for_readline(monkeypatch):
    monkeypatch.setitem(sys.modules, "readline", object())
    plex_style.enable()
    prompt = plex_style.line("Merge ", plex_style.artist("A"), "? ")
    assert plex_style.prompt_str(prompt) == "Merge \x01\x1b[36m\x02A\x01\x1b[0m\x02? "
