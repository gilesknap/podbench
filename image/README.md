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

Predicted before the image existed, by `du -sm /` inside a
`debian:bookworm-slim` pod on the cluster (arm64):

| Layer | Size |
|---|---|
| `debian:bookworm-slim` | 122 MiB |
| \+ the package set above (91 packages) | 336 MiB (**+214 MiB**) |
| \+ uv-managed CPython 3.11 at `/python` | +93 MiB |
| \+ `uv` binary and podbench's venv | tens of MiB |

≈ 450–500 MiB uncompressed, inside the brief's ~700 MiB Phase 1 budget.

**Measured on the built image**, `0.1.0b4` amd64, in a seat on the cluster
(2026-08-16), excluding what lands at runtime: **494 MiB**, of which `/python` is
95 MiB and `/app` 10 MiB. The prediction held.

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

Those were projections from S2, which never ran a GUI client. Measured since, in
a seat carrying a real Remote-SSH session (amd64, 2026-08-16):
`~/.vscode-server` is **1215 MiB**. The 1.1–1.3 GB figure was right, and it is a
per-seat cost on the workload's ephemeral-storage budget.

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

Two files, installed into `/usr/local/bin` from `image/bin/`. Both are
structural; neither is a convenience.

| On PATH | What it is | Why it has to exist |
|---|---|---|
| `podbench` | `exec /app/.venv/bin/podbench "$@"` | the venv is on no default `PATH`, and report 4.1's sshd_config sets `UsePAM no`, so `ssh <seat> podbench pids` gets sshd's compiled-in `PATH`. That includes `/usr/local/bin` and not `/app/.venv/bin`. |
| `gdb-podbench` | a shell wrapper around `/usr/bin/gdb`, installed as `gdb` too | its caller is a *third party* — debugpy's injection shells out to `gdb --nw --nh --nx --pid 1`, and without the wrapper it gets no `set sysroot` and a cwd VS Code may have deleted. No podbench subcommand can stand in for it. |

The `podbench` shim calls the venv by **absolute path** on purpose: `ssh <host>
podbench capreport` runs a non-login, non-interactive shell that sources
nothing, so the image's `ENV PATH` is not in effect. Interactive login shells
are covered separately by `/etc/profile.d/podbench.sh`, needed for the same
`UsePAM no` reason.

Everything a seat can do is reached as `podbench <verb>` — `podbench pids`,
`podbench dbg`, `podbench capreport`, `podbench debug-config`, `podbench
dev-bootstrap`, `podbench run`, `podbench stop` — and `podbench --help` lists
them all under "Inside the debug container". There are no per-subcommand
aliases; see deviation 6.

`gdb-podbench` is **not** a podbench subcommand. `podbench debug-config` points
`miDebuggerPath` at it because cpptools launches gdb inheriting its own
extension directory as a cwd, which VS Code deletes on update — gdb's libpython
then fails `getcwd()` and the process dies during startup with no signal name.
See the script's own comment, and `docs/how-to/debug-with-gdb.md`.

## Deviations from the brief

1. **`run`/`stop` were installed as `podbench-run`/`podbench-stop`,** because
   `/usr/local/bin` precedes `/usr/bin` and helpers called `run` and `stop`
   would shadow far too much of a user's own tooling inside their own shell.
   Superseded by deviation 6, which removed them; the reasoning is kept because
   it is half of that argument.
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
6. **The per-subcommand helpers are gone.** The brief's `bin/` sketch names
   `pids`, `dbg`, `capreport`, `debug-config`, `dev-bootstrap` and `run`/`stop`
   as files on `PATH`; the image ships none of them ([#47]). Each was literally
   `exec /app/.venv/bin/podbench <subcommand> "$@"`, so they added no behaviour,
   only a second name for every verb — a contract this README and CI both had
   to pin. Deviation 1 is the argument in miniature: two of the seven had to be
   renamed away from the brief's spelling to stop them shadowing a user's own
   `run` and `stop`, which left them *longer* to type than the `podbench run`
   they aliased. The discoverability they were meant to buy is already in
   `podbench --help`, which lists every in-pod verb.

   `gdb-podbench` stays, and it is the exception that shows the rule: its caller
   is debugpy, not a human, so no amount of `podbench --help` reaches it.

[#47]: https://github.com/gilesknap/podbench/issues/47

## Assumptions this image makes of the rest of podbench

* `podbench capreport --json` exits 0/10/20 per `Verdict`, and `podbench pids`
  accepts `--help`. CI checks both through `/usr/local/bin/podbench` by
  overriding the entrypoint. `podbench --version` must exit 0 — the build runs
  it as its own final step, so a broken CLI fails the build rather than the
  first attach.
* The venv stays at `/app/.venv`. The `podbench` shim hard-codes it.
* `HOME` is **not** set by the image. Root's ssh config lives under `/root`
  (report 4.1), while the Iterate-mode sidecar wants `HOME=/workspace` so uv's
  caches land in the `emptyDir` rather than the container's writable layer (S4).
  That is the launcher's call, per mode.
* `/run/sshd` and `/etc/podbench` exist in the image, but an entrypoint must
  recreate `/run/sshd` when the runtime mounts `/run` as a tmpfs.

## Verification status

**The image is built and published by CI on every push**, and has been run
against real clusters. It is still never built *here* — there is no docker,
podman, buildx, kind or hadolint in this devcontainer — so an in-image change is
tested by pushing the branch and pulling the prerelease image CI publishes for
it (`tests/e2e/README.md`).

What CI verifies on every build, per architecture:

* each arch builds on a runner of that arch, never under emulation, so the
  smoke tests below are a real execution of the artefact that gets published;
* `podbench --version` runs — as the final `RUN` of the build itself, and again
  through the entrypoint;
* `capreport --json` and `pids --help` run through `/usr/local/bin/podbench`
  named by absolute path — the `ENV PATH` puts the venv first, so a bare
  `podbench` would leave the shim every ssh session resolves untested — and
  `gdb`, `sshd`, `uv` and `git` resolve on `PATH`;
* the published manifest list is assembled from the digests that were tested,
  rather than by a second build.

On top of that, `_e2e.yml` runs S1–S5 against the image on kind, and the suite
has been run against a live k3s cluster.

The list below is what was verified *before* the first build, on a real cluster,
in namespace `podbench-s0` (throwaway pods, since deleted). It is kept because it
is why the package list looks the way it does:

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

Everything that section once listed as "unverified until the first build" has
since been answered: the layers assemble, the uv-managed interpreter copied out
of the Ubuntu `developer` stage is discovered by the runtime's newer uv, the
final image is 494 MiB on amd64, and both architectures are built and
smoke-tested natively rather than one being assumed symmetric with the other.

Still open, and not answerable by a build:

* **R3, the trim list.** Still validated only by "the server starts and serves
  `/version`". A GUI client has since run in a seat, but never against a
  *trimmed* server, so the trim is still not safe to make default.
* **R4, sources for debuginfod.** Unchanged: Debian serves symbols, not sources.
* **arm64 at large.** Both arches are built and smoke-tested, but the live
  cluster work has been on amd64. The runtime figures above are amd64.
