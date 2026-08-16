"""``image/bin/``, the two files that survived #47, checked as an interface.

The image cannot be built here, so what is checkable is the contract between
these scripts and the Python that names them. Since #47 that contract rests on
one file: ``/usr/local/bin/podbench`` is the *only* way an ssh session reaches
any in-pod verb, because sshd is built ``UsePAM no`` (report 4.1) and hands a
non-interactive command the compiled-in ``PATH`` — which has ``/usr/local/bin``
and not the venv. Before #47 seven files each hard-coded the venv path
independently; now a typo in this one takes every verb with it, and the failure
is ``exec: "podbench": executable file not found`` at the far end of a
transport, not a build error.

``gdb-podbench``'s own behaviour is exercised in ``test_gdb_wrapper.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from podbench.editor import DEBUG_CONFIG_ARGV
from podbench.launcher import CAPREPORT_ARGV
from podbench.spec import AGENT_COMMAND
from podbench.vscode import GDB_WRAPPER

BIN = Path(__file__).resolve().parents[1] / "image" / "bin"

VENV_PODBENCH = "/app/.venv/bin/podbench"
"""The absolute path the shim execs. Rewritten to a stub to run it here."""

INSTALL_DIR = "/usr/local/bin"
"""Where the Dockerfile copies ``image/bin/`` wholesale."""


def test_the_image_ships_the_two_structural_files_and_nothing_else() -> None:
    """A re-added alias has to argue with this test first.

    Same job ``tests/test_packaging.py`` does for runtime dependencies: the
    deletion in #47 was a deviation from ``design-brief.md``, argued in
    ``image/README.md`` deviation 6, and a helper that reappears without that
    argument reinstates the second contract the deviation was about.
    """
    assert sorted(path.name for path in BIN.iterdir()) == [
        "gdb-podbench",
        "podbench",
    ]


def test_the_shim_names_the_venv_by_absolute_path() -> None:
    """``ssh host 'podbench pids'`` sources nothing, so ``PATH`` cannot help.

    The image's ``ENV PATH`` puts the venv first, which means a bare
    ``podbench`` inside the shim would work under ``docker run`` and under
    ``kubectl exec`` and fail only over ssh — the one transport this file
    exists for.
    """
    source = (BIN / "podbench").read_text()

    assert VENV_PODBENCH in source
    assert 'exec /app/.venv/bin/podbench "$@"' in source, (
        'the arguments must be forwarded as "$@": unquoted, `podbench dbg '
        '--launch ./prog "a b"` loses the target\'s own quoting'
    )


def test_the_shim_forwards_argv_unchanged(tmp_path: Path) -> None:
    """Run it, with the venv rewritten to a stub that reports what it got.

    The assertion that the rewrite matched is the point of :data:`VENV_PODBENCH`
    being a constant: the day the shim reaches the venv differently, this fails
    rather than quietly testing a script it no longer resembles.
    """
    stub = tmp_path / "stub-podbench"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done\n')
    stub.chmod(0o755)

    source = (BIN / "podbench").read_text()
    assert VENV_PODBENCH in source, "this test's rewrite is stale"
    script = tmp_path / "podbench"
    script.write_text(source.replace(VENV_PODBENCH, str(stub)))
    script.chmod(0o755)

    given = ["dbg", "--launch", "./prog", "a b", ""]
    result = subprocess.run(
        [str(script), *given], capture_output=True, text=True, check=True
    )

    assert result.stdout.split("\n")[: len(given)] == given


def test_every_name_the_launcher_execs_is_a_file_the_image_installs() -> None:
    """The dependency #47's issue said did not exist.

    ``CAPREPORT_ARGV`` was ``("capreport", "--json")`` and resolved only through
    a per-subcommand alias, so deleting the aliases broke the capability report
    silently — a 127 that surfaces as "capreport produced no parsable JSON".
    Every argv is checked here so the next such argv has to name a shipped
    file. ``DEBUG_CONFIG_ARGV`` is the one that proved the point: ``--open``
    was written against an image that still had the aliases, and a bare
    ``debug-config`` over ``kubectl exec`` reaches the seat as
    ``executable file not found`` and is reported as "printed no JSON".
    """
    installed = {path.name for path in BIN.iterdir()}

    assert CAPREPORT_ARGV[0] in installed
    assert AGENT_COMMAND[0] in installed
    assert DEBUG_CONFIG_ARGV[0] in installed
    assert GDB_WRAPPER == f"{INSTALL_DIR}/gdb-podbench"
