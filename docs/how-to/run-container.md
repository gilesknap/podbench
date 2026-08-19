# The container image

The image is the half of podbench that runs in the cluster. It is a development
seat, not a CLI wrapper: a developer lands *inside* it over ssh, so the whole
toolchain has to be there.

:::{note}
Commands here are written `podbench <verb>` — the only spelling there is. If you
have not installed the launcher, run each as `uvx podbench <verb>`, or, before
the first PyPI release, as
`uvx --from git+https://github.com/gilesknap/podbench podbench <verb>`. See
[Setup](../tutorials/setup.md).
:::

```
ghcr.io/gilesknap/podbench:<launcher version>
```

Built and pushed by CI on tag; a numbered release tag pins a specific build.

**The default tag follows the launcher's own version**, so a launcher asks for
the image built from its own source. That matters because `uvx podbench` resolves
a launcher afresh on every run, and a version can move between two attaches with
nothing to see: pinning the tag to the version keeps the two halves in step,
where a fixed `:latest` would eventually let a launcher author a container spec
its image does not understand.

A launcher built from a checkout — a clone, or `uvx --from git+...` — matches no
published image and falls back to **`:main`**, the branch-tip image CI pushes on
every default-branch commit. That is the same-source counterpart to such a
launcher. `:latest` is deliberately not the fallback: it moves only on a
**final** release, so an unpinned user is never handed a prerelease — but for
the same reason it does not move at all on a project that has so far tagged only
prereleases, and a stale `:latest` is exactly the launcher/image skew this
scheme exists to prevent.

One release has two spellings, and CI pushes both onto the same digest: the git
tag and the chart use SemVer (`1.0.0-beta.1`), while the wheel — and so the
launcher's version — uses PEP 440 (`1.0.0b1`). Either tag pulls the same image.

:::{note}
Tags published before this scheme (`0.1.0-alpha.1` … `0.1.0-alpha.6`) carry the
SemVer spelling only. A launcher installed from one of those git tags asks for
its own PEP 440 spelling — `0.1.0-alpha.4` becomes `0.1.0a4` — which was never
pushed; pass the SemVer tag of that same release, e.g. `--image
ghcr.io/gilesknap/podbench:0.1.0-alpha.4`.
:::

You normally never pull it yourself. `podbench attach` names it in the
ephemeral container spec and the kubelet pulls it onto whichever node the target
pod is running on.

## Choosing a different image

```
$ podbench attach pod/foo --image ghcr.io/gilesknap/podbench:0.3.0
$ export PODBENCH_IMAGE=registry.internal/podbench@sha256:...
```

`--image` wins over `PODBENCH_IMAGE`, which wins over the default.

**Pin a digest for anything permanent.** An admission policy that says "this
image, as an ephemeral container only, with only `SYS_PTRACE`, for these users"
is only writable against a pinned, published image — and that policy is the
whole organisational argument for allowing podbench at all. See
[Security model](../explanations/security.md).

The Helm chart records the same reference under `image.repository` / `image.tag`
so a cluster has one place to state which build it trusts. Nothing in the chart
templates it — it is there to be read by the admission policy you write — so a
cluster that pins there must pin the launcher too, with `PODBENCH_IMAGE` or
`--image`. Left alone, the launcher tracks its own version and the chart's
`image.tag` stays empty.

## Mirroring it

Nothing in podbench requires ghcr.io specifically:

```
$ skopeo copy docker://ghcr.io/gilesknap/podbench:0.3.0 \
              docker://registry.internal/podbench:0.3.0
$ export PODBENCH_IMAGE=registry.internal/podbench:0.3.0
```

If your registry needs credentials, the target pod's namespace needs the
`imagePullSecrets` — the ephemeral container is pulled with the pod's service
account, like any other container.

## Running it on your laptop

You can, but there is very little point: outside a pod there is no target
container, no shared PID namespace and nothing to debug. It is useful for
exactly two things — checking a tag exists, and inspecting what is inside:

```
$ docker run --rm ghcr.io/gilesknap/podbench:latest --version
$ docker run --rm -it --entrypoint bash ghcr.io/gilesknap/podbench:latest
```

The entrypoint is `podbench`, so arguments are podbench verbs. In a pod the
launcher overrides the command with `podbench agent`, which prepares sshd and
then idles as PID 1.

## What is in it

`debian:bookworm-slim` (glibc 2.36), because vscode-server needs **glibc ≥ 2.28**
— Alpine and musl are unsupported, and the real failure there is a missing ELF
interpreter rather than symbol versions. It is also the base of
`gcr.io/distroless/*-debian12`, the most common Observe-mode target, which makes
build IDs and `-dbgsym` packages line up. Treat that as a convenience only:
`set sysroot /proc/<pid>/root` is what makes gdb correct, and a matched distro
*hides* the wrong-sysroot bug rather than fixing it.

