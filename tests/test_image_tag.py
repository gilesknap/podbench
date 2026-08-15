"""Tests for the default image tag.

The launcher's version and the image it attaches are one release published
twice, and under ``uvx`` the version floats between two attaches with nothing to
announce it. A launcher that asks for the wrong tag does not fail in the CLI: it
fails as an ``ImagePullBackOff`` inside an ephemeral container that cannot be
restarted, which burns that seat name for the life of the pod. So the mapping is
pinned here by table rather than left to whatever version happens to be
installed while the suite runs.

The versions below are the spellings the two publishers actually produce:
setuptools_scm's off-a-checkout form, and the PEP 440 normalisation CI tags the
image with beside its SemVer spelling (``.github/workflows/_container.yml``).
"""

from __future__ import annotations

import re

import pytest

from podbench import __version__
from podbench.model import (
    DEFAULT_IMAGE,
    FLOATING_TAG,
    IMAGE_REPOSITORY,
    image_tag_for,
)

# distribution's TagRegexp: what a registry will accept after the colon.
OCI_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # Final and prerelease alike are versions CI tagged, and a tagged build
        # publishes both this spelling and the SemVer one on the same digest.
        ("1.0.0", "1.0.0"),
        ("1.0.0b1", "1.0.0b1"),
        ("0.1.0a4", "0.1.0a4"),
        # setuptools_scm off a checkout: a dev segment, a local segment, or the
        # two together. Nothing was ever pushed under any of them, and `+` is
        # not a legal OCI tag character in the bargain.
        ("0.1.dev9", FLOATING_TAG),
        ("0.1.dev9+g1234567", FLOATING_TAG),
        ("0.1.0a4.dev2+ga9c0c42b2.d20260815", FLOATING_TAG),
        ("1.0.0+dirty", FLOATING_TAG),
        # An epoch is legal PEP 440 and impossible as a tag: `!` would make the
        # reference malformed rather than merely unpullable.
        ("1!2.0.0", FLOATING_TAG),
    ],
)
def test_the_tag_a_version_names(version: str, expected: str) -> None:
    assert image_tag_for(version) == expected


@pytest.mark.parametrize(
    "version",
    ["1.0.0", "1.0.0b1", "0.1.dev9+g1234567", "1!2.0.0", "", "-leading-dash"],
)
def test_every_derived_tag_is_pullable(version: str) -> None:
    """No version may yield a reference a registry would reject.

    A malformed tag fails at ``kubectl`` time on some paths and in the pod on
    others, so the guarantee has to hold for every input rather than for the
    spellings this project expects to see.
    """
    assert OCI_TAG.fullmatch(image_tag_for(version))


def test_default_image_names_the_published_repository() -> None:
    """Spelled out rather than derived, so a repository move is caught here.

    Restating ``model.py``'s f-string would pass for any implementation of
    :func:`image_tag_for`, including one returning the empty string.
    """
    assert IMAGE_REPOSITORY == "ghcr.io/gilesknap/podbench"
    repository, _, tag = DEFAULT_IMAGE.rpartition(":")
    assert repository == IMAGE_REPOSITORY
    assert OCI_TAG.fullmatch(tag)


def test_default_image_tracks_this_launchers_version() -> None:
    """The installed version, not a table entry, decides the tag.

    ``DEFAULT_IMAGE`` is computed once at import, so nothing else in the suite
    would notice it being pinned to a literal again.
    """
    _, _, tag = DEFAULT_IMAGE.rpartition(":")
    assert tag == image_tag_for(__version__)
    assert tag == __version__ or tag == FLOATING_TAG
