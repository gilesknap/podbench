from __future__ import annotations

from collections.abc import Sequence

import typer
from rich.console import Console

console = Console(highlight=False, markup=False, soft_wrap=True)
error_console = Console(stderr=True, highlight=False, markup=False, soft_wrap=True)


def new_app() -> typer.Typer:
    return typer.Typer(add_completion=False)


def require_subcommand(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(2)


def run(app: typer.Typer, args: Sequence[str] | None, *, prog: str) -> int:
    command = typer.main.get_command(app)
    try:
        command.main(args=None if args is None else list(args), prog_name=prog)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    return 0
