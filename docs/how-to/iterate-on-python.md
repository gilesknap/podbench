# Iterate on Python

Iterate mode is the one to reach for when you want to *change* code rather than
look at it: a sacrificial clone of a pod, with the app container idled and
podbench beside it as a real sidecar carrying the checkout, the interpreter and
the editor. Edit, relaunch, and see the change through the Service — **1.18 s**
per cycle, measured.

:::{note}
Commands here are written `podbench <verb>` — the only spelling there is. If you
have not installed the launcher, run each as `uvx podbench <verb>`, or, before
the first PyPI release, as
`uvx --from git+https://github.com/gilesknap/podbench podbench <verb>`. See
[Installation](../tutorials/installation.md).
:::

It is also the mode with no footguns about resources. A dev pod is authored by
podbench, so the sidecar has its own memory and ephemeral-storage requests and
its own workspace volume. The OOM and eviction warnings that apply to Observe
mode do not apply here. Anything heavier than looking belongs in a dev pod.

The origin pod is never touched.

## 1. Mint the dev pod

```
$ podbench dev api-5f6c9b7d8-qz4tn -n demo --port 8080
dev pod demo/api-5f6c9b7d8-qz4tn-podbench is running (clone of api-5f6c9b7d8-qz4tn;
api-5f6c9b7d8-qz4tn itself is untouched)
  target container : api (idled with sleep infinity)
  debug container  : podbench
  workspace        : /workspace (emptyDir, also $HOME)
  app port         : 8080 (readiness follows your process)
  service          : none — this pod receives no traffic until you pass
                     --take-traffic or --cutover

ssh config written to ~/.podbench/config.d/demo-api-5f6c9b7d8-qz4tn-podbench.conf
add this to ~/.ssh/config once:  Include ~/.podbench/config.d/*.conf
then:  ssh podbench-demo-api-5f6c9b7d8-qz4tn-podbench   (or Remote-SSH: Connect
       to Host -> podbench-demo-api-5f6c9b7d8-qz4tn-podbench)

next:
  ssh podbench-demo-api-5f6c9b7d8-qz4tn-podbench
  kubectl -n demo exec -it api-5f6c9b7d8-qz4tn-podbench -c podbench -- bash   # works when ssh does not
  podbench dev-bootstrap --repo <url> [--ref <ref>]
  podbench run --port 8080 -- <your command>

teardown (restores any borrowed Service selector, removes the pod, and takes the
ssh config with it):
  podbench dev --delete api-5f6c9b7d8-qz4tn-podbench -n demo
```

Two routes in, and the first one is the point of the mode: the editor lives in
the cluster. `dev` authorises your public key inside the sidecar
(`--identity`, default `~/.ssh/id_ed25519`) and writes the client stanza that
reaches it (`--config-dir`, `--host-alias`) — the same generator `attach` uses,
so [VS Code Remote-SSH](vscode-remote-ssh.md) applies unchanged. `kubectl exec`
stays listed because it works when ssh does not.

The key goes in when the pod is **authored**, and there is no second chance: an
ordinary container's environment is fixed once the pod exists. So `dev` refuses
before it creates anything if there is no key to authorise, and a dev pod made
with the wrong `--identity` has to be deleted and made again.

Do not reach a dev pod with `podbench attach`. That lands a *second*, ephemeral
container inside it and ignores the sidecar that already runs the agent.

`--dry-run` prints the authored pod instead of creating it. Read it once; it is
the clearest statement of what this mode does.

What podbench changes on the way from origin to clone:

* the app container's `command` becomes `sleep infinity` and its `args` are
  dropped, so PID 1 is inert **by construction** rather than by pausing anything;
* `readinessProbe`, `livenessProbe`, `startupProbe` and `lifecycle` are stripped
  from it, `restartPolicy: Never` is set, `nodeName` is cleared, and every
  server-owned metadata field (`uid`, `resourceVersion`, `ownerReferences`,
  `managedFields`, …) goes;
* podbench is added as a **real container** named `podbench`, with its own
  `resources` (by default requests `200m`/`512Mi`, limits `2`/`3Gi`), an
  `emptyDir` workspace mounted at `/workspace` (which is also `$HOME`),
  `capabilities.add: [SYS_PTRACE]`, and `shareProcessNamespace: true` on the pod;
