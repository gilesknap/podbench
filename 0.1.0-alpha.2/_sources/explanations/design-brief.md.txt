# Podbench

A debug-container image for Kubernetes that puts your editor, gdb, and a Python inner loop *inside* the pod — connected over nothing but your kubeconfig.

Working name — rename freely. This document is the handoff brief for implementation; the kickoff prompt is at the end.

## The idea

Kubernetes ephemeral containers (`kubectl debug --target=…`, stable since 1.25) can join any running container's PID namespace, and always share the pod's network namespace. Podbench is a single container image plus a small launcher that turns that primitive into a full development seat inside a pod:

- **Observe mode** — attach to a live pod, connect VS Code Remote-SSH through the Kubernetes API, and drive gdb against the workload's processes. Built for distroless targets that have no shell of their own.
- **Iterate mode** — mint a sacrificial copy of the pod with an idle main container, build an editable Python environment with uv inside the debug container, edit the checkout directly in VS Code (no laptop-to-cluster sync — the editor is already there), and relaunch on demand. This is the expected majority use case: small amounts of in-cluster dev for features that are hard to exercise in CI.
- **Patch mode** (final phase) — for emergency fixes that must outlive the session: a helm-enabled PVC mounted over the app's venv makes a patch survive container restarts and pod reschedules for the rest of an operational run (beamtime), then consolidates into a proper image rebuild at the next shutdown.

Tilt, Skaffold, Okteto and mirrord validate the demand; none of them offer this specific shape — no file sync, no traffic interception, just "the editor is inside the cluster." That's the differentiator to protect as the design evolves.

```
              laptop
     VS Code · Remote-SSH · kubectl
                 |
                 |  ssh over kubectl exec (API server only)
                 |
  +--------------+--------------------------------------+
  |  pod                                                 |
  |                                                      |
  |  +----------------------+  +----------------------+  |
  |  | app container        |  | podbench (ephemeral) |  |
  |  |                      |  |                      |  |
  |  | workload process     |  | sshd · vscode-server |  |
  |  |   (maybe distroless) |  | gdb · git · uv ·     |  |
  |  | fs visible at        |  |   debuginfod         |  |
  |  |   /proc/<pid>/root   |  |                      |  |
  |  +----------------------+  +----------------------+  |
  |                                                      |
  |  shared: PID namespace (via --target)                |
  |          network namespace (always)                  |
  +------------------------------------------------------+
```

## Mechanisms it stands on

Each of these is individually proven; the product is the packaging. They are also the load-bearing assumptions — Phase 0 exists to retire the risky ones before any real building.

| Mechanism | What it gives us |
|---|---|
| `kubectl debug --target` | Ephemeral container in the target's PID namespace; gdb can see and attach to workload processes. |
| `/proc/<pid>/root` | Full view of the target container's filesystem (rootfs + its volumes) without any shared mount. As root, transparently traversable. |
| `set sysroot /proc/<pid>/root` | gdb resolves the target's own shared libraries and loader, not the debug image's. |
| `sshd -i` over `kubectl exec` | SSH as a `ProxyCommand` through the API server — no pod IP exposure, no port-forward to babysit, auth is the kubeconfig. |
| Shared network namespace | A process relaunched from the debug container binds the same pod IP and port; Service traffic reaches it unchanged. |
| Editable installs (`uv pip install -e`) | Import-path redirection via a `.pth` finder — the running interpreter resolves imports to a live checkout. |
| `kubectl debug --copy-to` | A sacrificial clone of the pod with the app container's command replaced by an idle process and probes stripped — the safe arena for edit-relaunch. The launcher authors the copied spec itself, so the dev pod can also gain volumes, a true sidecar, and its own resource limits. |
| PVC mounted over the venv (helm-toggled) | Existing facility mechanism: patches persist across container restarts and pod reschedules. Mounted at the same path in the debug container, it is a genuinely shared filesystem — the durable substrate for Patch mode. |

```
# The headline connection path — ~/.ssh/config on the laptop:
Host mypod-debug
  ProxyCommand kubectl exec -i pod/foo -c podbench -- /usr/sbin/sshd -i -e
  User root
```

## Constraints that shape the design

