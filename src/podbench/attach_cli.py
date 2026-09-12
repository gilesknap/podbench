from __future__ import annotations

import os
import shlex
from collections.abc import Sequence
from typing import Annotated

import typer

from .cli import console, error_console, new_app, run
from .doctor import include_is_active
from .kubectl import KubectlError, Runner
from .launcher import DEFAULT_PULL_POLICY, LauncherError, attach, kubectl_for
from .model import DEFAULT_IMAGE, IMAGE_ENV
from .ssh_transport import (
    DEFAULT_IDENTITY,
    missing_ssh_capabilities,
    wire_ssh,
)

PULL_POLICIES = ("Always", "IfNotPresent", "Never")


def _pull_policy(value: str) -> str:
    for policy in PULL_POLICIES:
        if value.lower() == policy.lower():
            return policy
    raise typer.BadParameter(f"must be one of {', '.join(PULL_POLICIES)}")


def _build_app(runner: Runner | None = None) -> typer.Typer:
    app = new_app()

    @app.command()
    def attach_command(
        pod: Annotated[
            str, typer.Argument(metavar="POD", help="pod/NAME or bare NAME")
        ],
        target: Annotated[
            str | None,
            typer.Option("--target", metavar="NAME", help="workload container"),
        ] = None,
        image: Annotated[
            str | None,
            typer.Option("--image", metavar="REF", help="debug image reference"),
        ] = None,
        target_uid: Annotated[
            int | None,
            typer.Option("--target-uid", metavar="UID", help="override target UID"),
        ] = None,
        target_gid: Annotated[
            int | None,
            typer.Option("--target-gid", metavar="GID", help="override target GID"),
        ] = None,
        new: Annotated[
            bool, typer.Option("--new", help="land a new seat instead of reusing one")
        ] = False,
        verbose: Annotated[
            bool, typer.Option("--verbose", help="show the kubectl fallback")
        ] = False,
        pull: Annotated[
            str,
            typer.Option(
                "--pull",
                metavar="POLICY",
                help="image pull policy: Always, IfNotPresent or Never",
                callback=_pull_policy,
            ),
        ] = DEFAULT_PULL_POLICY,
        identity: Annotated[
            str,
            typer.Option("--identity", metavar="KEY", help="SSH private key"),
        ] = DEFAULT_IDENTITY,
        config_dir: Annotated[
            str | None,
            typer.Option(
                "--config-dir", metavar="DIR", help="generated SSH files directory"
            ),
        ] = None,
        timeout: Annotated[
            float,
            typer.Option(
                "--timeout", metavar="SECONDS", help="wait for the seat to start"
            ),
        ] = 120.0,
        namespace: Annotated[
            str | None,
            typer.Option(
                "-n", "--namespace", metavar="NAMESPACE", help="Kubernetes namespace"
            ),
        ] = None,
        context: Annotated[
            str | None,
            typer.Option("--context", metavar="NAME", help="kubeconfig context"),
        ] = None,
        kubectl: Annotated[
            str,
            typer.Option("--kubectl", metavar="BIN", help="kubectl binary"),
        ] = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        with console.status("Landing or reconnecting the seat..."):
            session = attach(
                kube,
                pod,
                target=target,
                image=image or os.environ.get(IMAGE_ENV, DEFAULT_IMAGE),
                target_uid=target_uid,
                target_gid=target_gid,
                force_new=new,
                pull_policy=pull,
                timeout=timeout,
                ssh=True,
            )
        action = "reusing" if session.reused else "landed"
        console.print(
            f"{action} {session.seat.container} "
            f"for {session.seat.pod}/{session.target}",
            style="green",
        )
        for warning in session.warnings:
            console.print(f"warning: {warning}", style="yellow")
        missing = missing_ssh_capabilities(
            kube, session.seat.pod, session.seat.container
        )
        if missing is None:
            console.print(
                "warning: SSH unavailable; capabilities not measured", style="yellow"
            )
        elif missing:
            console.print(
                f"warning: SSH unavailable; missing {', '.join(missing)}",
                style="yellow",
            )
        else:
            wiring = wire_ssh(
                kube,
                session.seat.pod,
                session.seat.container,
                identity=identity,
                config_dir=config_dir,
            )
            included = include_is_active(config_dir)
            command = f"ssh {wiring.alias}" if included else wiring.command
            console.print(f"connect: {command}", style="cyan")
            if not included:
                console.print("run podbench doctor --fix", style="red")
        if verbose:
            fallback = [kubectl]
            if context:
                fallback += ["--context", context]
            fallback += [
                "-n",
                session.seat.pod.namespace,
                "exec",
                "-it",
                session.seat.pod.name,
                "-c",
                session.seat.container,
                "--",
                "bash",
            ]
            console.print(f"fallback: {shlex.join(fallback)}")

    return app


def main(args: Sequence[str] | None = None, *, runner: Runner | None = None) -> int:
    argv = list(args) if args is not None else None
    if argv and argv[0] == "attach":
        argv.pop(0)
    try:
        return run(_build_app(runner), argv, prog="podbench attach")
    except (LauncherError, KubectlError) as error:
        error_console.print(f"podbench: {error}", style="red")
        return 2
