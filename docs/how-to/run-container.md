# The container image

The image is the half of podbench that runs in the cluster. It is a development
seat, not a CLI wrapper: a developer lands *inside* it over ssh, so the whole
toolchain has to be there.

```
ghcr.io/gilesknap/podbench:latest
```

Built and pushed by CI on tag; a numbered release tag pins a specific build.

You normally never pull it yourself. `kubectl podbench attach` names it in the
ephemeral container spec and the kubelet pulls it onto whichever node the target
pod is running on.

## Choosing a different image

```
$ kubectl podbench attach pod/foo --image ghcr.io/gilesknap/podbench:0.3.0
$ export PODBENCH_IMAGE=registry.internal/podbench@sha256:...
```

`--image` wins over `PODBENCH_IMAGE`, which wins over the default.

**Pin a digest for anything permanent.** An admission policy that says "this
image, as an ephemeral container only, with only `SYS_PTRACE`, for these users"
is only writable against a pinned, published image — and that policy is the
whole organisational argument for allowing podbench at all. See
[Security model](../explanations/security.md).

The Helm chart records the same reference under `image.repository` / `image.tag`
so a cluster has one place to state which build it trusts. Keep it and the
launcher's default in agreement.

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

Each is a one-line wrapper around the same Python implementation the launcher
uses, called by absolute path — `ssh <host> capreport` runs a non-login,
non-interactive shell that sources nothing.

| On `PATH` | Verb |
|---|---|
| `pids` | `podbench pids` |
| `dbg` | `podbench dbg` |
| `capreport` | `podbench capreport` |
| `dev-bootstrap` | `podbench dev-bootstrap` |
| `podbench-run` | `podbench run` |
| `podbench-stop` | `podbench stop` |

`run` and `stop` are installed under prefixed names on purpose: `/usr/local/bin`
precedes `/usr/bin`, and helpers called `run` and `stop` would shadow far too
much of your own tooling inside your own shell.

## Environment it reads

| Variable | Meaning |
|---|---|
| `PODBENCH_SSH_PUBKEY` | authorized key, injected by the launcher |
| `PODBENCH_SSH_PUBKEY_FILE` | read it from a file instead (default mount `/etc/podbench/ssh/authorized_keys`) |
| `PODBENCH_SSH_HOST_KEY` / `..._FILE` | supply a host key rather than minting one; the file default is `/etc/podbench/ssh/ssh_host_ed25519_key` |
| `PODBENCH_TARGET_CID` | the target container's runtime ID, injected at attach time; how `pids` and `dbg` find the workload |
| `DEBUGINFOD_URLS` | defaults to `https://debuginfod.debian.net`; point it at a mirror |

## Building it yourself

```
$ docker build -t podbench:dev .
```

The repository `Dockerfile` has `developer` → `build` → `runtime` stages; the
image is the `runtime` stage. To use a local build against a cluster you must
push it somewhere the cluster can pull from — the kubelet pulls it, so a local
daemon image is not enough unless your cluster shares that daemon (kind:
`kind load docker-image podbench:dev`).