- **ptrace needs granting.** gdb attach requires `SYS_PTRACE`; plain `kubectl debug` won't give it. Use `--profile=sysadmin` or a `--custom` profile adding only `SYS_PTRACE` (kubectl ≥ 1.28). The capability is outside both the *restricted* and *baseline* Pod Security Standards' allowed lists, so refusal is a mainstream scenario, not an edge case — which is why the degraded path below is a first-class mode, not an error state.
- **vscode-server dictates the base image.** It needs glibc ≥ 2.28 (no Alpine/musl), a writable `~/.vscode-server` with a few hundred MB of headroom, and egress to `update.code.visualstudio.com` on first connect. Base on debian-slim or ubi-minimal. Pre-baking the server is possible but version-locked to the client — treat as an optional later optimization, not the default.
- **Ephemeral containers are permanent.** They can't be removed or restarted; each launch appends to the pod spec until the pod dies. Design everything for *reconnection*: idempotent startup, sshd usable again on a second session, helpers that don't assume a fresh container.
- **No resource isolation on the live pod.** Ephemeral containers can't declare resources; vscode-server plus gdb symbol-loading can eat hundreds of MB inside the pod's existing memory limit and get the *workload* OOM-killed. This is the loudest warning the docs need — and the reason Observe mode has a strict weight budget (see "Storage, and staying agnostic").
- **Ephemeral-storage limits evict pods.** The debug container's writes — vscode-server, clones, caches — count toward the pod's ephemeral-storage accounting; exceed the limit (2 GB is a realistic facility default) and the kubelet evicts the whole pod, workload included.
- **Mount namespaces don't share paths.** A `.pth` written into the target's site-packages pointing at a checkout in the *debug* container's filesystem dangles — the target resolves paths in its own namespace. Interpreter, venv, and checkout must live on the same side. Podbench standardizes on: everything in the debug container.
- **Don't fight the kubelet.** If PID 1 exits, the container restarts with pristine image code; if it's SIGSTOPped, liveness probes kill it anyway, and a stopped process still holds its listening socket. Hence Iterate mode runs on a `--copy-to` clone where PID 1 is inert by construction, never by pausing the live pod. (Patch mode deliberately inverts this: once the venv persists on a PVC, the container restart *is* the relaunch mechanism.)
- **`readOnlyRootFilesystem` is common in prod.** Never depend on writing into the target container's filesystem; `/proc/<pid>/root` is a read path in the standard workflows.

## Without SYS_PTRACE

Some clusters will never grant the capability. That costs exactly one feature — gdb *attach* to the live process — and even that only partially. Everything else stands:

- **The seat is untouched.** ssh over `kubectl exec`, VS Code, git, uv — none of it needs any capability.
- **Iterate mode is untouched.** Relaunched processes are the debug container's own children, and debugging your own descendants is always permitted — Yama and the capability checks both exempt them. debugpy in the dev pod never needed ptrace.
- **Run-under-gdb survives.** In the `--copy-to` pod, launch the workload binary *under* gdb instead of attaching. Parent-child ptrace needs no capability. You lose "inspect the live misbehaving process"; you keep "reproduce under a debugger with full source-level control."
- **In-process debug servers are the ptrace-free live-attach.** debugpy (Python), Node's inspector, JDWP: the app listens, VS Code attaches over localhost through the shared network namespace and the ssh tunnel. For Python images the docs should recommend an opt-in debugpy listener guarded by an env var — done that way, the Python live-attach story never touches ptrace at all.

**The same-UID fallback.** The kernel's ptrace permission is "matching UID (real, effective, and saved) *or* `CAP_SYS_PTRACE`," with Yama's `ptrace_scope` layered on top. Launching the ephemeral container with `runAsUser` equal to the target's UID — no capability added, so admission under restricted PSS allows it — buys back a lot:

- All the *read* paths work: `/proc/<pid>/root` (sysroot, target file browsing), `maps`, `environ`, `fd`. Reads pass the credential check and are exempt from Yama.
- Attach then depends on the node's global `/proc/sys/kernel/yama/ptrace_scope`: `0` → same-UID attach works with no capability at all; `≥1` (Ubuntu's default) → attach is denied for non-descendants. It's host-global, node-OS-dependent, and readable from inside the container — so it can be detected and reported instead of discovered by failure.

**Diagnose, don't mystify.** Four different mechanisms deny attach with the same `EPERM`: a missing capability, Yama scope, a seccomp profile that filters `ptrace`, or AppArmor on Ubuntu-based nodes. Field report from a previous hand-rolled attempt at exactly this tool: same-UID was reached and attach still failed, with no indication of which layer said no — that's the failure mode to design out. The launcher and the `dbg` helper must probe (read yama scope, check the capability sets in `/proc/self/status`, attempt a scratch attach) and *name the blocker*: "denied by Yama (ptrace_scope=1)" is actionable; "ptrace: Operation not permitted" is a wasted afternoon.

