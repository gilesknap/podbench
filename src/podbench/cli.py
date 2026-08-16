"""The adapter between click's world and this package's ``(argv) -> int`` one.

Every ``main()`` here is a plain function from an argument list to an exit code,
because that is what the tests drive directly and what
``image/bin/podbench`` — the one wrapper in the image, an ``exec`` of the venv's
console script — passes straight through. Click's standalone mode
is the opposite shape: it renders its own errors — which is the whole reason for
using it, since the rich usage panel *is* the help this CLI exists to have — and
then calls :func:`sys.exit`. :func:`run` is the only place in podbench that
catches the resulting ``SystemExit``.

Two consequences worth knowing before adding a command:

* A command callback must end in ``raise typer.Exit(code)``, never ``return
  code``. Click discards a callback's return value on purpose (see the comment
  in ``click.BaseCommand.main``), so a returned exit code is a lost one, and the
  process quietly succeeds.
* Anything that is not a ``ClickException`` propagates out of :func:`run`
  untouched, which is what lets each verb keep its one ``except`` clause that
  turns a domain error into a sentence on stderr rather than a traceback.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

import typer

__all__ = ["new_app", "require_subcommand", "run"]


def new_app() -> typer.Typer:
    """A Typer app spelled the way every podbench app is spelled.

    ``add_completion`` is off deliberately. podbench is canonically run as
    ``uvx podbench <verb>`` with nothing installed, so an
    ``--install-completion`` that wires a shell to a program which is not on
    ``PATH`` is an offer that cannot be honoured, and it costs two lines in
    every help screen to make it.
    """
    return typer.Typer(add_completion=False)


def require_subcommand(ctx: typer.Context) -> None:
    """No verb at all: print the help and exit 2, as argparse did.

    Click's own ``no_args_is_help`` exits 0, which would make ``podbench hotfix``
    with nothing after it look like a command that succeeded. It is a usage
    error, and ``hotfix status`` is documented as usable in a shutdown checklist,
    so the difference between 0 and 2 is load-bearing there.

    ``ctx.get_help()`` returns the text under plain click but renders it to the
    console itself and returns an empty string under Typer's rich formatter, so
    the result is echoed only when there is one.
    """
    if ctx.invoked_subcommand is not None:
        return
    rendered = ctx.get_help()
    if rendered:
        typer.echo(rendered)
    raise typer.Exit(2)


def run(app: typer.Typer, args: Sequence[str] | None, *, prog: str) -> int:
    """Parse ``args`` with ``app`` and give back the exit code it asked for.

    ``args`` of ``None`` means ``sys.argv[1:]``, which is what both argparse and
    click do with it. ``prog`` is always passed explicitly: click would
    otherwise name the program after ``sys.argv[0]``, which is ``__main__.py``
    under ``python -m podbench`` and the wrong verb for every subcommand.
    """
    command = typer.main.get_command(app)
    try:
        command.main(args=None if args is None else list(args), prog_name=prog)
    except SystemExit as exit_:
        return _exit_code(exit_.code)
    # Unreachable: click's standalone mode exits on every path, including
    # success. Kept so the signature is honest rather than a lie pyright cannot
    # see through.
    return 0  # pragma: no cover


def _exit_code(code: object) -> int:
    """``SystemExit.code`` is whatever was raised; an exit status is an int."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return 1
