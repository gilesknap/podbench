# Hotfix a running pod

Hotfix mode changes the code a **live** application is running, keeps the change
across restarts, and never restarts the container to do it. The application's
project lives on a PersistentVolumeClaim mounted *beside* the image's own copy,
and a small supervisor in the pod's `args` relaunches the application in place.

This page is the sequence, start to finish. For why any of it is shaped the way
it is, see [What `hotfix` does](../explanations/hotfix-flow.md).

:::{note}
Commands here are written `podbench <verb>` — the only spelling there is. If you
have not installed the launcher, run each as `uvx podbench <verb>`. See
[Setup](../tutorials/setup.md).
:::

:::{warning}
This is the one mode that needs **deploy-time cooperation**: five values in the
application's own chart, landed the way that chart is normally landed. It is
also **Python-only** and **single-replica only** — the claim is ReadWriteOnce
and one checkout cannot serve two writers.
:::

Five verbs, in the order you run them:

```text
values   emit the chart values the mode needs, read off the target
check    say what would stop a hotfix here, before you start one
init     seed the claim from the running container, clone, rebuild
restart  relaunch the application on what is on the claim
retire   measure the way back out, and delete the claim
```

Committing and pushing are **ordinary git in the seat**. podbench had verbs for
both once and neither earned its place: `git commit` in the checkout is a commit,
and `consolidate`'s push handled no credentials, so it was exactly as hard with it
as without ([#232](https://github.com/gilesknap/podbench/issues/232)).

`podbench hotfix status` is not a step. It is the one to run habitually, and the
one to leave in a shutdown checklist.

## 1. Emit the values, and deploy them

```
$ podbench hotfix values --app bl47p-ea-fastcs-01 \
      --from-pod bl47p-ea-fastcs-01-0 -n p47-beamline
```

`--app` is the target's **helm release name** — the claim is rendered as
`<release>-podbench-project`, and the emitted `volumes:` entry names it. The
claim itself comes from the `podbench-hotfix-claim` chart dependency, which
[Setup](../tutorials/setup.md) covers; everything else the command prints is
five ordinary passthroughs of the target's own chart:

```text
volumes            the claim, plus podbench-home for the seat
volumeMounts       the claim at /podbench/app — beside, never over
command / args     the supervisor, wrapping the entrypoint the pod runs today
livenessProbe      the target's own exec probe, wrapped to honour the hold
podSecurityContext fsGroup, without which the claim is present and unwritable
```

The snippet goes to **stdout** and every note goes to stderr, so redirecting it
is safe.

### Let podbench merge it into the service's own values file

A fragment to merge by hand is the error-prone route. Given the service's values
file, podbench merges its keys in and emits the file whole:

```
$ podbench hotfix values --app bl47p-ea-fastcs-01 \
      --from-pod bl47p-ea-fastcs-01-0 -n p47-beamline \
      --values services/bl47p-ea-fastcs-01/values.yaml \
      --parent-values services/values.yaml \
      > /tmp/values.yaml
$ mv /tmp/values.yaml services/bl47p-ea-fastcs-01/values.yaml
```

Four things about that invocation, each of which costs a deployment to get
wrong:

* **Write to a new file and move it.** Not `> services/…/values.yaml`: a shell
  truncates a redirect target *before* podbench starts, so the merge reads an
  empty file and the output silently loses everything the input had.
* **`--parent-values` is not optional where a parent exists.** A helm list
  **replaces** across the parent/child merge rather than merging. A service
  declaring `volumes:` for the first time takes the shared list over completely,
  which silently unmounts whatever the parent mounted. Given the parent file
  podbench absorbs its entries first; without it, podbench says so rather than
  assuming there is nothing to inherit.
* **Do not paste the fragment over a values file that already sets these keys.**
  `volumes:` and `volumeMounts:` are a whole key each, so a paste drops whatever
  the file declared — which on `bl47p-mo-ioc-01` would have been its `dev-shm`.
  What the chart renders for *itself* is unaffected either way and **must not be
  copied in**: doing so declares it twice. Read from a live pod podbench cannot
  tell the two apart, which is why it names the volumes and leaves the merge to
  you; read from the values file it can, which is why `--values` does it.
* **`--values-under KEY`** names the mapping the chart keeps its pod-template
  keys under — `ioc-instance` for an EPICS IOC. It is read from the files when
  you do not pass it, and the output says where the keys went.

Re-running is safe: entries are matched by `name`, the file's own values win,
and its comments survive.

Then deploy that values change the way the service is normally deployed, and
wait for the pod to come back carrying it.

### Why `values` always reads the cluster

There is no offline emission and no flag that brings one back. Three of the five
keys — the entrypoint, the gid and the liveness probe — used to be typed by
hand, and that is how
[#176](https://github.com/gilesknap/podbench/issues/176) happened: **a chart
renders a supplied `livenessProbe` wholesale**, so a timing you leave out becomes
the Kubernetes default rather than the value the target had. One compiled IOC
went from 120s/30s to 0s/10s and was probed from the moment it started, before it
had reached its hardware.

So if the read fails, fix the read: `--from-pod POD` names the pod, `-n NS` and
`--context NAME` say where to look for it. What is left of stating a value by
hand is stating **one** of them on top of the read, where the pod genuinely
cannot answer:

* `--entrypoint CMD` — for a target whose command lives in the image's
  `ENTRYPOINT` and so appears nowhere in the pod spec.
* `--gid GID` — where the pod states no `runAsGroup`.
* `--liveness-probe JSON` — a whole probe, timings included. Only an `exec`
  probe can be short-circuited by the hold: an `httpGet` or `tcpSocket` probe
  answers from the application, and the application is what is down while a pod
  is held.

## 2. Check before you start

```
$ podbench hotfix check bl47p-ea-fastcs-01-0 -n p47-beamline
  [ok]    target         bl47p-ea-fastcs-01-0, container
                         bl47p-ea-fastcs-01, 1 replica
  [ok]    claim          bl47p-ea-fastcs-01-0 mounts podbench-app at
                         /podbench/app
  [ok]    supervisor     container bl47p-ea-fastcs-01 is running the
                         podbench supervisor
  [warn]  seat           no podbench container is running in
                         bl47p-ea-fastcs-01-0. Not a blocker: init lands
                         one.
  [warn]  target root    not measured: listing /proc/1/root is a
                         property of a seat, and none is running
  [ok]    project        the image keeps one at /app
  [ok]    interpreter    the image keeps one at /python
  [ok]    liveness       the container declares no livenessProbe, so the
                         hold has nothing to short-circuit
  [warn]  source         the image names
                         https://github.com/DiamondLightSource/ubuntu-devcontainer,
                         which its own repository
                         ghcr.io/diamondlightsource/fastcs-example-debug
                         does not correspond to: `hotfix init` with no
                         `--repo` would clone that repository, so pass
                         `--repo URL` if it is not this application's
                         source.
------------------------------------------------------------------------
VERDICT: nothing measured here blocks `podbench hotfix init` (exit 0)
```

That `source` row is the real one, measured against this IOC on 2026-08-23.
**OCI labels are inherited**: `fastcs-example-debug` never overrode its base
image's, so it advertises `ubuntu-devcontainer`'s repository, revision and title,
and the revision provably does not exist in `fastcs-example`. `init` with no
`--repo` would clone that repository and you would edit the wrong project. The
one corroborator that costs nothing is the image's own registry path — a base
image cannot have written that — so a label the path corresponds to (a `-debug`
or `-runtime` variant counts as corresponding) is `[ok]`, and one it does not is
this `warn`. It is a warning rather than a blocker because `init` does not refuse
the state; `--repo` is what settles it.

Two things that `[ok]` does **not** mean. It is a correspondence and not a
proof: an image named *after its base* — `ubuntu-devcontainer-python` built from
`ubuntu-devcontainer` — inherits the base's label and corresponds to it. And it
speaks for the **repository only**. The image's
`org.opencontainers.image.revision` is a separate question, gated separately,
and `init` records an `ASSUMED` base unless `--repo` or a seeded checkout's
`origin` agrees with the label — as the next section shows on this very image.

It is read-only, it lands no seat, and it exits **1** while anything blocks — so
run it, fix what it names, run it again. Each blocker it reports is a chart
change and a redeploy, which is the thing you do not want to discover one per
attempt in the middle of an emergency.

`check` asks `init`'s questions in `init`'s terms, so pass it the same
`--repo`, `--container`, `--seat`, `--image-project` and `--image-interpreter`
you intend to pass `init`.

### Where podbench looks in the image, and what to do when it is elsewhere

Two of those rows are about the **image's layout**, and the defaults are a
convention rather than a law:

```text
--image-project      /app      the application's project directory
--image-interpreter  /python   the interpreter its venv was built against
```

`/app` beside `/python` is what python-copier-template produces. An
epics-containers image is not that shape — its venv is at `/venv` with a separate
`/python` — and a compiled IOC has no Python project at all. Point podbench at
the layout the image actually has:

```
$ podbench hotfix check bl47p-ea-ioc-01-0 -n p47-beamline \
      --image-project /venv --image-interpreter /python
```

If the paths look right and podbench still cannot find them, check you are
naming the container you meant with `--container NAME`.

This is not a thing to work around by pointing podbench at the seat's own
`/app`: the seat is a *different image*, and the venv there is podbench's rather
than the application's.

A separate failure, and a different fix: if the seat cannot **list** the target's
root at all, the seed has nothing to read from. That path is the application
container's own filesystem seen through `/proc/1/root`, and reaching it needs the
ptrace rung — a seat that landed without `CAP_SYS_PTRACE`, or into a namespace
whose policy denies it, cannot see it. `podbench doctor` measures that rung and
names what denied it; see [Attach to a pod](attach-to-a-pod.md).

## 3. Seed the claim

```
$ podbench hotfix init bl47p-ea-fastcs-01-0 -n p47-beamline \
      --repo https://github.com/DiamondLightSource/fastcs-example
seeded /podbench/app from /proc/1/root/app
copied the interpreter to /podbench/app/.python
claim seeded, venv interpreter 3.12.7
cloned https://github.com/DiamondLightSource/fastcs-example to /podbench/app
the image's labels name https://github.com/DiamondLightSource/ubuntu-devcontainer,
not https://github.com/DiamondLightSource/fastcs-example: inherited from its
base image, so its revision is not this repository's
base commit 8f21c04 ASSUMED (the image names 603392d, but nothing outside the
image confirms its labels are this repository's); pass --base-commit SHA
rebuilt the venv at /podbench/app/.venv
wrote /podbench/app/.podbench-hotfix.json
```

That is the `check` warning above, arriving where it costs something. `--repo`
disagrees with the label, so nothing corroborates the labels, so the
`org.opencontainers.image.revision` is not believed either and the base falls
back to the clone's `HEAD` — honestly marked. On this IOC the truth is the tag:
`2025.10.1` is `3d55455` in `fastcs-example`, and passing
`--base-commit 3d55455` is what turns every later `+N commit(s)` into a
measurement. On an image whose own labels are correct, the same line reads
`base commit 4d9a1c2, from the image's org.opencontainers.image.revision`.

`init` is the one verb that lands a seat for you if none is running — the verbs
after it refuse instead, because by then one should exist. Useful flags:

* `--repo URL` — where the source is. Omit it and podbench reads the image's own
  `org.opencontainers.image.source` label.
* `--ref REF` and `--base-commit SHA` — the branch to clone and the commit the
  released image was built from. Without `--base-commit`, podbench prefers the
  image's `org.opencontainers.image.revision` label; failing that it falls back
  to the clone's `HEAD` and says **ASSUMED**, because a fresh clone's `HEAD` is
  the default branch's tip and almost never what the image was built from. Every
  `+N commit(s)` you see afterwards is a difference against this number.
* `--no-install` — skip the editable install, for an application image with no
  installer.

It is safe to run twice: a claim already carrying `pyproject.toml` short-circuits
the whole seed.

## 4. Edit, restart, commit

The edit happens **in the seat**, in the checkout on the claim. Get a seat the
usual way and work in `/podbench/app`, which is the claim's mount point and the
checkout both — the project sits at the root of the volume exactly as it sits in
the image:

```
$ podbench attach bl47p-ea-fastcs-01-0 -n p47-beamline
$ ssh podbench-p47-beamline-bl47p-ea-fastcs-01-0
$ cd /podbench/app && $EDITOR src/fastcs_example/temp_controller.py
```

[Attach to a pod](attach-to-a-pod.md) and [VS Code over
Remote-SSH](vscode-remote-ssh.md) are the two routes in; either works, and so
does `kubectl exec`.

Then, from anywhere, put the edit into the running process:

```
$ podbench hotfix restart bl47p-ea-fastcs-01-0 -n p47-beamline
stopped the supervisor child pid 7 and its tree, started pid 2446
the claim is dirty and running: 1 file uncommitted (src/fastcs_example/temp_controller.py)
```

The relaunch holds the pod's liveness probe for at most 120 seconds, kills the
supervisor's child and its whole descendant tree, and lets the supervisor start
it again. `restartCount` does not move and the seat is untouched.

To confirm it actually took, the check that does not pass while broken is
**whether the port changed owner**. A target that allocates a pty — every
epics-containers IOC — can leave its real process reparented onto PID 1 still
holding the port, and both "the pid file moved" and "the port answers" look fine
in that state.

The loop most editing actually is — change a line, run it, look — is twenty
restarts and one commit at the end, and the commit is git's:

```
$ cd /podbench/app && git commit -am "clamp the setpoint before the ramp"
```

`restart` writes nothing at all — no commit, no index, no manifest write — and
`restartCount` still does not move.

Four things about what it prints:

* **The dirty line is the point of the verb, not decoration.** An uncommitted
  change on a live process is the one divergence no repository anywhere records.
  When the tree is clean the line says so and names the sha the new process
  loaded. `hotfix status` measures the same thing from the other end.
* **`--reinstall` after a packaging change.** An editable install bakes the
  packaging in at install time, so a new entry point or a renamed package needs
  `uv sync` run again on the claim: `podbench hotfix restart … --reinstall`
  rebuilds the venv before the relaunch. Where `pyproject.toml` or the lockfile
  is among the uncommitted paths, `restart` says the install is stale and names
  the flag rather than rebuilding on every iteration.
* **The pid is the supervisor's own child**, which is what
  `/tmp/podbench-child.pid` holds. It is not the process you would set a
  breakpoint in: on `bl47p-ea-fastcs-01` the file held 7 — the `stdio-socket`
  wrapper — and the `fastcs-example` under it was 13, three levels down. The
  kill is a tree kill from that pid, so everything below it went too. Use
  [`podbench pids`](../reference/cli.md) in the seat to see the tree.
* **A debug configuration is refreshed only if you already made one.** If the
  claim has a `.vscode/launch.json`, restart re-runs `podbench debug-config
  --provision` in the seat so F5 still reaches the new process — every
  configuration podbench can author names a pid, and the relaunch just changed
  it. With no `launch.json` it does nothing: `--provision` ptraces the workload,
  and a restart is not an ask for a debugger. See [VS Code over
  Remote-SSH](vscode-remote-ssh.md) for the debug step itself.

**A `git fetch` or `git push` in the seat needs `--forward-agent`.** The seat
holds no credential of its own, and the first thing an ssh remote fails on there
is `Host key verification failed` rather than authentication. Landing the seat
with `podbench attach --forward-agent` — or adding it to an existing one with
`podbench ssh-config --forward-agent` — lends it your agent for the session and
seeds its `known_hosts` from your own for the forge the claim's remote names. If
you were already connected, an ssh master left open by the earlier session
inherits its own settings rather than the new stanza's; podbench checks for that
and names the `ssh -O exit` that clears it.
Read what that costs first: it is {ref}`one section <git-in-the-seat>` of the
VS Code how-to, and the short version is that `authorized_keys` gates ssh and does not gate `kubectl
exec`.

## 5. Watch it: `hotfix status`

```
$ podbench hotfix status -n p47-beamline
  [ok]    p47-beamline/bl47p-ea-fastcs-01-0  active — hotfixed, and nothing here needs attention
    claim   9c1f2ab is 1 commit ahead of 4d9a1c2
    dirty   nothing uncommitted; what is running is what is committed
    remote  9c1f2ab is the tip of hotfix/beamtime-14, checked just now
    image   unchanged since the hotfix was made (sha256:aaaa1111ffff)
```

**Every one of those four rows is measured on the run, and none of it is
recorded.** The manifest keeps only what git cannot know — where the claim came
from, and which commit and image it was seeded against — because the seat has
ordinary git in it and one hand commit would make a recorded count false while
`status` went on printing it.

* `claim` and `dirty` come from `git` in the claim, over `kubectl exec`. They
  need no network and no credential. Where the *application* container has no
  git — a distroless image, and `status` is most useful on a colleague's pod
  where you have no seat — all three git rows collapse into one saying
  `unmeasured`. Never "clean".
* `remote` is asked **from your laptop**, not from the pod: an exec session has
  no `SSH_AUTH_SOCK`, so the pod cannot use your agent even when one is
  forwarded to a seat. podbench reads the shas out over exec and runs `git
  ls-remote` here, once per repository and under a five-second bound. A forge
  that does not answer makes the row `unmeasured` — never "not pushed" — and
  `--no-remote` skips it. It compares branch *tips*, so it can say a commit is
  the tip of a branch and never that it has been merged.
* `image` needs no git at all, and is the most valuable of the four: an image
  that moved under the mount means the claim's venv is now shadowing whatever
  was released.

It exits **0** only when every row is `active` and unheld, which is what makes it
usable as an assertion rather than a report. Nothing on the four rows moves that:
a dirty claim is the ordinary inner loop, an unpushed one is the ordinary state of
a fix made an hour ago, and a row that could not be measured is not an assertion
in either direction.

```
$ podbench hotfix status -A || echo "something is still hotfixed"
```

`-A` runs it over every namespace in the cluster with the same exit code. The
verdicts worth knowing on sight:

```text
image-changed  the image was upgraded under a live hotfix, so the upgrade has
               not reached the running code — the claim shadows the image
interpreter    the venv's bin/python will not run, or its version moved
not-hotfixed   held, but nothing hotfixed here — a relaunch that died mid-flight
held …         a hold, orthogonal to all of the above; "expiry unmeasured" is
               never "no deadline"
```

## 6. Get it back into an image, then retire the claim

Push the branch with git, from wherever your credentials are:

```
$ cd /podbench/app && git push origin HEAD:refs/heads/hotfix/beamtime-14
```

In the seat if it can reach your forge, and from a laptop clone if it cannot —
measured on this very pod, the seat has no key and no `known_hosts`, so a push
to its `git@github.com:…` remote answers `Host key verification failed`. Whether
it landed is the `remote` row of `hotfix status`, which asks the forge from your
laptop.

Then: open the PR, merge it, let CI build the image, roll the workload onto it,
take the volumes, volumeMount, command, args and podSecurityContext back out of
the application's own values, and turn the claim off.

The last two are the ones nobody does, so they are measured rather than
remembered:

```
$ podbench hotfix retire bl47p-ea-fastcs-01-0 -n p47-beamline
  [ ]     image          the deployed image is still sha256:aaaa1111,
                         the one the hotfix was made against
  [ ]     wiring         bl47p-ea-fastcs-01-0 still carries the
                         podbench-app volume, a volumeMount at
                         /podbench/app and the supervisor loop in
                         command and args. Those are fields in the
                         application's own pod template, not in the
                         claim's chart, so turning the claim off does
                         not remove them: take those entries - and not
                         the whole `volumes` and `volumeMounts` keys,
                         which carry the service's own - back out of the
                         application's values and redeploy.
                         podbench-home is declared as well, and it is
                         the seat's rather than the hotfix's: `attach`
                         and `vscode` use it, so take it out only if no
                         seat is wanted on this pod again.
                         podSecurityContext.fsGroup is 37887, which
                         `hotfix values` emits too; whether this pod had
                         one before the hotfix is not measured here, so
                         check it against the values before taking it
                         out.
  [ ]     claim          bl47p-ea-fastcs-01-podbench-project still
                         exists
------------------------------------------------------------------------
VERDICT: 3 of 3 steps of retirement remain (exit 1)
REMAINING: image, wiring, claim
```

**Take the entries out, not the keys.** `hotfix values` emitted podbench's own
`volumes` and `volumeMounts` entries and nothing else, while the service's keys
usually carry its own as well — `beamline-data` on this IOC. A helm list
*replaces* across the parent/child merge rather than merging into it, which is
why the service repeats what it inherits, so deleting either key wholesale
unmounts the beamline directory.

**Two of the six values are named rather than counted**, because they are not
the hotfix's to claim. The `fsGroup` is one podbench emits and an application
may have declared for itself, and the pod cannot say which. `podbench-home` is
the **seat's** home volume: `attach` and `vscode` mount it on any pod that
declares it, so a pod that finished its retirement and kept it has still
finished — counted, it would leave `retire` red forever on a step nothing could
close. Both are named on the row whether or not anything else is outstanding.

**There is no `branch` row**, and its absence is deliberate. It read a field
`consolidate` wrote into the manifest, and one push by hand made that field a
lie. Whether the fix exists anywhere but the claim is a live question now, asked
by `hotfix status` against the forge; retirement turns only on what can be
measured from the cluster in front of you. Check `status`'s `remote` row before
`--delete-claim`, because the deletion is the step that discards commits.

Read-only, it lands no seat, and it exits **1** while any measured step is
outstanding. `[x]` means *measured done*; a step that could not be measured stays
`[ ]` with a detail saying why, and moves the exit code in neither direction.

**`wiring` and `claim` are two separate acts, and this is where a retirement
usually stalls.** `podbench-hotfix-claim.enabled: false` disables the *subchart*
— the PVC — and nothing else. The `volumes`, `volumeMounts`, `command`, `args`
and `podSecurityContext` live in the target's own values, and turning the boolean
off does not touch them. A pod left wired to a claim its chart no longer declares is
worse than either end of the checklist: it fails only when that PVC is finally
pruned, and only at the next reschedule.

Once nothing mounts the claim, podbench can take the last step:

```
$ podbench hotfix retire bl47p-ea-fastcs-01-0 -n p47-beamline --delete-claim
```

It declines while any pod in the **namespace** still mounts the claim — the
second pod of a rollout holds it just as hard — and it declines when the pod
listing could not be read at all, because "found no mounters" and "could not
look" must not be one answer. The deletion is irreversible and the claim cannot
be read first, so what was on it goes unverified.

## See also

* [What `hotfix` does](../explanations/hotfix-flow.md) — the flow diagrams, the
  supervisor, and every cluster call in order.
* [Setup](../tutorials/setup.md) — the `podbench-hotfix-claim` chart dependency
  and the RBAC this mode needs.
* [Attach to a pod](attach-to-a-pod.md) — the seat the claim is reached through.
* [Ways in](../explanations/ways-in.md) — why you would reach for this rather
  than `attach` or `dev`.