**Core dumps** are the remaining capability-free route to gdb on the live workload's state (gdb on a core file needs no ptrace), but harvesting the dump is node-dependent — `core_pattern` is host-global and often piped to systemd-coredump on the host. Spike-worthy, not load-bearing.

**The organizational escape hatch.** PSS exemptions and policy engines (Kyverno, OPA/Gatekeeper) can express "*this pinned image*, as an *ephemeral container only*, with *only* SYS_PTRACE, launched by *these users*." A published, minimal, pinned Podbench image is precisely what makes that policy writable — a far easier ask of a security team than "privileged," and an argument *for* the project.

**Net design rule:** the launcher walks a capability ladder — full (`SYS_PTRACE`) → same-UID no-cap → seat-only — falling down it automatically on admission refusal, and prints an honest capability report for the rung it landed on.

## Storage, and staying agnostic

Design principle: **the target needs nothing.** Observe and Iterate modes must work against any pod, from any chart, completely unmodified — only Patch mode may ask for deploy-time cooperation. Where each mode's disk comes from follows from that principle:

- **The debug container's own writable layer is the default.** Ephemeral containers can't mount new volumes, but their rootfs is writable and node-backed — vscode-server already lives there. The catch is the accounting above: on the live pod, every byte competes with the workload's ephemeral-storage budget.
- **Observe mode is a cockpit, not a workshop.** No full clone on the live pod: gdb fetches both symbols *and sources* over debuginfod, VS Code reads target files through the sysroot, extensions stay minimal. Soft budget: fit comfortably inside ~1 GB so a 2 GB-limit pod is never at eviction risk. Anything heavier than looking belongs in a dev pod.
- **Iterate mode escapes every limit, because the dev pod is authored.** `--copy-to` is only a convenience; the launcher builds the pod spec itself and may change anything: podbench as a true sidecar (not an ephemeral container) with its own memory and ephemeral-storage requests, plus a workspace volume. The clone, venv, uv cache, and vscode-server all live here — the OOM and eviction footguns do not apply to this mode.
- **The scratch PVC is podbench-side scaffolding, never a target-side requirement.** `podbench init` (optional, once per namespace) provisions a per-user or per-team PVC; `podbench dev` mounts it so workspaces survive dev-pod teardown and reattach takes seconds. Without it, `emptyDir` works with zero setup. RWO is fine — one dev pod mounts it — at the cost of pinning that pod to the volume's node.
- **Patch mode is the one licensed exception, and unavoidably so.** Durable-across-restart code must sit on a volume present in the pod spec at creation: pod volumes are immutable, and a container's rootfs is reset on every restart. The chart-untouching alternatives are strictly worse — a mutating admission webhook is a cluster component to own; launcher-recreated pods fight the controller and helm. The helm parameter is the right mechanism.

## The image

Debian-slim (bookworm) base, multi-arch (amd64 + arm64). Contents:

- **Connection:** openssh-server (host keys generated at first start; authorized key injected via env var or mounted Secret), tini or equivalent as an idle, restart-tolerant PID 1 (`sleep infinity` is acceptable for v1).
- **Debugging:** gdb, gdbserver, binutils, procps, lsof, strace, elfutils/debuginfod-client (honor `DEBUGINFOD_URLS`).
- **Iteration:** git, uv, curl, rsync; CPython comes via `uv python install` on demand rather than baked-in version sprawl.
- **Helpers on PATH:** `pids` (list target processes), `dbg <pid>` (gdb with sysroot preset to `/proc/<pid>/root`), `capreport` (probe ptrace permissions and name the blocker), `dev-bootstrap` (clone + uv sync + editable install), `run`/`stop` (relaunch loop).

## Phased plan

Phases are ordered by risk, not by component: nothing gets built on an assumption that hasn't survived a spike. Each phase ends in a commit (or several) with its acceptance criteria demonstrably met.

### Phase 0 — Retire the risks (spikes, against a kind cluster)

Four throwaway experiments, each written up as a short note in `docs/spikes/` — findings, exact commands, and any deviation from this brief's assumptions. Use a scratch Dockerfile; nothing here is the real image.