| Group | Contents |
|---|---|
| Connection | `openssh-server`, `openssh-sftp-server`, `openssh-client` |
| TLS | `ca-certificates` — **mandatory**; without it `libdebuginfod` fails the TLS handshake *silently* and every library reports "missing debugging information" |
| Debugging | `gdb`, `gdbserver`, `binutils`, `elfutils`, `debuginfod` |
| Inspection | `procps`, `lsof`, `strace`, `less`, `iproute2` |
| Iteration | `git`, `curl`, `xz-utils`, `rsync`, `uv`, a pre-seeded CPython |
| PID 1 | `tini`, for the Iterate-mode sidecar |

Roughly 450–500 MiB uncompressed, inside the 700 MiB budget the design brief
sets.

Three deliberate omissions:

* **vscode-server** — the client/server version check is a hard handshake
  rejection, so a baked server is correct for about four weeks. It downloads on
  first connect in 2.17 s.
* **ssh host keys** — a private key baked into a published image is the same
  private key on every pod in the world. They are minted per attach.
* **A compiler** — the ptrace probe uses the bundled interpreter's `ctypes`, so
  ~200 MiB of toolchain is not carried.

`gdb` keeps its Python. Debian's gdb 13 *hard-fails* when its Python stdlib is
missing rather than degrading, and installing it from apt solves that by
construction. Do not prune `/usr/lib/python3.11` to save space.

## Helpers on `PATH`

Two, and both are structural rather than convenient.

| On `PATH` | What it is |
|---|---|
| `podbench` | `exec /app/.venv/bin/podbench "$@"` — the venv is on no default `PATH`, and `ssh <host> podbench capreport` runs a non-login, non-interactive shell that sources nothing, so this file by absolute path is what makes the verb resolve even when the agent's `SetEnv` line did not reach the session |
| `gdb-podbench` | installed as `gdb` as well, so anything that shells out to `gdb --pid <n>` in the seat gets the same startup sequence `podbench dbg` runs — sysroot, an exec file gdb cannot canonicalise back into this container (issue #90), auto-load safe path and SIGURG handling — and a working directory that exists |

Every in-pod verb is reached as `podbench <verb>`: `podbench pids`, `podbench
dbg`, `podbench capreport`, `podbench debug-config`, `podbench dev-bootstrap`,
`podbench run`, `podbench stop`. There are no shorter aliases — `podbench
--help` lists the lot.

## Environment it reads

| Variable | Meaning |
|---|---|
| `PODBENCH_SSH_PUBKEY` | authorized key, injected by the launcher |
| `PODBENCH_SSH_PUBKEY_FILE` | read it from a file instead (default mount `/etc/podbench/ssh/authorized_keys`) |
| `PODBENCH_SSH_HOST_KEY` / `..._FILE` | supply a host key rather than minting one; the file default is `/etc/podbench/ssh/ssh_host_ed25519_key` |
| `PODBENCH_TARGET_CID` | the target container's runtime ID, injected at attach time; how `podbench pids` and `podbench dbg` find the workload |
| `PODBENCH_TARGET` | the target container's name, injected at attach time; what `podbench pids` heads its listing with |
| `PODBENCH_POD_CONTAINERS` | every container in the pod, comma-separated, injected at attach time; how `podbench pids` names the containers this seat is not in |
| `DEBUGINFOD_URLS` | defaults to `https://debuginfod.debian.net`; point it at a mirror. The agent connects to it once at start-up and drops it from ssh sessions when nothing answers |
| `DEBUGINFOD_TIMEOUT` | seconds gdb will wait on that server, per file. Defaults to `2`; gdb's own default is 90, spent after the attach with the workload stopped |

sshd passes none of its own environment to the commands it runs, so `podbench
agent` names the ones that matter in the sshd config it generates: every
`PODBENCH_*` variable except the keys, plus `PATH`, `DEBUGINFOD_URLS` and
`DEBUGINFOD_TIMEOUT`. Anything else you set on the container reaches `kubectl
exec` and a shell, and not an ssh session. If a value contains whitespace sshd
cannot carry it, and the agent says so in the container's start-up log rather
than dropping it quietly — `kubectl logs <pod> -c <the debug container>`.

## Building it yourself

```
$ docker build -t podbench:dev .
```

The repository `Dockerfile` has `developer` → `build` → `runtime` stages; the
image is the `runtime` stage. To use a local build against a cluster you must
push it somewhere the cluster can pull from — the kubelet pulls it, so a local
daemon image is not enough unless your cluster shares that daemon (kind:
`kind load docker-image podbench:dev`).
