from datetime import datetime
from unittest.mock import MagicMock

import pytest

import plex_common as pc


def scripted_input(answers):
    remaining = list(answers)

    def fake_input(prompt=""):
        return remaining.pop(0)

    return fake_input


def test_attr_returns_none_when_absent():
    class Partial:
        title = "x"

    assert pc.attr(Partial(), "title") == "x"
    assert pc.attr(Partial(), "rating") is None


@pytest.mark.parametrize("path, expected", [
    ("E:\\MP3\\A", True),
    ("e:/mp3/a", True),
    ("\\\\nas\\music\\a", True),
    ("/music/a", False),
    ("music/a", False),
])
def test_is_windows_path(path, expected):
    assert pc.is_windows_path(path) is expected


def test_server_dirname_uses_server_path_style():
    assert pc.server_dirname("E:\\MP3\\A\\01.mp3") == "E:\\MP3\\A"
    assert pc.server_dirname("/music/A/01.mp3") == "/music/A"


def test_folder_key_windows_case_and_separators():
    assert pc.folder_key("E:\\MP3\\A\\") == pc.folder_key("e:/mp3/a")


def test_folder_key_posix_is_case_sensitive():
    assert pc.folder_key("/m/A") != pc.folder_key("/m/a")
    assert pc.folder_key("/m/A/") == "/m/A"
    assert pc.folder_key("/") == "/"


def test_dedupe_folders_keeps_first_seen_and_sorts():
    assert pc.dedupe_folders(["E:\\B", "E:\\A", "e:/a"]) == ["E:\\A", "E:\\B"]


def test_same_folders():
    assert pc.same_folders(["E:\\A", "E:\\B"], ["e:/b", "e:/a"])
    assert not pc.same_folders(["E:\\A"], ["E:\\A", "E:\\B"])


def test_album_year_prefers_year_then_release_date():
    assert pc.album_year(MagicMock(year=1993, originallyAvailableAt=None)) == 1993
    assert pc.album_year(MagicMock(year=None, originallyAvailableAt=datetime(2011, 1, 1))) == 2011
    assert pc.album_year(MagicMock(year=None, originallyAvailableAt=None)) is None


@pytest.mark.parametrize("answer, expected", [("y", True), (" YES ", True), ("", False), ("no", False)])
def test_ask_yes_no(answer, expected):
    assert pc.ask_yes_no("? ", scripted_input([answer])) is expected


def test_ask_yes_no_reprompts(capsys):
    assert pc.ask_yes_no("? ", scripted_input(["maybe", "n"])) is False
    assert "Please answer y or n." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# question() / ask_yes_no() with Text prompts
# ---------------------------------------------------------------------------

def test_question_appends_hint():
    text = pc.question('Split "{title}"?', title=pc.style.album("[Live]"))
    assert text.plain == 'Split "[Live]"? [y/N] '
    assert pc.YES_NO_HINT == "[y/N]"


def test_ask_yes_no_text_prompt_passes_plain_str_when_off():
    seen = []

    def fake_input(prompt):
        seen.append(prompt)
        return "y"

    prompt = pc.question('Merge "{a}"?', a=pc.style.artist("A"))
    assert pc.ask_yes_no(prompt, fake_input) is True
    assert seen == ['Merge "A"? [y/N] ']
    assert isinstance(seen[0], str)


def test_ask_yes_no_text_prompt_answer_is_not_colored_when_on():
    seen = []
    pc.style.enable()
    prompt = pc.question('Merge "{a}"?', a=pc.style.artist("A"))
    pc.ask_yes_no(prompt, lambda p: seen.append(p) or "")
    assert seen[0] == 'Merge "\x1b[36mA\x1b[0m"? [y/N] '


# ---------------------------------------------------------------------------
# shorten_path()
# ---------------------------------------------------------------------------

SIAMESE = "E:\\MP3\\Rock\\Smashing Pumpkins\\1993 - Siamese Dream"


