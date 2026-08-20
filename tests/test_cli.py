"""The dispatcher: what `podbench` itself owns, before any verb is imported."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from typing import Any

import pytest

from podbench import __version__
from podbench.__main__ import ENTRY_POINTS, main


def test_cli_version():
    """One token on stdout, and nothing else on the line.

    Not cosmetic. ``launcher.probe_seat_version`` execs exactly this inside the
    seat and reads stdout as the seat's build, so a banner, a `podbench,
    version X` prefix or a second line would make every seat report as unknown
    — and the skew this exists to catch would go back to being invisible.
    """
    cmd = [sys.executable, "-m", "podbench", "--version"]
    printed = subprocess.check_output(cmd).decode().strip()
    assert printed == __version__
    assert len(printed.split()) == 1


def test_help_lists_every_verb_under_where_it_runs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The two panels are the first question a reader has — "will this even work
    # from here?" — and they are generated from ENTRY_POINTS, so a verb added
    # without a panel shows up here rather than in a screenshot.
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "On your machine" in out
    assert "Inside the debug container" in out
    for verb in ENTRY_POINTS:
        assert verb in out


def test_no_verb_prints_the_help_and_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 2
    assert "Usage: podbench" in capsys.readouterr().out


def test_an_unknown_verb_is_refused_here(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["frobnicate"]) == 2
    assert "frobnicate" in capsys.readouterr().err


def test_help_after_a_verb_belongs_to_the_verb(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # This level deliberately declares no --help of its own: `podbench dev
    # --help` has to reach dev's parser, or the only help a reader can get for a
    # verb is the one-line summary in the index above.
    assert main(["dev", "--help"]) == 0
    out = capsys.readouterr().out
    assert "Usage: podbench dev" in out
    assert "--take-traffic" in out


def test_a_verbs_own_flags_are_never_claimed_here(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --launch consumes the rest of the command line in gdbcmd, and --fast is
    # not a flag this level or dbg knows. Both have to arrive intact.
    assert main(["dbg", "--dry-run", "--launch", "./victim", "--fast"]) == 0
    assert "set args --fast" in capsys.readouterr().out


def test_a_verbs_pass_through_command_survives_the_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sibling above passes for `dbg` because --launch swallows the rest by
    # its own design. `run` instead relies on `--`, and this level eats it:
    # forwarding argv verbatim is done with ignore_unknown_options, and click
    # consumes the separator on the way through, so dev's parser never sees
    # one. `podbench run --port 8080 -- python -m api` — the relaunch loop's
    # documented form — then died on `No such option: -m`, having started
    # nothing. Order matters as much as arrival: this is an argv, not a set.
    from podbench import dev

    seen: list[list[str]] = []

    def capture(command: Sequence[str], **_: Any) -> dev.LaunchResult:
        seen.append(list(command))
        return dev.LaunchResult(ok=True, detail="")

    monkeypatch.setattr(dev, "start", capture)
    assert main(["run", "--port", "8080", "--", "python", "-m", "api", "-c", "x"]) == 0
    assert seen == [["python", "-m", "api", "-c", "x"]]