* a `tcpSocket` readinessProbe **on the podbench container** watching your app's
  port. Without it a probe-less clone is `Ready` the instant it starts, joins the
  Service while nothing is listening, and serves about half errors;
* labels are copied *minus* every controller label (`pod-template-hash`,
  `controller-revision-hash`, job and StatefulSet labels) — otherwise a
  `replicas: 1` ReplicaSet sees two matching pods and reaps one.

podbench authors this spec itself rather than shelling out to `kubectl debug
--copy-to`, which strips **all** labels and annotations (so the clone is
invisible to the Service and the headline demo simply cannot work), gives the
added container `resources: {}` with no way to set them, has no `--dry-run`, and
prints nothing at all on success.

### If the origin declares `podbench-identity`, the sidecar is not root

A dev pod's sidecar is an **ordinary** container, so — unlike an `attach` seat —
it may mount a file with `subPath`. Where the origin pod declares the
`podbench-identity` volume (the ConfigMap the podbench chart emits; see
[Attach to a pod](attach-to-a-pod.md)), the clone carries it and the sidecar is
authored differently:

* `passwd` is mounted read-only over `/etc/passwd` and `group` over `/etc/group`;
* the sidecar runs as the **application's own uid and gid** — the pair that
  record names — because sshd resolves the login name `podbench` to that uid, and
  a root sidecar would be logging in as somebody else. That failure surfaces as
  `Permission denied (publickey)`, so the two are decided together;
* `SYS_PTRACE` goes with the root it no longer has. A capability added to a
  non-root uid reaches the bounding set only, so it could not have worked anyway.
  For Python this costs nothing: use `debugpy` (below), which never needed it;
* a declared `podbench-home` volume is mounted at `/home/podbench`, the home that
  same record names, so an ssh session lands somewhere writable. `$HOME` for the
  sidecar's own tooling stays `/workspace`;
* `fsGroup` is inherited from the origin, and set to the identity's gid if the
  origin sets none — an `emptyDir` is `root:root` until it is not, and the
  sidecar has just stopped being root.

The line is printed when the pod comes up:

```
  seat identity    : podbench-identity projected over /etc/passwd and
                     /etc/group, so the sidecar runs as 1000:1000 (the app's
                     own) with no SYS_PTRACE
```

This is also what makes a dev pod admissible in a namespace enforcing the
**restricted** Pod Security Standard, which refuses the root sidecar outright.
The prompts further down this page read `root@…` because they were captured
without an identity; with one they read `podbench@…`, and everything else on the
page is the same.

If the origin declares the volume but pins no uid and gid in its manifest,
podbench cannot know which uid the record was written for, says so on stderr, and
authors the root sidecar it always did. Nothing changes for an origin that does
not declare the volume at all.

## 2. Get in and populate the workspace

```
$ ssh podbench-demo-api-5f6c9b7d8-qz4tn-podbench
root@api-...-podbench:~# dev-bootstrap --repo https://github.com/you/api --ref my-branch
```

or, when ssh is not what you want:

```
$ kubectl -n demo exec -it api-5f6c9b7d8-qz4tn-podbench -c podbench -- bash
root@api-...-podbench:/workspace# dev-bootstrap --repo https://github.com/you/api --ref my-branch
```

The two land in different directories, which is a fact about sshd rather than
about podbench: an ssh session gets the home the *passwd record* names (`/root`,
or `/home/podbench` with the identity volume), while `kubectl exec` inherits the
container's environment, where `$HOME` is `/workspace`. `dev-bootstrap` takes
absolute paths and defaults to `/workspace/src`, so it does not care either way
— but `~` means different things in the two shells.

`dev-bootstrap` does three things, in order: `git clone` into
`/workspace/src`, `uv sync --frozen` from the project's own lockfile, and
`uv pip install -e .`. Flags: `--dir` for a different checkout path, `--python`
for an interpreter version, `--no-sync` and `--no-editable` to skip a step.

