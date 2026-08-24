"""What an image says about itself, read from the registry and nowhere else.

Hotfix mode measures everything from one number: the commit the released image
was built from. ``status``'s ``+N commit(s)``, :func:`podbench.hotfix.drift_commits`
and the set ``consolidate`` pushes are all differences against it, so a base that
was guessed makes every figure downstream a guess wearing a measurement's
clothes. Until #205 it *was* guessed — ``git rev-parse HEAD`` of a fresh clone,
which without ``--ref`` is the default branch's tip and is almost never what the
image was built from.

The image knows. An OCI image config carries ``Labels``, and the two that matter
here are :data:`REVISION_LABEL` and :data:`SOURCE_LABEL`, which every image built
by python-copier-template, epics-containers or ``docker/metadata-action`` sets.

**Kubernetes cannot answer this.** A pod carries the image *reference* and its
resolved digest and nothing else; labels live in the config blob, which only the
registry serves. So this module speaks the registry API directly, over
``urllib`` — no dependency, no ``docker``, no ``skopeo``, and no credentials: the
anonymous token flow is enough for a public image, and a registry that wants
credentials is simply an image whose provenance could not be measured.

That is the whole contract of :func:`image_labels`: it answers ``None`` for
every failure alike — an unparseable reference, no network, a 401, a manifest
list with nothing in it — because the caller has exactly one thing to do with
any of them, which is to record the base as *assumed* rather than invent one.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from .model import as_dict

__all__ = [
    "DEFAULT_TIMEOUT",
    "REVISION_LABEL",
    "SOURCE_LABEL",
    "Fetch",
    "ImageRef",
    "Response",
    "image_labels",
    "parse_reference",
]

REVISION_LABEL = "org.opencontainers.image.revision"
"""The commit the image was built from. This is the whole point of the module."""

SOURCE_LABEL = "org.opencontainers.image.source"
"""The repository it was built from, which is what ``init`` would otherwise
have to be told with ``--repo``."""

DEFAULT_TIMEOUT = 5.0
"""Seconds per request, and deliberately short.

This is a best-effort read on the way into an emergency. A beamline laptop
behind a proxy that black-holes outbound 443 must cost the person one timeout,
not four: the first request's failure aborts the whole sequence, so this is the
worst case rather than a per-hop tax.
"""

_MAX_BODY = 4 * 1024 * 1024
"""Cap on a body this module will read. A manifest is kilobytes and a config
blob is tens of kilobytes; anything claiming to be larger is not what we asked
for, and a registry is not a thing to be trusted with the size of our heap."""

_DEFAULT_REGISTRY = "docker.io"
_DOCKER_HUB_API = "registry-1.docker.io"
"""Docker Hub is the one registry whose canonical name is not the host that
serves its API, so ``docker.io/library/alpine`` has to be redirected by hand."""

_MANIFEST_TYPES = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
"""Both spellings of both shapes. A registry serves whichever the client says it
understands, and an image pushed by buildx is OCI while one pushed by an older
docker is not — asking for only one is how a manifest read comes back as a 406
that looks like a missing image."""

_USER_AGENT = "podbench"

_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')

_FETCHABLE_SCHEMES = frozenset({"http", "https"})
"""The only schemes this module may open. See :func:`_fetchable`."""
"""The ``key="value"`` pairs of a ``WWW-Authenticate: Bearer`` challenge."""

_BARE_DIGEST = re.compile(r"[a-z0-9]+(?:[.+_-][a-z0-9]+)*:[0-9a-f]{32,}")
"""A digest with no repository in front of it.

Some containerd/CRI configurations report a container status ``imageID`` in
exactly this form when the image has no repo digest. Split on the colon like
any other reference it becomes the repository ``library/sha256`` on Docker Hub,
so podbench would issue an anonymous GET fabricated out of somebody's image id
and pay the timeout for it - having already discarded the tag that would have
worked.
"""

_PREFERRED_PLATFORM = ("linux", "amd64")
"""Which manifest to read out of an index. The labels are the same on every
architecture in practice, so this is a tie-break rather than a requirement —
but Diamond is x86, so prefer the one that is actually running."""


@dataclass(frozen=True)
class Response:
    """One HTTP response, reduced to the three things this module reads.

    *headers* keys are lowercased by the fetcher, because HTTP header names are
    case-insensitive and a plain mapping is not.
    """

    status: int
    headers: Mapping[str, str]
    body: bytes


Fetch = Callable[[str, Mapping[str, str]], Response]
"""How :func:`image_labels` reaches the network.

