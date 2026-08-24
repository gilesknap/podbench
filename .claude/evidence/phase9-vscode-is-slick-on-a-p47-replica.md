# `podbench vscode` is slick — proved on a p47 replica

Slice 6 of `.claude/plans/vscode-attach-is-slick.md`, run 2026-08-24 against a
**reproduction** of `bl47p-ea-fastcs-01-0` rather than the pod itself: p47 went
down for an upgrade mid-session, and its node stopped satisfying the
StatefulSet's taints. The replica lives in namespace `podbench-p47` on the k3s
test bed and is built from the same two sources the real one is —
`p47-services`' helm values on branch `podbench-hotfix-claim`, and a live
capture of the StatefulSet taken before the API went away.

Launcher `0.7.3.dev50+g75e5ef79a` (working tree of `vscode/is-slick`), seat
`0.8.1.dev7+gfdc3dfd06` from
`ghcr.io/gilesknap/podbench:0.8.1-beta.1-vscode-is-slick`, digest
`sha256:007e256b…60bb9d` confirmed against an anonymous ghcr manifest request
answering a real 200 rather than a masked 404.

## 1 The checklist

| # | the plan asked | result |
|---|---|---|
| 1 | the first run lands a seat — no 409, no re-run | **pass** |
| 2 | the resize is announced when it happens, not only in the report | **pass** |
| 3 | the window opens with no "no Python found" popup | **pass** |
| 4 | `git status` in the seat answers, and `.podbench-debugpy` is invisible | **pass** |
| 5 | `git status` on the claim is clean except `.vscode/launch.json` | **partial** — §4 |
| 6 | a breakpoint in `controllers.py` binds and is hit | **pass** |
| 7 | a second identical run leaves the port answering, memory unchanged | **partial** — §5 |
| 8 | podbench's own adapter is absent from the `notes` row | **pass** |
| 9 | `restartCount` still 0 on both application containers | **pass** |

Nothing needed a manual repair, which was the slice's own falsification
condition. Two rows are qualified rather than ticked, and both are below.

## 2 The measurement that took four runs to obtain

The first four runs **did not resize at all**, and the report said why in a line
that reads like a bug and is not one:

```
memory      no pod memory limit, so no ceiling for the seat to share
```

The pod's *application* container is capped at 256Mi. Its sidecar,
`temp-controller-simulator`, declares `resources: {}` — and a pod's memory
ceiling is the sum of its containers' limits, so one uncapped container makes
the pod uncapped. **podbench was right**: there was no ceiling to raise, so it
raised nothing.

p47 differs by one object the replica did not have. Diamond's namespace carries
a `LimitRange`, which defaults a limit onto the sidecar and gives the pod a real
ceiling. Adding the equivalent — `default.memory: 256Mi`,
`maxLimitRequestRatio.memory: 10` — changed the result immediately:

```
WARNING  resized bl47p-ea-fastcs-01 to memory 615Mi/6Gi; restore it on
         detach. memory request raised to 615Mi alongside the limit:
         this namespace's LimitRange caps limit/request at 10, and
         raising a limit on its own only ever widens that ratio
```

`6144/615 = 9.99` against a cap of 10 — the same arithmetic the `k3s-test-bed`
skill records from a Diamond pod resized on 2026-08-21.

**This is worth keeping**: a bench without a `LimitRange` silently exercises a
*different branch* of the resize decision, and the run looks fine either way.
The reproduction was flagged as missing a LimitRange when it was built; the
consequence was larger than expected.

### Slice 1 and D5

That line is **line 1 of stdout**, before the `seat` row and before the report —
the mutation announced by the path that made it, which is what D5 asked for. The
same ordering holds for an explicit `podbench attach --resize 6Gi`.

The seat landed on the **first** run, which is the checkbox. **The 409 itself did
not occur**, so the retry added in `add_ephemeral_container` was not exercised
here — a resourceVersion conflict is a race against the kubelet's writeback and
does not reproduce on demand. It stays covered by the injected-runner unit tests
only, and that should be said plainly rather than implied by a green run.

## 3 Slices 3 and 4, which are unambiguous

`.vscode/` after six runs, in the tree `vscode` opened:

```
-rw-rw-r-- 1 podbench 37887   81 10-02_11:56 extensions.json
-rw-rw-r-- 1 podbench 37887 6224 08-24_14:20 launch.json
-rw-rw-r-- 1 podbench 37887  330 10-02_11:56 settings.json
-rw-rw-r-- 1 podbench 37887  399 10-02_11:56 tasks.json
```

Three files still carry the project's own October timestamps. **podbench wrote
exactly one file**, and the JSONC merge kept the project's comments and its two
pre-existing configurations. That is D1b.

The seat's machine settings carry the target's interpreter, and — the half that
actually answers #219 — the window *resolved* it:

```
$ vsc.py eval 'return vscode.workspace.getConfiguration("python").get("defaultInterpreterPath")'
/podbench/app/.python/cpython-3.11.13-linux-x86_64-gnu/bin/python3.11
```

Slice 4 has a clean before and after, because the claim was first populated by a
`hotfix init` seat running the **released** image. On that seat:

```
$ git -C /podbench/app log --oneline -1
fatal: detected dubious ownership in repository at '/podbench/app'
    → exit 128
```

On the branch-image seat, the same command answers, and 18 MB of provisioned
debugpy is invisible to it:

```
$ git -C /podbench/app status --porcelain     → rc=0
 M .vscode/launch.json
?? .podbench-hotfix.json
$ du -sh /podbench/app/.podbench-debugpy       → 18M
```

The mechanism, read out of the seat: `/etc/gitconfig` baked with
`core.excludesFile = /etc/podbench/gitignore` and `include.path =
/tmp/podbench-gitconfig`; the agent's include carrying `directory =
"/podbench/app"` — the claim's real mountPath, quoted; the excludes file
carrying `.podbench-debugpy/`. Nothing was written inside the user's repository.

## 4 The one thing `git status` still shows

`?? .podbench-hotfix.json`. It is podbench's own file, untracked in the user's
checkout, and it is **not** in the baked ignore file — which lists only
`.podbench-debugpy/`.

It is authored by `hotfix init` rather than by the `vscode` run, so it is
outside what slice 4 set out to fix, and there is a real argument that the
hotfix marker *should* be visible. But slice 4's falsification condition is
written as "anything podbench created other than the `launch.json` change", and
by that wording this is a miss. Whether to add it to the excludes is a decision,
not a bug fix, and is left open.

## 5 Slice 2: the gate fired, the discovery did not

The measured p47 defect reproduced exactly, and was then handled:

```
--provision: injected in 1.4s, but nothing is listening on 127.0.0.1:42855
(Connection refused): the injector returned 0 and left no server behind
no debug session could be started on 127.0.0.1:42855 (Connection refused),
so no debugpy configuration is written for pid 12 and whatever launch.json
already holds is kept - replacing a configuration that worked with one
naming a port nothing answers is worse than changing nothing
```

The working port `48535` survived that run. Under the old code it would have
been overwritten with `42855`, which is #218.

**The other half of slice 2 could not be exercised.** Finding a live adapter by
parentage needs a live adapter at the moment of a later run, and on this setup
there never was one: the adapter answered `initialize` in 0.01 s at injection
time and was gone before the next run started, with no `debugpy/adapter` process
anywhere in the target container. Two runs were staged specifically to catch it
— a fresh app child, provision, then an immediate re-run — and the adapter had
already exited by the second. So:

- the **handshake gate** — the fix for #218 — is proved on live evidence;
- **adapter discovery by parentage** is proved by unit tests over a synthetic
  `/proc` tree and by nothing here.

That the adapter is this short-lived is itself worth knowing, and it is
consistent with `phase8-why-the-adapter-never-answers.md`: "the injector
returned 0 and left no server behind" is not only an injection failure mode, it
is where an idle adapter *ends up*.

### And a second-order effect

`launch.json` accumulates. Six runs left seven `"port"` entries: each run
authors configurations named after the target's pids, and the artificial app
restarts in this session changed those pids, so nothing matched and nothing was
replaced. On a pod whose pids are stable this does not arise, and it is outside
every decision the plan took — but a user who restarts their app a few times
will collect dead configurations in a committed file.

## 6 The breakpoint

Set through the bridge on the seat's own file, on a `@scan(0.1)` coroutine:

```
$ vsc.py bp src/fastcs_example/controllers.py 90
uri: vscode-remote://ssh-remote%2B…/podbench/app/src/fastcs_example/controllers.py
$ vsc.py debug "podbench: attach to fastcs-example [pid 12 fastcs-example] (debugpy)"
{"started": true, "since": 3, …}
$ vsc.py stack
update_voltages          line 90   …/proc/12/root/podbench/app/src/fastcs_example/controllers.py
_run                     line 84   …/asyncio/events.py
_run_once                line 1936 …/asyncio/base_events.py
run_forever              line 608  …/asyncio/base_events.py
```

Stopped in the live IOC, source resolved through `/proc/12/root`, and
`workbench.action.debug.continue` let it run on. As the plan instructed, no
`dap.*` events were asked for — the bridge cannot see them for a seat-side
adapter, which is now recorded in `tools/vscode-bridge/README.md`.

## 7 What this run does not model

Stated because a green run here is not a green run at Diamond.

- **`ptrace_scope = 0`, AppArmor off.** The DLS-node-faithful setting, and per
  `k3s-test-bed` explicitly *not* the acceptance setting. Nothing here was
  measured at scope 1.
- **The four "shared" PVCs are node-local hostPath, not NFS.** The local-path
  provisioner refuses RWX outright, so static PVs in a fake storage class keep
  the claims *declaring* RWX. Nothing about root_squash, chown refusal or
  locking is reproduced — and the claim being NFS is the premise of D1b.
- **The `LimitRange` was added by hand** in §2. It matches Diamond's ratio cap;
  it is not a copy of Diamond's object.
- **The app was restarted in place by hand** between runs 2 and 3 — a hold file
  and a `kill`, imitating what `hotfix apply` does — to get a process debugpy
  had not already served. That orphaned the old tree, which was then killed.
  `restartCount` stayed 0 throughout, but the pid churn is why §5's
  `launch.json` accumulation showed up so plainly.
- **One node**, so `hostNetwork` has the port space to itself; **no Kyverno**,
  **no Argo CD**, **no SELinux**, **amd64 only**.
- The pod was **recreated cold before each timed run**, per `vscode-in-a-seat`.
  The seat's home is an `emptyDir`, so vscode-server was genuinely re-downloaded
  each time.

## 8 Teardown

`ssh root@187.124.114.170 /root/podbench-p47/99-teardown.sh` — deletes the
namespace and the four cluster-scoped PVs, which deleting the namespace alone
would leave behind. The `LimitRange` is namespaced and goes with it.
