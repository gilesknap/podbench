# What `attach` does

Observe mode. `podbench attach` adds an ephemeral container to a pod that is already
running, measures what that container is actually allowed to do, and writes you an ssh
stanza that reaches it through `kubectl exec`. The workload is not restarted, not
cloned and not modified.

This page is the mechanism: every check, in the order it happens, and every `kubectl`
command it turns into. It assumes you know Kubernetes and VS Code and nothing about
this codebase.

## The whole flow

```text
podbench attach [POD] [--target NAME] [--new] [--resize 6Gi] [--resize-cpu 4]
                [--mount CLAIM:PATH]
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ LOCAL — nothing has touched the cluster yet                      │
│                                                                  │
│   namespace : -n, else the kubeconfig context's own              │
│   ssh key   : ~/.ssh/id_ed25519.pub is *read*, never generated   │
│              (checked first: a missing key refuses the attach    │
│               whichever pod you would have picked)               │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ no .pub file ───────────────▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ WHICH POD?                                                       │
│                                                                  │
│   exact name typed → get pod NAME -o name    (one cheap call;    │
│                      never lists, so `get` without `list` RBAC   │
│                      keeps working)                              │
│   substring / none → get pods -o json, then match                │
│   exactly 1 match  → used, and echoed to stderr                  │
│   more than 1      → prompt if stdin is a tty, else refuse       │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ 0 matches ──────────────────▶ exit 2
                                  ├─ >1 and no tty (or --no-prompt) ▶ exit 2
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ --resize 6Gi / --resize-cpu 4 ?   opt-in, and never fatal        │
│                                                                  │
│   get pod POD -o json          → name the workload container     │
│   get limitranges -o json      → maxLimitRequestRatio, max       │
│   patch pod POD --subresource resize --type strategic            │
│                                                                  │
│   Requests move with limits: a ratio cap bounds limit/request,   │
│   so raising a limit alone only ever widens it (96 against a     │
│   cap of 10, measured at Diamond).  Never to equal its limit,    │
│   which would change the pod's QoS class and be refused.         │
│                                                                  │
│   Before the seat, not after: vscode-server starts allocating    │
│   into a limit podbench cannot reserve.  Success and failure     │
│   are both reported loudly — the raised limits live on the pod,  │
│   so any rollout regenerates them away silently.                 │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ READ THE POD          get pod POD -o json                        │
│                                                                  │
│   workload container = --target, else spec.containers[0]         │
│   --mount CLAIM:PATH → resolved against spec.volumes only        │
│   podbench-home volume declared? → mounted by convention         │
│   podbench-identity declared?    → *never* mounted here          │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ --mount names no declared volume ▶ exit 2
                                  ├─ the app mounts it with subPath ─▶ exit 2
                                  ▼
                   ┌────────────────────────────────┐
                   │ is a podbench-N container      │
                   │ already *running* in this pod? │
                   └────────┬──────────────────┬────┘
                        yes │                  │ no, or --new
                            ▼                  ▼
                   ┌─────────────────┐  ┌───────────────────────┐
                   │ RECONNECT       │  │ WALK THE LADDER       │
                   │ no cluster      │  │ (next diagram)        │
                   │ writes at all;  │  │ appends one ephemeral │
                   │ rung/uid/$HOME  │  │ container to the pod  │
                   │ read back from  │  │ spec, permanently     │
                   │ the spec        │  │                       │
                   └────────┬────────┘  └───────────┬───────────┘
                            └─────────┬─────────────┘
                                      ▼
┌──────────────────────────────────────────────────────────────────┐
│ MEASURE THE SEAT — inside the container, on that node            │
│                                                                  │
│   exec -c SEAT -- podbench agent --print-login-user              │
│        can sshd resolve a login name for the uid it runs as?     │
│   exec -c SEAT -- podbench capreport --json (unless --no-probe)  │
│        which of ptrace's four blockers is saying no?             │
│                                                                  │
│   Nothing here is inferred from the spec that was submitted.     │
└─────────────────────────────────┬────────────────────────────────┘
                                  ▼
                      print the capability report
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ WIRE THE CLIENT                                                  │
│                                                                  │
│   get pod POD -o json    → metadata.uid, for the HostKeyAlias    │
│   exec -c SEAT -- podbench agent --print-host-key --no-self-check│
│   write ~/.podbench/known_hosts                                  │
│   write ~/.podbench/config.d/<ns>-<pod>.conf                     │
└─────────────────────────────────┬────────────────────────────────┘
                                  ├─ seat has no NSS login ──▶ print why, no stanza
                                  ▼
        ssh podbench-<ns>-<pod>   ·   Remote-SSH: Connect to Host      exit 0
                                  │
                                  ▼   (only with --open)
┌──────────────────────────────────────────────────────────────────┐
│ DRIVE THE CLIENT                                                 │
│                                                                  │
│   exec -c SEAT -- podbench debug-config --print-config           │
│        one assessment; its adapter types name the extensions     │
│   write <home>/.vscode/settings.json   ← the /proc excludes,     │
│        BEFORE the window: the walk starts when it opens          │
│   write <home>/.vscode/launch.json, extensions.json              │
│   code --remote ssh-remote+<alias> --install-extension …         │
│        the "Install in SSH:" button, and only this flavour's     │
│   code --remote ssh-remote+<alias> <home>       never /          │
└──────────────────────────────────────────────────────────────────┘
```