A seam and not a courtesy: the unit suite must not touch a registry any more
than it touches a cluster, so every test passes a table of canned responses.
"""


@dataclass(frozen=True)
class ImageRef:
    """A docker/OCI image reference, split into the parts the API needs."""

    registry: str
    repository: str
    reference: str
    """A tag or a ``sha256:...`` digest. Both are valid in the manifest URL."""


def parse_reference(image: str) -> ImageRef | None:
    """Split an image reference, or ``None`` if it is not one.

    The rule for telling a registry from the first path component is docker's
    own and looks arbitrary: a first component containing a dot or a colon, or
    spelled ``localhost``, is a host — otherwise there is no registry in the
    string and the reference is Docker Hub's.

    >>> parse_reference("ghcr.io/acme/api:1.4.0")
    ImageRef(registry='ghcr.io', repository='acme/api', reference='1.4.0')
    >>> parse_reference("acme/api@sha256:abc")
    ImageRef(registry='docker.io', repository='acme/api', reference='sha256:abc')
    >>> parse_reference("alpine")
    ImageRef(registry='docker.io', repository='library/alpine', reference='latest')
    >>> parse_reference("registry:5000/api:v2")
    ImageRef(registry='registry:5000', repository='api', reference='v2')
    >>> parse_reference("") is None
    True
    >>> parse_reference("sha256:" + "a" * 64) is None
    True
    """
    # The kubelet reports an imageID with this prefix on some runtimes, and it
    # is the most accurate reference available — the digest actually running.
    text = image.strip().removeprefix("docker-pullable://")
    if not text or _BARE_DIGEST.fullmatch(text):
        return None
    remainder, _, digest = text.partition("@")
    head, slash, tail = remainder.partition("/")
    if slash and ("." in head or ":" in head or head == "localhost"):
        registry, path = head, tail
    else:
        registry, path = _DEFAULT_REGISTRY, remainder
    name, colon, tag = path.rpartition(":")
    if colon and "/" not in tag:
        path, reference = name, tag
    else:
        reference = "latest"
    if not path:
        return None
    if registry == _DEFAULT_REGISTRY and "/" not in path:
        path = f"library/{path}"
    return ImageRef(registry, path, digest or reference)


def image_labels(image: str, *, fetch: Fetch | None = None) -> dict[str, str] | None:
    """The image config's ``Labels``, or ``None`` when they cannot be read.

    Never raises. Every failure is the same answer to the caller — "this could
    not be measured" — and a hotfix that refused to start because a registry was
    unreachable would be a worse tool than one that says its base is assumed.
    """
    reference = parse_reference(image)
    if reference is None:
        return None
    get = fetch if fetch is not None else _urlopen
    host = (
        _DOCKER_HUB_API
        if reference.registry == _DEFAULT_REGISTRY
        else reference.registry
    )
    base = f"https://{host}/v2/{urllib.parse.quote(reference.repository)}"
    try:
        return _labels(get, base, reference.reference)
    except (OSError, ValueError, http.client.HTTPException):
        # OSError covers urllib's whole transport family (URLError is one), and
        # ValueError covers a body that is not the JSON it claimed to be.
        # HTTPException covers neither, and is what a truncated or malformed
        # response raises - IncompleteRead and BadStatusLine inherit from it
        # alone, and the read happens outside urllib's own URLError wrapping.
        # "Never raises" is the whole contract: a registry that answers badly
        # must degrade to an assumed base, not traceback out of `hotfix init`.
        return None


def _labels(get: Fetch, base: str, reference: str) -> dict[str, str] | None:
    response, token = _authorised(get, f"{base}/manifests/{reference}", _MANIFEST_TYPES)
    if response.status != 200:
        return None
    document = _document(response.body)
    listed = document.get("manifests")
    if isinstance(listed, list):
        digest = _pick(cast(list[Any], listed))
        if digest is None:
            return None
        response, token = _authorised(
            get, f"{base}/manifests/{digest}", _MANIFEST_TYPES, token
        )
        if response.status != 200:
            return None
        document = _document(response.body)
    config = as_dict(document.get("config")).get("digest")
    if not isinstance(config, str) or not config:
        return None
    response, _ = _authorised(get, f"{base}/blobs/{config}", "application/json", token)
    if response.status != 200:
        return None
    labels = as_dict(as_dict(_document(response.body).get("config")).get("Labels"))
    return {str(key): value for key, value in labels.items() if isinstance(value, str)}


def _pick(manifests: list[Any]) -> str | None:
    """The digest to follow out of an index."""
    fallback: str | None = None
    for entry in manifests:
        item = as_dict(entry)
        digest = item.get("digest")
        if not isinstance(digest, str) or not digest:
            continue
        platform = as_dict(item.get("platform"))
        if (platform.get("os"), platform.get("architecture")) == _PREFERRED_PLATFORM:
            return digest
        # An attestation manifest declares os "unknown"; never fall back to one.
        if fallback is None and platform.get("os") != "unknown":
            fallback = digest
    return fallback


def _authorised(
    get: Fetch, url: str, accept: str, token: str | None = None
) -> tuple[Response, str | None]:
    """One GET, retried once with an anonymous bearer token if asked for one.

    A public image on ghcr.io still answers 401 to an unauthenticated read: the
    token flow is the protocol, not a sign that credentials are needed. Retried
    only when we had no token, so a token the registry rejects is an answer
    rather than a loop.
    """
    headers = {"Accept": accept, "User-Agent": _USER_AGENT}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    response = get(url, headers)
    if response.status != 401 or token is not None:
        return response, token
    fresh = _anonymous_token(get, response.headers)
    if fresh is None:
        return response, None
    headers["Authorization"] = f"Bearer {fresh}"
    return get(url, headers), fresh


def _anonymous_token(get: Fetch, headers: Mapping[str, str]) -> str | None:
    """Follow a ``WWW-Authenticate: Bearer`` challenge with no credentials."""
    challenge = headers.get("www-authenticate", "")
    if not challenge.lower().startswith("bearer"):
        return None
    fields = dict(_CHALLENGE.findall(challenge))
    realm = fields.get("realm")
    if not realm or not _fetchable(realm):
        return None
    query = urllib.parse.urlencode(
        {key: fields[key] for key in ("service", "scope") if key in fields}
    )
    response = get(
        f"{realm}?{query}" if query else realm,
        {"Accept": "application/json", "User-Agent": _USER_AGENT},
    )
    if response.status != 200:
        return None
    payload = _document(response.body)
    for key in ("token", "access_token"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _document(body: bytes) -> dict[str, Any]:
    parsed: object = json.loads(body.decode("utf-8", "replace"))
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}


def _fetchable(url: str) -> bool:
    """Whether ``url`` is one this module may open at all.

    The ``realm`` of a ``WWW-Authenticate`` challenge is chosen by whatever
    registry the *image string* named, and :func:`_urlopen` goes through
    urllib's default opener, which also serves ``file:``. Unchecked, a registry
    can answer 401 with ``realm="file:///etc/passwd"`` - or a link-local
    metadata address - and :func:`_anonymous_token` will read it and send what
    came back to that same registry as ``Authorization: Bearer``. So the realm
    is the one URL here built from a stranger's bytes, and the scheme is what
    makes it safe to open.

    The **host** is deliberately not checked. A real registry's token endpoint
    is routinely a different host from its API - Docker Hub answers
    ``registry-1.docker.io`` with a realm on ``auth.docker.io`` - so pinning
    the realm to the host already being talked to would refuse the default
    registry.

    >>> _fetchable("https://auth.docker.io/token")
    True
    >>> _fetchable("file:///etc/passwd")
    False
    >>> _fetchable("http://169.254.169.254/latest/meta-data/")
    True
    """
    return urllib.parse.urlsplit(url).scheme in _FETCHABLE_SCHEMES


def _urlopen(url: str, headers: Mapping[str, str]) -> Response:
    """The real fetcher. GET only, and it reads a bounded body.

    The scheme is re-checked here rather than only at the realm, because this
    is the single place the module actually opens anything: a ``ValueError``
    lands in :func:`image_labels`'s own catch and degrades to "not measured",
    which is what every other failure here does.
    """
    if not _fetchable(url):
        raise ValueError(f"refusing to fetch a non-http(s) URL: {url!r}")
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as handle:
            return Response(
                handle.status, _lowered(handle.headers), handle.read(_MAX_BODY)
            )
    except urllib.error.HTTPError as error:
        # A 401 is the token challenge and a 404 is a private repository; both
        # are responses to read, not exceptions to raise.
        return Response(error.code, _lowered(error.headers), error.read(_MAX_BODY))


def _lowered(headers: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}
