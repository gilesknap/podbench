"""Typer commands for the prototype hotfix lifecycle."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Annotated

import typer

from .cli import console, error_console, new_app, require_subcommand, run
from .hotfix_core import HotfixError
from .hotfix_runtime import init as init_hotfix
from .hotfix_runtime import restart as restart_hotfix
from .hotfix_status import retire as retire_hotfix
from .hotfix_status import status as hotfix_status
from .hotfix_values import render_values
from .kubectl import KubectlError, Runner
from .launcher import LauncherError, kubectl_for

Namespace = Annotated[str | None, typer.Option("-n", "--namespace")]
Context = Annotated[str | None, typer.Option("--context")]
KubectlBinary = Annotated[str, typer.Option("--kubectl")]
Container = Annotated[str | None, typer.Option("--container")]


def _build_app(runner: Runner | None = None) -> typer.Typer:
    app = new_app()

    @app.callback(invoke_without_command=True)
    def root(ctx: typer.Context) -> None:
        """Durable source edits on a workload-scoped PVC."""
        require_subcommand(ctx)

    @app.command()
    def values(
        app_name: Annotated[str, typer.Option("--app", help="Helm release name")],
        from_pod: Annotated[str, typer.Option("--from-pod", help="pod to inspect")],
        entrypoint: Annotated[str | None, typer.Option("--entrypoint")] = None,
        gid: Annotated[int | None, typer.Option("--gid")] = None,
        size: Annotated[str, typer.Option("--size")] = "10Gi",
        container: Container = None,
        namespace: Namespace = None,
        context: Context = None,
        kubectl: KubectlBinary = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        pod = kube.get_pod(from_pod.removeprefix("pod/"))
        typer.echo(
            render_values(
                pod,
                app_name,
                container_name=container,
                command=entrypoint,
                gid=gid,
                size=size,
            ),
            nl=False,
        )

    @app.command(name="init")
    def init_command(
        pod: Annotated[str, typer.Argument(metavar="POD")],
        repo: Annotated[str, typer.Option("--repo", help="git repository to clone")],
        ref: Annotated[str | None, typer.Option("--ref", help="branch or tag")] = None,
        container: Container = None,
        namespace: Namespace = None,
        context: Context = None,
        kubectl: KubectlBinary = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        for line in init_hotfix(kube, pod, repo, ref=ref, container=container):
            console.print(line)

    @app.command(name="restart")
    def restart_command(
        pod: Annotated[str, typer.Argument(metavar="POD")],
        reinstall: Annotated[bool, typer.Option("--reinstall")] = False,
        deadline: Annotated[
            int, typer.Option("--deadline", help="hold timeout in seconds")
        ] = 120,
        container: Container = None,
        namespace: Namespace = None,
        context: Context = None,
        kubectl: KubectlBinary = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        for line in restart_hotfix(
            kube, pod, container=container, reinstall=reinstall, deadline=deadline
        ):
            console.print(line)

    @app.command(name="status")
    def status_command(
        namespace: Namespace = None,
        context: Context = None,
        kubectl: KubectlBinary = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        lines, healthy = hotfix_status(kube)
        for line in lines:
            console.print(line)
        if not healthy:
            raise typer.Exit(1)

    @app.command(name="retire")
    def retire_command(
        target: Annotated[str, typer.Argument(metavar="POD_OR_PVC")],
        delete_claim: Annotated[bool, typer.Option("--delete-claim")] = False,
        namespace: Namespace = None,
        context: Context = None,
        kubectl: KubectlBinary = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        lines, complete = retire_hotfix(kube, target, delete_claim=delete_claim)
        for line in lines:
            console.print(line)
        if not complete:
            raise typer.Exit(1)

    return app


def main(args: Sequence[str] | None = None, *, runner: Runner | None = None) -> int:
    argv = list(sys.argv[1:] if args is None else args)
    if argv and argv[0] == "hotfix":
        argv.pop(0)
    try:
        return run(_build_app(runner), argv, prog="podbench hotfix")
    except (HotfixError, LauncherError, KubectlError, ValueError) as error:
        error_console.print(f"podbench: {error}", style="red")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
