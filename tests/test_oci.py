"""Tests for reading an image's OCI labels.

Nothing here opens a socket. :data:`podbench.oci.Fetch` is the seam — the same
injected-runner discipline the rest of the suite uses for ``kubectl`` — so every
case below is a table of canned HTTP responses, including the ones that are the
whole point: a registry that will not answer is not an error, it is an image
whose provenance could not be measured.
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Mapping

from podbench import oci

TOKEN = "a-bearer-token"
CONFIG_DIGEST = "sha256:cfff"
AMD64_DIGEST = "sha256:aaaa"
ARM64_DIGEST = "sha256:bbbb"

LABELS = {
    oci.REVISION_LABEL: "1111111111111111111111111111111111111111",
    oci.SOURCE_LABEL: "https://github.com/acme/api",
}

CHALLENGE = (
    'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
    'scope="repository:acme/api:pull"'
)

MANIFESTS = "https://ghcr.io/v2/acme/api/manifests"
BLOBS = "https://ghcr.io/v2/acme/api/blobs"
TOKEN_URL = "https://ghcr.io/token?service=ghcr.io&scope=repository%3Aacme%2Fapi%3Apull"


def body(payload: object) -> bytes:
    return json.dumps(payload).encode()


def index(*, attestation: bool = False) -> bytes:
    manifests = [
        {"digest": ARM64_DIGEST, "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": AMD64_DIGEST, "platform": {"os": "linux", "architecture": "amd64"}},
    ]
    if attestation:
        manifests.insert(0, {"digest": "sha256:dead", "platform": {"os": "unknown"}})
    return body({"manifests": manifests})


class FakeRegistry:
    """A registry that answers from a table and records what it was asked.

    Keyed on the URL *and* on whether the request carried a bearer token, which
    is the only way to script the 401-then-retry the protocol requires.
    """

    def __init__(self, pages: Mapping[tuple[str, bool], oci.Response]) -> None:
        self.pages = dict(pages)
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> oci.Response:
        authorised = "Authorization" in headers
        self.calls.append((url, authorised))
        page = self.pages.get((url, authorised))
        if page is None:
            return oci.Response(404, {}, b"")
        return page


def a_registry(**overrides: oci.Response) -> FakeRegistry:
    """The whole happy sequence: challenge, token, index, manifest, config."""
    pages: dict[tuple[str, bool], oci.Response] = {
        (f"{MANIFESTS}/1.4.0", False): oci.Response(
            401, {"www-authenticate": CHALLENGE}, b""
        ),
        (TOKEN_URL, False): oci.Response(200, {}, body({"token": TOKEN})),
        (f"{MANIFESTS}/1.4.0", True): oci.Response(200, {}, index()),
        (f"{MANIFESTS}/{AMD64_DIGEST}", True): oci.Response(
            200, {}, body({"config": {"digest": CONFIG_DIGEST}})
        ),
        (f"{BLOBS}/{CONFIG_DIGEST}", True): oci.Response(
            200, {}, body({"config": {"Labels": LABELS}})
        ),
    }
    for key, page in overrides.items():
        pages[_named(key)] = page
    return FakeRegistry(pages)


def _named(key: str) -> tuple[str, bool]:
    return {
        "challenge": (f"{MANIFESTS}/1.4.0", False),
        "token": (TOKEN_URL, False),
        "index": (f"{MANIFESTS}/1.4.0", True),
        "manifest": (f"{MANIFESTS}/{AMD64_DIGEST}", True),
        "config": (f"{BLOBS}/{CONFIG_DIGEST}", True),
    }[key]


# -- references ------------------------------------------------------------


def test_an_image_id_from_the_kubelet_parses() -> None:
    """`containerStatuses[].imageID` is the most accurate reference available —
    the digest actually running — and some runtimes prefix it."""
    reference = oci.parse_reference("docker-pullable://ghcr.io/acme/api@sha256:abcd")

    assert reference == oci.ImageRef("ghcr.io", "acme/api", "sha256:abcd")


def test_a_port_in_the_registry_is_not_read_as_a_tag() -> None:
    assert oci.parse_reference("localhost:5000/api") == oci.ImageRef(
        "localhost:5000", "api", "latest"
    )


# -- the read --------------------------------------------------------------


def test_labels_come_back_through_the_token_dance() -> None:
    """A *public* image on ghcr.io still answers 401 first: the anonymous token
    flow is the protocol, not a sign that credentials are needed."""
    registry = a_registry()

    assert oci.image_labels("ghcr.io/acme/api:1.4.0", fetch=registry) == LABELS
    assert registry.calls[0] == (f"{MANIFESTS}/1.4.0", False)
    assert registry.calls[1] == (TOKEN_URL, False)


def test_the_token_is_reused_rather_than_fetched_per_request() -> None:
    registry = a_registry()

    oci.image_labels("ghcr.io/acme/api:1.4.0", fetch=registry)

    assert registry.calls.count((TOKEN_URL, False)) == 1


def test_an_index_resolves_to_the_platform_that_is_running() -> None:
    """Diamond is x86. The labels match across architectures in practice, so
    this is a tie-break — but an attestation manifest declares os `unknown` and
    carries no config to read, so it must never be the one picked."""
    registry = a_registry(index=oci.Response(200, {}, index(attestation=True)))

    assert oci.image_labels("ghcr.io/acme/api:1.4.0", fetch=registry) == LABELS
    assert (f"{MANIFESTS}/{AMD64_DIGEST}", True) in registry.calls


def test_a_single_manifest_needs_no_second_hop() -> None:
    registry = a_registry(
        index=oci.Response(200, {}, body({"config": {"digest": CONFIG_DIGEST}}))
    )

    assert oci.image_labels("ghcr.io/acme/api:1.4.0", fetch=registry) == LABELS
    assert (f"{MANIFESTS}/{AMD64_DIGEST}", True) not in registry.calls


def test_a_registry_that_wants_credentials_is_unmeasured_and_not_an_error() -> None:
    """The whole contract. A private image is one whose base commit `init` will
    record as assumed, and there is nothing else to be done about it."""
    registry = a_registry(token=oci.Response(403, {}, b""))

    assert oci.image_labels("ghcr.io/acme/api:1.4.0", fetch=registry) is None


def test_no_network_at_all_is_the_same_answer() -> None:
    def refuse(url: str, headers: Mapping[str, str]) -> oci.Response:
        raise OSError("Network is unreachable")

    assert oci.image_labels("ghcr.io/acme/api:1.4.0", fetch=refuse) is None


def test_a_body_that_is_not_json_is_the_same_answer() -> None:
    registry = a_registry(config=oci.Response(200, {}, b"<html>nope</html>"))

    assert oci.image_labels("ghcr.io/acme/api:1.4.0", fetch=registry) is None


def test_an_image_with_no_labels_is_an_empty_answer_not_a_failed_one() -> None:
    """Distinct from `None` on purpose: "the image says nothing" and "the image
    could not be asked" are the same *outcome* for `init`, but only one of them
    is a fact about the image."""
    registry = a_registry(config=oci.Response(200, {}, body({"config": {}})))

    assert oci.image_labels("ghcr.io/acme/api:1.4.0", fetch=registry) == {}


def test_a_truncated_response_is_the_same_answer() -> None:
    """`http.client.HTTPException` is neither an OSError nor a ValueError, and
    the read happens outside urllib's own URLError wrapping. "Never raises" is
    the whole contract: a bad answer degrades to an assumed base rather than a
    traceback out of `hotfix init`."""

    def truncate(url: str, headers: Mapping[str, str]) -> oci.Response:
        raise http.client.IncompleteRead(b"{")

    assert oci.image_labels("ghcr.io/acme/api:1.4.0", fetch=truncate) is None


def test_an_unparseable_reference_never_reaches_the_network() -> None:
    registry = a_registry()

    assert oci.image_labels("", fetch=registry) is None
    assert registry.calls == []


def test_a_bare_digest_is_not_a_reference_to_docker_hub() -> None:
    """Some CRI configurations report a container's imageID as a bare
    `sha256:<hex>`. Split like any other reference that is the Docker Hub
    repository `library/sha256`, so podbench would fabricate a third-party
    request out of somebody's image id and pay the timeout for it."""
    registry = a_registry()

    assert oci.parse_reference("sha256:" + "a" * 64) is None
    assert oci.image_labels("sha256:" + "a" * 64, fetch=registry) is None
    assert registry.calls == []