Budget from a real run: authored spec → Ready **4.3 s**; `uv python install
3.12` **2.3 s** (the image pre-seeds one, so usually zero); `uv venv` plus
`uv pip install -e .` **1.0 s**.

:::{important}
**Interpreter, venv and checkout must all live on the same side.** The debug
container and the app container do not share a mount namespace, so a `.pth`
written into the app's `site-packages` pointing at a checkout in the debug
container's filesystem *dangles*. A path-style `.pth` is then **silently
ignored** — `site.py` only appends directories that exist — and surfaces much
later as an unrelated-looking `ModuleNotFoundError`. An exec-style `.pth` (what
modern PEP-660 editable installs emit) prints a traceback and is non-fatal.

podbench standardises on: everything in the debug container. That is why
`dev-bootstrap` refuses a `--dir` on the wrong side of the boundary.
:::

Once you are connected with VS Code Remote-SSH, `/workspace/src` is just a
folder in the editor. There is no file sync — the checkout was never on your
laptop.

## 3. The relaunch loop

```
root@...:/workspace# podbench-run --port 8080 -- python -m api
pid 214 owns the listening socket on port 8080
```

Then edit, and run it again. `podbench run` stops the previous process (by
recorded pid), starts yours, and **verifies** the result. Inside the container
the helper is `podbench-run` rather than `run`, because `/usr/local/bin`
precedes `/usr/bin` and a helper called `run` would shadow far too much of your
own tooling. `podbench run` works from anywhere.

The verification is not decoration. Three separate silent failures make a naive
"poll the port" wrapper lie to you:

* **`SO_REUSEPORT` split.** If the previous listener set `SO_REUSEPORT` —
  uvicorn multi-worker, gunicorn `reuse_port`, many Go and Rust servers — a
  second bind **succeeds with no error** and the kernel splits traffic between
  old and new code. Measured through a Service: 5 requests new, 3 old, 2 new.
  Nothing in any log says so.
* **`TIME_WAIT` lockout.** After such a listener has served connections, a plain
  `SO_REUSEADDR` rebind fails for the full ~60 s `TIME_WAIT` window *while
  `ss -lntp` shows no listener at all*. `podbench run` counts `TIME_WAIT`
  entries so you are told to wait rather than staring at `EADDRINUSE`.
* **The false PASS.** A naive wrapper reported `LISTENING after 1 polls`, exit 0,
  for a relaunch that had already died with `EADDRINUSE`; the Service kept
  serving stale code. So `podbench run` tracks its own child pid, confirms it is
  alive, and matches the listening socket's inode against `/proc/<pid>/fd`.

Stop it explicitly with `podbench-stop` (`--grace` seconds before `SIGKILL`).
Nothing here ever uses `pkill -f`: under `shareProcessNamespace: true` that
matches the invoking shell and every other container's processes.

## 4. See the change through the Service

By default the dev pod receives **no traffic**. That is deliberate — a clone
carrying the origin's selector labels takes production traffic the moment it
becomes ready. Two opt-ins:

```
# share traffic with the original pod (both are in the endpointslice)
$ podbench dev api-... -n demo --port 8080 --take-traffic

# send the Service exclusively to the dev pod
$ podbench dev api-... -n demo --port 8080 --cutover api
```

`--cutover` repoints the Service's selector with a JSON **replace** patch and
records the original selector on the dev pod, so teardown restores it exactly. A
merge patch would silently *union* the selector maps, which adds the dev pod
without removing the original — the opposite of a cutover, and invisible until
you notice half your responses are stale.

Then, from anywhere in the cluster:

```
$ kubectl -n demo run curl --rm -it --image=curlimages/curl --restart=Never -- \
    curl -s http://api/
```

Edit a string, `podbench run` again, curl again. That round trip is the whole
point of the mode.

## 5. Tear down

```
$ podbench dev --delete api-5f6c9b7d8-qz4tn-podbench -n demo
restored selector {"app": "api"} on service/api
deleted pod/api-5f6c9b7d8-qz4tn-podbench
removed ~/.podbench/config.d/demo-api-5f6c9b7d8-qz4tn-podbench.conf
dropped podbench-3edcc84e-… from ~/.podbench/known_hosts
```

