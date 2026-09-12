from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Annotated

import typer

from .cli import console, error_console, new_app, run
from .kubectl import KubectlError, Runner
from .launcher import DEFAULT_PULL_POLICY, LauncherError, attach, kubectl_for
from .model import DEFAULT_IMAGE, IMAGE_ENV
from .ssh_transport import DEFAULT_IDENTITY, read_public_key, wire_ssh


def _build_app(runner: Runner | None = None) -> typer.Typer:
    app = new_app()

    @app.command()
    def attach_command(
        pod: Annotated[str, typer.Argument(metavar="POD")],
        target: Annotated[str | None, typer.Option("--target")] = None,
        image: Annotated[str | None, typer.Option("--image")] = None,
        target_uid: Annotated[int | None, typer.Option("--target-uid")] = None,
        target_gid: Annotated[int | None, typer.Option("--target-gid")] = None,
        new: Annotated[bool, typer.Option("--new")] = False,
        pull: Annotated[str, typer.Option("--pull")] = DEFAULT_PULL_POLICY,
        identity: Annotated[str, typer.Option("--identity")] = DEFAULT_IDENTITY,
        config_dir: Annotated[str | None, typer.Option("--config-dir")] = None,
        timeout: Annotated[float, typer.Option("--timeout")] = 120.0,
        namespace: Annotated[str | None, typer.Option("-n", "--namespace")] = None,
        context: Annotated[str | None, typer.Option("--context")] = None,
        kubectl: Annotated[str, typer.Option("--kubectl")] = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        private_key, public_key = read_public_key(identity)
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
            public_key=public_key,
        )
        action = "reusing" if session.reused else "landed"
        console.print(
            f"{action} degraded seat {session.seat.container} "
            f"for {session.seat.pod}/{session.target}",
            style="green",
        )
        for warning in session.warnings:
            console.print(f"warning: {warning}", style="yellow")
        wiring = wire_ssh(
            kube,
            session.seat.pod,
            session.seat.container,
            identity=str(private_key),
            config_dir=config_dir,
        )
        console.print(f"ssh config: {wiring.config}")
        console.print(f"connect: {wiring.command}", style="cyan")
        console.print(f"for normal `ssh {wiring.alias}` and future Remote-SSH, add:")
        console.print(f"  {wiring.include}")
        context_flag = f"--context {context} " if context else ""
        console.print(
            f"fallback: {kubectl} {context_flag}-n {session.seat.pod.namespace} "
            f"exec -it {session.seat.pod.name} "
            f"-c {session.seat.container} -- bash",
        )

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
