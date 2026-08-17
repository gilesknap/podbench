# Glossary

Every term the rest of these docs uses without stopping to explain it, grouped by
where it comes from and sorted within each group. Definitions say what the thing is
first and where it bites podbench second — several of these are ordinary Kubernetes or
Linux vocabulary that happens to decide something load-bearing here.

## Podbench's own words

```{glossary}
:sorted:

agent
  The process podbench runs as PID 1 inside a debug container — `podbench agent`. It
  writes the {term}`sshd` config, the host key and the `authorized_keys` at start-up,
  then idles and reaps orphans. Every one of its start-up steps is an *ensure* rather
  than a *create*, because a restarted container has a completely fresh filesystem.

burnt name
  A container name that can never be used again for the life of a pod. An
  {term}`ephemeral container` cannot be removed, restarted or edited, so once one has
  been created — even if it immediately died, or was rejected by the
  {term}`kubelet` — its name is spent. Podbench therefore allocates `podbench-1`,
  `podbench-2`, … and a failed {term}`rung` takes a *fresh* name rather than retrying
  its own.

capability ladder
  The ordered list of {term}`rung`s `attach` tries. It exists because a cluster that
  refuses the privileged option should still get a working editor rather than an
  error.

capreport
  The probe that runs *inside* the landed seat, on that node, and reports what
  debugging is actually possible — and, when it is not, which of the four
  {term}`ptrace` {term}`blocker`s said no. Its exit code is its verdict: 0 live
  attach, 10 read-only, 15 {term}`launch-only`, 20 nothing. Everything `attach` prints
  about capability comes from here, never from the spec that was submitted.

blocker
  The named mechanism that denied {term}`ptrace`. Four unrelated subsystems refuse
  with the same {term}`EPERM` — a missing {term}`CAP_SYS_PTRACE`, {term}`Yama`,
  {term}`seccomp` and {term}`AppArmor` — so naming which one is the entire point of
  {term}`capreport`.

dev pod
  Iterate mode's sacrificial clone of a running pod: same image, same volumes, same
  labels if you asked for them, but with the application container idled and a
  podbench {term}`sidecar` added. It is a second copy of the workload, which is why
  the mode is unsafe for a {term}`singleton`.

launcher
  The half of podbench that runs on your machine — the `attach`, `dev`, `hotfix`,
  `status`, `list` and `doctor` verbs. It shells out to {term}`kubectl` rather than
  linking a Kubernetes client library, so authentication, contexts and exec credential
  plugins are inherited rather than reimplemented.

hotfix manifest
  Not a Kubernetes {term}`manifest`. A JSON file — `.podbench-hotfix.json` — written
  at the root of the hotfix {term}`claim`, recording what the fix was made against:
  the repo, the base commit, the base image and its digest, the {term}`venv`'s
  interpreter version, and the commits since. A copy travels in a pod annotation so
  that `hotfix status` needs one `get pods` and no `exec`.

manifest
  A Kubernetes object as JSON or YAML. Podbench authors these itself for the
  {term}`dev pod` rather than using `kubectl debug --copy-to`. See also
  {term}`hotfix manifest`, which is a different thing with an unfortunately similar
  name.

launch-only
  The {term}`capreport` verdict for a seat that can debug processes *it* starts, but
  cannot inspect the target: `/proc/<pid>/root`, `maps` and `environ` are denied, and
  so is attach. It is a rung of its own because the two halves of it are
  independent — tracing your own descendant needs no permission at all, so
  `podbench dbg --launch ./prog` still gives breakpoints, `run` and backtraces on a
  pod whose own memory is shut. Reported as exit code 15. The name matters: called
  read-only, it sends you to a sysroot that will not open; called nothing, it hides
  the one inner loop that works. It is the ptrace-gated paths that are gone, not the
  whole of `/proc` — `cmdline`, `status` and `fd` need no permission and answer here
  as they do anywhere, which is why `podbench pids` still lists the target's
  processes.

mode
  One of the three ways in. **Observe** is the `attach` verb, **Iterate** is `dev`,
  and **Hotfix** is `hotfix`. The design documents use the mode names and the CLI uses
  the verbs; they refer to the same things.

origin
  The pod a {term}`dev pod` was cloned from, or the workload a hotfix is applied to.
  It is never modified by Iterate mode, and is recorded on the clone as the
  `podbench.dev/origin` annotation.

rung
  One step of the {term}`capability ladder`, and the security context that goes with
  it. **Full** is `runAsUser: 0` plus {term}`CAP_SYS_PTRACE`. **Degraded** is the
  target's own uid with all capabilities dropped. **Seat** is whatever the namespace
  will admit. There is no rung between full and degraded, because a capability added
  to a non-root container is a silent no-op.

  A rung is what was *asked for*, and the seat's rung is read back off the container
  the cluster admitted — which is not the same as what the seat can do. A mutating
  webhook that strips `capabilities.add` leaves a root seat indistinguishable from
  the degraded rung while it attaches perfectly well, so the rung names no verdict.
  What a seat can do is measured by {term}`capreport`, and `status` reports that
  measurement — or `not probed`, never the rung — beside each seat it lists.

seat
  A podbench container you can work in: an editor, a shell, git and a debugger, inside
  the cluster. An `attach` seat is an {term}`ephemeral container` in the live pod; a
  `dev` seat is an ordinary {term}`sidecar` in the clone. Both run the {term}`agent`
  and both get the same ssh wiring.

seat identity
  Whether {term}`sshd` inside a seat can resolve a login name for the uid the seat
  runs as. Without one, ssh is refused before a key is even looked at — see
  {term}`NSS`. A live-pod seat gets one with `--seat-gid-root`; a `dev` sidecar can be
  given a projected `/etc/passwd` file instead.

sidecar
  An ordinary container added beside the application in a {term}`dev pod`. Unlike an
  {term}`ephemeral container` it may declare `resources`, mount volumes with
  {term}`subPath`, and carry a {term}`readiness probe` — which is why Iterate mode is
  built on a clone rather than on the live pod.

singleton
  A workload where a second copy is not merely wasteful but wrong: it holds a device
  that accepts one connection, claims a name on the network, or takes a lock. An EPICS
  IOC is usually all three. `attach` and `hotfix` are safe for one; `dev` is not,
  because the clone *is* the second copy.

spike
  A time-boxed experiment run against a real cluster to test an assumption before
  building on it. S1 (the ssh transport), S2 (vscode-server in an
  {term}`ephemeral container`), S3 (gdb against a {term}`distroless` target), S4 (the
  Python relaunch loop) and S5 (the no-capability fallback) were the Phase 0 gate, and
  are collated in the Phase 0 gate report — which is where most of the non-obvious code
  here comes from, and which wins wherever it and the design brief disagree. S6 came
  later and records a route *not* taken: suspending an {term}`Argo CD`-managed workload.

sysroot
  The target container's filesystem as seen from the seat, at `/proc/PID/root`. gdb is
  pointed at it so that a {term}`distroless` binary's libraries and sources resolve,
  with no shared mount and no cooperation from the target.
```

