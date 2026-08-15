# Attach to a pod

Observe mode: put a debug seat into a **live** pod without disturbing it, and
find out what that seat can actually do. For the guided version, see
[Your first session](../tutorials/first-session.md); this page is the recipes.

:::{note}
Commands here are written `podbench <verb>` — the only spelling there is. If you
have not installed the launcher, run each as `uvx podbench <verb>`, or, before
the first PyPI release, as
`uvx --from git+https://github.com/gilesknap/podbench podbench <verb>`. See
[Installation](../tutorials/installation.md).
:::

:::{warning}
On a live pod podbench shares the workload's memory and ephemeral-storage limits
and **cannot reserve its own** — an ephemeral container may not declare
`resources` at all. A VS Code session is a 1.1–1.3 GB working set, so attaching
to a tightly limited pod can get the workload OOM-killed or the whole pod
evicted, and an OOM inside an ephemeral container is unrecoverable. Anything
heavier than looking belongs in a dev pod
([Iterate on Python](iterate-on-python.md)).
:::

## Attach, and re-attach

```
$ podbench attach pod/web-6c9d7f4b8b-hq2vn -n demo
```

`pod/NAME` and a bare `NAME` are both accepted. Namespace defaults to your
current context's.

Running it again **reconnects** to the running podbench container rather than
adding a second one. That is not an optimisation: ephemeral containers cannot be
removed or restarted, every attach appends to the pod spec permanently, and a
container name once used is burnt for the pod's lifetime. `--new` forces a fresh
container with the next free `podbench-<n>` name — use it when the previous one
died, not out of habit.

## Choosing the target container

podbench needs to know *which* container's PID namespace to join and whose UID
to match:

```
$ podbench attach pod/web-... --target api
```

Without `--target` it picks the pod's first container. On a multi-container pod,
name it — the target choice determines the sysroot, the UID of the degraded
rung, and what `pids` calls a target process.

If the pod spec does not state a `runAsUser` for the target (so the UID comes
from the image), tell podbench with `--target-uid 1000`. The degraded rung must
match the target's UID exactly; it never defaults to root, because root without
`CAP_SYS_PTRACE` is strictly *worse* than the target's own UID — it cannot even
read `/proc/<pid>/root`.

## When the cluster refuses `SYS_PTRACE`

Nothing to do — that is the normal path. podbench catches the refusal and falls
to the next rung automatically, and still exits `0`:

```
rung        degraded - target UID, no capabilities (read-only inspection)
ladder
  full      refused  Pod Security Admission: must not include "SYS_PTRACE" in
                     securityContext.capabilities.add
  degraded  landed   admitted by the API server and the kubelet
supports
  [ ] live attach (gdb -p <pid>)
      CAP_SYS_PTRACE is not in this container's effective set...
  [x] read-only inspect (/proc/<pid>/root, maps, environ)
  [x] seat (editor, shell, git)
```

The degraded rung is genuinely useful. It reads the target's rootfs, `maps`,
`environ`, `exe` and `cwd`, and it gives you **full source-level debugging of
programs gdb starts itself** — breakpoints, `run`, `continue`, backtraces,
locals — with `CapEff: 0000000000000000`. What you lose is attach to an
already-running process. See [Debug with gdb](debug-with-gdb.md).

Two things it cannot do, so do not plan on them: `/proc/<pid>/mem` and
`/proc/<pid>/syscall` use `PTRACE_MODE_ATTACH` and are denied.

## Making memory headroom first

```
$ podbench attach pod/web-... --resize 6Gi
```

This raises the **target container's** memory limit in place
(`kubectl patch pod --subresource resize`) before the seat lands, because the
headroom has to exist before vscode-server starts allocating into a limit
podbench cannot reserve.

It is opt-in and it prints a warning either way, because it is only lightly
proven: one Kubernetes version, one pod, never against a `LimitRange`, a
`ResourceQuota`, or a controller that would fight the change. Failure is
reported, not fatal — a seat that lands with a loud warning beats one that does
not land.