A degraded seat is still `exit 0`. Returning non-zero because the cluster would not
grant `SYS_PTRACE` would make an honest report look like a failure.

## The capability ladder

Three rungs, and the shape is forced rather than chosen: `SYS_PTRACE` on a container
whose `runAsUser` is not 0 lands in the bounding set only, leaving `CapEff: 0` — so
there is no useful middle rung to invent, and `spec.py` raises rather than author one.

The **order** is the target's to imply. Where the target's uid is known and is not
root, rung 2 already matches it, which is what the kernel's credential check wants,
and it is tried first: the capability rung is not spent proving what the uid already
says, and a root seat whose capability a policy strips reads *fewer* of the target's
`/proc` files than rung 2 does (report 3.11). A root target, a target whose uid the
pod spec does not carry, and a pod sharing one PID namespace between containers of
different uids keep the classic order, rung 1 first — for them rung 2 cannot be
authored, or cannot reach what the user came for. `--max-rung` states the starting
rung explicitly and overrides all of it; it is also the only way to insist on rung 1
for a node whose Yama `ptrace_scope` is 1 or more, which is per-node and cannot be
read before a seat exists.

```text
                 ┌───────────────────────────────────────┐
                 │ name = next free podbench-N           │
                 │ (a used name is burnt for the pod's   │
                 │  lifetime — they are never reused)    │
                 └──────────────────┬────────────────────┘
                                    ▼
┌──────────────────────────────────────────────────────────────────┐
│ rung 1 · FULL      runAsUser: 0 + capabilities.add: [SYS_PTRACE] │
│                    → live attach: gdb -p <pid> on the workload   │
└─────────────────────────────────┬────────────────────────────────┘
                                  │
      pre-skipped, no API call, no name burnt:
        the pod or the container sets runAsNonRoot: true
        or --max-rung named a lower rung as the ceiling
      withdrawn at the dry run, no name burnt:
        admission would strip SYS_PTRACE, or add runAsNonRoot: true
      refused synchronously, in kubectl's stderr:
        PSA — 'must not include "SYS_PTRACE" in ...capabilities.add'
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ rung 2 · DEGRADED  runAsUser = the target's own uid,             │
│                    drop ALL, runAsNonRoot: true                  │
│                    → read-only: /proc/<pid>/root, maps, environ  │
└─────────────────────────────────┬────────────────────────────────┘
                                  │
      pre-skipped:
        the target's uid is not in the pod spec — re-run with
        --target-uid once the report below has read it from /proc
        (guessing root would cost this rung its entire value)
        or the target runs as uid 0, which runAsNonRoot cannot express
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────┐
│ rung 3 · SEAT      whatever the namespace will admit             │
│                    → editor, shell, git; no view of the target   │
└──────────────────────────────────────────────────────────────────┘
                                  │
                  all three refused → exit 2, listing each reason
```

Refusal arrives through two unrelated channels, and only one of them is catchable
around the API call:

```text
  replace --raw .../ephemeralcontainers?dryRun=All      the rehearsal
        │        admission runs, nothing is stored, no name is spent
        │        → a refusal here ends the rung; a rewrite here is read
        │          out of the response body, which is the only place a
        │          mutating policy is visible at all
        ▼
  replace --raw .../ephemeralcontainers
        │
        ├── non-zero exit, PSA text in stderr ──── synchronous refusal
        │        the name was never taken; try the next rung with it
        │
        └── exit 0 ──▶ poll get pod POD -o json every 0.5 s
                 │
                 ├── status.…ephemeralContainerStatuses[N].state.running
                 │        → started; this is the only readiness signal
                 │
                 ├── state.waiting.reason == CreateContainerConfigError
                 │        → the *kubelet* refused, seconds later.
                 │          The name is burnt; the next rung takes a new one
                 │
                 └── state.terminated → same, name burnt
```

### The third channel, which is not a refusal

A **mutating** admission policy neither refuses synchronously nor fails at the
kubelet. It admits the container and rewrites its `securityContext` on the way
through — and since mutating admission runs before validating admission, nothing
downstream is left with a `SYS_PTRACE` to object to. What lands is a root
container with no capability, which is worse than rung 2: root that cannot
ptrace cannot read `/proc/<pid>/root`, `maps` or `environ` either.