## Kubernetes

```{glossary}
:sorted:

Argo CD
  A {term}`GitOps` controller. It stamps what it applied from git with the
  `argocd.argoproj.io/instance` label or `argocd.argoproj.io/tracking-id` annotation
  — on the *workload*, not on the pods it makes. Podbench detects only those two,
  deliberately: the tracking key is configurable and its default collides with an
  ordinary Helm label, so erring toward missing a detection is safer than refusing a
  mode that works.

claim
  A PersistentVolumeClaim: a request for storage that a pod mounts as a volume. Hotfix
  mode's claim is mounted over the application's {term}`venv` path, which is what
  makes a fix survive a restart. Pod volumes are immutable after creation, so a claim
  can never be added to a running pod — hence the deploy-time cooperation that mode
  asks for.

CreateContainerConfigError
  The waiting reason the {term}`kubelet` reports when it refuses a container the API
  server already accepted. It arrives *seconds after* a successful `kubectl` exit, and
  for an {term}`ephemeral container` it leaves a {term}`burnt name` behind. This is
  the second, asynchronous half of the two ways a debug container gets refused.

distroless
  An image containing an application and its runtime dependencies and nothing else —
  no shell, no package manager, often no libc utilities. You cannot `exec` into one
  usefully, which is exactly the case podbench exists for: the seat has the tools and
  reads the target's filesystem through {term}`sysroot`.

drift
  A live object differing from what git says it should be. Under a {term}`GitOps`
  controller with {term}`self-heal` on, anything podbench writes to a git-managed
  object is drift and gets reverted, usually within seconds and always without telling
  you.

EndpointSlice
  The object listing the pod addresses behind a {term}`Service`. A pod joins it by
  matching the Service's {term}`selector` *and* being Ready, which is why a
  {term}`dev pod` needs a {term}`readiness probe` that follows your process — without
  one it joins the moment it starts, while nothing is listening.

ephemeral container
  A container added to an already-running pod, through the `pods/ephemeralcontainers`
  subresource. It is the mechanism `attach` is built on, and it has three properties
  that shape everything: it cannot be removed, restarted or edited (see
  {term}`burnt name`); it may not declare `resources` at all; and it starts from the
  image every time, so nothing may live only in its writable layer.

eviction
  The {term}`kubelet` removing a pod to reclaim a resource — most relevantly
  ephemeral storage. An `attach` seat shares the pod's storage budget and cannot
  reserve its own, and a vscode-server plus extensions is a 1.1–1.3 GB working set, so
  the whole pod including the workload can be evicted.

GitOps
  Managing a cluster by reconciling it against git. Iterate mode refuses outright
  against a GitOps-managed workload, because a Service cutover is {term}`drift` on a
  tracked object and {term}`self-heal` reverts it with no error anywhere.

JSON patch
  A patch that applies an explicit list of operations — RFC 6902,
  `kubectl patch --type=json`. Podbench uses it to *replace* a Service
  {term}`selector`, because a {term}`merge patch` would union the two maps instead:
  the {term}`dev pod` gets added without the original being removed, which is the
  opposite of a cutover and invisible until half the responses are stale.

kind
  Kubernetes IN Docker — a cluster that runs inside containers on one machine. CI runs
  podbench's end-to-end suite on it.

k3s
  A lightweight, single-binary Kubernetes distribution. The six {term}`spike`s ran
  against a real 6-node k3s cluster.

kubectl
  The Kubernetes CLI. Podbench shells out to it for everything, including the ssh
  transport, so that one kubeconfig — with its contexts, proxies and exec credential
  plugins — serves both the API calls and the data path.

kubelet
  The agent on each node that actually runs containers. It is the second, later voice
  in the two-channel refusal: the API server can accept a container the kubelet then
  rejects (see {term}`CreateContainerConfigError`), and the two need separate handling
  because only the first can be caught by wrapping the API call.

liveness probe
  A periodic check the {term}`kubelet` uses to decide whether to *restart* a
  container. A process stopped at a breakpoint stops answering it, and the kubelet
  cannot tell that from a hang — so on a probed pod a debugging session is on a timer,
  and `attach` prints the arithmetic before you set one.

merge patch
  A patch that unions map keys — RFC 7386, `kubectl patch --type=merge`. Right for
  *adding* the hotfix provenance annotations to whatever else an object carries; wrong
  for a Service {term}`selector`, where {term}`JSON patch` is used instead.

ownerReferences
  Metadata naming the object that created this one. Podbench walks them upward — pod →
  {term}`ReplicaSet` → Deployment — to find the workload that carries the
  {term}`GitOps` mark, and to find the pod template that hotfix annotations must go on.

pod-template-hash
  A label the Deployment controller puts on the pods of each {term}`ReplicaSet`.
  Podbench deliberately strips it (and the other controller labels) from a
  {term}`dev pod` while keeping the {term}`Service` {term}`selector` labels: that puts
  the clone in the {term}`EndpointSlice` without making it a ReplicaSet member, which
  would otherwise get one of the two matching pods reaped.

PSA
Pod Security Admission
  The built-in admission controller that enforces the
  {term}`Pod Security Standards` on a namespace, configured with
  `pod-security.kubernetes.io/enforce` and friends. It is what refuses a
  {term}`CAP_SYS_PTRACE` container *synchronously*, in `kubectl`'s stderr, and so what
  makes the {term}`capability ladder` necessary. Its wording differs between levels,
  so podbench matches only the one stable fragment of the message.

PSS
Pod Security Standards
  The three named policy levels {term}`Pod Security Admission` enforces.
  *Privileged* is unrestricted; *baseline* blocks known escalations; *restricted*
  additionally requires non-root, `drop: ["ALL"]`, `allowPrivilegeEscalation: false`
  and a `RuntimeDefault` seccomp profile. Podbench's degraded and seat
  {term}`rung`s are authored to be admissible under *restricted*.

readiness probe
  A periodic check the {term}`kubelet` uses to decide whether a pod should receive
  {term}`Service` traffic. Failing one removes the pod's address from service quietly
  — no restart count, nothing that survives afterwards — which is why it is the
  deadline that matters most when you stop a process in a debugger.

ReplicaSet
  The controller a Deployment creates to maintain a pod count. Podbench reads it while
  walking {term}`ownerReferences` but never annotates it: the next rollout would
  discard the edit.

resize subresource
  `pods/resize` — the in-place change of a running container's resource limits,
  `kubectl patch pod --subresource resize`. `attach --resize` uses it to make headroom
  for the editor, opt-in and never fatal: the raised limit lives on the pod alone, so
  a rollout, scale, image bump or eviction regenerates it away silently.

RWO
  ReadWriteOnce — a volume access mode allowing one node to mount the volume for
  writing. The hotfix {term}`claim` is RWO, which is why that mode supports exactly one
  replica: a second either cannot schedule or, under ReadWriteMany, races on the same
  checkout.

selector
  The label query that decides which pods a {term}`Service` sends traffic to.
  `dev --cutover` replaces one so the {term}`dev pod` alone receives traffic, and
  records the original on the clone so teardown can restore it exactly.

self-heal
  A {term}`GitOps` controller setting that reverts {term}`drift` automatically rather
  than only reporting it.

Service
  The stable name and address in front of a set of pods. See {term}`selector` and
  {term}`EndpointSlice`. Podbench never joins one silently: a dev pod carrying the
  origin's labels would take production traffic, so it is always behind an explicit
  flag.

shareProcessNamespace
  A pod-level setting making all containers share one PID namespace, so each can see
  the others' processes. A {term}`dev pod` sets it; an `attach` seat gets the same
  visibility by targeting one container instead. Under it, PID 1 is `/pause` rather
  than the workload, and `pkill -f` matches processes in every container — both traps
  podbench is written around.

startup probe
  A probe that must succeed before the {term}`liveness probe` and
  {term}`readiness probe` begin. While it is still running it is the only deadline in
  force, which is why podbench works out which probe actually applies rather than
  listing all three.

strategic merge patch
  Kubernetes' own patch type, which knows that lists like `containers` are keyed by
  name rather than by position. Podbench uses it for the {term}`resize subresource`:
  a {term}`JSON patch` would address the container by *index*, and silently resize the
  wrong one if the spec changed underneath.

subPath
  A `volumeMount` field projecting one file or directory from inside a volume, rather
  than the whole thing. The API server **forbids it on an ephemeral container**, and
  refuses the entire request when it sees one — which is why an `attach` seat can
  never be given a `/etc/passwd` file, and a `dev` {term}`sidecar` can.
```

