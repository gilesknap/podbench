# The devcontainer should use the developer target and run as root with podman
# or docker with user namespaces.
FROM ghcr.io/diamondlightsource/ubuntu-devcontainer:resolute AS developer

# Add any system dependencies for the developer/build environment here
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    graphviz \
    curl \
    ca-certificates \
    && apt-get dist-clean

# helm, and the schema plugin the `helm-schema` pre-commit hook shells out to.
#
# The base image carries neither, which until now meant `just helm` failed and
# tests/test_chart_contract.py skipped itself in every devcontainer as well as in
# CI - a chart test that never runs. The hook makes it load-bearing: it
# regenerates Charts/podbench/values.schema.json, and with the plugin absent it
# fails the commit outright rather than skipping.
#
# Both versions are pinned: the plugin to the hook's `rev` in
# .pre-commit-config.yaml, because a different plugin generates a different
# schema and the disagreement surfaces in CI as a diff nobody wrote, and helm to
# the workflows' HELM_VERSION_TO_INSTALL, so a chart that renders here renders
# the same way there.
#
# The tarball is checked against the digest helm publishes beside it before
# anything is extracted. Piping curl straight into tar cannot do that - the
# archive is unpacked as it arrives, so by the time a bad download is noticed it
# has already been written - and a release artefact that lands unverified in
# every developer's image and every CI container is the wrong place to save two
# lines.
ARG HELM_VERSION=v3.17.1
RUN arch="$(dpkg --print-architecture)" \
    && tarball="helm-${HELM_VERSION}-linux-${arch}.tar.gz" \
    && curl -fsSL "https://get.helm.sh/${tarball}" -o "/tmp/${tarball}" \
    && curl -fsSL "https://get.helm.sh/${tarball}.sha256sum" -o "/tmp/${tarball}.sha256sum" \
    && (cd /tmp && sha256sum -c "${tarball}.sha256sum") \
    && tar -xz -C /tmp -f "/tmp/${tarball}" \
    && mv /tmp/linux-*/helm /usr/local/bin/helm \
    && rm -rf /tmp/linux-* "/tmp/${tarball}" "/tmp/${tarball}.sha256sum" \
    && helm plugin install https://github.com/losisin/helm-values-schema-json \
    --version v2.5.0

# kubectl, which is the launcher's only way to reach a cluster: podbench has one
# runtime dependency and it is the CLI, so every API call is a `kubectl` shelled
# out to (never a Kubernetes client library - see CLAUDE.md). The devcontainer
# shipped without it, which meant the one tool the product is built around could
# not be run in the environment it is developed in, and `podbench doctor`,
# tests/e2e and any hand check against the k3s bed all failed at the first call.
#
# Pinned to ci.yml's `kubectl-version`, which is the version the field cluster
# and the k3s bed both run: matching it means a `kubectl` here behaves as it does
# where the evidence was taken. It is only a floor for the launcher, which asks
# for MIN_KUBECTL (1.25) and no more, so bumping this is safe; keep the two
# numbers equal so there is one thing to bump.
#
# Verified against the digest published beside it, for the reason the helm block
# above states. kubectl's .sha256 carries the bare hash with no filename, so the
# name has to be supplied before `sha256sum -c` will read it.
ARG KUBECTL_VERSION=v1.36.1
RUN arch="$(dpkg --print-architecture)" \
    && base="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}" \
    && curl -fsSL "${base}/kubectl" -o /tmp/kubectl \
    && curl -fsSL "${base}/kubectl.sha256" -o /tmp/kubectl.sha256 \
    && echo "$(cat /tmp/kubectl.sha256)  /tmp/kubectl" | sha256sum -c - \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
    && rm -f /tmp/kubectl /tmp/kubectl.sha256 \
    && kubectl version --client

# The build stage installs the context into the venv
FROM developer AS build

# Change the working directory to the `app` directory
# and copy in the project
WORKDIR /app
COPY . /app
RUN chmod o+wrX .

# Tell uv sync to install python in a known location so we can copy it out later
ENV UV_PYTHON_INSTALL_DIR=/python

# Sync the project without its dev dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --no-dev --managed-python

# The runtime stage is the podbench debug image itself. Unlike the template it
# came from, it is not a thin launcher wrapper: a developer lands *inside* this
# image over ssh, so the whole toolchain has to be here.
#
# Debian 12 rather than the template's ubuntu:resolute, for two measured
# reasons. Its glibc 2.36 clears vscode-server's hard 2.28 floor (report 4.2),
# and every timing and size in S2/S3/S4 was taken on bookworm, so the image
# matches its own evidence. It is also the base of gcr.io/distroless/*-debian12,
# by far the most common Observe-mode target, which is what makes build-ids and
# dbgsym packages line up (S3). That match is a convenience only - `set sysroot
# /proc/<pid>/root` is what makes gdb correct, and S3 documents the plausible
# wrong backtrace you get by relying on the match instead.
FROM debian:bookworm-slim AS runtime

