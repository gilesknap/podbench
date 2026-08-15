# The devcontainer should use the developer target and run as root with podman
# or docker with user namespaces.
FROM ghcr.io/diamondlightsource/ubuntu-devcontainer:resolute AS developer

# Add any system dependencies for the developer/build environment here
RUN apt-get update -y && apt-get install -y --no-install-recommends \
    graphviz \
    && apt-get dist-clean

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

# The seat's uid is the *target's* uid, discovered at attach time from a pod
# podbench did not build, so no account for it can be baked in here. sshd
# resolves the login name through NSS before it looks at a key, and ssh-keygen
# calls getpwuid() unconditionally - it dies with "No user exists for uid 1000"
# and a -C comment does not help. The convention for containers that run as an
# arbitrary uid is OpenShift's: leave /etc/passwd group-writable by GID 0 and
# let the entrypoint append its own record (podbench agent's ensure_passwd_entry
# does). It buys nothing unless the seat is landed with GID 0, which is what
# `kubectl podbench attach --seat-gid-root` asks for; with any other gid the
# file is unwritable, the agent says so and the seat lands without ssh.
#
# This grants no privilege on its own: gid 0 is a group, not root, and every
# other permission in the image is unchanged. /etc/group gets the same treatment
# so an image change that starts needing a group record is not a second fix.
RUN chmod g=u /etc/passwd /etc/group

# Symbols, but never sources, on Debian targets: S3 got a fully symbolised
# glibc + coreutils backtrace for 4.7 MB of client cache, and every source fetch
# 404'd (report 3.2). Debian already falls back to this same URL via
# /etc/debuginfod/elfutils.urls when the variable is unset (verified), so this
# only makes the setting visible and gives the launcher one place to override.
ENV DEBUGINFOD_URLS=https://debuginfod.debian.net

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

# The helpers the brief puts on PATH. They are one-line `exec podbench <sub>`
# wrappers so there is a single tested implementation rather than a second one
# in shell - see image/README.md for why `run`/`stop` are `podbench-run` and
# `podbench-stop` here.
COPY --chmod=0755 image/bin/ /usr/local/bin/

# sshd is built with UsePAM no in report 4.1's config, so login shells get
# sshd's compiled-in default PATH and never see the ENV above. This fixes
# interactive sessions; `ssh host <cmd>` sources nothing at all, which is why
# the helpers in image/bin/ call podbench by absolute path.
RUN printf '%s\n' 'PATH="/app/.venv/bin:$PATH"' 'export PATH' \
    > /etc/profile.d/podbench.sh

# Fail the build rather than the first attach if the interpreter copied out of
# the Ubuntu build stage cannot run on this base.
RUN podbench --version

# change this entrypoint if it is not the same as the repo
ENTRYPOINT ["podbench"]
CMD ["--version"]
