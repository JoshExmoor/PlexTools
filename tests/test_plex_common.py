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