# Everything here is baked rather than installed at attach time, because
# `apt-get install openssh-server` measured 14-24 s (S2) and the whole apt step
# was 10.6 s of S4's 19 s dev-loop bootstrap - in both spikes apt, not the
# vscode-server download, was the cold path's dominant cost.
#
#   connection  openssh-server needs openssh-sftp-server for the sshd_config
#               Subsystem line and openssh-client for `ssh-keygen -A`; neither
#               arrives with --no-install-recommends.
#   ca-certificates  MANDATORY: without it libdebuginfod fails the TLS
#               handshake *silently* and every library reports "missing
#               debugging information" (report 4.3).
#   identity    libnss-extrausers, the NSS source a seat appends its own passwd
#               record to. It is what lets a seat running as an arbitrary uid
#               *and* gid be resolved by sshd without writing to /etc/passwd -
#               see the nsswitch.conf line below and issue #102.
#   debugging   gdb/gdbserver/binutils/elfutils/debuginfod(-find); binutils and
#               eu-readelf are what diagnose a build-id miss.
#   inspection  procps/lsof/strace/less, and iproute2 for `ss`, which is how the
#               relaunch loop pre-flights a port across containers (S4).
#   iteration   git/curl/xz-utils/rsync, joined by uv and a CPython below.
#   tini        an optional reaping PID 1 for the Iterate-mode sidecar, where
#               podbench is a real container rather than an ephemeral one.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    openssh-server \
    openssh-sftp-server \
    openssh-client \
    libnss-extrausers \
    gdb \
    gdbserver \
    binutils \
    elfutils \
    debuginfod \
    procps \
    lsof \
    strace \
    less \
    git \
    curl \
    xz-utils \
    iproute2 \
    rsync \
    tini \
    && rm -rf /var/lib/apt/lists/*

# Debian's gdb 13 is built against libpython3.11 and *hard-fails* when its
# Python stdlib is absent rather than degrading to a no-Python gdb (report 4.5).
# Keeping the distro gdb solves that by construction: apt pulls
# libpython3.11-stdlib as a dependency, so /usr/lib/python3.11 (17 MiB,
# confirmed on this base) is present and no PYTHONHOME/PYTHONPATH is needed.
# Never prune /usr/lib/python3.11 to save space - that is exactly the failure
# S5 hit. gdb's pretty-printers are the payoff for the 17 MiB.

# sshd will not start without its privilege separation directory ("Missing
# privilege separation directory: /run/sshd", reproduced on this base; with the
# directory present the 5-line config from report 4.1 gives CONFIG_OK). Some
# runtimes mount /run as a tmpfs, so an entrypoint still has to recreate it -
# baking it covers the common case. /etc/podbench is where the launcher writes
# the generated sshd_config.
#
# Host keys are deliberately NOT baked: a private key inside a published image
# is the same private key on every pod in the world. They are minted per attach
# (report R9 - host key identity is still an open design question).
RUN mkdir -p /run/sshd /etc/podbench

# The seat's uid *and gid* are the target's, discovered at attach time from a
# pod podbench did not build, so no account for them can be baked in here. sshd
# resolves the login name through NSS before it looks at a key, and ssh-keygen
# calls getpwuid() unconditionally - it dies with "No user exists for uid 36070"
# and a -C comment does not help. So the seat appends a record for itself at
# start-up (podbench agent's ensure_passwd_entry), and the default route for
# that is libnss-extrausers: nsswitch.conf is pointed at
# /var/lib/extrausers/passwd after files, and that file is world-writable, so
# the append needs no capability, no particular gid and no edit to the
# workload's manifest.
#
# It is not unconditional: this NSS source has floors compiled into it (MINUID
# 500, MINGID 500, with gid 100 exempted - s_config.h, 0.6-4.1) and silently
# ignores any record below them, for getpwnam as well as getpwuid. A seat under
# a floor takes /etc/passwd instead - and a seat under a floor generally cannot
# write /etc/passwd either, because the uids below 500 are exactly the ones whose
# gids are below 500 too (grafana 472:472, nginx-unprivileged 101:101). It had no
# route at all. So this image pre-seeds a record for every free uid under 500
# below, and that whole range needs no write.
# agent.extrausers_serves is where the routing is decided and argued.
#
# The alternative it replaces as the default is why this exists at all. The
# convention for containers running as an arbitrary uid is OpenShift's -
# /etc/passwd group-writable by GID 0, appended to by the entrypoint - and it
# buys nothing unless the seat carries gid 0. `--seat-gid-root` used to pin that
# outright and retired in #103: against a target whose gid is not 0 (36070 at
# Diamond) it destroyed the credential match ptrace needs and took the debugger
# with it, so the documented way out of "no ssh" cost the thing the seat is for
# (issue #102, measured on a k3s bed).
#
# `chmod g=u` therefore stays, and the reason is now a narrower one than it was.
# The seat that used to need it - runAsUser with no runAsGroup, so gid 0 by
# default - no longer arrives: podbench measures the target's real gid from
# /proc and pins it (#103). What is left is a target whose group genuinely *is*
# 0 at a uid of 500 or more, which the static records below do not reach and
# extrausers refuses; 0644 would leave it with no route. /etc/passwd is also
# still the file a `podbench dev` sidecar projects its identity over. gid 0
# grants no privilege on its own - it is a group, not root - and /etc/group gets
# the same treatment so an image change that starts needing a group record is
# not a second fix. World-write is a different question and the answer is no:
# 0666 here would be the seat's own uid writing a file a root sshd resolves
# from, which is #98's shape.
#
# Mode 0666 on the record file is not an escalation, and the reason is different
# on each rung, so both are stated. On `degraded` and `seat` sshd runs as the
# seat's own uid (SshdLayout.for_uid(n), run_as_root=False): it skips privilege
# separation and never setuids out of a passwd record, so a forged record buys
# its author the uid it already had, and NoNewPrivs has already made every
# setuid binary in the seat inert. On `full` sshd *is* root - that rung ships
# today, and pretending otherwise is how this comment was wrong once - and there
# the reason is that a root seat has no unprivileged principal: every process in
# it, the kubectl exec carrying ssh included, is already uid 0. Because that is
# an accident of the rung rather than an enforced property, the agent enforces
# it anyway and takes group/other write off this file on a root seat
# (agent.restrict_seat_nss_database).
#
# What must not ship is the combination in between, which is #98's shape: a root
# sshd that setuids into a *non-root* session, i.e. one container holding both an
# unprivileged writer of this file and a privileged reader of it. Whichever of
# the two lands second has to close the other off - either the database gains an
# owner and loses group/other write on that rung too, or the root sshd is not
# allowed to resolve from it.
RUN chmod g=u /etc/passwd /etc/group
RUN sed -i 's/^passwd:.*/passwd:         files extrausers/' /etc/nsswitch.conf \
    && mkdir -p /var/lib/extrausers \
    && touch /var/lib/extrausers/passwd \
    && chmod 0666 /var/lib/extrausers/passwd

