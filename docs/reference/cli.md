# Command-line reference

One binary serves both halves of podbench, under one spelling. On your machine
it is reached as `podbench <verb>`; inside the debug container the same binary
is PID 1 and answers to the same `podbench <verb>`. Keeping it as one package
means the capability logic that decides what a session can do is the same code
in both places, rather than a launcher's guess and a helper's separate guess.

```
$ podbench --help

 Usage: podbench [OPTIONS] COMMAND [ARGS]...

 A development seat inside a Kubernetes pod.

 Run `podbench VERB --help` for a verb's own options.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --version  -v        show the launcher's version and exit                                        │
│ --help               Show this message and exit.                                                 │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ On your machine ────────────────────────────────────────────────────────────────────────────────╮
│ doctor         check this machine can attach, and name what stops it                             │
│ attach         add or reconnect a podbench container and print the report                        │
│ ssh-config     regenerate the ssh stanza for an existing session                                 │
│ status         the podbench containers in one pod and what each supports                         │
│ list           every pod in the namespace carrying a podbench container                          │
│ dev            create or delete the dev pod                                                      │
│ hotfix         durable in-place fixes on a claim-backed venv                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Inside the debug container ─────────────────────────────────────────────────────────────────────╮
│ agent          prepare the container for ssh and idle as its PID 1                               │
│ capreport      name the mechanism that denies ptrace in this container                           │
│ pids           list the pod's processes                                                          │
│ dbg            debug a process                                                                   │
│ debug-config   write VS Code's launch.json for this seat                                         │
│ dev-bootstrap  clone, sync and editable-install a checkout                                       │
│ run            relaunch the app and verify it                                                    │
│ stop           stop the recorded child                                                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

| Where it runs | Verbs |
|---|---|
| Your machine | `doctor`, `attach`, `ssh-config`, `status`, `list`, `dev`, `hotfix` |
| Inside the debug container | `agent`, `capreport`, `pids`, `dbg`, `debug-config`, `dev-bootstrap`, `run`, `stop` |

Every verb below is written as `podbench <verb>`, which is the only spelling
there is — there is no kubectl plugin. How you reach that program is your
choice, and all three run the same code:

| Invocation | Why |
|---|---|
| `uvx podbench <verb>` | the canonical one. uv fetches the launcher for the run and leaves nothing installed |
| `uvx podbench@<version> <verb>` | pinned, so a session is reproducible and the image tag it picks is known in advance |
| `uv tool install podbench` (or pipx, or pip) | for `podbench` permanently on `PATH` |

See [Setup](../tutorials/setup.md) for the details, including how
to run it before the first PyPI release.

The in-pod verbs are spelled the same way from a terminal in the seat:
`podbench pids`, `podbench dbg`, and so on. There are no shorter aliases on
`PATH` — the image once shipped one file per subcommand and no longer does
(`image/README.md`, deviation 6).

## Common options

The four launcher verbs — `attach`, `ssh-config`, `status`, `list` — take these,
and so does `doctor`:

```
--namespace  -n  NAMESPACE  namespace (default: the kubeconfig context's own)
--context        NAME       kubeconfig context
--kubectl        BIN        kubectl binary to use [default: kubectl]
--config-dir     DIR        where the generated ssh config and known_hosts live
                            (default ~/.podbench)
```

`dev` takes `-n`/`--namespace`, `--context` and — because it writes an ssh
config too — `--identity`, `--config-dir` and `--host-alias`. It does not take
`--kubectl`: it shells out to `kubectl` on `PATH`. Under `hotfix` the same three —
`-n`/`--namespace`, `--context` and `--kubectl` — sit on each **sub-verb**, not
on `hotfix` itself, so it is `podbench hotfix status -n demo` and never
`podbench hotfix -n demo status`. `hotfix` writes no ssh config, so nothing under
it takes `--config-dir`.

podbench shells out to `kubectl` deliberately, so it inherits your kubeconfig,
your current context and any exec credential plugin. There is no second
credential and no client library.

(naming-the-pod)=
## Naming the pod

`attach`, `ssh-config` and `status` take a `POD`, and none of them needs the
whole name. Resolution is the same in all three:

| you type | what happens |
|---|---|
| the full name, or `pod/NAME` | used as typed, in one `kubectl get pod` — an exact name is never ambiguous, even when it is also a substring of another pod's name |
| a substring matching **one** pod | resolved to that pod, and the name it resolved to is echoed on stderr |
| a substring matching **several** | the matches are listed and you are asked which |
| nothing at all | every pod in the namespace is listed and you are asked which — unless the namespace holds exactly one, which resolves and is echoed like any other single match |
| a substring matching **none** | an error naming the namespace searched, with what is in it |

```
$ podbench attach api -n demo
'api' matches 2 pods in namespace demo
      NAME        READY  STATUS   AGE  PODBENCH
  1.  api-7f9     1/1    Running  3h   podbench-1
  2.  api-canary  0/1    Pending  3h   -
which one? [number or name, empty to cancel] 1
```

The listing carries what you choose *by*: ready containers, status, age, and the
podbench container already in the pod — which is the difference between landing
a seat and reconnecting to yours. Answer with the number, the name, or a longer
substring; an empty line cancels.

The prompt is only ever offered on a terminal. **When stdin is not a tty — a
script, a CI job, an `ssh host podbench ...` — a prompt would be a hang**, so
podbench prints the same listing, explains that it will not ask, and exits `2`.
`--no-prompt` asks for that behaviour on a terminal too. Both the listing and
the "matched" echo go to **stderr**, so a redirected stdout still holds only the
report.

Resolution lists every pod in the namespace, which is not what `podbench list`
does: `list` shows the pods that already carry a podbench container, and
resolution offers the pods that could. A fully typed name is answered without
listing at all, so `attach` still works with RBAC that grants `get` on pods but
not `list`.

---

## Cluster-side verbs

### `doctor`

Everything that has to be true of **this machine** before the first attach, and
the name of whatever is not. `status` is about pods; `doctor` is about your
laptop.

```

 Usage: podbench doctor [OPTIONS]

 Name what will block the first attach from this machine.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --fix                            make the two changes podbench can make safely: create the       │
│                                  config directory, and add the ssh Include above any Host *      │
│                                  block. Never creates an ssh key                                 │
│ --identity            KEY        the ssh key attach would use [default: ~/.ssh/id_ed25519]       │
│ --namespace   -n      NAMESPACE  namespace to test RBAC in (default: the context's own)          │
│ --context             NAME       kubeconfig context                                              │
│ --kubectl             BIN        kubectl binary to use [default: kubectl]                        │
│ --config-dir          DIR        where the generated ssh config and known_hosts live (default    │
│                                  ~/.podbench)                                                    │
│ --help                           Show this message and exit.                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

```
$ uvx podbench doctor -n demo
============================= podbench doctor ==============================
THIS MACHINE
  launcher       1.0.0b1
  image          ghcr.io/gilesknap/podbench:1.0.0b1
  context        prod-eu
  namespace      demo
CHECKS
  [ok]    kubectl        v1.31 at /usr/local/bin/kubectl
  [ok]    kubeconfig     context prod-eu
  [ok]    ssh client     /usr/bin/ssh
  [ok]    ssh identity   /home/dev/.ssh/id_ed25519 and /home/dev/.ssh/id_ed25519.pub
  [warn]  ssh agent      agent on /run/user/1000/keyring/ssh holds SHA256:Ql+7…: ssh will sign with the AGENT, not with /home/dev/.ssh/id_ed25519
          that socket is gnome-keyring standing in for ssh-agent, which has a long history of refusing ED25519 keys with `agent refused operation`
          prove it is the agent and not the seat:  SSH_AUTH_SOCK= ssh podbench-demo-<pod>
          if it refuses, sign with the file instead — put this in ~/.ssh/config below the Include line, where it cannot shadow the generated stanza:
              Host podbench-*
                  IdentityAgent none
          never for a FIDO/sk-* key or a smartcard, though: those can only sign through an agent
  [ok]    config dir     /home/dev/.podbench/config.d
  [FAIL]  ssh include    /home/dev/.ssh/config does not include the generated stanzas
          add this line above any Host * block:  Include /home/dev/.podbench/config.d/*.conf
          or run:  podbench doctor --fix
RBAC in demo (kubectl auth can-i, as your kubeconfig's user)
  [ok]    attach         all 5 verbs allowed
  [warn]  iterate        missing: create pods, delete pods
          grant it with the chart's rbac.iterate=true, or the equivalent Role
  [warn]  resize         missing: patch pods/resize
          grant it with the chart's rbac.resize=true, or the equivalent Role
  [ok]    hotfix         all 5 verbs allowed
----------------------------------------------------------------------------
VERDICT: 1 blocker before `podbench attach` can work (exit 1)
BLOCKERS: ssh include
============================================================================
```

What it checks:

| Check | `FAIL` when | `warn` when |
|---|---|---|
| `kubectl` | not on `PATH`, or older than **1.25** | it printed no version to read |
| `kubeconfig` | there is no current context | — |
| `ssh client` | `ssh` is not on `PATH` | — |
| `ssh identity` | either half of the key is missing | — |
| `ssh agent` | — | an agent is running **and holds the identity** (unless your config already sets `IdentityAgent none`), its socket is set but dead, or the comparison could not be made |
| `config dir` | — | `~/.podbench/config.d` does not exist yet |
| `ssh include` | `~/.ssh/config` does not include the generated stanzas | it includes them **below** a `Host`/`Match` block |
| RBAC `attach` | any of its verbs is denied | kubectl could not answer |
| RBAC `iterate`, `resize`, `hotfix` | — | any of its verbs is denied, or kubectl could not answer |

Notes:

* **Exit code is `0` when nothing blocks the headline attach path and `1` when
  something does**; a warning never changes it. A cluster that will not grant
  Iterate mode is a fact about that cluster, not a failure — the same call
  `attach` makes when it lands a degraded seat. `2` remains a usage error.
* The RBAC verbs are asked one `kubectl auth can-i` at a time, in the namespace
  in play, as your kubeconfig's user. The table lives in
  `podbench.doctor.FEATURES` and names the `rbac.<flag>` of
  [the chart](../explanations/security.md) that grants each feature;
  `tests/test_chart_contract.py` renders the chart and asserts they are the same
  list, so the flag a report tells you to set really is the one that fixes it.
* Only two things are ever written, and only with `--fix`: `~/.podbench/config.d`
  is created, and the `Include` line is prepended to `~/.ssh/config` above any
  `Host *` block. Your file is not rewritten — the line is added at the top and
  everything you had stays where it was — and the write goes through a temporary
  file, because a half-written `~/.ssh/config` locks you out of every host you
  have, not only podbench's. Running `--fix` twice changes nothing the second
  time.
* **`--fix` never creates an ssh key.** A missing identity is named, with the
  `ssh-keygen` line to run, because a key podbench minted would be a credential
  you never chose and `attach` would then authorise it inside your cluster.
* **The `ssh agent` check names what will *sign*, which is not always the file.**
  With `SSH_AUTH_SOCK` unset, ssh signs with the key file and a passphrase prompt
  is expected. With it set, ssh offers the agent's keys first, so an identity the
  agent also holds is signed for by the **agent** — the private file is never
  opened, and `IdentitiesOnly yes` in the generated stanza does not change that:
  it limits which keys are *offered*, not who signs for them. doctor compares
  `ssh-keygen -lf <identity>.pub` against `ssh-add -l` and says which of the two
  it will be. It does not ask for a signature, so it reports what would be asked,
  never what it would answer — hence a warning and never a blocker. Any part of
  that comparison it cannot make — a listing that failed, a `.pub` it cannot
  read, `ssh-add` off `PATH` — is reported as *not measured*, with what the
  command said, rather than folded into either answer.
* **The warning goes away when you act on it.** Once the agent holds the
  identity, doctor asks `ssh -G` what `IdentityAgent` resolves to for the alias
  `attach` would generate in this namespace, and reports `ok` when the answer is
  `none` — so the check can see its own advice taken. It asks about
  `podbench-<namespace>-pod`, which is what a `Host podbench-*` block matches; if
  you attach with `--host-alias NAME`, that block has to name `NAME` instead, and
  doctor cannot see it.
* A refusing agent is the one failure that looks like podbench's fault and is
  not: ssh reports `agent refused operation` and then `Permission denied
  (publickey,keyboard-interactive)`, which reads as the seat rejecting the key.
  `SSH_AUTH_SOCK= ssh <alias>` settles it in one line — if that logs in, the
  agent was the only thing refusing. A socket under `/run/user/*/keyring/` is
  gnome-keyring standing in for ssh-agent, which has a long history of refusing
  ED25519 keys exactly this way.
* **`IdentityAgent none` is never recommended unconditionally**, by doctor or by
  these docs: a FIDO/`sk-*` key or a smartcard has no private half on disk and
  can *only* sign through an agent, so the fix that rescues an ED25519 key
  disables those outright. doctor also does not write it — the generated stanza
  is rewritten on every attach, so the keyword belongs in a `Host podbench-*`
  block in your own `~/.ssh/config`, **below** the `Include` line: a `Host` block
  above it shadows the generated stanza, which is the next warning down this
  list.
* An `Include` below a `Host *` block is a warning rather than a blocker: the
  stanza is still read, but ssh takes the **first** value it sees for each
  keyword, so anything that block also sets — a `ControlPath`, a `ProxyCommand` —
  has already won.

### `attach`

Land a debug seat in a **live** pod, walking the capability ladder, and print
what that seat can actually do.

```
                                                                                                    
 Usage: podbench attach [OPTIONS] [POD]                                                             
                                                                                                    
 add or reconnect a podbench container and print the report                                         
                                                                                                    
╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   POD      <str>  pod/NAME, a bare NAME, or any substring of one. Anything that does not settle  │
│                   on a single pod lists the namespace and asks                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --target                    NAME             workload container name                             │
│ --image                     REF              debug image (default: $PODBENCH_IMAGE, else the     │
│                                              image built from this launcher's version)           │
│ --target-uid                UID              the target's uid, when its pod spec does not say    │
│ --target-gid                GID              the target's gid, when its pod spec does not say.   │
│                                              The seat must share it: __ptrace_may_access         │
│                                              compares the group ids as peers of the user ids, so │
│                                              a seat at the target's uid in another group can log │
│                                              in and cannot trace. Rarely needed - podbench       │
│                                              measures the target's real gid from /proc and lands │
│                                              a corrected seat itself - but it costs one          │
│                                              container name instead of two, and it is not        │
│                                              overridden by the measurement                       │
│ --max-rung                  RUNG             highest rung of the capability ladder to try: full, │
│                                              degraded or seat. It is where the walk starts, and  │
│                                              the ladder still falls through the rungs below.     │
│                                              Without it a target whose uid is known and not root │
│                                              has its own rung tried first. Use `full` to insist  │
│                                              on the capability rung - a node with Yama           │
│                                              ptrace_scope >= 1 exempts nothing else - or         │
│                                              `degraded` where a mutating admission policy strips │
│                                              SYS_PTRACE instead of refusing it. A running seat   │
│                                              above the ceiling is not reused                     │
│ --mount                     CLAIM:MOUNTPATH  mount a volume the pod already declares into the    │
│                                              seat, named by claim or by volume name. MOUNTPATH   │
│                                              defaults to the application container's own, which  │
│                                              Hotfix mode requires it to equal. Repeatable        │
│ --new                                        add a container even if one is running (its name is │
│                                              permanent)                                          │
│ --no-correct-ids                             keep the first seat even when it landed in the      │
│                                              wrong group. Without this, a seat whose measured    │
│                                              uid:gid disagrees with the target's is replaced     │
│                                              once by a corrected one, which spends a second      │
│                                              container name for the pod's lifetime - an          │
│                                              ephemeral container's securityContext cannot be     │
│                                              changed in place. Use --target-gid to get it right  │
│                                              on the first name                                   │
│ --no-seat-identity                           do not mount the pod's podbench-home volume, which  │
│                                              is otherwise mounted by convention when the pod     │
│                                              declares it and keeps everything the seat writes    │
│                                              off the workload's ephemeral-storage budget. The    │
│                                              podbench-identity volume is never mounted by        │
│                                              attach: it needs a subPath per file, which an       │
│                                              ephemeral container may not have - a live-pod seat  │
│                                              registers its own NSS record instead, and needs no  │
│                                              volume for it                                       │
│ --no-probe                                   skip capreport; the report then says nothing was    │
│                                              measured                                            │
│ --pull                      POLICY           imagePullPolicy for the seat: IfNotPresent          │
│                                              (default), Always or Never. Use Always when         │
│                                              iterating on a tag that moves - `main`, or a branch │
│                                              image - since a node that already has a copy will   │
│                                              otherwise serve it. It cannot be the default:       │
│                                              Always is the one policy that needs a registry, so  │
│                                              it breaks an image side-loaded with `kind load` or  │
│                                              `ctr import`                                        │
│                                              [default: IfNotPresent]                             │
│ --resize                    MEMORY           raise the target's memory in place first, as LIMIT  │
│                                              or REQUEST:LIMIT, e.g. 6Gi or 1Gi:6Gi. The request  │
│                                              is raised too where a LimitRange bounds             │
│                                              limit/request                                       │
│ --resize-cpu                CPU              raise the target's cpu in place first, as LIMIT or  │
│                                              REQUEST:LIMIT, e.g. 4 or 500m:4                     │
│ --identity                  KEY              ssh key to authorise in the seat and name in the    │
│                                              generated stanza                                    │
│                                              [default: ~/.ssh/id_ed25519]                        │
│ --ssh-user                  NAME             login name to put in the stanza                     │
│ --host-alias                NAME             ssh Host name for the seat                          │
│ --print-config                               print the ssh stanza instead of writing it to the   │
│                                              config dir                                          │
│ --open                                       open the seat's home in VS Code over Remote-SSH     │
│                                              once it lands, with the /proc and /sys excludes,    │
│                                              this target's launch.json and only the extensions   │
│                                              its debugger needs. Needs `code` on PATH            │
│ --provision                                  with --open, make the target debuggable: install    │
│                                              debugpy when it cannot import one, then start the   │
│                                              server so F5 has something to connect to -          │
│                                              otherwise a stock Python workload gets no           │
│                                              launch.json at all. Mutates the workload: ~15 MB of │
│                                              shared ephemeral storage, needs egress from the     │
│                                              pod, ptraces the app for a few seconds, and no      │
│                                              restart survives it                                 │
│ --timeout                   SECONDS          seconds to wait for the seat [default: 120.0]       │
│ --no-prompt                                  never ask which pod: an ambiguous or missing POD is │
│                                              refused with the candidates instead. Already        │
│                                              implied when stdin is not a tty                     │
│ --namespace         -n      NAMESPACE        namespace (default: the kubeconfig context's own)   │
│ --context                   NAME             kubeconfig context                                  │
│ --kubectl                   BIN              kubectl binary to use [default: kubectl]            │
│ --config-dir                DIR              where the generated ssh config and known_hosts live │
│                                              (default ~/.podbench)                               │
│ --help                                       Show this message and exit.                         │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Notes:

* `pod` accepts `pod/NAME`, a bare `NAME`, a substring of one, or nothing at
  all — see {ref}`Naming the pod <naming-the-pod>`.
* `--image` has no fixed default to print: the launcher asks for the image
  built from its own version — `ghcr.io/gilesknap/podbench:<launcher version>`,
  and `:main` when the launcher is a dev build off a checkout. `--image` wins
  over `PODBENCH_IMAGE`, which wins over that. See
  [The container image](../how-to/run-container.md).
* Re-running `attach` **reconnects** to a running seat. `--new` appends another
  ephemeral container, whose name is then burnt for the pod's lifetime.
* `--target-uid` matters to the degraded rung, which must match the target's UID
  exactly and never defaults to root — and to the walk's *order*, since a known
  non-root UID is what makes that rung the one tried first. It is also the
  answer to a cluster that allow-lists `runAsUser`: the refusal names the UIDs
  it would take, and this is how one of them is chosen.
* `--max-rung` states where the walk starts — the rungs above it are skipped,
  the ones below still tried. Without it the target decides: a target at a known
  non-root UID has the UID-matching rung tried first, because that rung already
  satisfies the kernel's credential check and a root seat whose capability was
  stripped reads *fewer* of the target's `/proc` files than it does. Pass `full`
  to insist on the capability rung, which is the only one Yama exempts, or
  `degraded` on a cluster whose policy **mutates** rather than refuses. A
  running seat that the ceiling would not have landed is **not** reconnected to,
  since an ephemeral container's `securityContext` is fixed for the pod's
  lifetime. See {ref}`When the cluster strips SYS_PTRACE <stripped-sys-ptrace>`.
* `--mount` is how a seat reaches a Hotfix-mode claim. An ephemeral container may
  mount the volumes its pod **already declares** and may not introduce one —
  `spec.volumes` is immutable once the pod exists — so a name the pod does not
  carry is refused with that explanation rather than submitted. That immutability
  is the whole reason Hotfix mode asks for the chart's cooperation at deploy time;
  `podbench hotfix --print-values` emits the volume, the volumeMount and the
  seeding initContainer that put it there.
  * The argument is a **claim** name or the pod's **volume** name; a claim is
    resolved to the volume entry that references it.
  * `MOUNTPATH` is optional and usually should be. Where the application
    container mounts that volume, its mountPath is copied, because Hotfix mode
    only works when the claim resolves at the *same* path on both sides — the
    venv's `bin/python` and the checkout's editable install are absolute paths
    recorded on the volume. An explicit path that disagrees is honoured and
    warned about; a volume the application does not mount has no path to copy,
    so one must be given.
  * An application mount that uses a **`subPath` is refused**, before anything
    is submitted. An ephemeral container's volumeMounts may not carry one — the
    API server answers `Forbidden: cannot be set for an Ephemeral Container` and
    rejects the whole request — and dropping it silently would give the seat the
    volume root where the application sees one directory inside it, so every
    path Hotfix mode recorded would resolve to the wrong thing. Deploy the claim
    mounted whole over the venv path, or use `podbench dev`, whose seat is an
    ordinary container.
  * Mounts are fixed when a container is created, so `--mount` against a
    reconnect warns and does nothing. Use `--new` for a seat with a new mount.
* **The seat's home is mounted by convention, not by flag.** If the pod declares
  a volume named `podbench-home`, `attach` mounts it read-write at
  `/home/podbench` and makes it the seat's `$HOME`, which keeps vscode-server and
  everything else the seat writes off the workload's ephemeral-storage budget.
  * It is a convention because the volume cannot be there by accident: an
    ephemeral container may only mount volumes the pod already declares and
    `spec.volumes` is immutable, so anything called `podbench-home` was put in
    the pod at deploy time on purpose.
  * It needs the pod to set `fsGroup` to the application's gid, or it arrives
    owned by `root:root` and the seat cannot write to it. The agent reports that
    by name at start-up.
  * An explicit `--mount` for the same mountPath **wins** over the convention.
    `--no-seat-identity` turns the convention off.
* **`attach` cannot mount `podbench-identity`, however plainly the pod declares
  it.** The identity has to land as two *files* — `passwd` over `/etc/passwd`,
  `group` over `/etc/group` — and one file at a time takes a `subPath` per
  mount, which an ephemeral container may not have: the API server answers
  `spec.ephemeralContainers[0].volumeMounts[0].subPath: Forbidden: cannot be set
  for an Ephemeral Container` and refuses the *whole* request, so no seat lands
  at all. Mounting the volume whole is not an alternative either; a directory
  mount replaces the path, and over `/etc` it would take `nsswitch.conf` with it
  — the very lookup the identity exists to satisfy.
  * **On a live pod the seat writes its own record, and needs no volume and no
    flag to.** The image installs `libnss-extrausers`, points `nsswitch.conf`'s
    `passwd` line at it and ships `/var/lib/extrausers/passwd` world-writable, so
    the agent appends a record for the uid *and gid* the seat turned out to run
    as. That is the whole mechanism: no capability, no `runAsGroup`, nothing in
    the workload's manifest. The exception is a seat under that database's
    compiled-in floors — it ignores a record whose uid or gid is below 500, gid
    100 excepted — which falls back to `/etc/passwd`, where the image has
    pre-seeded a static record for every free uid below 500 so that nothing
    needs to be written.
  * The volume is for a seat that is an **ordinary** container, which is what
    `podbench dev` authors — `subPath` is legal there and nothing is written at
    runtime. (The dev sidecar does not mount it yet; see the follow-up note in
    `Charts/podbench/values.yaml`.)
  * The capability report says so where it matters: when the pod declares the
    volume, the `ssh seat` line explains that it cannot be projected into an
    ephemeral container and names the seat's own record as the route instead, so
    that a pod somebody prepared for podbench does not read as one whose
    preparation failed. Where a seat *does* carry the identity, the same line
    credits it.
* `--resize` and `--resize-cpu` are opt-in and only partly proven, and need
  `pods/resize` `patch`. An attach that used neither prints one line offering
  them; one that used either prints what it cost — including that the raised
  limit is on the pod and not on its controller, so a rollout reverts it.
  Both take `LIMIT` or `REQUEST:LIMIT`, and raise the request alongside the
  limit where a `LimitRange` bounds the ratio between them.
* **`--target-gid` and the automatic correction** are one mechanism seen from
  two ends. `__ptrace_may_access()` compares `gid`, `egid` and `sgid` as peers
  of `uid`, `euid` and `suid`, so a seat that mirrors the target's uid and
  leaves the group at the debug image's `0` is denied every ptrace-gated
  operation — live attach, and `/proc/<pid>/root`, `maps`, `environ` and `exe`
  with it. That is the *usual* shape, because a manifest usually states
  `runAsUser` and no `runAsGroup` and the real group comes from the workload
  image's own user (p47-blueapi-0: `runAsUser: 1000`, real gid 1000).
  * Nothing laptop-side can read that gid. `/proc/<pid>/status` can, it is
    world-readable, and a seat at the wrong ids can still read it — so podbench
    measures it *after* landing and, where it disagrees with what was authored,
    lands one corrected seat by itself. An ephemeral container's
    `securityContext` cannot be changed in place, so this spends a second
    container name, permanently, and says so in one line.
  * It happens **once**: the corrected attach cannot correct itself, and a later
    attach finds the corrected seat instead of landing a third. A manifest that
    states both ids costs one name as it always did.
  * `--target-gid` states the group up front and costs one name instead of two.
    It is a pin, not a hint: the measurement never overrides it. `--no-correct-ids`
    keeps the first seat and leaves the mismatch reported as the
    `gid-mismatch` blocker.
* **`--open`** takes the seat from "landed" to "bound breakpoint" in one
  command. It needs `code` on your PATH, and the local VS Code needs the
  **Remote - SSH** extension; both are checked at the point of use and named in
  the failure rather than reported as a traceback. `code` is looked for
  *before* the seat is landed, because an ephemeral container's name is
  permanent and a run that was always going to end at "no `code`" must not burn
  one.

  It has to be the **desktop** `code`. Inside a Remote-SSH window, a
  devcontainer or a Codespace, the `code` on your PATH is VS Code's *remote*
  CLI, which forwards to the window that terminal already belongs to:
  `--install-extension` would install into that machine rather than into the
  seat, leaving breakpoints that never bind. podbench refuses that one by name
  before landing anything — run it from a terminal on the machine your VS Code
  itself runs on, or drop `--open` and use **Remote-SSH: Connect to Host**.

  In order it:
  * writes `<home>/.vscode/settings.json` with every exclude `podbench agent`
    writes at machine scope — the watcher, search, Pylance and cpptools entries
    for `/proc`, `/sys`, `/dev` and `~/.vscode-server` — **before** the window
    opens, because the walk starts the moment it does. A single folder makes
    that file the *workspace* settings, so none of the keys is dropped there,
    and this is the one copy that survives *Kill/Uninstall VS Code Server on
    Host*. Inside a home, `**/.vscode-server/**` is the entry that earns its
    place first, and `C_Cpp.files.exclude` the only one that stops cpptools'
    tag parser walking on its own account;
  * runs `podbench debug-config --print-config` in the seat and merges the result into
    `<home>/.vscode/launch.json`, matching on configuration name, so a second
    `--open` updates its own entries rather than appending copies;
  * installs **only** the extensions the emitted configurations name, with
    `code --remote ssh-remote+<alias> --install-extension` — which is the
    "Install in SSH: `<alias>`" button as a flag. A locally installed extension
    runs the debug adapter on your laptop, where no `/proc/<pid>/root` path
    means anything, and the failure looks like a bad `launch.json`. They are
    also recommended in `<home>/.vscode/extensions.json` as a fallback. An
    install only unpacks into the seat's `~/.vscode-server`, so a window that
    was **already** connected keeps the extension host it started and never
    loads it — the adapter stays unregistered and its `launch.json` entry
    cannot run. A first `--open` is unaffected, since the install finishes
    before the window opens; a later run needs the reload only where it put a
    *new* extension in the seat, and the *Developer: Reload Window* reminder is
    printed whenever an install **succeeded**, because `code` exits 0 for
    "already installed" too and this side cannot tell an open window from a
    fresh one. A run whose every install failed prints no reminder, having
    unpacked nothing;
  * opens the **seat's home** — `/root`, or `/home/podbench` on a
    `podbench-home` volume. Never `/`: a folder there points the watcher at
    `/proc/<pid>/root`, which is a symlink into another container's rootfs, and
    the walk has no bottom.

  It cannot be combined with `--print-config`, which writes no stanza:
  `code --remote ssh-remote+<alias>` resolves the alias through ssh, and ssh
  reads the config dir. A target no debugger fits is not a failure — the
  excludes, the folder and the terminals are the rest of the seat.

  `debug-config`'s own stderr is relayed line by line rather than summarised.
  It is the only thing in the run that can see the target, so its narration is
  the diagnosis — it names every mechanism that said no, and on success it also
  carries the injection command, which the emitted debugpy configuration needs
  and cannot state: the entry is written once the *prerequisites* are met, and
  nothing is listening until that command is run.
* **`--provision`** passes through to that `debug-config` run and means *make
  this target debuggable*. It is the answer to the commonest empty-handed case:
  a Python target whose image has no debugpy. The injection bootstrap runs
  inside the target's interpreter, so debugpy has to be importable *there*;
  without it no configuration can be emitted and `--open` writes no
  `launch.json` at all.

  It does both halves. The seat installs debugpy into the target with `uv`,
  resolved for the *target's* Python version rather than the seat's, and then
  starts the debugpy server inside the app — so the emitted configuration has
  something to connect to and F5 works when the command finishes. The two are
  one flag because issue #45 ordered these mutations and put *installing* above
  *injecting*: a run already allowed the larger one has been allowed the
  smaller, and asking twice left the configuration emitted, the port closed and
  the first F5 at `ECONNREFUSED`.

  It is opt-in and stays so. It writes ~15 MB into the workload's writable
  layer, on an ephemeral-storage budget the seat shares with the workload and
  **cannot reserve** — an ephemeral container may not declare `resources`
  (report 3.9); it needs egress from the pod, since uv resolves and downloads
  from an index; starting the server ptraces the app, so it stops answering
  probes for the few seconds that takes (~3 s measured, against the deadlines
  the report above prints); and a restart of the target container ends the
  debugging. The two halves do not expire together: the **server** never
  survives a restart, being a live process in the container that died, while the
  **install** survives one where `--provision-dest` names a volume mounted into
  the target — an `emptyDir` is pod-scoped and outlives a container — and not at
  the default `/opt/podbench-debugpy`, which is the container's own writable
  layer. Either way the next step is `--provision` again, since without the
  server nothing is listening. Installing debugpy into the app image, or baking
  `debugpy.listen()` into the app, is the durable answer.

  A **bare** `debug-config` still only prints the injection command. That is
  `injection_command`'s rule unchanged — authoring a `launch.json` may not
  ptrace the workload on its own — and `--provision` is what revokes it.

  Without `--open` it is **refused**, not ignored: there is no `debug-config`
  run for it to change, and a flag that reads as a promise to mutate the
  workload must not quietly decline to keep it. Where the target's rootfs is
  read-only the write fails with `EROFS` — the mount flag lives in the target's
  mount namespace — and the seat's own `podbench debug-config --provision-dest`
  is what points it at a writable volume instead.

  Each extension unpacks into the seat's `~/.vscode-server`, which in Observe
  mode is on the **workload's** ephemeral-storage budget: a server plus one
  extension measured 1215 MiB live, and `ms-vscode.cpptools` alone is 330 MiB.
  That is why only the flavour's own extensions are installed — though "only"
  is the *list*, not the outcome: VS Code resolves each entry's dependencies,
  and `ms-python.python` is an extension pack, so a Python target also lands
  `ms-python.vscode-pylance` (117 MiB) and `ms-python.vscode-python-envs`. The
  excludes are written for what actually arrives, which is why
  `python.analysis.exclude` is among them.
* Exit code is `0` for any seat that lands, including a degraded one; `2` for a
  real error.

### `ssh-config`

Regenerate the ssh stanza for a seat that is already running, without touching
the pod.

```

 Usage: podbench ssh-config [OPTIONS] [POD]

 regenerate the ssh stanza for an existing session

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   POD      <str>  pod/NAME, a bare NAME, or any substring of one. Anything that does not settle  │
│                   on a single pod lists the namespace and asks                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --identity              KEY        ssh key to authorise in the seat and name in the generated    │
│                                    stanza                                                        │
│                                    [default: ~/.ssh/id_ed25519]                                  │
│ --ssh-user              NAME       login name to put in the stanza                               │
│ --host-alias            NAME       ssh Host name for the seat                                    │
│ --print-config                     print the ssh stanza instead of writing it to the config dir  │
│ --no-prompt                        never ask which pod: an ambiguous or missing POD is refused   │
│                                    with the candidates instead. Already implied when stdin is    │
│                                    not a tty                                                     │
│ --namespace     -n      NAMESPACE  namespace (default: the kubeconfig context's own)             │
│ --context               NAME       kubeconfig context                                            │
│ --kubectl               BIN        kubectl binary to use [default: kubectl]                      │
│ --config-dir            DIR        where the generated ssh config and known_hosts live (default  │
│                                    ~/.podbench)                                                  │
│ --help                             Show this message and exit.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Fails if there is no running podbench container in the pod.

### `status`

Every podbench container in one pod, including dead ones whose names remain
burnt.

```

 Usage: podbench status [OPTIONS] [POD]

 the podbench containers in one pod and what each supports

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   POD      <str>  pod/NAME, a bare NAME, or any substring of one. Anything that does not settle  │
│                   on a single pod lists the namespace and asks                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --no-prompt                      never ask which pod: an ambiguous or missing POD is refused     │
│                                  with the candidates instead. Already implied when stdin is not  │
│                                  a tty                                                           │
│ --namespace   -n      NAMESPACE  namespace (default: the kubeconfig context's own)               │
│ --context             NAME       kubeconfig context                                              │
│ --kubectl             BIN        kubectl binary to use [default: kubectl]                        │
│ --no-probe                       do not run capreport in the seats; every verdict then reads     │
│                                  `not probed`, which is what this listing has to say when it has │
│                                  measured nothing                                                │
│ --config-dir          DIR        where the generated ssh config and known_hosts live (default    │
│                                  ~/.podbench)                                                    │
│ --help                           Show this message and exit.                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### `list`

The same, across the namespace.

Both verbs end each pod's block with the ssh alias to connect with, read out of
the stanza in `--config-dir` rather than derived from the pod's name: `attach
--host-alias NAME` is recorded nowhere in the cluster, so a derived alias would
be wrong for whoever used the flag. When there is no stanza on this machine —
the seat was landed from another one — they say so and name `ssh-config`, the
verb that writes the missing half. Neither verb writes anything.

```

 Usage: podbench list [OPTIONS]

 every pod in the namespace carrying a podbench container

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --namespace   -n      NAMESPACE  namespace (default: the kubeconfig context's own)               │
│ --context             NAME       kubeconfig context                                              │
│ --kubectl             BIN        kubectl binary to use [default: kubectl]                        │
│ --config-dir          DIR        where the generated ssh config and known_hosts live (default    │
│                                  ~/.podbench)                                                    │
│ --help                           Show this message and exit.                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### `dev`

Author a sacrificial dev pod from a target's spec — Iterate mode.

```

 Usage: podbench dev [OPTIONS] [POD]

 create or delete the dev pod (runs on the laptop)

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   POD      <str>  the pod to clone, or the dev pod to delete: pod/NAME, a bare NAME, or any      │
│                   substring of one. Anything that does not settle on a single pod lists the      │
│                   candidates and asks — every pod in the namespace, or with --delete only the    │
│                   dev pods                                                                       │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --namespace     -n      NAMESPACE  namespace (default: the kubeconfig context's own)             │
│ --context               NAME       kubeconfig context                                            │
│ --container             NAME       container to take over                                        │
│ --name                  NAME       dev pod name (default: POD-podbench)                          │
│ --image                 REF        podbench image (default: the image built from this launcher's │
│                                    version)                                                      │
│ --port                  PORT       the port your app serves                                      │
│ --take-traffic                     copy the origin's labels so the dev pod shares Service        │
│                                    traffic with it. Off by default: joining a production Service │
│                                    silently is a foot-cannon                                     │
│ --cutover               SERVICE    point SERVICE exclusively at the dev pod, recording its       │
│                                    selector for an exact restore at teardown                     │
│ --identity              KEY        ssh key to authorise in the sidecar and name in the generated │
│                                    stanza                                                        │
│                                    [default: ~/.ssh/id_ed25519]                                  │
│ --config-dir            DIR        where the generated ssh config and known_hosts live (default  │
│                                    ~/.podbench)                                                  │
│ --host-alias            NAME       ssh Host name for the sidecar                                 │
│ --delete                           tear the dev pod down                                         │
│ --timeout               SECONDS    seconds to wait [default: 120.0]                              │
│ --dry-run                          print the authored pod instead of creating it                 │
│ --no-prompt                        never ask which pod: an ambiguous or missing POD is refused   │
│                                    with the candidates instead. Already implied when stdin is    │
│                                    not a tty                                                     │
│ --help                             Show this message and exit.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Notes:

* `POD` is resolved exactly as `attach` resolves it, through the same helper:
  `pod/NAME`, a bare `NAME`, a substring of one, or nothing at all, in which
  case the candidates are listed and you are asked. A substring that settles on
  one pod is echoed rather than assumed, and `--no-prompt` — or a stdin that is
  not a tty — turns the question into a refusal that lists the candidates. With
  `--delete` the candidates are the dev pods alone, since nothing else in the
  namespace is something it would agree to delete.
* The namespace comes from your kubeconfig context when `-n` is not given, the
  same as everywhere else. It used to mean the literal namespace `default`
  here, which is the fix in issue #44.
* The origin pod is never modified, and a pod podbench itself authored is
  refused as one: cloning a dev pod would copy its sidecar in as an ordinary
  container. Name the workload it was made from.
* `--take-traffic` and `--cutover` are the only ways the dev pod sees Service
  traffic, and both are explicit. `--cutover` uses a JSON *replace* patch — a
  merge patch would union the selector maps and quietly leave the original pod
  serving half the requests.
* `--identity` is authorised inside the sidecar and named as the stanza's
  `IdentityFile`, exactly as for `attach` — same flag, same default, same
  refusal when the public key is missing. It is read **before** anything is
  created, because the key reaches the sidecar through its environment and a
  container's environment cannot be changed after the pod exists.
* The generated stanza is written to the same `config.d` file `attach` would
  use for that pod, and the summary ends with the alias to `ssh`. The
  `kubectl exec` line is printed as well: it works when ssh does not.
* `--delete` restores any borrowed selector, removes the pod, then removes the
  stanza and the `known_hosts` entry it wrote. `attach` deliberately leaves its
  stanza in place — that seat is reconnectable while its pod lives, this one is
  not.
* `--delete` takes either the dev pod's name or its origin's, since one derives
  from the other, and anything it has to search for is searched for among the
  dev pods alone. That is what keeps teardown scriptable: a reference matching
  no dev pod — including one that still matches the origin's own replicas — is
  a teardown that has already happened, so it exits 0 saying "nothing to
  delete" rather than refusing an ambiguity it could not have acted on. A dev
  pod created with `--name` is found the same way, by its label.
* `--dry-run` is the best available description of what this mode does. It
  still needs a readable public key, so that what it prints is what `dev` would
  actually create.

### `hotfix`

Durable in-place fixes: a venv on a ReadWriteOnce claim, every change a git
commit, and a `status` that will not let a hotfixed pod go unnoticed.

:::{warning}
Hotfix mode has never been run against a cluster. It is unit-tested only.
:::

The seat must mount the claim at the application's own mountPath, since that is
how `hotfix` reads `pyvenv.cfg` and runs `git` against the checkout. Land it that
way with `attach --mount`:

```
podbench attach myapp-0 --mount myapp-venv --new
```

`--local` remains the alternative when `hotfix` is run from a terminal inside the
seat, where the claim is already in this process's own mount namespace.

```

 Usage: podbench hotfix [OPTIONS] COMMAND [ARGS]...

 Durable in-place fixes: a venv on a claim, every change a commit, and a status command that will
 not let a hotfixed pod go unnoticed.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --print-values              emit the helm values an application's chart needs, and exit          │
│ --app                 NAME  application name, for --print-values                                 │
│ --venv-path           PATH  the application's venv path, for --print-values                      │
│ --size                SIZE  claim size, for --print-values [default: 2Gi]                        │
│ --app-image           REF   image the seeding initContainer runs, for --print-values             │
│                             [default: <the application's own image>]                             │
│ --uid                 UID   the application container's uid, for --print-values                  │
│                             [default: <the application's runAsUser>]                             │
│ --gid                 GID   the application container's gid, for --print-values                  │
│                             [default: <the application's runAsGroup>]                            │
│ --help                      Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────────────────────────╮
│ init         verify the seeded claim, clone the source, editable-install                         │
│ apply        commit the change on the claim and roll the workload                                │
│ status       every hotfixed pod in the namespace, and its drift                                  │
│ consolidate  push the claim's checkout as a branch for the rebuild                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

| Sub-verb | Does |
|---|---|
| `init --repo URL --venv PATH TARGET` | verify the claim was seeded from the image's venv, clone the source onto it, editable-install, record the base commit |
| `apply -m MSG --venv PATH TARGET` | commit the checkout, reinstall if packaging metadata changed, write the manifest, annotate, roll the workload |
| `status` | every hotfixed pod in the namespace, its drift, and what is wrong with it |
| `consolidate --branch B --venv PATH TARGET` | push the checkout as a branch and print the retirement checklist |

`TARGET` is `pod/NAME`, `deployment/NAME` or `statefulset/NAME`. Shared flags:
`--venv` (the mountPath the claim is mounted at, which *is* the application's
venv path), `--container`, `--seat`, `--local`, `--author`.

Notes:

* **Single replica only**, refused otherwise: the claim is `ReadWriteOnce`, so a
  second replica either fails to schedule or races on one checkout.
* `init` **verifies** the seed, never performs it. Once the claim is mounted over
  the venv path the image's own venv is hidden in every container, so the copy
  can only happen in an initContainer — which is what `--print-values` emits.
* The editable install runs in the **application** container, not the seat: the
  venv is shared but its interpreter is not. `--no-install` skips it.
* `consolidate` does not open a PR; it prints the `gh pr create` line.
* `status` exits **1** when any pod needs attention, so "no unretired hotfixes" is
  a testable shutdown assertion.

```
$ podbench hotfix --print-values --app myapp --venv-path /opt/venv
```

emits both halves of the chart wiring: `hotfixVenv` values for the podbench
release, and the volume, volumeMount and seeding initContainer for the
application's own chart.

---

## In-pod verbs

### `capreport`

Name the mechanism that denies ptrace in this container. The launcher runs it
automatically after every attach; run it yourself when something changes.

```

 Usage: podbench capreport [OPTIONS] [PID]

 Name the mechanism that denies ptrace in this container.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   [PID]      <int>  target pid; discovered from the target container id if omitted               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --container-id        ID  target container id (default: $PODBENCH_TARGET_CID)                    │
│ --json                    emit the stable JSON form instead of the human report                  │
│ --help                    Show this message and exit.                                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

**Exit codes are the interface**, so a shell script can branch without parsing:

| Code | Verdict |
|---|---|
| `0` | live attach available |
| `10` | read-only inspection of the target (rootfs, `maps`, `environ`); no live attach |
| `15` | launch-only: no read-only inspection of the target, but `podbench dbg --launch` works |
| `20` | neither; the seat itself still works |

`10` says nothing about gdb-launch: the two are measured separately, and a seat
whose own forked child refuses to be traced can still read all three gated paths
at the target's uid. `child_attach_ok` in the JSON is the only thing that claims
that rung.

It reads `CapEff`/`CapBnd`/`CapAmb`, `Seccomp`, `NoNewPrivs`, the AppArmor
profile of both itself and the target, and `yama/ptrace_scope`; then runs a
scratch `PTRACE_ATTACH` on its own forked child (always permitted by Yama, so a
failure there is structural) and a live attach on the target; then a six-path
`/proc` read matrix.

The live attach is a `PTRACE_SEIZE`, which takes the same
`PTRACE_MODE_ATTACH_REALCREDS` check as `PTRACE_ATTACH` and leaves the tracee
**running**, so probing costs the workload no pause. The report says which
primitive was used and what it cost — `attach_method` in the JSON, and a
`workload pause` line in the human form, normally `none`. `PTRACE_ATTACH` is the
fallback where the kernel answers `EIO` (pre-3.4), and that one does stop the
workload for as long as reaping the stop and detaching takes. Yama is a **node-level** knob that differs by kernel
flavour, so this must be re-run per pod and never cached cluster-wide.

Only three of those six paths decide the `10`. `root`, `maps` and `environ` take
`PTRACE_MODE_READ`; `cmdline`, `status` and `fd` need no permission at all and are
therefore readable on a pod where nothing else is, so they are reported and never
counted as evidence. The JSON form carries both — the full matrix as `proc_reads`,
and the decision as `reads_ok`. The matrix comes back alphabetical rather than
grouped — `podbench capreport --json` emits with `sort_keys`, so the two halves
cannot disagree about ordering:

```
$ podbench capreport --json | jq '{verdict, reads_ok, proc_reads}'
{
  "verdict": "launch_only",
  "reads_ok": false,
  "proc_reads": {
    "cmdline": true,
    "environ": false,
    "fd": true,
    "maps": false,
    "root": false,
    "status": true
  }
}
```

A **DEBUGGERS** block sits beside the verdict, listing what the image actually
ships — so what `debug-config` emits and what the seat can run cannot drift
apart:

```
DEBUGGERS (what this image ships)
  yes  gdb: /usr/local/bin/gdb
  no   lldb: absent (CodeLLDB brings its own to the remote, so this is optional)
  no   dlv: absent (delve, for Go targets)
  yes  gdb-podbench: /usr/local/bin/gdb-podbench (`gdb` on PATH is the shim)
  yes  debugpy: /opt/podbench/debugpy (attach helpers: attach_linux_amd64.so)
```

Two lines say more than yes/no on purpose. `gdb-podbench` reports whether a bare
`gdb` *resolves* to the wrapper, because that is what a tool shelling out to
`gdb --pid` will run; and debugpy lists its attach helpers by name, because on
arm64 the package is present and the mechanism is not.

### `pids`

List the processes in the pod's shared PID namespace and say which container
owns each.

```

 Usage: podbench pids [OPTIONS]

 List the processes in this pod's shared PID namespace, and say which container owns each one.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --container-id        ID  target container id (default: $PODBENCH_TARGET_CID)                    │
│ --targets                 list only the target container's processes                             │
│ --json                    emit the stable JSON form instead of the table                         │
│ --help                    Show this message and exit.                                            │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Attribution substring-matches the target's container runtime ID against
`/proc/<pid>/cgroup`. Without one, every other container's processes look like
targets — the JSON carries `attribution` and `warning` fields, and a consumer
that ignores them is reading a guess as a fact.

### `dbg`

gdb, with sysroot, source path and auto-load path set in the one order that
produces a correct backtrace.

```

 Usage: podbench dbg [OPTIONS] [PID]

 Run gdb against a process in another container of this pod, with the sysroot, source path and
 auto-load path set in the order that produces a correct backtrace.

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   [PID]      <int>  pid to attach to; discovered from the container id if omitted                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --container-id                    ID       target container id used to discover the pid          │
│                                            (default: $PODBENCH_TARGET_CID)                       │
│ --source-dir                      DIR      extra source directory, wired with gdb's `directory`. │
│                                            debuginfod serves symbols but no sources on Debian,   │
│                                            so this is how source text outside the target's       │
│                                            rootfs is found. Repeatable                           │
│ --no-debuginfod                            do not enable debuginfod (it needs ca-certificates    │
│                                            and network). Library symbols are fetched after the   │
│                                            attach, with the target stopped, so this is the flag  │
│                                            to reach for when the pause is what costs             │
│ --run                                      with --launch, start the program immediately          │
│ --dry-run,--print-commands                 print the generated gdb commands and exit, without    │
│                                            probing or starting gdb                               │
│ --print-exec-file                          print the one path to give gdb's `file` command and   │
│                                            exit. It is the target's own path under the sysroot   │
│                                            unless this container has a file of its own at that   │
│                                            path, in which case gdb would read ours (issue #90)   │
│                                            and a copy is staged instead. What `gdb-podbench`     │
│                                            calls for a third-party `gdb --pid`                   │
│ --launch                          PROGRAM  debug a program gdb starts itself instead of          │
│                                            attaching. Needs no capability. Consumes the rest of  │
│                                            the command line, so put other flags first            │
│ --help                                     Show this message and exit.                           │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

`--launch` consumes the remainder of the command line, so any other flag must
come first. See [Debug with gdb](../how-to/debug-with-gdb.md).

`--print-exec-file` exists because `file /proc/<pid>/root<exe>` is not always
the right answer. gdb canonicalises the exec file's name, the kernel resolves
`/proc/<pid>/root` to `/`, and a seat that has a file of its own at the target's
path then reads *ours* — a `.gnu.version_r invalid entry` if the two builds
differ enough, and the wrong symbols in silence if they do not. Where that
happens `dbg` copies the target's binary somewhere nothing shadows it, says so
in one line, and points `file` at the copy; `--dry-run` prints the same command
it would run, so the sequence stays pasteable.

### `debug-config`

The VS Code debug configuration for this seat, written the way `attach` writes
the ssh stanza — so nobody hand-fills a pid, a sysroot-prefixed `program`, a
setup ordering or a path mapping, each of which fails *silently* when wrong.

Which debugger is not one choice but three: **language x mode x architecture**.
Every configuration that applies is emitted at once, each named for its flavour,
so `launch.json`'s list and VS Code's own dropdown become the choice. Every
flavour that does *not* apply gets a sentence naming the mechanism.

```
                                                                                                    
 Usage: podbench debug-config [OPTIONS] [PID]                                                       
                                                                                                    
 Write the VS Code debug configuration for this seat: one entry per debugger flavour that applies,  
 with the pid, the sysroot-prefixed program path and the mode's path mappings already filled in.    
                                                                                                    
╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   [PID]      <int>  pid to attach to; discovered from the container id if omitted                │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --container-id            ID                        target container id used to discover the pid │
│                                                     (default: $PODBENCH_TARGET_CID)              │
│ --flavour                 <gdb|lldb|delve|debugpy>  emit only this debugger flavour, and say why │
│                                                     if it cannot be emitted. Repeatable; the     │
│                                                     default is every flavour that applies        │
│ --mode                    <observe|dev>             override the detected mode. Observe attaches │
│                                                     to another container and needs path          │
│                                                     mappings; dev launches in this one and must  │
│                                                     not have any                                 │
│ --port                    PORT                      pin the debugpy port. The default looks for  │
│                                                     an existing server on 5678 and lets the      │
│                                                     kernel choose a free port for one            │
│                                                     --provision starts, so two seats on a node   │
│                                                     cannot collide. Always on 127.0.0.1: the     │
│                                                     seat shares the target's network namespace   │
│ --program                 PATH                      the target's binary as its own rootfs spells │
│                                                     it, when /proc/<pid>/exe cannot be read. It  │
│                                                     is prefixed with the sysroot here, so do not │
│                                                     prefix it yourself                           │
│ --source-dir              DIR                       extra source directory in *this* container,  │
│                                                     wired with gdb's `directory`. Repeatable     │
│ --source-map              FROM=TO                   map a DWARF compilation directory (`info     │
│                                                     source` prints it) onto a readable path.     │
│                                                     Repeatable                                   │
│ --no-debuginfod                                     do not enable debuginfod (it needs           │
│                                                     ca-certificates and network). Library        │
│                                                     symbols are fetched after the attach, with   │
│                                                     the target stopped, so this is the flag to   │
│                                                     reach for when the pause is what costs       │
│ --lldb                                              shorthand for --flavour lldb                 │
│ --provision                                         make the target debuggable: install debugpy  │
│                                                     with uv when it cannot import one, then      │
│                                                     start the server inside it so the emitted    │
│                                                     configuration has something to connect to.   │
│                                                     Mutates the workload: ~15 MB of shared       │
│                                                     ephemeral storage, needs egress from the     │
│                                                     pod, ptraces the app for a few seconds, and  │
│                                                     no restart survives it                       │
│ --provision-dest          PATH                      where --provision installs it, as the        │
│                                                     *target* spells it, and the one extra path   │
│                                                     searched for the target's copy. Point it at  │
│                                                     a writable mount when the target's rootfs is │
│                                                     read-only                                    │
│                                                     [default: /opt/podbench-debugpy]             │
│ --provision-python        X.Y                       the target's Python version for uv to        │
│                                                     resolve against, when it cannot be read from │
│                                                     the target itself                            │
│ --print-config                                      print the configuration instead of writing   │
│                                                     it, and measure nothing: this run touches no │
│                                                     workload                                     │
│ --output                  PATH                      where to write it (default:                  │
│                                                     ./.vscode/launch.json)                       │
│ --help                                              Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

#### The three axes

| axis | how it is decided | what it changes |
|---|---|---|
| language | `/proc/<pid>/exe` and `argv[0]` for an interpreter; the target's ELF sections for Go (`.gopclntab`) | which adapter: `cppdbg`, CodeLLDB, the Go extension, debugpy |
| mode | whether the target shares this container's **mount namespace** — a `podbench dev` pod relaunches the app from the seat, so its process is on this side | attach vs launch, and whether `pathMappings` is populated **or empty** |
| architecture | the target *binary*'s `e_machine`, not the node label | whether debugpy's attach-to-pid exists at all |

`pathMappings` is the field with no error message: get it wrong and breakpoints
simply never bind. In Observe mode the editor sees the source through
`/proc/<pid>/root` and the debuggee reports its own path, so a mapping is
required; in dev mode both are the same inodes and the mapping must be empty.
`127.0.0.1` is right in both, because the seat and the app share the pod's
network namespace — no port-forward, no tunnel.

#### When a flavour cannot be emitted

The refusal names the mechanism, in `capreport`'s house style, and lists *every*
unmet prerequisite rather than only the first — fixing one to meet the next wall
is the experience this replaces:

```
debug-config: debugpy unavailable: no debugpy in this seat to drive the injection
debug-config:   also: debugpy is not importable by the target: the bootstrap runs
                inside the target's interpreter, and debugpy injects a dlopen of the
                path the *driver* sees, which the target's mount namespace does not have
debug-config:   also: no sysroot-aware gdb on PATH: debugpy shells out to a bare
                `gdb --nx --pid`, which reads this seat's libraries for the target's process
```

On arm64 the architecture prerequisite is promoted to the headline, because it
is the only one with no remedy anywhere: debugpy ships `attach_linux_amd64.so`
alone and publishes no aarch64 Linux wheel, so there is nothing to install.
`debugpy.listen()` baked into the app is pure Python and works on any
architecture — as does `podbench dev`.

The helper is looked for in the tree the **injection** loads, and the message
names that tree: `PYTHONPATH` points the driver at the *target's* copy whenever
there is one, so the seat's copy answers a different question. On amd64 the
helper is in every wheel, so a tree without one is an incomplete install with a
re-install to fix it, not the architecture.

A seat the kernel refuses `/proc/<pid>/root` is refused **before** any of that,
and the refusal says so on its own:

```
debug-config: debugpy unavailable: this seat may not read /proc/597/root, which the
              kernel gates on the same ptrace_may_access() credentials an attach takes
              - so nothing in the target's filesystem could be searched, and
              PTRACE_MODE_ATTACH is strictly stronger than the read, so the injection's
              `gdb --pid` would be refused too. Not the capability: the credentials
debug-config:   `podbench capreport 597` names which of the four mechanisms says no;
                where it is a uid mismatch, `podbench attach --max-rung full` lands a
                seat that runs as root
```

Nothing about the target's own filesystem is claimed beside it. The search for
its debugpy stats through that same directory, so "debugpy is not importable by
the target" would be this one refusal reported a second time — with a remedy,
`--provision`, that writes through the very path the kernel just refused. It is
the whole flavour that declines and not the verb: any other candidate in the
pod still gets its configurations, and the file is still written.

#### Installing debugpy into the target (`--provision`)

A stock Python image has no debugpy, and debugpy's pid-injection needs it
importable **by the target**: the bootstrap runs in the target's interpreter,
and the path debugpy injects is the one the *driver* sees, so
`/proc/<pid>/root/...` is the only spelling valid in both mount namespaces. The
seat can supply it — it ships `uv`, live attach already requires `runAsUser: 0`,
and `/proc/<pid>/root` is the target's own filesystem — so the refusal prints
the command rather than asking for an image rebuild:

```
uv pip install --no-cache --python-version 3.12 --target /proc/1/root/opt/podbench-debugpy debugpy
```

`--python-version` is the load-bearing flag, and the reason this is a uv install
and not a copy of the seat's tree. The image installs debugpy for the seat's own
interpreter, so its copy carries `pydevd_cython.cpython-311-*.so` alone; put
that in a 3.12 target and the accelerator is skipped and pydevd falls back to
pure Python **silently**. uv resolves for an interpreter it is not running, and
a 3.12 target then loads the cp312 accelerators out of the provisioned
directory. Copying the seat's tree is the fallback for a pod with no egress, not
the route.

`--provision` runs it for you, and is **opt-in on purpose**. The injection
command is printed rather than run because it ptraces the workload; writing
~15 MB into the workload's own writable layer is the larger mutation of the two,
and a verb that authors a configuration file has to stay safe to re-run. Behind
the flag it probes the destination for writability first and names what refuses:

| cost | why it cannot be ignored |
|---|---|
| network egress from the pod | uv resolves and downloads from an index; a locked-down namespace refuses it, and the fallback is a copy of the seat's tree with the accelerator caveat above |
| no restart survives it | neither the install nor the injection — a restart brings back the app image exactly as built |
| ~15 MB of ephemeral storage | on a budget the seat **shares with the workload and cannot reserve**, because an ephemeral container may not carry `resources` |

`--no-cache` is what keeps that last number true. uv downloads into its cache in
the *seat's* writable layer and materialises from there into `--target`; the two
are different filesystems, so no hardlink is possible and both copies would
exist — against the one pod-level budget. The install is also echoed before it
runs, because uv's output is captured for the failure message and a resolve
against an unroutable index is otherwise silence indistinguishable from a hang.

`readOnlyRootFilesystem: true` is the one genuinely new precondition, and it is
not readable from the seat: the mount flag lives in the target's mount
namespace, so it arrives as `EROFS` on the write. Uid 0 in the seat carries
`CAP_DAC_OVERRIDE`, so the target's own uid and file modes are never the
explanation — a `permission denied` here is the `/proc/<pid>/root` traversal,
which takes `PTRACE_MODE_READ` and is refused to a root seat with no
`CAP_SYS_PTRACE` (report 3.11), or an LSM denying the cross-container write.
Where the rootfs is read-only there is usually still a writable
`emptyDir` or tmpfs in the pod — `--provision-dest` puts the copy there instead,
and is also the extra path `debug-config` searches on a later run.

At that destination "already installed" is not a refusal: an installed tree
records no version, and `--provision-dest` is searched **first**, so a copy
resolved for the wrong `X.Y` would otherwise shadow the target's own correct one
for the pod's lifetime. Re-running installs over it. The target's own
site-packages is never written over — only supplemented, when its copy is
missing the architecture helper.

Only **Observe** mode needs any of this. A `dev` pod relaunches the app as the
seat's own child in this container, where debugpy is an ordinary workspace-venv
dependency; Hotfix mounts the same PVC over the venv at the same path in both
containers. `--provision` says so rather than installing anyway.

`miDebuggerPath` names `/usr/local/bin/gdb-podbench`, never `/usr/bin/gdb`:
cpptools launches gdb inheriting its own extension directory as a working
directory, which VS Code deletes on extension update, and gdb's libpython then
dies in `getcwd()` during startup with no signal name. `--source-map /` is
refused rather than emitted — gdb re-applies a root substitution on display and
the editor is handed `/proc/<pid>/root/proc/<pid>/root/...`.

Re-running replaces its own entries by name and leaves a hand-written
configuration beside them untouched — which is why every generated name carries
its flavour. A `launch.json` it cannot parse — VS Code permits comments, `json`
does not — is refused rather than rewritten. See
[Debug with gdb](../how-to/debug-with-gdb.md).

### `dev-bootstrap`

Populate the dev pod's workspace: clone, sync, editable install.

```

 Usage: podbench dev-bootstrap [OPTIONS]

 clone, sync and editable-install (runs in the pod)

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ *  --repo               URL      git URL to clone [required]                                     │
│    --ref                REF      branch, tag or commit to check out                              │
│    --dir                DIR      checkout directory (must be in this container)                  │
│                                  [default: /workspace/src]                                       │
│    --python             VERSION  CPython version for uv to use                                   │
│    --no-sync                     skip uv sync --frozen                                           │
│    --no-editable                 skip uv pip install -e .                                        │
│    --help                        Show this message and exit.                                     │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

"must be in this container" is enforced, not advisory: a checkout under
`/proc/<pid>/root/...` is refused, because an editable install whose `.pth`
names a path in another mount namespace is **silently ignored** by `site.py`.

### `run`

Relaunch the workload from the debug container and verify that your child owns
the port.

```

 Usage: podbench run [OPTIONS] [COMMAND]...

 relaunch the app and verify it (runs in the pod)

╭─ Arguments ──────────────────────────────────────────────────────────────────────────────────────╮
│   [COMMAND]...      <str>  the command, after `--`                                               │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ *  --port             PORT     the port it must serve [required]                                 │
│    --workspace        DIR      workspace root [default: /workspace]                              │
│    --dir              DIR      working directory (default: workspace)                            │
│    --timeout          SECONDS  seconds to verify [default: 15.0]                                 │
│    --help                      Show this message and exit.                                       │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Exits non-zero when the port is not owned by the process it started — a socket
poll alone gives a false PASS, and `SO_REUSEPORT` will otherwise split traffic
between old and new code with nothing in any log to say so.

### `stop`

Stop it, by recorded pid.

```

 Usage: podbench stop [OPTIONS]

 stop the recorded child (runs in the pod)

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --workspace        DIR      workspace root [default: /workspace]                                 │
│ --grace            SECONDS  seconds before SIGKILL [default: 5.0]                                │
│ --help                      Show this message and exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Never `pkill -f`: under `shareProcessNamespace: true` that matches the invoking
shell and every other container's processes.

### `agent`

The debug container's PID 1. The launcher sets it as the container's command;
you should not need to run it yourself.

```

 Usage: podbench agent [OPTIONS]

 Prepare the debug container for ssh and idle as its PID 1.

╭─ Options ────────────────────────────────────────────────────────────────────────────────────────╮
│ --ensure-only                      prepare the container and exit instead of idling              │
│ --self-check                       run the startup checks and exit; non-zero if any fails        │
│ --print-host-key                   print the host public key for the launcher's known_hosts      │
│ --print-login-user                 print the login name sshd will resolve for this uid; non-zero │
│                                    with the reason on stderr when there is none                  │
│ --no-self-check                    skip the startup checks (they cost a subprocess and ~0.2 s)   │
│ --idle-interval           SECONDS  seconds between reap sweeps while idling [default: 30.0]      │
│ --help                             Show this message and exit.                                   │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
```

Every step is *ensure*, never *create*: running it twice against the same
container is normal operation. The host key, the authorized keys and the sshd
config are rebuilt from the environment or a mounted Secret on each start, which
is what makes "the ephemeral container is strictly disposable" true rather than
aspirational.

No step is fatal either. PID 1 of an unrestartable container that exits burns its
name for the pod's lifetime, so a step that cannot do its job records the reason
and the agent idles anyway — `kubectl exec` needs none of sshd. Two steps are
worth knowing by name:

* **home-dir** creates `$HOME` and the `.ssh` / `.podbench` directories in it. A
  mounted `podbench-home` arrives *empty*, and sshd creates nothing. If the
  directory is not writable the failure names `fsGroup`, which is almost always
  the cause: a projected volume is `root:root` until the pod's `fsGroup` hands it
  to the seat's group, and a seat running as the target's uid can chown nothing.
* **nss-identity** is a no-op when NSS already resolves the seat's uid — what a
  mounted `podbench-identity` achieves for an ordinary container, and it stays a
  no-op even though the projected `/etc/passwd` is read-only. In an ephemeral
  seat, which cannot be given that file at all, it appends a record to
  `/var/lib/extrausers/passwd` instead, which needs no particular gid — or to
  `/etc/passwd`, for the seats that database will not serve. It is allowed to
  fail: the agent records the reason and idles, because a container that exits
  burns its name.

`--print-login-user` is how the launcher decides whether an ssh stanza is worth
writing: the name on stdout, or exit 1 with the mechanism and, on stderr, either
the way out or the `kubectl logs` command that shows why the registration step
failed. It is a pure read and ensures nothing, so it reports the state sshd will
actually find.

`--self-check` includes the fd-2 tripwire — a `kubectl exec` round trip with a
delayed second line, which fails if anything in the path has broken the CRI exec
stream.

---

## Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `PODBENCH_IMAGE` | launcher | debug image to attach; `--image` overrides. Both override the default, which is `ghcr.io/gilesknap/podbench:` plus the launcher's own version (`main` for a dev build) |
| `PODBENCH_CONFIG_DIR` | launcher, `dev` | where the ssh config and `known_hosts` go; `--config-dir` overrides. Default `~/.podbench` |
| `PODBENCH_TARGET_CID` | `pids`, `dbg`, `capreport`, `run` | the target container's runtime ID, injected at attach time |
| `PODBENCH_SSH_PUBKEY` | agent | authorized key, injected into the seat's spec by `attach` and by `dev` |
| `PODBENCH_SSH_PUBKEY_FILE` | agent | read it from a file instead. Default mount `/etc/podbench/ssh/authorized_keys` |
| `PODBENCH_SSH_HOST_KEY` | agent | host private key, rather than minting one |
| `PODBENCH_SSH_HOST_KEY_FILE` | agent | the same from a file. Default mount `/etc/podbench/ssh/ssh_host_ed25519_key` |
| `DEBUGINFOD_URLS` | gdb, `dbg` | symbol server. The image sets `https://debuginfod.debian.net`; the seat drops it from ssh sessions when nothing answers there |
| `DEBUGINFOD_TIMEOUT` | gdb, `dbg` | seconds gdb will wait on that server, per file. The image sets `2`; gdb's own default is 90 |

sshd leaks none of its own environment to the commands it runs, so a variable
set on the debug container reaches `kubectl exec` and a shell but not an ssh
session. The agent's generated sshd config carries the ones the seat needs —
every `PODBENCH_*` except the keys, plus `PATH`, `DEBUGINFOD_URLS` and
`DEBUGINFOD_TIMEOUT` — and reports in the container's start-up log if a value
contains whitespace, which sshd's `SetEnv` cannot carry.

`DEBUGINFOD_URLS` is the one of those the agent may decide *not* to carry. It
opens a connection to that server once, at start-up, and drops the variable
from the session when nothing answers — gdb's client has nothing to query
without it, so its absence is the off switch. The reason is one line in the
container's start-up log (`kubectl logs <pod> -c <seat>`). A `kubectl exec`
session inherits the image's value regardless, where `podbench dbg
--no-debuginfod` is the same decision taken per run.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success — including a degraded seat, which is an honest outcome and not a failure |
| `1` | an Iterate-mode operation failed (`dev`, `dev-bootstrap`, `run`, `stop`); `hotfix status` found a pod needing attention; or `doctor` found something blocking an attach |
| `2` | a launcher error, a `hotfix` error, an unanswerable `POD` (see {ref}`Naming the pod <naming-the-pod>`), a `doctor` usage error, or `podbench` with no verb |
| `0` / `10` / `15` / `20` | `capreport` only: the capability verdict |
