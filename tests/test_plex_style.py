import sys
import types

import pytest

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


@pytest.fixture
def posix(monkeypatch):
    """Pretend not to be on Windows so configure() never touches colorama."""
    monkeypatch.setattr(plex_style, "_IS_WINDOWS", False)


def configure(**kwargs):
    kwargs.setdefault("stdout", FakeTTY(True))
    kwargs.setdefault("stderr", FakeTTY(True))
    kwargs.setdefault("env", {})
    return plex_style.configure(**kwargs)


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", WRAPPERS)
def test_off_by_default_returns_text_unchanged(name):
    assert getattr(plex_style, name)("Siamese Dream") == "Siamese Dream"
    assert getattr(plex_style, name)("x", err=True) == "x"
    assert plex_style.paint(name, 5) == "5"


@pytest.mark.parametrize("name", WRAPPERS)
def test_enabled_wraps_with_role_code_and_reset(name):
    plex_style.enable()
    code = plex_style.STYLES[name]
    assert getattr(plex_style, name)("x") == f"\x1b[{code}mx\x1b[0m"
    assert getattr(plex_style, name)(1993) == f"\x1b[{code}m1993\x1b[0m"
    assert getattr(plex_style, name)("") == ""


def test_scheme_codes():
    assert plex_style.STYLES == {
        "artist": "36", "album": "1", "year": "33", "count": "32", "path": "2",
        "key": "2", "warn": "33", "error": "31", "ok": "32",
    }
    # Never the bright 9x variants.
    assert not any(code.startswith("9") for code in plex_style.STYLES.values())


def test_streams_are_independent():
    plex_style.enable(out=True, err=False)
    assert plex_style.warn("w") == "\x1b[33mw\x1b[0m"
    assert plex_style.warn("w", err=True) == "w"


@pytest.mark.parametrize("status, code", [("ok", "32"), ("warn", "33"), ("error", "31")])
def test_tag_colors(status, code):
    plex_style.enable()
    assert plex_style.tag("[same folder]", status) == f"\x1b[{code}m[same folder]\x1b[0m"


def test_tag_bad_status_raises():
    with pytest.raises(ValueError):
        plex_style.tag("[x]", "blue")


def test_strip_removes_codes_and_readline_markers():
    plex_style.enable()
    text = f"merge {plex_style.artist('A')} ({plex_style.count('5 albums')})? "
    assert plex_style.strip(text) == "merge A (5 albums)? "
    assert plex_style.strip("\x01\x1b[36m\x02A\x01\x1b[0m\x02") == "A"


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


# ---------------------------------------------------------------------------
# configure(): Windows / colorama
# ---------------------------------------------------------------------------

class Recorder:
    """A stand-in console method that records calls and returns (or raises) a fixed result."""

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
    """Pretend to be on Windows with native VT and colorama.init() both failing (override per test)."""
    monkeypatch.setattr(plex_style, "_IS_WINDOWS", True)
    native = Recorder(False)
    init = Recorder(False)
    monkeypatch.setattr(plex_style, "_enable_native_vt", native)
    monkeypatch.setattr(plex_style, "_colorama_init", init)
    return types.SimpleNamespace(native=native, init=init)


def fake_colorama(monkeypatch, just_fix=True):
    """Installs a fake colorama; just_fix=False mimics colorama < 0.4.6 (no just_fix_windows_console)."""
    module = types.ModuleType("colorama")
    module.__version__ = "0.4.6" if just_fix else "0.4.4"
    module.calls = 0
    module.init_calls = 0

    def just_fix_windows_console():
        module.calls += 1

    def init():
        module.init_calls += 1

    if just_fix:
        module.just_fix_windows_console = just_fix_windows_console
    module.init = init
    monkeypatch.setitem(sys.modules, "colorama", module)
    return module


def test_windows_fixes_console_once(windows, monkeypatch):
    module = fake_colorama(monkeypatch)
    assert configure() is True
    assert configure() is True
    assert module.calls == 1


def test_windows_no_console_fix_when_color_off(windows, monkeypatch):
    module = fake_colorama(monkeypatch)
    configure(stdout=FakeTTY(False), stderr=FakeTTY(False))
    assert module.calls == 0


def test_windows_without_colorama_turns_color_off(windows, monkeypatch):
    monkeypatch.setitem(sys.modules, "colorama", None)  # import raises ImportError
    assert configure() is False
    assert not plex_style.is_enabled(err=True)


def test_windows_colorama_failure_turns_color_off(windows, monkeypatch):
    module = fake_colorama(monkeypatch)

    def broken():
        raise OSError("no console")

    module.just_fix_windows_console = broken
    assert configure() is False


def test_windows_force_color_without_colorama_stays_on(windows, monkeypatch):
    monkeypatch.setitem(sys.modules, "colorama", None)
    assert configure(env={"FORCE_COLOR": "1"}) is True


def test_windows_fix_failure_is_not_cached(windows, monkeypatch):
    monkeypatch.setattr(plex_style, "_fix_windows_console", lambda: False)
    assert configure() is False


def test_windows_new_colorama_is_used_first(windows, monkeypatch):
    fake_colorama(monkeypatch)
    assert configure() is True
    assert plex_style._console_method == "colorama"
    assert windows.native.calls == 0 and windows.init.calls == 0


def test_windows_old_colorama_falls_back_to_native_vt(windows, monkeypatch):
    # Regression: colorama < 0.4.6 has no just_fix_windows_console, which used to turn color off.
    module = fake_colorama(monkeypatch, just_fix=False)
    windows.native.result = True
    assert configure() is True
    assert plex_style.is_enabled(err=True)
    assert plex_style._console_method == "native VT"
    assert windows.native.calls == 1 and windows.init.calls == 0
    assert module.init_calls == 0