## Linux, and why ptrace says no

```{glossary}
:sorted:

ambient set
  The capability set that would let a *non-root* process keep a capability. No
  container runtime populates it, which is the whole reason
  `capabilities.add: ["SYS_PTRACE"]` beside a non-zero `runAsUser` is a silent no-op:
  the capability reaches the {term}`bounding set` and nothing else, leaving
  {term}`CapEff` zero. Podbench refuses to author that combination rather than ship a
  seat that looks privileged and behaves unprivileged.

AppArmor
  A Linux security module that confines processes by profile. One of the four
  {term}`blocker`s: a profile can deny ptrace between two domains, returning the same
  {term}`EPERM` as everything else.

bounding set
  The ceiling on the capabilities a process can ever hold. A capability here but not
  in {term}`CapEff` has no effect at all — see {term}`ambient set`.

CAP_SYS_PTRACE
  The capability that permits attaching a debugger to a process you do not own, and
  reading another container's filesystem through {term}`sysroot`. Podbench's full
  {term}`rung` asks for it; {term}`Pod Security Admission` is the thing most likely to
  refuse.

CapEff
  The effective capability set, readable in `/proc/PID/status`. `CapEff: 0` on a
  container that asked for {term}`CAP_SYS_PTRACE` is the measured symptom of the
  {term}`ambient set` problem.

cgroup
  The kernel's resource-grouping mechanism. Its path in `/proc/PID/cgroup` contains
  the container's runtime id, which is the only attribution of a process to its
  container that stays correct under {term}`shareProcessNamespace` and with a second
  podbench session attached. Podbench injects the target's id as
  `PODBENCH_TARGET_CID` and matches it as a *substring*, because the debug container's
  own cgroup namespace makes the path it reads relative.

EADDRINUSE
  The error a bind gets when the address is taken. In the relaunch loop it can also
  appear when nothing is listening — see {term}`TIME_WAIT` — so podbench says
  explicitly when that is what happened, rather than leaving you to suspect your code.

EPERM
  "Operation not permitted". Every one of the four {term}`blocker`s returns it, which
  is why a report that only prints the errno is worth nothing.

LSM
  Linux Security Module — the kernel's hook framework for access-control policies
  layered on top of the ordinary uid/gid rules. {term}`Yama`, {term}`AppArmor` and
  SELinux are all LSMs, and `security_ptrace_access_check()` is the **last** thing
  `__ptrace_may_access()` calls: a ptrace that satisfies credentials, dumpability and
  {term}`CAP_SYS_PTRACE` can still be refused here, with the same {term}`EPERM` as
  everything else. That is what denies `PTRACE_MODE_READ` at Diamond on a seat sharing
  the target's uid — established by elimination, since every other check demonstrably
  passes (issue #52). An LSM refusal names nothing on the way out, which is why
  `capreport` reaching "unknown" is a real outcome rather than a defect.

NSS
  Name Service Switch — how a Linux process turns a uid into a login name, usually via
  `/etc/passwd`. {term}`sshd` resolves the name a client offers through NSS *before* it
  looks at any key, so a seat running as a uid NSS cannot resolve refuses every login
  with `Permission denied (publickey)`, a message naming nothing about identity. It
  also breaks `ssh-keygen`, which calls `getpwuid()` whatever it is asked to do.

PID 1
  The first process in a namespace, which inherits orphans and whose exit ends the
  container. The {term}`agent` is PID 1 in a seat, which is why it never exits over a
  failed start-up step — and why Iterate mode idles the application container by
  authoring `sleep infinity` into the spec rather than killing the live PID 1, which
  the {term}`kubelet` would simply restart with pristine image code.

/proc/PID/root
  A symlink to the root of another process's mount namespace. Reading it gives the
  seat a complete view of the target container's filesystem — rootfs and volumes —
  with no shared mount and no cooperation from the target. The bridge is
  one-directional: the seat can read the application's filesystem, the application
  cannot see the seat's, which removes several tempting workarounds.

ptrace
  The system call debuggers use to attach to and inspect a running process. Whether it
  is permitted is the single question the {term}`capability ladder` and
  {term}`capreport` exist to answer.

reaping
  Collecting the exit status of terminated child processes so they do not accumulate
  as zombies. {term}`PID 1` inherits every orphan in the namespace, so the
  {term}`agent` does it.

seccomp
  A kernel facility filtering which system calls a process may make. One of the four
  {term}`blocker`s: a profile can reject `ptrace` itself.

SO_REUSEPORT
  A socket option letting several processes bind the same port, with the kernel
  splitting incoming connections between them. It is why the relaunch loop refuses to
  start while anything is listening: a second bind succeeds with no error and traffic
  is served from old and new code at once, with nothing in any log to say so.

ss
  The socket listing tool, from `iproute2`. Podbench runs it twice per relaunch — once
  for listeners, once for {term}`TIME_WAIT` — because `ss -l` cannot see the second.

TIME_WAIT
  The state a closed TCP socket sits in for up to about a minute. A port with sockets
  in it can refuse a rebind while `ss -l` shows no listener at all, so podbench warns
  rather than letting an {term}`EADDRINUSE` look like a bug in your code.

Yama
  A Linux security module whose `ptrace_scope` setting restricts who may attach to
  whom: 0 is classic behaviour, 1 restricts attachment to descendants, 2 requires
  admin, 3 forbids it entirely. It is *host-global and node-local* — two nodes in one
  cluster can disagree by kernel flavour — which is why podbench measures it on the
  node it landed on and prints the node name, and why "it worked yesterday" is
  explicable. {term}`CAP_SYS_PTRACE` overrides it.
```