# Static records for the sub-500 uids, which is precisely the range
# libnss-extrausers refuses (MINUID 500). A seat there could reach neither
# database: the extrausers append is ignored, and /etc/passwd is group-writable
# by gid 0 which a seat mirroring a 472:472 target does not have. Baked in, the
# lookup succeeds with nothing writable anywhere and ensure_passwd_entry returns
# having touched no file - which is the only identity mechanism in this image
# that costs no writable surface at all.
#
# Bounded because the range is: 499 lines, a few of them already taken by the
# distribution's own system accounts, which `getent` skips rather than shadows -
# a duplicate uid would make getpwuid answer with whichever came first and hand
# somebody `daemon`'s home. The uids above 500 get no such treatment because
# there is no end to them; that is what extrausers is for.
#
# The gid field is 65534 (`nogroup`), not a guess at the target's. It is not the
# process's group - the kernel gave the seat that from runAsGroup, and it is the
# target's - and a non-root sshd never setgid()s out of a passwd record, so this
# field is read by nothing that matters here. Claiming a group the seat does not
# have would be the only alternative.
#
# The home is sshcfg.FALLBACK_HOME_PREFIX + uid, which is where default_home()
# already sends a seat with no record, so nothing moves for the seats this
# changes. The one cost is a seat that *also* mounts the pod's podbench-home
# volume: sshd takes an interactive session's HOME from the passwd record, so
# that session starts in /tmp rather than on the volume. A record baked at build
# time cannot know a mount decided at attach time, and the alternative for these
# uids is the status quo ante - no login at all.
RUN set -eu; \
    for uid in $(seq 1 499); do \
        if ! getent passwd "$uid" >/dev/null; then \
            printf 'podbench-%s:x:%s:65534:podbench seat:/tmp/podbench-%s:/bin/bash\n' \
                "$uid" "$uid" "$uid"; \
        fi; \
    done >> /etc/passwd

