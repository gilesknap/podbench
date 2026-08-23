#!/usr/bin/env python3
"""Drive a VS Code window through the bridge extension beside this file.

Usage::

    vsc.py ls                         # windows, with remote name and folders
    vsc.py info [--pid N]
    vsc.py cmd <command-id> [json-arg ...]
    vsc.py open <path> [line]
    vsc.py text <path> [from] [to]
    vsc.py bp <path> <line> [<path> <line> ...]    # replaces the whole set
    vsc.py debug <config-name>
    vsc.py stack
    vsc.py term <name> <shell command>
    vsc.py diag
    vsc.py events [since]
    vsc.py eval '<js>'                # vscode/ctx/require in scope, awaited
    vsc.py raw '<json>'

``--pid`` picks a window. With one window it is inferred; with several, the
newest *remote* window wins, because that is the one ``podbench vscode`` just
opened.

Stdlib only, and deliberately not a package: this is a developer tool, not part
of podbench's runtime surface, and the repo's one-runtime-dependency rule is
asserted by ``tests/test_packaging.py``.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

DIR = Path.home() / ".local/state/podbench-vscode-bridge"
"""Where the extension writes one descriptor and one socket per window.

Not under the checkout: several windows on several branches share one bridge,
and a path inside a worktree would be removed with it.
"""


def windows() -> list[dict[str, Any]]:
    """Live windows, newest first.

    A descriptor whose socket refuses a connection is stale - VS Code was
    killed rather than closed, so ``deactivate`` never ran - and is pruned here
    rather than offered to a caller that would then hang.
    """
    out: list[dict[str, Any]] = []
    if not DIR.exists():
        return out
    for desc in DIR.glob("*.json"):
        try:
            found: dict[str, Any] = json.loads(desc.read_text())
        except (OSError, ValueError):
            continue
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(2)
            sock.connect(str(found["socket"]))
        except (OSError, KeyError):
            desc.unlink(missing_ok=True)
            continue
        finally:
            sock.close()
        out.append(found)
    return sorted(out, key=lambda d: int(d.get("started", 0)), reverse=True)


def pick(pid: int | None) -> dict[str, Any]:
    """The window to talk to, or a refusal naming how to look."""
    live = windows()
    if not live:
        sys.exit("no VS Code window is carrying the bridge (see: vsc.py ls)")
    if pid is not None:
        for found in live:
            if found["pid"] == pid:
                return found
        sys.exit(f"no bridge window with pid {pid}")
    remote = [found for found in live if found.get("remoteName")]
    return (remote or live)[0]


def call(
    request: dict[str, Any], pid: int | None = None, timeout: float = 120.0
) -> Any:
    """One request, one reply. Newline-delimited JSON, one object per line."""
    found = pick(pid)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(found["socket"]))
    sock.sendall((json.dumps(request) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            sys.exit("the bridge closed the connection")
        buf += chunk
    sock.close()
    reply: dict[str, Any] = json.loads(buf.split(b"\n")[0])
    if not reply.get("ok"):
        if reply.get("stack"):
            print(reply["stack"], file=sys.stderr)
        sys.exit(f"bridge error: {reply.get('error')}")
    return reply["result"]


def _request(verb: str, rest: list[str]) -> dict[str, Any]:
    """The wire request for one command line, or a refusal."""
    if verb == "info":
        return {"op": "info"}
    if verb == "cmd":
        return {
            "op": "command",
            "id": rest[0],
            "args": [json.loads(a) for a in rest[1:]],
        }
    if verb == "open":
        out: dict[str, Any] = {"op": "open", "uri": rest[0]}
        if len(rest) > 1:
            out["line"] = int(rest[1])
        return out
    if verb == "text":
        out = {"op": "text", "uri": rest[0]}
        if len(rest) > 1:
            out["from"] = int(rest[1])
        if len(rest) > 2:
            out["to"] = int(rest[2])
        return out
    if verb == "bp":
        pairs = [
            {"path": rest[i], "line": int(rest[i + 1])} for i in range(0, len(rest), 2)
        ]
        return {"op": "breakpoints", "clear": True, "set": pairs}
    if verb == "debug":
        return {"op": "debug", "name": rest[0]}
    if verb == "stack":
        return {"op": "stack"}
    if verb == "term":
        return {"op": "terminal", "name": rest[0], "text": " ".join(rest[1:])}
    if verb == "diag":
        return {"op": "diagnostics"}
    if verb == "events":
        return {"op": "events", "since": int(rest[0]) if rest else 0}
    if verb == "eval":
        return {"op": "eval", "code": rest[0]}
    if verb == "commands":
        return {"op": "commands", "filter": rest[0] if rest else None}
    if verb == "raw":
        parsed: dict[str, Any] = json.loads(rest[0])
        return parsed
    sys.exit(f"unknown verb: {verb}")


def main(argv: list[str]) -> int:
    pid: int | None = None
    if "--pid" in argv:
        at = argv.index("--pid")
        pid = int(argv[at + 1])
        del argv[at : at + 2]
    if not argv:
        print(__doc__)
        return 2

    if argv[0] == "ls":
        live = windows()
        if not live:
            print("(no bridge windows)")
        for found in live:
            listed: list[Any] = found.get("folders") or []
            folders = ", ".join(str(entry) for entry in listed) or "-"
            where = found.get("remoteName") or "local"
            print(f"pid={found['pid']}  remote={where}  {folders}")
        return 0

    result = call(_request(argv[0], argv[1:]), pid)
    print(result if isinstance(result, str) else json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
