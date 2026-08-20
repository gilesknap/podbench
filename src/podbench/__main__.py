"""Interface for ``python -m podbench``, and the ``podbench`` console script.

One binary serves both halves of the tool. On a developer's machine it is
reached as ``podbench <verb>`` — canonically ``uvx podbench <verb>``, with nothing
installed — and drives the cluster; inside the debug container the same binary is
PID 1 (``podbench agent``) and answers to the same spelling for the in-pod verbs
(``podbench pids``, ``podbench dbg``, ``podbench capreport``, ...). Keeping it as
one package means the capability logic that decides what a session can do is the
same code in both places, rather than a launcher's guess and a helper's separate
guess.

Dispatch is deliberately dumb: each verb names a module, and the module owns its
own argument parsing. Two of them predate the dispatcher and parse an argv that
does not include the verb, so those are sliced; the rest are forwarded verbatim.
Imports are done lazily per verb because ``podbench --version`` runs as the
image's build-time smoke test, and it should not depend on every module in the
package importing cleanly.

That laziness is why the verbs here are declared with ``add_help_option=False``
and swallow everything after their own name. ``podbench dev --help`` has to
reach ``dev``'s parser to be worth reading, so this level must not claim
``--help`` for itself — the only help it owns is the two-panel index below,
which is built from :data:`ENTRY_POINTS` and needs no verb imported to render.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Annotated, NamedTuple

import typer

from . import __version__
from .cli import new_app, run

__all__ = ["ENTRY_POINTS", "main"]

#: Where a verb runs. Rendered as the two panels of ``podbench --help``, because
#: "will this even work from here?" is the first question every verb raises.
LAPTOP = "On your machine"
IN_POD = "Inside the debug container"


class Verb(NamedTuple):
    """One row of the dispatch table, and one line of the top-level help."""

    handler: Callable[[Sequence[str]], int]
    #: False for a module whose parser is a single command and so does not
    #: expect its own name in argv - typer collapses a one-command app into the
    #: app itself. The others own a subcommand keyed on the verb and need it
    #: kept.
    keep_verb: bool
    help: str
    panel: str


def _agent(args: Sequence[str]) -> int:
    from .agent import main as run_verb

    return run_verb(args)


def _capreport(args: Sequence[str]) -> int:
    from .probe import main as run_verb

    return run_verb(args)


def _gdbcmd(args: Sequence[str]) -> int:
    from .gdbcmd import main as run_verb

    return run_verb(args)


def _doctor(args: Sequence[str]) -> int:
    from .doctor import main as run_verb

    return run_verb(args)


def _dev(args: Sequence[str]) -> int:
    from .dev import main as run_verb

    return run_verb(args)


def _launcher(args: Sequence[str]) -> int:
    from .launcher import main as run_verb

    return run_verb(args)


def _hotfix(args: Sequence[str]) -> int:
    from .hotfix import main as run_verb

    return run_verb(args)


def _vscode(args: Sequence[str]) -> int:
    from .vscode import main as run_verb

    return run_verb(args)


#: Verb -> everything this level knows about it. Ordered, because that is the
#: order the two help panels list them in.
ENTRY_POINTS: dict[str, Verb] = {
    # Laptop side: `podbench ...`. `doctor` leads because under `uvx` there is
    # no install step to hang first-run setup on, so it is the verb that has to
    # be findable before the first attach rather than after it fails.
    "doctor": Verb(
        _doctor,
        False,
        "check this machine can attach, and name what stops it",
        LAPTOP,
    ),
    "attach": Verb(
        _launcher,
        True,
        "add or reconnect a podbench container and print the report",
        LAPTOP,
    ),
    # `_launcher`, not `_vscode`: the handler named for that module serves the
    # *in-pod* `debug-config`, and this verb is the laptop half that drives it.
    "vscode": Verb(
        _launcher,
        True,
        "land a seat sized and provisioned for an editor, and open it",
        LAPTOP,
    ),
    "ssh-config": Verb(
        _launcher, True, "regenerate the ssh stanza for an existing session", LAPTOP
    ),
    "status": Verb(
        _launcher,
        True,
        "the podbench containers in one pod and what each supports",
        LAPTOP,
    ),
    "list": Verb(
        _launcher,
        True,
        "every pod in the namespace carrying a podbench container",
        LAPTOP,
    ),
    "dev": Verb(_dev, True, "create or delete the dev pod", LAPTOP),
    # `hotfix status` is a *sub*-verb of that parser and does not collide with
    # the launcher's own `status`.
    "hotfix": Verb(
        _hotfix, True, "durable in-place fixes on a claim-backed venv", LAPTOP
    ),
    # In-pod: PID 1 and the helpers on PATH.
    "agent": Verb(
        _agent, False, "prepare the container for ssh and idle as its PID 1", IN_POD
    ),
    "capreport": Verb(
        _capreport,
        False,
        "name the mechanism that denies ptrace in this container",
        IN_POD,
    ),
    "pids": Verb(_gdbcmd, True, "list the target container's processes", IN_POD),
    "dbg": Verb(_gdbcmd, True, "debug a process", IN_POD),
    "debug-config": Verb(
        _vscode, False, "write VS Code's launch.json for this seat", IN_POD
    ),
    "dev-bootstrap": Verb(
        _dev, True, "clone, sync and editable-install a checkout", IN_POD
    ),
    "run": Verb(_dev, True, "relaunch the app and verify it", IN_POD),
    "stop": Verb(_dev, True, "stop the recorded child", IN_POD),
}


_Tail = Annotated[
    list[str] | None, typer.Argument(metavar="[ARGS]...", help="arguments for the verb")
]


def _forwarder(verb: str) -> Callable[[list[str] | None], None]:
    """A command whose only job is to hand its whole tail to the owning module.

    Everything after the verb is a positional so that click never interprets it:
    ``podbench dbg --launch ./prog --fast`` has to reach ``gdbcmd`` with
    ``--fast`` intact, and ``podbench dev --help`` has to reach ``dev``.
    """

    def forward(args: _Tail = None) -> None:
        entry = ENTRY_POINTS[verb]
        rest = list(args or ())
        raise typer.Exit(entry.handler([verb, *rest] if entry.keep_verb else rest))

    return forward


def _build_app() -> typer.Typer:
    app = new_app()

    @app.callback(invoke_without_command=True)
    def root(
        ctx: typer.Context,
        version: Annotated[
            bool,
            typer.Option(
                "-v", "--version", help="show the launcher's version and exit"
            ),
        ] = False,
    ) -> None:
        """A development seat inside a Kubernetes pod.

        Run `podbench VERB --help` for a verb's own options.
        """
        if version:
            print(__version__)
            raise typer.Exit(0)
        if ctx.invoked_subcommand is None:
            rendered = ctx.get_help()
            if rendered:
                typer.echo(rendered)
            raise typer.Exit(2)

    for verb, entry in ENTRY_POINTS.items():
        app.command(
            name=verb,
            help=entry.help,
            rich_help_panel=entry.panel,
            # The verb's own parser owns --help; see the module docstring.
            add_help_option=False,
            context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
        )(_forwarder(verb))
    return app


def main(args: Sequence[str] | None = None) -> int:
    """Dispatch a verb to the module that implements it."""
    return run(_build_app(), args, prog="podbench")


if __name__ == "__main__":
    sys.exit(main())