This restores any borrowed Service selector *before* removing the pod, and
leaves nothing behind — on the laptop either. The stanza and its pinned host key
go with the pod, because the `HostKeyAlias` is keyed on a pod UID no pod will
ever have again, so a stanza left behind could only ever fail. (An `attach`
seat's stanza is kept instead: that seat is reconnectable for as long as its pod
lives.) The origin pod was never modified.

## Alternative: `PYTHONPATH` shadowing

The editable install is the tidy route, but it needs the project to be
installable. When it is not — or when you want to override one module inside a
dependency you do not own — put your checkout ahead of everything on the path:

```
root@...:/workspace# PYTHONPATH=/workspace/src podbench-run --port 8080 -- python -m api
```

`PYTHONPATH` entries are inserted before site-packages, so `/workspace/src/api/`
shadows the installed `api`. Caveats worth knowing:

* it shadows by **directory**, not by distribution, so package metadata,
  entry points and `importlib.metadata.version()` still come from the installed
  copy;
* namespace packages and compiled extension modules do not shadow cleanly;
* it is invisible in `pip list`, so a colleague reading the container will not
  see why the code differs from the image.

Use it for a quick override; use `uv pip install -e .` for a session you will
still be in tomorrow.

## Optional: auto-reload with `watchfiles`

If you would rather not type `podbench run` after every save, wrap the command:

```
root@...:/workspace# uv pip install watchfiles
root@...:/workspace# podbench-run --port 8080 -- \
    watchfiles --filter python 'python -m api' /workspace/src
```

`watchfiles` restarts the child on change, which keeps `podbench run`'s
supervision one level up: it tracks the `watchfiles` process, and the port
ownership check is against that process tree.

Two things to know before you rely on it. A reloader that rebinds the port
sits squarely in the `SO_REUSEPORT`/`TIME_WAIT` territory above, so a
restart-storm can leave you serving stale code with nothing in the logs; and an
editor saving through ssh can produce write patterns that fire the watcher more
than once. If a change does not appear, fall back to an explicit
`podbench-stop` and `podbench-run` — that path verifies.

## The ptrace-free live attach for Python: debugpy

This is the pattern to reach for instead of gdb when the code is Python, and it
is the reason the Python story never needs `SYS_PTRACE` at all. The app opens
a debug port itself; VS Code attaches over **localhost**, through the shared
network namespace and the ssh tunnel. No capability, no Yama, no admission
argument.

Bake an **opt-in** listener into the application image, guarded by an
environment variable so it is inert in production:

```python
import os

if os.environ.get("DEBUGPY_LISTEN"):
    import debugpy

    debugpy.listen(("127.0.0.1", 5678))
    if os.environ.get("DEBUGPY_WAIT"):
        debugpy.wait_for_client()
```

Bind to `127.0.0.1`, never `0.0.0.0`: the pod's network namespace is shared with
the debug container, so loopback is exactly the reach you want and nothing more.
A debug port on the pod IP is an unauthenticated code-execution endpoint.

Run the app with the variable set:

```
root@...:/workspace# DEBUGPY_LISTEN=1 podbench-run --port 8080 -- python -m api
```

Then attach from the remote VS Code window:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "podbench: attach to the app (debugpy)",
      "type": "debugpy",
      "request": "attach",
      "connect": { "host": "127.0.0.1", "port": 5678 },
      "pathMappings": [
        { "localRoot": "/workspace/src", "remoteRoot": "/workspace/src" }
      ],
      "justMyCode": false
    }
  ]
}
```

`localRoot` and `remoteRoot` are the same string here because the editor's
"local" *is* the debug container — there is no laptop in the path. That is the
whole shape of the tool in one config block.

If the app runs in the **idled app container** rather than under `podbench run`,
loopback still reaches it (one network namespace), but the app container needs
`debugpy` installed in its own environment — the debug container's venv is not
importable from there.

## What you cannot do in Observe mode

The relaunch loop is Iterate-only, and the report says so explicitly when you
attach to a live pod. Killing PID 1 in a live container makes the kubelet
restart it with pristine image code, and SIGSTOPping it leaves the listening
socket held while liveness probes kill the container anyway. Do not fight the
kubelet: mint a dev pod.