def test_windows_without_colorama_uses_native_vt(windows, monkeypatch):
    monkeypatch.setitem(sys.modules, "colorama", None)
    windows.native.result = True
    assert configure() is True
    assert plex_style._console_method == "native VT"


def test_windows_colorama_init_is_last_resort(windows, monkeypatch):
    fake_colorama(monkeypatch, just_fix=False)
    windows.init.result = True
    assert configure() is True
    assert plex_style._console_method == "colorama.init"
    assert windows.native.calls == 1 and windows.init.calls == 1


def test_windows_all_methods_fail_turns_color_off(windows, monkeypatch):
    fake_colorama(monkeypatch, just_fix=False)
    assert configure() is False
    assert not plex_style.is_enabled(err=True)
    assert plex_style._console_method == "none"
    assert configure(env={"FORCE_COLOR": "1"}) is True


def test_windows_failure_is_retried_next_time(windows, monkeypatch):
    monkeypatch.setitem(sys.modules, "colorama", None)
    assert configure() is False
    windows.native.result = True
    assert configure() is True
    assert windows.native.calls == 2


def test_windows_exceptions_from_every_method_are_swallowed(windows, monkeypatch):
    module = fake_colorama(monkeypatch)

    def broken():
        raise RuntimeError("boom")

    module.just_fix_windows_console = broken
    windows.native.raises = OSError("no kernel32")
    windows.init.raises = ValueError("bad stream")
    assert configure() is False
    assert windows.native.calls == 1 and windows.init.calls == 1


def test_windows_success_is_cached(windows, monkeypatch):
    monkeypatch.setitem(sys.modules, "colorama", None)
    windows.native.result = True
    configure()
    configure()
    configure()
    assert windows.native.calls == 1


def test_non_windows_never_attempts_a_fix(posix, monkeypatch):
    native, init, just_fix = Recorder(), Recorder(), Recorder()
    monkeypatch.setattr(plex_style, "_enable_native_vt", native)
    monkeypatch.setattr(plex_style, "_colorama_init", init)
    monkeypatch.setattr(plex_style, "_colorama_just_fix", just_fix)
    assert configure() is True
    assert configure(stdout=FakeTTY(False), stderr=FakeTTY(False)) is False
    assert native.calls == init.calls == just_fix.calls == 0
    assert plex_style._console_method == ""


def test_colorama_init_skipped_without_a_real_terminal(monkeypatch):
    module = fake_colorama(monkeypatch, just_fix=False)
    monkeypatch.setattr(sys, "stdout", FakeTTY(False))
    monkeypatch.setattr(sys, "stderr", FakeTTY(False))
    assert plex_style._colorama_init() is False
    assert module.init_calls == 0


def test_colorama_init_used_for_a_real_terminal(monkeypatch):
    module = fake_colorama(monkeypatch, just_fix=False)
    monkeypatch.setattr(sys, "stdout", FakeTTY(True))
    assert plex_style._colorama_init() is True
    assert module.init_calls == 1


def test_colorama_just_fix_reports_old_or_missing_colorama(monkeypatch):
    fake_colorama(monkeypatch, just_fix=False)
    assert plex_style._colorama_just_fix() is False
    monkeypatch.setitem(sys.modules, "colorama", None)
    assert plex_style._colorama_just_fix() is False


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows failure path")
def test_native_vt_fails_quietly_off_windows():
    assert plex_style._enable_native_vt() is False


# ---------------------------------------------------------------------------
# diagnose()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("is_windows", [True, False])
def test_diagnose_runs(monkeypatch, capsys, is_windows):
    monkeypatch.setattr(plex_style, "_IS_WINDOWS", is_windows)
    monkeypatch.setattr(plex_style, "_enable_native_vt", Recorder(False))
    monkeypatch.setattr(plex_style, "_colorama_init", Recorder(False))
    fake_colorama(monkeypatch, just_fix=False)
    plex_style.diagnose()
    out = capsys.readouterr().out
    assert sys.executable in out
    assert "0.4.4 (just_fix_windows_console: no)" in out
    assert "Console method:" in out
    for role in plex_style.STYLES:
        assert f"Sample text for {role}" in out


def test_diagnose_without_colorama(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "colorama", None)
    monkeypatch.setattr(plex_style, "_IS_WINDOWS", False)
    plex_style.diagnose()
    assert "not installed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# input_prompt()
# ---------------------------------------------------------------------------

def test_input_prompt_unchanged_when_off(monkeypatch):
    monkeypatch.setitem(sys.modules, "readline", object())
    assert plex_style.input_prompt("Merge? ") == "Merge? "


def test_input_prompt_marks_codes_for_readline(monkeypatch):
    monkeypatch.setitem(sys.modules, "readline", object())
    plex_style.enable()
    prompt = f"Merge {plex_style.artist('A')}? "
    marked = plex_style.input_prompt(prompt)
    assert marked == "Merge \x01\x1b[36m\x02A\x01\x1b[0m\x02? "
    assert plex_style.strip(marked) == "Merge A? "


def test_input_prompt_unchanged_without_readline(monkeypatch):
    monkeypatch.delitem(sys.modules, "readline", raising=False)
    plex_style.enable()
    prompt = f"Merge {plex_style.artist('A')}? "
    assert plex_style.input_prompt(prompt) == prompt