It also needs `pods/resize` `patch`, which the chart grants separately from the
rest.

## Getting the ssh stanza again

```
$ podbench ssh-config pod/web-... -n demo
$ podbench ssh-config pod/web-... -n demo --print-config
```

`ssh-config` regenerates the stanza for a seat that is already running, without
touching the pod. `--print-config` writes it to stdout instead of to
`~/.podbench/config.d/`, for piping somewhere else.

Useful flags:

* `--host-alias myseat` — the ssh `Host` name. Defaults to
  `podbench-<namespace>-<pod>`.
* `--ssh-user` — the login name. `root` on the full rung; on a degraded rung
  sshd resolves the name through NSS, so the image needs an account for that
  UID and you may need to say which.
* `--identity ~/.ssh/id_work` — the key to offer, and the one whose public half
  is injected into the container.

## Host keys and `known_hosts`

podbench mints a host key per attach and manages its own `known_hosts` at
`~/.podbench/known_hosts`, keyed on an alias derived from the **pod UID**. It
deliberately does not ship `StrictHostKeyChecking no`: a debugging tool that
teaches you to skip host verification has taught you something you will apply
elsewhere.

A consequence: a pod that restarts is a new pod UID and therefore a new host,
not a man-in-the-middle warning. A container that restarts *within* the same pod
gets a fresh rootfs and a fresh host key, and podbench replaces the entry on
re-attach.

To make host keys survive, deliver one from a Secret via
`PODBENCH_SSH_HOST_KEY_FILE` (default mount
`/etc/podbench/ssh/ssh_host_ed25519_key`).

## Seeing what is out there

```
$ podbench status pod/web-... -n demo    # every seat in one pod
$ podbench list -n demo                  # every seat in the namespace
```

`status` shows dead containers too, because their names remain burnt.

## Removing a seat

You cannot. An ephemeral container lives until its pod dies. Delete the pod (a
controller will replace it) or leave it — an idle podbench container is
`sleep`-cheap, but it still counts against the pod's ephemeral-storage budget
for whatever it has written.

## When it goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| `ssh_dispatch_run_fatal: ... Broken pipe`, `command terminated with exit code 255` | something closed or redirected sshd's stderr; closing fd 2 in a `kubectl exec`'d process tears down the whole CRI exec stream | do not hand-edit the generated `ProxyCommand`. `-i` and `-e` are both mandatory and `2>&1` breaks it |
| ssh hangs forever with no output | a *stalled* transport (apiserver or konnectivity hiccup) | the generated config sets `ServerAliveInterval 15`/`CountMax 3`, which fails in ~19 s instead. Do not remove them |
| `ControlPath too long ('...' >= 108 bytes)` | the control socket is not under `/tmp/podbench-cm` | keep the generated `ControlPath`; `sun_path` is 108 bytes |
| container status `CreateContainerConfigError`, `container's runAsUser breaks non-root policy` | the kubelet refused a root container *after* the API server accepted it | podbench pre-empts this by reading `runAsNonRoot` and skips the full rung; if you forced it, do not |
| attach lands but `blocker: yama-scope` | Yama's `ptrace_scope >= 1` on **that node** forbids attaching to non-descendants | `dbg --launch`, or have the target call `prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)` |
| every library reports `missing debugging information` | `ca-certificates` absent, so `libdebuginfod` fails the TLS handshake silently | use the published image; it is mandatory there for exactly this reason |
| attach works on one pod, is denied on the next | Yama differs **per node**, by kernel flavour, not by architecture | nothing to fix. The report prints the node name and Yama state for this reason |
| `pods "web-..." is forbidden: User cannot update resource "pods/ephemeralcontainers"` | your kubeconfig lacks a verb podbench needs, discovered mid-attach | `podbench doctor -n demo` asks for every verb up front and names the chart flag that grants it |

`podbench attach` returns `2` only for a real error. A degraded seat is
a success: returning non-zero for "the cluster would not grant `SYS_PTRACE`"
would make an honest report look like a failure.