- **S1 — ssh transport:** `sshd -i -e` over `kubectl exec` as ProxyCommand. Stdio framing must be clean (`-e` keeps logs off stdout). Pass: VS Code Remote-SSH connects and opens a terminal.
- **S2 — vscode-server in an ephemeral container:** server downloads, extensions install, survives disconnect/reconnect. Pass: C/C++ extension installed and functional.
- **S3 — gdb attach:** launch with a `--custom` profile adding `SYS_PTRACE` against a distroless C target; `set sysroot /proc/<pid>/root`; hit a breakpoint with resolved symbols. Pass: backtrace with source lines.
- **S4 — Python takeover:** `--copy-to` clone with idle PID 1; uv editable env in the debug container; app relaunched from there binds the pod port; a code edit is visible through the Service. Pass: `curl` via the Service shows the edited response.
- **S5 — no-cap fallback:** repeat S3 with no added capability and `runAsUser` matching the target. Confirm `/proc/<pid>/root` and `maps` reads work; record attach behavior with the kind node's `yama/ptrace_scope` toggled between 0 and 1; verify the probe names the actual blocker in each configuration. Pass: reads work in both; the printed diagnosis is correct in both.

Gate

Do not start Phase 1 until all five pass or the brief is amended with what was learned. S1 and S2 are the highest-uncertainty items.

### Phase 1 — The image, for real

Containerfile + entrypoint implementing the contents list above. Entrypoint must be idempotent (safe on reconnect and on a second `kubectl debug` into the same pod), generate host keys once, and read the authorized key from `PODBENCH_SSH_PUBKEY` or a mounted Secret. Local build docs; no registry yet.

Accept

From a clean kind cluster: one `kubectl debug` command + one ssh config stanza → VS Code connected, `dbg` attached to a target process. Image builds on amd64 and arm64; keep it under ~700 MB uncompressed.

### Phase 2 — Launcher UX

A kubectl plugin, `kubectl-podbench` (plain bash for v1), that wraps the ceremony: `kubectl podbench attach pod/foo --target app` launches the ephemeral container, waits for readiness, and prints — or writes to an `Include`-able file — the ready-made ssh config stanza. `attach` on a pod that already has a podbench container reconnects instead of appending a new one.

The launcher walks the capability ladder automatically: try the minimal custom profile (SYS_PTRACE, run-as-root); on admission refusal, relaunch same-UID/no-cap (reading the target's `runAsUser` from the pod spec); if that too is refused, seat-only. Once attached it runs `capreport` and prints what this pod supports — live attach / read-only inspect / iterate — and, for whatever is missing, which mechanism blocked it (admission policy, Yama, seccomp, AppArmor).

Accept

Cold start to "VS Code connected" is one command plus one click, on a pod the user has never touched. Re-running the command against the same pod is safe. On a restricted-PSS namespace the same command still lands a working seat and prints an honest capability report rather than an error.

### Phase 3 — Debug workflow polish

Make the gdb path first-class: `pids`/`dbg` helpers hardened; debuginfod wired through (`DEBUGINFOD_URLS` passthrough from the launcher — sources as well as symbols come over it, keeping full clones off live pods); documented `launch.json` attach templates with `sourceFileMap` examples for C/C++ (gdb) and Rust (CodeLLDB); a walkthrough doc: distroless target → breakpoint → edit source mapping → step.

Accept

Following the walkthrough verbatim on the demo app lands a breakpoint with source shown in VS Code, symbols via debuginfod, in under ten minutes.

### Phase 4 — Iterate mode (Python)

`kubectl podbench dev pod/foo`: authors a sacrificial dev pod from the target's spec — `--copy-to` semantics, but built by the launcher so nothing is off-limits: app container idled, probes stripped, podbench added as a true sidecar with its own memory and ephemeral-storage requests, and a workspace volume mounted (`emptyDir` by default; the `podbench init` scratch PVC when present). Then `dev-bootstrap` — git clone (URL/ref flags), `uv sync` from the app's lockfile, `uv pip install -e .` — and `run`/`stop` to drive the relaunch loop. Document the `PYTHONPATH`-shadowing alternative and optional `watchfiles` auto-reload. Also document the ptrace-free live-attach pattern for Python: an opt-in debugpy listener baked into the app image, attached from VS Code over localhost through the tunnel. `kubectl podbench dev --delete` cleans up the dev pod.

Accept

End-to-end demo: dev-pod minted → edit in VS Code → `run` → change visible through the Service; original pod untouched throughout. Teardown leaves nothing behind.

### Phase 5 — Ship it

Publish multi-arch to ghcr.io on tag; GitHub Actions CI with a kind-based e2e job that runs S1–S4 as regression tests; README covering install, both modes, and the security/footgun section (Pod Security Standards requirement, the OOM warning, ephemeral-container permanence) prominently — these caveats are documentation, not fine print.

Accept

A stranger with a kind cluster can go from README to a connected VS Code session using only published artifacts.

### Phase 6 — Patch mode — durable in-place fixes

Deliberately last: it builds on everything before it and on an existing facility mechanism — a helm parameter that mounts a PVC over the app container's venv path. The operational story: an emergency fix goes in mid-run when a full release cycle is too expensive, survives every restart and reschedule until the next shutdown, and is then consolidated into a proper image rebuild and the PVC retired.

The PVC changes the physics of the earlier phases, in Podbench's favor:

- **The mount-namespace gotcha dissolves.** The ephemeral container mounts the same PVC (ephemeral containers may mount existing pod volumes, via the `--custom` profile) at the *identical* mountPath. With venv and checkout both on the PVC, editable-install paths resolve correctly in both containers — a genuinely shared filesystem at last.
- **Restart becomes the relaunch.** With the venv persistent, killing PID 1 — or simply deleting the pod — is the clean way to pick up a patch. The kubelet is now doing the work instead of fighting it, probes and all.

Podbench's contribution is the workflow around the mount:

- `podbench patch init` — seed an empty PVC from the image's own venv (or verify the helm chart's initContainer did), clone the source onto it, editable-install.
- `podbench patch apply` — commit the change on the PVC checkout, reinstall if metadata changed, bounce the pod.
- **Provenance is non-negotiable.** Every patch is a git commit; a manifest on the PVC records base image digest, interpreter version, commit sha, author, and timestamp; the pod gets an annotation marking it patched.
- `podbench patch status` — list every patched pod in a namespace with its drift (commits ahead of the released image). Silently-diverged pods are the operational risk this mode must never create.
- `podbench patch consolidate` — push the PVC checkout as a branch/PR at shutdown, ready for the rebuild; after the new image rolls out, flip the helm flag off and retire the PVC.

