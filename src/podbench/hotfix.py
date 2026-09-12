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

Namespace = Annotated[
    str | None,
    typer.Option("-n", "--namespace", metavar="NAMESPACE", help="Kubernetes namespace"),
]
Context = Annotated[
    str | None, typer.Option("--context", metavar="NAME", help="kubeconfig context")
]
KubectlBinary = Annotated[
    str, typer.Option("--kubectl", metavar="BIN", help="kubectl binary")
]
Container = Annotated[
    str | None,
    typer.Option("--container", metavar="NAME", help="application container"),
]
Pod = Annotated[str, typer.Argument(metavar="POD", help="pod/NAME or bare NAME")]


def _build_app(runner: Runner | None = None) -> typer.Typer:
    app = new_app()

    @app.callback(invoke_without_command=True)
    def root(ctx: typer.Context) -> None:
        """Durable source edits on a workload-scoped PVC."""
        require_subcommand(ctx)

    @app.command(help="emit the Helm values needed by the workload")
    def values(
        app_name: Annotated[
            str, typer.Option("--app", metavar="NAME", help="Helm release name")
        ],
        from_pod: Annotated[
            str,
            typer.Option("--from-pod", metavar="POD", help="pod to inspect"),
        ],
        entrypoint: Annotated[
            str | None,
            typer.Option(
                "--entrypoint", metavar="COMMAND", help="override application command"
            ),
        ] = None,
        gid: Annotated[
            int | None,
            typer.Option("--gid", metavar="GID", help="override application GID"),
        ] = None,
        size: Annotated[
            str, typer.Option("--size", metavar="SIZE", help="claim size")
        ] = "10Gi",
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

    @app.command(name="init", help="clone the source and build the claim environment")
    def init_command(
        pod: Pod,
        repo: Annotated[
            str, typer.Option("--repo", metavar="URL", help="git repository to clone")
        ],
        ref: Annotated[
            str | None,
            typer.Option("--ref", metavar="REF", help="branch or tag to clone"),
        ] = None,
        container: Container = None,
        namespace: Namespace = None,
        context: Context = None,
        kubectl: KubectlBinary = "kubectl",
    ) -> None:
        kube = kubectl_for(namespace, context=context, binary=kubectl, runner=runner)
        for line in init_hotfix(kube, pod, repo, ref=ref, container=container):
            console.print(line)

    @app.command(name="restart", help="relaunch the application from the claim")
    def restart_command(
        pod: Pod,
        reinstall: Annotated[
            bool, typer.Option("--reinstall", help="rebuild the claim environment")
        ] = False,
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

    @app.command(name="status", help="show hotfix state in the namespace")
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

    @app.command(name="retire", help="check or complete hotfix claim retirement")
    def retire_command(
        target: Annotated[str, typer.Argument(metavar="POD_OR_PVC")],
        delete_claim: Annotated[
            bool,
            typer.Option(
                "--delete-claim", help="delete the unmounted claim and its data"
            ),
        ] = False,
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
