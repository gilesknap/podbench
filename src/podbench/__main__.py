"""Interface for ``python -m podbench``, and the ``podbench`` console script.

One binary serves both halves of the tool. On a developer's machine it is
reached as ``kubectl podbench <verb>`` and drives the cluster; inside the debug
container the same binary is PID 1 (``podbench agent``) and backs the helpers on
``PATH`` (``pids``, ``dbg``, ``capreport``, ...). Keeping it as one package means
the capability logic that decides what a session can do is the same code in both
places, rather than a launcher's guess and a helper's separate guess.

Dispatch is deliberately dumb: each verb names a module, and the module owns its
own argument parsing. Two of them predate the dispatcher and parse an argv that
does not include the verb, so those are sliced; the rest are forwarded verbatim.
Imports are done lazily per verb because ``podbench --version`` runs as the
image's build-time smoke test, and it should not depend on every module in the
package importing cleanly.
"""

from __future__ import annotations

import sys
from argparse import REMAINDER, ArgumentParser
from collections.abc import Callable, Sequence

from . import __version__

__all__ = ["ENTRY_POINTS", "main"]


def _agent(args: Sequence[str]) -> int:
    from .agent import main as run

    return run(args)


def _capreport(args: Sequence[str]) -> int:
    from .probe import main as run

    return run(args)


def _gdbcmd(args: Sequence[str]) -> int:
    from .gdbcmd import main as run

    return run(args)


def _dev(args: Sequence[str]) -> int:
    from .dev import main as run

    return run(args)


def _launcher(args: Sequence[str]) -> int:
    from .launcher import main as run

    return run(args)


#: Verb -> (handler, keep_verb). ``keep_verb`` is False for the two modules whose
#: parsers were written before this dispatcher existed and so do not expect their
#: own name in argv; the others own a subparser keyed on it and need it kept.
ENTRY_POINTS: dict[str, tuple[Callable[[Sequence[str]], int], bool]] = {
    # In-pod: PID 1 and the helpers on PATH.
    "agent": (_agent, False),
    "capreport": (_capreport, False),
    "pids": (_gdbcmd, True),
    "dbg": (_gdbcmd, True),
    "dev-bootstrap": (_dev, True),
    "run": (_dev, True),
    "stop": (_dev, True),
    # Laptop side: `kubectl podbench ...`.
    "attach": (_launcher, True),
    "ssh-config": (_launcher, True),
    "status": (_launcher, True),
    "list": (_launcher, True),
    "dev": (_dev, True),
}

_IN_POD = ("agent", "capreport", "pids", "dbg", "dev-bootstrap", "run", "stop")
_LAUNCHER = ("attach", "ssh-config", "status", "list", "dev")


def _build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="podbench",
        description=(
            "A development seat inside a Kubernetes pod. Run `podbench <verb> "
            "--help` for a verb's own options."
        ),
        epilog=(
            "cluster-side verbs (also reachable as `kubectl podbench <verb>`): "
            + ", ".join(_LAUNCHER)
            + "\nin-pod verbs: "
            + ", ".join(_IN_POD)
        ),
    )
    parser.add_argument("-v", "--version", action="version", version=__version__)
    parser.add_argument(
        "verb",
        nargs="?",
        choices=sorted(ENTRY_POINTS),
        metavar="VERB",
        help="the subcommand to run",
    )
    # REMAINDER hands the verb's own flags through untouched, so that `podbench
    # dbg --launch ./prog --fast` reaches gdbcmd with `--fast` intact instead of
    # being claimed by this parser.
    parser.add_argument("args", nargs=REMAINDER, help="arguments for the verb")
    return parser


def main(args: Sequence[str] | None = None) -> int:
    """Dispatch a verb to the module that implements it."""
    parser = _build_parser()
    parsed = parser.parse_args(args)
    verb: str | None = parsed.verb
    if verb is None:
        parser.print_help()
        return 2

    handler, keep_verb = ENTRY_POINTS[verb]
    rest: list[str] = list(parsed.args)
    return handler([verb, *rest] if keep_verb else rest)


def kubectl_plugin(args: Sequence[str] | None = None) -> int:
    """Entry point for the ``kubectl-podbench`` plugin.

    kubectl invokes a plugin as ``kubectl-podbench <args>`` after stripping its
    own name, so this is the launcher's argv with none of the in-pod verbs
    offered — a plugin advertising `agent` in its help would be confusing.
    """
    from .launcher import main as run

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
