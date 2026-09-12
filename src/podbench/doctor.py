"""Report and safely fix prerequisites for podbench."""

from __future__ import annotations

import os
import shlex
import stat
import sys
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .cli import new_app, run
from .doctor_checks import (
    ATTACH_GRANTS,
    HOTFIX_GRANTS,
    Check,
    Status,
    cluster_checks,
    rbac_check,
    ssh_checks,
)
from .model import DEFAULT_IMAGE, IMAGE_ENV
from .ssh_transport import DEFAULT_IDENTITY, client_directory, include_line

SSH_CONFIG = Path("~/.ssh/config")
FIX_BANNER = "# Added by podbench doctor --fix."


class IncludeState(Enum):
    ACTIVE = "active"
    SHADOWED = "shadowed"
    MISSING = "missing"


def _directive(raw: str) -> tuple[str | None, list[str]]:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        return None, []
    head, separator, tail = stripped.partition("=")
    try:
        words = shlex.split(tail if separator else stripped, comments=True)
    except ValueError:
        return None, []
    if separator:
        return head.strip().lower(), words
    return words[0].lower(), words[1:]


def include_state(text: str, wanted_line: str) -> IncludeState:
    _, wanted_arguments = _directive(wanted_line)
    wanted = {os.path.expanduser(value) for value in wanted_arguments}
    conditional = False
    shadowed = False
    for raw in text.splitlines():
        keyword, arguments = _directive(raw)
        if keyword in ("host", "match"):
            conditional = True
        elif keyword == "include" and wanted.intersection(
            os.path.expanduser(value) for value in arguments
        ):
            if not conditional:
                return IncludeState.ACTIVE
            shadowed = True
    return IncludeState.SHADOWED if shadowed else IncludeState.MISSING


def _write_include(config: Path, line: str) -> None:
    target = config.resolve() if config.is_symlink() else config
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existed = target.is_file()
    previous = target.read_text() if existed else ""
    mode = stat.S_IMODE(target.stat().st_mode) if existed else 0o600
    temporary = target.with_name(f"{target.name}.podbench-new")
    temporary.write_text(f"{FIX_BANNER}\n{line}\n\n{previous}")
    temporary.chmod(mode)
    os.replace(temporary, target)


def _config_checks(config_dir: str | None, fix: bool) -> list[Check]:
    directory = client_directory(config_dir)
    generated = directory / "config.d"
    config = SSH_CONFIG.expanduser()
    line = include_line(directory)
    checks: list[Check] = []
    existed = generated.is_dir()
    if fix and not existed:
        try:
            generated.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            checks.append(Check("config directory", Status.FAIL, str(error)))
    if generated.is_dir():
        status = Status.FIXED if fix and not existed else Status.OK
        checks.append(Check("config directory", status, str(generated)))
    else:
        checks.append(
            Check(
                "config directory",
                Status.WARN,
                f"{generated} will be created on attach",
            )
        )

    try:
        state = include_state(config.read_text() if config.is_file() else "", line)
    except OSError as error:
        checks.append(Check("ssh Include", Status.FAIL, str(error)))
        return checks
    include_fixed = False
    if fix and state is not IncludeState.ACTIVE:
        try:
            _write_include(config, line)
            state = IncludeState.ACTIVE
            include_fixed = True
        except OSError as error:
            checks.append(Check("ssh Include", Status.FAIL, str(error)))
            return checks
    if state is IncludeState.ACTIVE:
        checks.append(
            Check(
                "ssh Include",
                Status.FIXED if include_fixed else Status.OK,
                f"{config} loads {generated}/*.conf",
            )
        )
    else:
        status = Status.WARN if state is IncludeState.SHADOWED else Status.FAIL
        checks.append(
            Check(
                "ssh Include",
                status,
                f"{line} is {state.value}; run podbench doctor --fix",
            )
        )
    return checks


def doctor(
    *,
    fix: bool = False,
    identity: str = DEFAULT_IDENTITY,
    namespace: str | None = None,
    context: str | None = None,
    kubectl: str = "kubectl",
    config_dir: str | None = None,
) -> int:
    checks = [
        Check("podbench", Status.OK, __version__),
        Check("image", Status.OK, os.environ.get(IMAGE_ENV, DEFAULT_IMAGE)),
        *ssh_checks(identity),
        *_config_checks(config_dir, fix),
    ]
    cluster, context, namespace, connected = cluster_checks(kubectl, context, namespace)
    checks += cluster
    if connected and namespace:
        checks += [
            rbac_check(
                "attach RBAC",
                ATTACH_GRANTS,
                binary=kubectl,
                context=context,
                namespace=namespace,
                blocking=True,
            ),
            rbac_check(
                "hotfix RBAC",
                HOTFIX_GRANTS,
                binary=kubectl,
                context=context,
                namespace=namespace,
                blocking=False,
            ),
        ]
    else:
        checks += [
            Check("attach RBAC", Status.WARN, "not measured"),
            Check("hotfix RBAC", Status.WARN, "not measured"),
        ]

    table = Table(title="podbench doctor", show_header=False, box=None, pad_edge=False)
    table.add_column(width=5)
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    for check in checks:
        label, style = check.status.value
        table.add_row(Text(label, style=style), check.name, check.detail)
    Console(highlight=False, markup=False).print(table)
    return 1 if any(check.status is Status.FAIL for check in checks) else 0


def _build_app() -> typer.Typer:
    app = new_app()

    @app.command()
    def doctor_command(
        fix: Annotated[
            bool,
            typer.Option(
                "--fix", help="create the SSH directory and install its Include"
            ),
        ] = False,
        identity: Annotated[
            str, typer.Option("--identity", metavar="KEY")
        ] = DEFAULT_IDENTITY,
        namespace: Annotated[str | None, typer.Option("-n", "--namespace")] = None,
        context: Annotated[str | None, typer.Option("--context")] = None,
        kubectl: Annotated[str, typer.Option("--kubectl", metavar="BIN")] = "kubectl",
        config_dir: Annotated[str | None, typer.Option("--config-dir")] = None,
    ) -> None:
        """Name local or cluster prerequisites that need attention."""
        raise typer.Exit(
            doctor(
                fix=fix,
                identity=identity,
                namespace=namespace,
                context=context,
                kubectl=kubectl,
                config_dir=config_dir,
            )
        )

    return app


def main(args: Sequence[str] | None = None) -> int:
    argv = list(args) if args is not None else None
    if argv and argv[0] == "doctor":
        argv.pop(0)
    return run(_build_app(), argv, prog="podbench doctor")


if __name__ == "__main__":
    sys.exit(main())