## Binaries and symbols

These decide what a debugger can *tell* you once it has attached, and they fail in a
way worth naming: "cannot read the file" and "the file carries no debug information"
look alike from the editor and are nothing alike underneath. The first is a refusal,
the second is a working session with addresses instead of source.

```{glossary}
:sorted:

BFD
  The *Binary File Descriptor* library, part of GNU binutils — the layer that actually
  parses object files. gdb, `objdump`, `ld`, `readelf` and `strip` all read {term}`ELF`
  through it rather than each implementing the format. So a `BFD: …` line is the
  library complaining and not gdb, which matters when deciding what to upgrade: gdb's
  ability to read a binary is its binutils' ability. gdb renders BFD's refusals in its
  own words, of which `bad value` — `bfd_error_bad_value` — is the one meaning "I
  parsed this far and the file does not make sense".

build-id
  A hash the linker embeds in `.note.gnu.build-id`, identifying an exact build of a
  binary. It is what {term}`debuginfod` looks a binary up by, so a {term}`stripped`
  binary that kept its build-id can still be given symbols, and one without it cannot.
  Podbench reads it when deciding what to say about a target: "no `.debug_info`, but
  the build-id is present" and "no `.debug_info` and no build-id either" are different
  predictions about what you are about to see.

debuginfod
  A protocol and public service that serves {term}`DWARF` for a {term}`build-id`, so a
  stripped distro binary can be debugged without installing a `-dbg` package. Needs
  egress and `ca-certificates`, which is why `--no-debuginfod` exists. On Debian it
  serves symbols but not sources (report §3.2), so expect named frames and no source
  view.

DWARF
  The debug-information format, carried in `.debug_*` sections. It is what turns
  addresses into file, line, variable and type — everything a source-level debugger
  shows beyond a raw backtrace. Its absence is not an error: gdb attaches to a binary
  with no DWARF perfectly well and shows disassembly and whatever names the symbol
  tables hold.

ELF
  *Executable and Linkable Format*, the object-file format on Linux. Podbench parses
  just enough of it by hand — the machine, the section names — to decide which
  debugger flavour applies, because that decision must be made from the target's own
  binary rather than from the node's architecture or from anything the user typed.

stripped
  A binary whose symbol table, debug sections or both have been removed. Debuggable —
  breakpoints by address, frames from the dynamic symbol table, library names intact —
  just not at source level, unless {term}`debuginfod` can serve the missing
  {term}`DWARF`.

symbol versioning
  The scheme letting one shared library export several incompatible versions of a
  symbol, recorded in the `.gnu.version_r` and `.gnu.version_d` sections. Ordinarily
  invisible; it matters here because a {term}`BFD` older than the toolchain that
  linked a binary can reject its `.gnu.version_r` outright, and then the file cannot
  be read *at all*. Measured on a Debian bookworm seat (binutils 2.40) against a
  RHEL-family target image: the target's own application binaries read fine, its
  distro binaries — `/usr/bin/bash` among them — did not.
```

