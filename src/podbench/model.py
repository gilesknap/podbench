"""Small shared vocabulary for the prototype."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from . import __version__

IMAGE_REPOSITORY = "ghcr.io/gilesknap/podbench"
FLOATING_TAG = "main"
IMAGE_ENV = "PODBENCH_IMAGE"
HOTFIX_CLAIM_VOLUME = "podbench-app"
HOTFIX_APP_PATH = "/podbench/app"
HOTFIX_INTERPRETER_PATH = f"{HOTFIX_APP_PATH}/.python"
HOTFIX_HOLD_PATH = "/tmp/podbench-hold"
HOTFIX_CHILD_PID_PATH = "/tmp/podbench-child.pid"
SEAT_HOME_VOLUME = "podbench-home"

_OCI_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")


def image_tag_for(version: str) -> str:
    """Use an exact release image, otherwise the image built from main."""
    if ".dev" in version or "+" in version or not _OCI_TAG.fullmatch(version):
        return FLOATING_TAG
    return version


DEFAULT_IMAGE = f"{IMAGE_REPOSITORY}:{image_tag_for(__version__)}"


def as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def and_list(items: Sequence[str]) -> str:
    values = list(items)
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + f" and {values[-1]}"


@dataclass(frozen=True)
class PodRef:
    namespace: str
    name: str

    def __str__(self) -> str:
        return f"{self.namespace}/{self.name}"