Risks to document: the PVC venv *shadows* the image's — an image upgrade under a live patch mount runs the old venv, and an interpreter version bump breaks it (the manifest records the interpreter so `status` can warn); multi-replica deployments need RWX or a per-replica story — v1 scopes to single-replica; a stale PVC left mounted after consolidation silently reverts the fix's provenance.

Accept

On the demo app: patch applied mid-"run", pod deleted and rescheduled, patch still live; `status` names the patched pod and shows the exact diff; `consolidate` yields a branch that rebuilds cleanly, and dropping the helm flag returns the pristine image.

Biggest footgun

On a *live* pod, Podbench shares the workload's memory and ephemeral-storage limits and cannot reserve its own — the debugger's presence can get the workload OOM-killed or the pod evicted. Iterate mode is immune (the authored dev pod gives podbench its own limits); Observe mode manages it by staying slim. Every user-facing surface — README, launcher output — should say this once, clearly.

## Repository layout

```
podbench/
  Containerfile
  entrypoint.sh
  bin/                 # in-image helpers: pids, dbg, dev-bootstrap, run, stop
  plugin/
    kubectl-podbench   # launcher (bash)
  docs/
    spikes/            # Phase 0 findings
    walkthrough-gdb.md
    walkthrough-dev.md
  test/
    e2e/               # kind-based, mirrors the spikes
    apps/              # demo targets: distroless C app, Python service
  .github/workflows/
```

## Kickoff prompt for Claude Code

Paste something like this to start the work on the workstation:

```
Read podbench.html (or the copy of this brief in the repo) in full.

Start with Phase 0 only. Create a kind cluster, run spikes S1–S4 in
order, and write each up in docs/spikes/ with the exact commands used
and whether the brief's assumption held. One commit per spike.

Stop after Phase 0 and report: which spikes passed, what deviated
from the brief, and what you'd amend before Phase 1. Do not begin
Phase 1 without that checkpoint.

Constraints: don't fight the kubelet (no pausing live PID 1), never
assume the target's filesystem is writable, and keep the ssh-over-
kubectl-exec path as the primary transport unless a spike disproves
it.
```

After the Phase 0 checkpoint, subsequent phases can be handed over one at a time with "proceed to Phase N per the brief" — the acceptance criteria above are the definition of done for each.


Design brief & implementation plan