Neither arm of the walk sees anything, so rung 1 "succeeds" and rungs 2 and 3 are
never tried. The spec reads back afterwards as `runAsUser: 0` with nothing added,
which is indistinguishable from rung 2 (issue #94) — so no rung read off a spec
is an honest answer about such a seat. The rung `attach` prints is not read off
one: every attach ends by asking the seat for its own `/proc/self/status`, and
the uid and `CapEff` in it are what the line names.

So the rung is rehearsed first. Every rung goes through `?dryRun=All` before it is
created: the API server runs the whole admission chain, returns the object as it
*would* have stored it, and stores nothing. A strip is then visible in the response
body, and rung 1 is withdrawn rather than spent — landing it would be worse than not
landing it. The same read catches the more expensive rewrite, a `runAsNonRoot: true`
added beside `runAsUser: 0`, which the API server takes and the kubelet then refuses
seconds later with a container name already gone.

A dry run is the admission chain and nothing else, so it never sees that kubelet
refusal itself; that one is still pre-empted by reading the target's `runAsNonRoot`
up front rather than provoked. And a rewrite that costs the rung nothing — the
thirteen capabilities a DLS policy adds to a container that asked for none, measured
2026-08-19 — is reported as one line rather than acted on.

`--max-rung` remains the way to state a cap up front, and is what somebody who knows
their own cluster reaches for. Measured at DLS, 2026-08-18.

## Every cluster call, in order

```text
 1  kubectl [--context C] config view --minify -o jsonpath={..namespace}
                                                        # only when -n is absent
 2  kubectl -n NS get pod POD -o name                    # exact-name fast path
 2' kubectl -n NS get pods -o json                       # substring, listing, prompt
 3  kubectl -n NS get pod POD -o json                    # --resize only
 4  kubectl -n NS patch pod POD --type=strategic \
        -p '{"spec":{"containers":[{"name":C,"resources":…}]}}' \
        --subresource=resize                             # --resize only
 5  kubectl -n NS get pod POD -o json                    # the pod attach works from
 --- only when a new seat is landed, once per rung attempted: ---
 6  kubectl -n NS get pod POD --subresource=ephemeralcontainers -o json
 7  kubectl -n NS replace --raw \
        /api/v1/namespaces/NS/pods/POD/ephemeralcontainers?dryRun=All -f -
                                                        # the rehearsal: admission
                                                        # runs, nothing is stored
 6' kubectl -n NS get pod POD --subresource=ephemeralcontainers -o json
 7' kubectl -n NS replace --raw \
        /api/v1/namespaces/NS/pods/POD/ephemeralcontainers -f -
 8  kubectl -n NS get pod POD -o json                    # polled until running
 --- always: ---
 8' kubectl -n NS exec -c SEAT POD -- cat /proc/self/status
                                                        # the measured rung: the
                                                        # uid and CapEff the
                                                        # kernel gave the seat
 8" kubectl -n NS exec -c SEAT POD -- podbench --version # which build answered
 9  kubectl -n NS exec -c SEAT POD -- podbench agent --print-login-user
10  kubectl -n NS exec -c SEAT POD -- podbench capreport --json
                                                        # unless --no-probe
11  kubectl -n NS get pod POD -o json                    # metadata.uid
12  kubectl -n NS exec -c SEAT POD -- podbench agent --print-host-key --no-self-check
```

The RBAC that adds up to — `rbac.observe` in the chart — is `get`/`list`/`watch` on
`pods`, `get`/`patch`/`update` on `pods/ephemeralcontainers` (`update` is the one that
matters: the container is added by PUTting the subresource), and `create` on
`pods/exec`. `--resize` needs `patch` on `pods/resize`, granted separately because it
changes a running workload's limits.

Note what is **not** there: no `kubectl debug`. It merges its chosen profile *after*
your `--custom` JSON, so asking for `runAsUser: 1000` yields a container that also
carries `SYS_PTRACE` — precisely the combination that is invalid by construction. The
subresource takes the spec verbatim, so the launcher posts to it directly.

## The checks, and what each one prevents

| Check | Failure it prevents |
|---|---|
| public key read before the pod is chosen | picking a pod, then being told the attach was never possible |
| `--mount` resolved against `spec.volumes` | an API-server error naming a volume podbench invented — pod volumes are immutable, so an attach can never *add* one |
| application mount uses `subPath` → refuse | a seat that silently resolves a different tree at the same path (an ephemeral container may not carry `subPath`) |
| `runAsNonRoot: true` read up front | the kubelet accepting-then-refusing a root container seconds later, burning the name |
| target uid absent → skip the degraded rung | a root seat that quietly loses the sysroot, maps, environ and exe reads that rung exists for |
| `SYS_PTRACE` beside a non-zero `runAsUser` → raise | a container that looks privileged and behaves unprivileged (`CapEff: 0`, bare `EPERM` on every ptrace) |
| readiness is `state.running.startedAt` only | treating the API server's acceptance as the node's |
| `podbench capreport` runs in the container, on the node | reporting what was *requested* rather than what was granted; Yama differs per node by kernel flavour |
| login name measured, not derived | writing a stanza for a login sshd refuses before it looks at a key |

## What the seat is, once it is up

The container's command is `podbench agent`, never `sleep infinity` — a debug
container with a short-lived command reaches `Completed` and burns its name, and the
transport's server-side files do not exist until the agent has written them. The
agent's start-up is a sequence of *ensure* steps, because a restart yields a
completely fresh rootfs and nothing may live only in the writable layer:

```text
  podbench agent (PID 1)
        │
        ├─ ensure $HOME                (/root, or the mounted home volume,
        │                               or /tmp/podbench-home for a non-root seat)
        ├─ ensure /run/sshd            (root layout only — sshd's privsep dir)
        ├─ ensure an NSS record for its own uid
        │      before the host key, because ssh-keygen calls getpwuid()
        │      whatever it is asked to do, and fails on a uid NSS cannot
        │      resolve.  It goes to /var/lib/extrausers/passwd, which the
        │      image ships world-writable, so a seat running as the
        │      target's uid *and gid* can append it with no flag and no
        │      privilege.  That database ignores a uid or gid below 500;
        │      the image pre-seeds /etc/passwd with a static record for
        │      every free uid under 500, so those seats resolve without
        │      writing anything at all
        ├─ ensure the host key         (ssh-keygen)
        ├─ ensure authorized_keys      (from the PODBENCH_PUBKEY env the spec carried)
        ├─ ensure the sshd config      (its own file, not the distro's)
        ├─ ensure VS Code machine settings
        │
        └─ idle as PID 1, reaping orphans
```

No step is fatal. PID 1 of a container that cannot be restarted must not exit while
explaining why it has no ssh — half the seat is reachable by `kubectl exec` regardless.

## The transport

There is no listening socket in the pod, no port-forward and no pod IP. The stanza
podbench writes carries a `ProxyCommand` that is a `kubectl exec` running sshd in
inetd mode:

```text
  your machine                                  the pod
  ────────────                                  ───────

   ssh podbench-<ns>-<pod>
        │
        │  ssh reads ~/.podbench/config.d/<ns>-<pod>.conf and runs the
        │  ProxyCommand podbench generated into it:
        │
        └──▶ kubectl -n NS exec -i POD -c podbench-1 -- \
               /usr/sbin/sshd -i -e -f <config> -o LogLevel=ERROR
                  │
                  │  that exec's stdin and stdout ARE the ssh connection
                  ▼
             ┌────────────────────────────────────────────────────┐
             │ podbench-1   (the seat)                            │
             │                                                    │
             │   sshd -i     inetd mode — no listening socket,    │
             │               no port-forward, no pod IP           │
             │   shares the target's PID and network namespaces   │
             │   /proc/<pid>/root → the application's whole rootfs│
             └────────────────────────────────────────────────────┘

   outer auth = the kubeconfig (contexts, exec credential plugins)
   inner auth = your ssh key, authorised in the container by the agent
```

Three flags are not negotiable, and each fails misleadingly rather than loudly:

* `-i` — inetd mode, which is what makes the exec channel itself the transport.
* `-e` — this is about **keeping fd 2 open**, not about logging. Without it sshd
  points fd 2 at `/dev/null`, the CRI stderr pipe hits EOF, and containerd tears down
  the whole exec session mid-key-exchange. You see `ssh_dispatch_run_fatal: … Broken
  pipe` and go looking for a network fault.
* `-o LogLevel=ERROR` — zero stderr bytes on a healthy connection, without closing
  the descriptor.

And never `-t`: from a script `kubectl` silently degrades to non-tty and looks fine,
but with a real TTY forced onto the ProxyCommand the ssh client hangs forever.

The `HostKeyAlias` is keyed on the pod's UID, so a re-created pod shows up as a new
host rather than as a man-in-the-middle warning.

## What you get back

The report is six lines of *measured* capability, not of requested capability:

* **live attach** — `gdb -p <pid>`, qualified by the probe deadline this pod puts on a
  breakpoint (a stopped process stops answering probes, and the kubelet cannot tell
  that from a hang);
* **read-only inspect** — `/proc/<pid>/root`, maps, environ; ticked from those three
  reads themselves and never from the verdict, with the matrix printed under it, and
  all three have to land. The other three reads capreport takes — `cmdline`, `status`,
  `fd` — need no permission and so are reported but never counted (issue #51). `exe`
  takes the same permission as the first three but is not in the matrix, so it does
  not decide anything either;
* **debug launched processes** — `podbench dbg --launch ./prog`, from the scratch
  attach on the probe's own forked child. It is the rung that survives when the reads
  do not, and it is measured rather than assumed: a seccomp filter that rejects
  `ptrace` takes it away along with everything else;
* **iterate** — always unavailable here, naming `podbench dev` as the way to it;
* **ssh seat** and **exec seat**, reported separately, because the ssh half needs an
  NSS identity the exec half does not.

When ptrace is denied, the report names *which* of the four mechanisms said no —
missing capability, Yama's `ptrace_scope`, seccomp or the node's LSM — because
all four
return the same `EPERM`, and that naming is the point of the whole probe.

Then, inside the seat, `podbench debug-config` writes a `.vscode/launch.json`
whose pid, sysroot-prefixed program path and setup ordering are things the
launcher knows and a human cannot guess. `attach --open` runs that same command
over `kubectl exec` and follows it with the two steps nobody should have to get
right by hand: which folder to open — a verb that picks the folder is a verb
that cannot pick `/` — and installing the extension in the *remote* window,
where the debug adapter has to run for any `/proc/<pid>/root` path to mean
anything.

## See also

* [Glossary](../reference/glossary.md) — PSA, Yama, the ambient set, `subPath` and every
  other term used here without explanation.
* [Ways in](ways-in.md) — why you would pick this mode over the other two.
* [Architecture](architecture.md) — why each mechanism has the shape it has.
* [Phase 0 gate report](spikes/phase0-report.md) — the measurements behind every
  "silently" on this page.
* [Attach to a pod](../how-to/attach-to-a-pod.md) — the same thing as instructions.