def test_shorten_path_short_path_unchanged():
    assert pc.shorten_path("E:\\MP3\\Gish", 40) == "E:\\MP3\\Gish"
    assert pc.shorten_path(SIAMESE, len(SIAMESE)) == SIAMESE


@pytest.mark.parametrize("width, expected", [
    (34, "E:\\MP3\\Rock\\\u2026\\1993 - Siamese Dream"),
    (29, "E:\\MP3\\\u2026\\1993 - Siamese Dream"),
    (25, "E:\\\u2026\\1993 - Siamese Dream"),
])
def test_shorten_path_windows_keeps_drive_and_album_folder(width, expected):
    assert pc.shorten_path(SIAMESE, width) == expected


def test_shorten_path_grows_tail_with_parent_folder():
    path = "E:\\MP3\\Rock\\Alt\\Pumpkins\\Gish"
    # Head first (MP3), then the parent folder (Pumpkins), then Rock.
    assert pc.shorten_path(path, 24) == "E:\\MP3\\\u2026\\Pumpkins\\Gish"
    assert pc.shorten_path(path, 28) == "E:\\MP3\\Rock\\\u2026\\Pumpkins\\Gish"


def test_shorten_path_posix():
    assert pc.shorten_path("/music/a/b/c/Gish", 14) == "/music/\u2026/Gish"
    assert pc.shorten_path("/music/a/b/c/Gish", 16) == "/music/\u2026/c/Gish"
    assert pc.shorten_path("music/a/b/c/Gish", 12) == "music/\u2026/Gish"


def test_shorten_path_forward_slash_windows_keeps_slash():
    assert pc.shorten_path("E:/MP3/Rock/Smashing Pumpkins/Gish", 18) == "E:/MP3/Rock/\u2026/Gish"


def test_shorten_path_unc_keeps_share():
    path = "\\\\nas\\music\\Rock\\Smashing Pumpkins\\Gish"
    result = pc.shorten_path(path, 25)
    assert result.startswith("\\\\nas\\music\\")
    assert result.endswith("\\Gish")
    assert len(result) <= 25


def test_shorten_path_trailing_separator_dropped():
    assert pc.shorten_path("/music/a/b/c/Gish/", 14) == "/music/\u2026/Gish"


def test_shorten_path_cuts_at_separators():
    path = "/music/Alpha/Beta/Gamma/Delta/Epsilon/Album"
    for width in range(len("/\u2026/Album"), len(path)):
        result = pc.shorten_path(path, width)
        assert len(result) <= width
        kept = [p for p in result.split("/") if p and p != "\u2026"]
        assert all(p in path.split("/") for p in kept)
        assert result.endswith("/Album")


def test_shorten_path_single_long_component_cuts_characters():
    result = pc.shorten_path("A" * 10 + "B" * 20, 12)
    assert len(result) == 12
    assert result == "AAA\u2026" + "B" * 8


def test_shorten_path_album_folder_too_long_cuts_characters():
    result = pc.shorten_path("E:\\MP3\\" + "x" * 50, 20, "...")
    assert len(result) == 20 and "..." in result


@pytest.mark.parametrize("width, ellipsis, expected", [
    (0, "\u2026", ""),
    (-3, "\u2026", ""),
    (1, "\u2026", "\u2026"),
    (2, "...", ".."),
    (3, "...", "..."),
])
def test_shorten_path_very_narrow(width, ellipsis, expected):
    assert pc.shorten_path(SIAMESE, width, ellipsis) == expected


def test_shorten_path_ascii_ellipsis():
    assert pc.shorten_path(SIAMESE, 31, "...") == "E:\\MP3\\...\\1993 - Siamese Dream"


@pytest.mark.parametrize("width", range(0, 60))
def test_shorten_path_never_exceeds_width(width):
    for path in (SIAMESE, "/music/a/b/c/Gish", "\\\\nas\\music\\a\\b", "Gish"):
        assert len(pc.shorten_path(path, width)) <= width
