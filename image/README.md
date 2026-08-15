# The podbench image

The container a developer lands in. It is built by the repository root
`Dockerfile` (`runtime` target); this file records what is in it, what is
deliberately *not*, and where it departs from the design brief.

Every number below was measured — in-cluster on `debian:bookworm-slim`, or by
the Phase 0 spikes (`docs/explanations/spikes/`). None is an estimate unless it
says so.

## Base

`debian:bookworm-slim` (glibc 2.36, verified `ldd 2.36-9+deb12u14`).

* vscode-server needs **glibc ≥ 2.28** — the real failure on musl is a missing
  ELF interpreter, not symbol versions (report 3.19). Alpine is unsupported.
* Bookworm is what S2, S3 and S4 measured on, so the image matches its own
  evidence.
* It is also the base of `gcr.io/distroless/*-debian12`, the most common Observe
  target, which makes build-ids and `-dbgsym` packages line up (S3). Treat that
  as a convenience: `set sysroot /proc/<pid>/root` is what makes gdb correct,
  and a matched distro *hides* the wrong-sysroot bug rather than fixing it.

## What is in it, and why

| Group | Packages | Why |
|---|---|---|
| Connection | `openssh-server`, `openssh-sftp-server`, `openssh-client` | `apt-get install openssh-server` measured **14–24 s** and was the dominant cold-path cost (S2), so it is baked. `--no-install-recommends` drops sftp-server (the `Subsystem` line in report 4.1's config) and `ssh-keygen` (host keys are minted per attach), so both are named explicitly. |
| TLS | `ca-certificates` | **Mandatory.** Without it `libdebuginfod` fails the TLS handshake *silently* and every library reports "missing debugging information" — users read that as "debuginfod is broken" (report 4.3). |
| Debugging | `gdb`, `gdbserver`, `binutils`, `elfutils`, `debuginfod` | `debuginfod` is what ships `debuginfod-find`; `readelf`/`eu-readelf` are how a build-id miss gets diagnosed. |
| Inspection | `procps`, `lsof`, `strace`, `less`, `iproute2` | `ss` (iproute2) is how the relaunch loop pre-flights a port across containers — it sees every container via the shared netns + PID ns + `CAP_SYS_PTRACE` (S4). |
| Iteration | `git`, `curl`, `xz-utils`, `rsync`, `uv`, a pre-seeded CPython | apt was **10.6 s of S4's 19 s** dev-loop bootstrap (55 %); baking it takes that to ~4 s. |
| PID 1 | `tini` | Optional, for Iterate mode where podbench is a real sidecar. Ephemeral containers get their PID 1 from the target pod, so `sleep infinity` remains the launcher's command there. |

**gdb keeps its Python.** A gdb built with Python *hard-fails* when its stdlib is
missing instead of degrading (report 4.5). Debian's gdb 13.1 is such a build, and
installing it from apt solves the problem by construction: `libpython3.11-stdlib`
arrives as a dependency, so `/usr/lib/python3.11` (17 MiB) is present and no
`PYTHONHOME`/`PYTHONPATH` is needed. Verified in-cluster:
`gdb -q -batch -ex 'python import json; ...'` prints `3.11.2`. Do not prune
`/usr/lib/python3.11` to save space; pretty-printers are the payoff.

**The pre-seeded CPython is podbench's own interpreter.** The build stage
installs a uv-*managed* CPython at `/python`, and the runtime stage points
`UV_PYTHON_INSTALL_DIR` at it, so `uv venv` in a workspace discovers it instead
of downloading one. That is 93 MiB not spent on a second copy. Verified on
bookworm: the managed interpreter links only against base libc components
(`libc`, `libm`, `libdl`, `libpthread`, `librt`, `libutil`) and imports `ssl`,
`sqlite3`, `ctypes`, `lzma`, `curses`, `readline` and `bz2` with no extra apt
packages. Another version costs one `uv python install X` — 2.3 s in S4.

## What is deliberately not in it

* **vscode-server.** The client/server version check is a hard handshake
  rejection with no negotiation (`Client refused: version mismatch`, report
  3.7/A3), so a baked server is correct for at most ~4 weeks and breaks stale
  and Insiders clients immediately. The download is 2.17 s and the whole cold
  path 5.76 s; downloading on first connect is strictly the better trade.
* **Host keys.** A private key baked into a published image is the same private
  key on every pod. They are minted per attach — which is why `known_hosts`
  handling is still an open question (report R9).
* **A compiler.** `capreport`'s ptrace probe uses the venv interpreter's
  `ctypes`, so the `cc`/`gcc` fallback backend is not needed and ~200 MiB of
  toolchain is not carried.
* **Sources for debuginfod.** Debian's debuginfod serves symbols but not sources
  (404 on every source path, plus `DW_AT_comp_dir: .` client-side rejection —
  report 3.2). Nothing in the image can fix that; source provisioning is an
  unsolved design problem (R4).

## Size budget

Image, measured with `du -sm /` inside a `debian:bookworm-slim` pod on the
cluster (arm64):

| Layer | Size |
|---|---|
| `debian:bookworm-slim` | 122 MiB |
| \+ the package set above (91 packages) | 336 MiB (**+214 MiB**) |
| \+ uv-managed CPython 3.11 at `/python` | +93 MiB |
| \+ `uv` binary and podbench's venv | tens of MiB |

≈ 450–500 MiB uncompressed, inside the brief's ~700 MiB Phase 1 budget.

What lands *at runtime* is much larger, and disk — not memory — is the
constraint (report 4.2):

| Item | amd64 | arm64 |
|---|---|---|
| vscode-server, extracted | **680.8 MiB** | **638.3 MiB** |
| `ms-vscode.cpptools` | 330 MiB | 261 MiB |
| server idle RSS | ~97 MiB | ~92 MiB |
| `~/.vscode-server`, one extension | — | 995 MiB |
| two server versions + six extensions | 2.2 GB | — |
| **realistic Observe-mode footprint** | **1.1–1.3 GB of node disk** | |

The brief's "~1 GB" Observe budget is already exceeded by the stock server
alone. Restate it as ~1.5 GB, or trim.

### The trim list

This belongs in the **bootstrap path, not the image** — the server is downloaded
per connect, so there is nothing to trim at build time. After extraction:

```sh
rm -rf "$SRV"/extensions/{copilot,copilot-chat,mermaid-markdown-features}
rm -rf "$DATA"/data/CachedExtensionVSIXs
```

−218 MiB (−34 %, 646 M → 428 M) and a further −190 MiB after six extension
installs. Caveat R3: verified only by "the server still starts and serves
`/version`" — a real GUI client may want what was deleted, so re-validate before
making the trim default.

## Helpers on PATH

Installed into `/usr/local/bin` from `image/bin/`. Each is a one-line
`exec /app/.venv/bin/podbench <subcommand> "$@"` wrapper, so there is a single
tested Python implementation instead of a second one in shell.

| On PATH | Subcommand | Purpose |
|---|---|---|
| `pids` | `podbench pids` | processes belonging to the target container |
| `dbg` | `podbench dbg` | gdb with sysroot/source/auto-load path preset |
| `capreport` | `podbench capreport` | probe ptrace permissions, name the blocker |
| `dev-bootstrap` | `podbench dev-bootstrap` | clone + sync + editable install |
| `podbench-run` | `podbench run` | relaunch the workload |
| `podbench-stop` | `podbench stop` | stop it, by recorded PID |

They call podbench by **absolute path** on purpose: `ssh <host> capreport` runs a
non-login, non-interactive shell that sources nothing, so the image's `ENV PATH`
is not in effect. Interactive login shells are covered separately by
`/etc/profile.d/podbench.sh`, which is needed because report 4.1's sshd_config
sets `UsePAM no` and sshd then supplies its own compiled-in `PATH`.

## Deviations from the brief

1. **`run`/`stop` are installed as `podbench-run`/`podbench-stop`.**
   `/usr/local/bin` precedes `/usr/bin`, so helpers called `run` and `stop` would
   shadow far too much of a user's own tooling inside their own shell.
2. **Helpers are wrappers, not bash implementations.** The brief's
   `bin/` sketch implies shell scripts; the logic they need (container-id
   substring matching in `/proc/<pid>/cgroup`, socket-inode ownership checks,
   the four-subsystem ptrace probe) is not shell-sized and is tested once in
   Python.
3. **The image lives at the repo root `Dockerfile`, not `Containerfile`**, and
   keeps the copier template's developer → build → runtime layering plus the
   `ENTRYPOINT ["podbench"] / CMD ["--version"]` smoke test that CI runs.
4. **`tini` is present but unused by default.** The brief offers "tini or
   `sleep infinity`"; the launcher passes a long-running command, and a dead
   ephemeral container name is burnt for the pod's lifetime (report 4.2), so the
   choice is the launcher's.
5. **`DEBUGINFOD_URLS` is set explicitly** to `https://debuginfod.debian.net`.
   Debian falls back to the same URL via `/etc/debuginfod/elfutils.urls` when the
   variable is unset (verified in-cluster), so this changes no behaviour — it
   makes the setting visible in `docker inspect` and gives the launcher one
   place to point at a mirror. S3 measured the endpoint working.

## Assumptions this image makes of the rest of podbench

* Subcommand names: `pids`, `dbg`, `capreport`, `dev-bootstrap`, `run`, `stop`,
  and `--version`. `capreport` must accept `--json` and exit 0/10/20 per
  `Verdict`; `pids` must accept `--help`. CI checks both by overriding the
  entrypoint. `podbench --version` must exit 0 — the build runs it as its own
  final step, so a broken CLI fails the build rather than the first attach.
* The venv stays at `/app/.venv`. The wrappers hard-code it.
* `HOME` is **not** set by the image. Root's ssh config lives under `/root`
  (report 4.1), while the Iterate-mode sidecar wants `HOME=/workspace` so uv's
  caches land in the `emptyDir` rather than the container's writable layer (S4).
  That is the launcher's call, per mode.
* `/run/sshd` and `/etc/podbench` exist in the image, but an entrypoint must
  recreate `/run/sshd` when the runtime mounts `/run` as a tmpfs.

## Verification status

**The image has never been built.** There is no docker, podman, buildx, kind or
hadolint in this devcontainer, so what follows is what *was* verified, on a real
cluster, in namespace `podbench-s0` (throwaway pods, since deleted):

* Every apt package name resolves in the bookworm archive:
  `apt-get install --dry-run --no-install-recommends <the exact list>` → rc=0,
  91 packages, no errors.
* A real install of that list succeeds and yields `sshd`, `ssh-keygen`,
  `/usr/lib/openssh/sftp-server`, `gdbserver`, `readelf`, `eu-readelf`,
  `debuginfod-find`, `ss`, `ip`, `lsof`, `strace`, `rsync`, `git`, `curl`, `xz`,
  `less`; gdb's embedded Python works; `sshd -t` on report 4.1's config returns
  `CONFIG_OK` once `/run/sshd` exists (and fails without it).
* `ghcr.io/astral-sh/uv:0.12.5` exists, is multi-arch, and `/uv --version` runs
  from it as a pod command.
* A uv-managed CPython runs on `debian:bookworm-slim` with no extra packages.

Unverified until the first build: that the layers assemble (`COPY --chmod`,
`COPY --from=<image>`), that a uv-managed interpreter built in the Ubuntu
`developer` stage is discovered as a managed install by the runtime's newer uv,
the true final image size, and multi-arch build on amd64 (all checks above ran
on an arm64 node; the amd64 archive is assumed symmetric).