## ssh, and the transport

```{glossary}
:sorted:

ControlMaster
  The ssh feature that multiplexes further sessions over one existing connection.
  Podbench's generated stanza turns it on with a `ControlPersist` timeout, which is
  what makes a reconnect cost 0.058 s against 0.345 s cold.

CRI
  Container Runtime Interface — the API between the {term}`kubelet` and the runtime
  (containerd, CRI-O). Its exec streaming is what carries podbench's ssh connection,
  and its one sharp edge is {term}`fd 2`.

fd 2
  File descriptor 2, standard error. Closing or replacing it in a `kubectl exec`'d
  process makes the {term}`CRI` tear down the *entire* exec stream, truncating stdin
  and stdout mid-transfer with a zero exit code. This is why {term}`sshd` is run with
  `-e` (which keeps fd 2 owned and open) and `-o LogLevel=ERROR` (which keeps it
  silent), and why no wrapper may redirect it.

HostKeyAlias
  The name ssh records a host key under, independent of the hostname. Podbench keys it
  on the *pod UID*, so a re-created pod appears as a new host rather than as a
  man-in-the-middle warning.

inetd mode
  `sshd -i`: sshd serving one connection on its own stdin and stdout instead of
  listening on a socket. It is what lets a `kubectl exec` channel *be* the ssh
  transport — no listening socket in the pod, no port-forward, no pod IP, and no
  inbound network path of any kind.

known_hosts
  The file ssh records host keys in. Podbench writes its own under `~/.podbench/`
  rather than touching yours, and the generated stanza points `UserKnownHostsFile` at
  it.

ProxyCommand
  An ssh option naming a command whose stdin and stdout carry the connection, in place
  of a TCP socket. Podbench's is a `kubectl exec` running {term}`sshd` in
  {term}`inetd mode`, which is the whole network story: the outer authentication is
  your kubeconfig and the inner one is your ssh key.

Remote-SSH
  The VS Code extension that runs a server on a remote host and edits there over ssh.
  Pointing it at the generated host alias is how the editor gets into the cluster.

sftp
  The file-transfer subsystem of ssh. It works here like everything else, because the
  transport is a real ssh connection rather than an emulation of one.

sshd
  The OpenSSH server. Podbench runs it per-connection from the {term}`ProxyCommand`,
  against a generated config file of its own rather than the image's, so the working
  configuration is a reviewable artifact and the distro's sshd is left alone.

vscode-server
  The server half of {term}`Remote-SSH`, unpacked into the seat's home on first
  connect. It is about 700 MiB, and a working session with extensions and a language
  server index reaches 1.1–1.3 GB — the number behind every warning about memory and
  ephemeral storage in Observe mode.
```