# Symbols, but never sources, on Debian targets: S3 got a fully symbolised
# glibc + coreutils backtrace for 4.7 MB of client cache, and every source fetch
# 404'd (report 3.2). Debian already falls back to this same URL via
# /etc/debuginfod/elfutils.urls when the variable is unset (verified), so this
# only makes the setting visible and gives the launcher one place to override.
ENV DEBUGINFOD_URLS=https://debuginfod.debian.net

# And a bound on it, because gdb's default is 90 seconds *per shared library*
# and it spends them after the attach, with the workload stopped - against
# readiness deadlines podbench computes in tens of seconds. Two seconds is the
# value agent.DEBUGINFOD_TIMEOUT_SECONDS supplies to a session that arrives
# without one; this ENV is what bounds the routes no sshd config reaches, the
# kubectl exec shell and the gdb cpptools starts through gdb-podbench. The seat
# also probes the server once at start-up and drops the URL above from ssh
# sessions when nothing answers (agent.check_debuginfod), so this timeout is the
# bound on a server that is reachable and slow, not on one that is absent.
ENV DEBUGINFOD_TIMEOUT=2

# The static uv binary, not the install script: no curl at build time and a
# digest renovate can bump. uvx is skipped (~35 MiB for what `uv tool run` does).
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

# This is both podbench's own interpreter and the pre-seeded CPython that report
# 4.4 asks for: it is a uv *managed* install, so pointing UV_PYTHON_INSTALL_DIR
# at it means `uv venv` in a workspace finds it instead of downloading one
# (93 MiB saved over seeding a second copy). Other versions cost one
# `uv python install X` - 2.3 s measured in S4.
COPY --from=build /python /python
ENV UV_PYTHON_INSTALL_DIR=/python

# Copy the environment, but not the source code
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH

# debugpy, for the Python half of `debug-config`. 15 MiB against the brief's
# ~700 MB cap, and it buys the one thing a seat cannot improvise: the *driver*
# side of debugpy's attach-to-pid, which starts a debug server inside an
# uncooperative CPython with no cooperation from the app image (issue #20).
#
# `--target`, not a dependency and not this venv: podbench's wheel has exactly
# one runtime dependency and tests/test_packaging.py asserts it. The injection
# recipe also has to put this directory on PYTHONPATH by hand, which is easier
# to state as one path than as a venv's site-packages.
#
# What lands here is architecture-dependent and is *supposed* to be: debugpy
# publishes no aarch64 Linux wheel, so an arm64 image gets the same
# `py2.py3-none-any` wheel carrying only `attach_linux_amd64.so`. That is why
# `capreport` lists the attach helpers by name rather than reporting "debugpy:
# yes" — on arm64 the package is present and the mechanism is not.
RUN uv pip install --python /app/.venv/bin/python \
    --target /opt/podbench/debugpy debugpy==1.8.21

# Two files, both structural, and deliberately not the brief's per-subcommand
# helpers - see image/README.md, deviation 6, for why those went away.
#
#   podbench      the venv at /app/.venv/bin is on no default PATH, and
#                 /usr/local/bin is on sshd's compiled-in one whatever happens.
#                 The agent's sshd_config now carries the container's PATH into
#                 a session with SetEnv, so this is no longer the only route -
#                 it is the route that does not depend on that config having
#                 been written, which is the point of it.
#   gdb-podbench  installed as `gdb` below, for third-party callers.
COPY --chmod=0755 image/bin/ /usr/local/bin/

# `gdb` on PATH is the wrapper, and /usr/local/bin comes first. Every tool that
# shells out to `gdb --pid <n>` in a seat is broken twice without it — no
# sysroot, so it reads this container's libraries for another container's
# process, and a cwd cpptools may have deleted. debugpy's injection is the
# instance that was caught; the bug belongs to the seat, not to debugpy, so the
# fix goes where every caller gets it. `podbench dbg` and `podbench debug-config`
# are unaffected: they set the sysroot themselves and never pass --pid.
RUN ln -s gdb-podbench /usr/local/bin/gdb

# For interactive login shells, and still needed after SetEnv: Debian's
# /etc/profile assigns PATH outright rather than appending, so a login shell
# overwrites whatever the session was handed and the ENV above is lost again.
# This fragment is sourced afterwards and puts the venv back. Non-interactive
# `ssh host <cmd>` sources none of it and is covered by SetEnv (agent.py,
# SESSION_ENV_NAMES) plus /usr/local/bin/podbench naming the venv in full.
RUN printf '%s\n' 'PATH="/app/.venv/bin:$PATH"' 'export PATH' \
    > /etc/profile.d/podbench.sh

# Fail the build rather than the first attach if the interpreter copied out of
# the Ubuntu build stage cannot run on this base.
RUN podbench --version

# change this entrypoint if it is not the same as the repo
ENTRYPOINT ["podbench"]
CMD ["--version"]
