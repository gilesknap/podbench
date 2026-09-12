"""The prototype has workstation commands and an in-seat debugger."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Annotated

import typer

from . import __version__
from .cli import new_app, run

_Tail = Annotated[list[str] | None, typer.Argument(metavar="[ARGS]...")]


def _forward(
    handler: Callable[[Sequence[str]], int], verb: str
) -> Callable[[list[str] | None], None]:
    def command(args: _Tail = None) -> None:
        raise typer.Exit(handler([verb, *(args or [])]))

    return command


def _attach(args: Sequence[str]) -> int:
    from .attach_cli import main

    return main(args)


def _hotfix(args: Sequence[str]) -> int:
    from .hotfix import main

    return main(args)


def _doctor(args: Sequence[str]) -> int:
    from .doctor import main

    return main(args)


def _debug(args: Sequence[str]) -> int:
    from .debug import main

    return main(args)


def _build_app() -> typer.Typer:
    app = new_app()

    @app.callback(invoke_without_command=True)
    def root(
        ctx: typer.Context,
        version: Annotated[
            bool, typer.Option("-v", "--version", help="show the version and exit")
        ] = False,
    ) -> None:
        """A small Kubernetes attach and hotfix prototype."""
        if version:
            print(__version__)
            raise typer.Exit(0)
        if ctx.invoked_subcommand is None:
            typer.echo(ctx.get_help())
            raise typer.Exit(2)

    settings = {"ignore_unknown_options": True, "allow_extra_args": True}
    app.command(
        name="attach",
        help="land or reconnect to a debug seat",
        add_help_option=False,
        context_settings=settings,
        rich_help_panel="Workstation commands",
    )(_forward(_attach, "attach"))
    app.command(
        name="doctor",
        help="check local and cluster prerequisites",
        add_help_option=False,
        context_settings=settings,
        rich_help_panel="Workstation commands",
    )(_forward(_doctor, "doctor"))
    app.command(
        name="hotfix",
        help="work on durable code beside a live application",
        add_help_option=False,
        context_settings=settings,
        rich_help_panel="Workstation commands",
    )(_forward(_hotfix, "hotfix"))
    app.command(
        name="debug",
        help="select a process and attach GDB from inside a seat",
        add_help_option=False,
        context_settings=settings,
        rich_help_panel="Seat commands",
    )(_forward(_debug, "debug"))
    return app


def main(args: Sequence[str] | None = None) -> int:
    return run(_build_app(), args, prog="podbench")


if __name__ == "__main__":
    sys.exit(main())