## Python packaging

```{glossary}
:sorted:

debugpy
  The debug adapter for Python, used to attach VS Code's debugger to a Python process.
  The native equivalent is gdb.

dist-info
  The metadata directory an install writes beside a package, recording its version,
  entry points and file list. An {term}`editable install` bakes these in at install
  time, which is why changing `pyproject.toml` needs the installer run again while
  changing code does not.

editable install
  An install that points at your checkout instead of copying it, so edits take effect
  without reinstalling — `pip install -e` or `uv pip install -e`. It works by writing
  a {term}`.pth file` into the interpreter's `site-packages`, which is what makes it
  sensitive to the mount namespace the checkout lives in.

PEP 660
  The standard defining modern {term}`editable install`s. Its exec-style
  {term}`.pth file` prints a traceback and then carries on with exit 0 when the path it
  names does not exist — a failure loud enough to scroll past and quiet enough to
  miss.

.pth file
  A file in `site-packages` that Python's `site` module processes at start-up. A line
  naming a directory is added to `sys.path` **only if that directory exists** — with
  no warning when it does not, which is why an install spanning two containers'
  filesystems dangles silently and surfaces much later as an unrelated-looking
  `ModuleNotFoundError`.

pyvenv.cfg
  The file at the root of a {term}`venv` recording which interpreter created it. In
  Hotfix mode it is on the {term}`claim`, which makes it the one record of the
  interpreter that stays true across an image upgrade — so podbench reads the version
  from it and measures the live one separately.

uv
  The Python package and project manager podbench's image ships. `uv sync --frozen`
  installs exactly what the lockfile says, because a dev pod that silently re-resolves
  dependencies is no longer running what production runs.

uvx
  `uv tool run` — fetch a tool and run it in one shot, leaving nothing installed. It
  is podbench's canonical invocation: `uvx podbench attach my-pod`.

venv
  A Python virtual environment: an interpreter, a `bin/` and a `site-packages` of its
  own. Hotfix mode mounts a {term}`claim` over the application's, which is what makes
  a fix outlive the container — and also what makes the venv shadow the image's after
  an upgrade, since its `bin/python` is a symlink to an interpreter path *inside the
  image*.
```
