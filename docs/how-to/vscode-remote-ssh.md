# VS Code Remote-SSH

podbench's transport is a complete, ordinary OpenSSH connection whose only
carrier is `kubectl exec`. Remote-SSH does not know or care: it sees a host in
your ssh config and connects. This page is the client setup, and the numbers you
need to size a pod **before** you attach.

:::{note}
Commands here are written `podbench <verb>` — the only spelling there is. If you
have not installed the launcher, run each as `uvx podbench <verb>`. See
[Setup](../tutorials/setup.md).
:::

:::{warning}
A real VS Code GUI client has now connected — and the numbers still have not
been taken. On 2026-08-17 a Remote-SSH client reached a seat, started an
extension host, unpacked `ms-vscode.cpptools` and drove gdb through the C++
adapter into a live IOC. The transport was verified at the protocol level
besides — a real vscode-server completed an HTTP `200` plus a WebSocket
`101 Switching Protocols` handshake through `ssh -L`, with no port-forward and
no pod IP — and the server was driven headlessly. Every
memory figure below is therefore a **lower bound**: no extension host and no
language server has been measured. Treat this page as the best available
guidance, not as a proven result.
:::

:::{warning}
**In Observe mode, a breakpoint on a probed pod is on a timer.** Sitting on a
breakpoint stops the app answering its probes, and the kubelet cannot tell that
from a hang: the readiness budget takes the pod out of its Service quietly, and
the liveness budget restarts the container and kills the seat with it — an
ephemeral container cannot be restarted, so the session and the seat's name both
go. `podbench attach` prints both deadlines for the pod you name, computed from
its spec; on the demo Deployment in `tests/e2e/apps/` they are 11–16 s and
21–31 s.

VS Code's own tools are the way to stay inside them: **logpoints** (right-click
the gutter → *Add Logpoint*) print and carry on without stopping the process,
and a conditional breakpoint stops only on the iteration you care about. For an
unlimited pause, debug in a dev pod ([Iterate on Python](iterate-on-python.md)),
which has no probes by construction. [Debug with gdb](debug-with-gdb.md) has the
arithmetic and the measurements.
:::

## Size the pod first

Disk, not memory, is the binding constraint. Measured:

| | amd64 | arm64 (RK3588) |
|---|---|---|
| Server tarball | 213.6 MiB | 205.4 MiB |
| Download | 2.17 s | 2.26 s |
| Extract | 5.62 s | 5.56 s |
| **Extracted server on disk** | **680.8 MiB** | **638.3 MiB** |
| Server idle RSS | ~97 MiB | ~92 MiB |
| `ms-vscode.cpptools` on disk | 330 MiB | 261 MiB |
| cpptools install | 8.35 s | 8.07 s |
| Cold bootstrap over ssh | 5.76 s | ~10 s |

`~/.vscode-server` reached **995 MiB with exactly one extension**, and **2.2 GB**
with two server versions and six extensions. `data/data/CachedExtensionVSIXs` is
another 190 MiB after six extensions (safe to delete once they are installed).

**Plan for 1.1–1.3 GB of node disk per Observe-mode session, and ~1.5 GB if you
want headroom.** The design brief's "~1 GB" budget is exceeded by the stock
server alone.

arm64 is *not* slower here: the RK3588 extracted 646 MiB in 5.56 s and
downloaded 205 MiB in 2.26 s, statistically identical to the x86 NUC. That old
claim is about image pulls.

Which pod that lands in decides how much it matters:

* **Iterate mode** (`podbench dev`) — the sidecar has its own memory and
  ephemeral-storage requests and a workspace volume. Ask for what you need.
* **Observe mode** (`podbench attach`, `podbench vscode`) — every byte competes
  with the live workload's limits. Exceeding memory OOM-kills something in the
  pod cgroup; exceeding ephemeral storage evicts the whole pod. `podbench
  vscode` raises the target's memory limit in place for you when the headroom is
  short; read the caveats on [Attach to a pod](attach-to-a-pod.md) before you
  rely on it. Ephemeral storage cannot be raised in place at all — that one
  needs a `podbench-home` volume in the chart.

### Declaring the volume

`spec.volumes` is immutable, so this is a chart change and not something `attach`
can do:

```yaml
spec:
  securityContext:
    fsGroup: 1000          # without this the volume arrives root-owned
  volumes:                 # and unwritable, and the seat can chown nothing
    - name: podbench-home
      emptyDir: {}         # or a claim, to survive a restart
```

No `volumeMount` on the application container: only the seat mounts it. One
caveat (#42): a **root** seat takes `$HOME` from its passwd record and ignores
the volume, so the storage is bought with `--max-rung degraded` — which is also
what gives up the live attach.

## Client setup

1. Install the **Remote - SSH** extension (`ms-vscode-remote.remote-ssh`).
2. Make sure ssh can see podbench's generated stanzas. podbench writes one file
   per pod into `~/.podbench/config.d/` and never edits `~/.ssh/config`, so add
   the include once, **above** any `Host *` block:

   ```
   Include ~/.podbench/config.d/*.conf
   ```

   `podbench doctor` checks that line is there and in that position;
   `podbench doctor --fix` adds it.

3. Land a seat and note the alias it prints. Both modes write the same kind of
   stanza to the same place, and both print the alias on the last line:

   ```
   # Iterate mode — a dev pod whose sidecar is the seat
   podbench dev api-5f6c9b7d8-qz4tn -n demo --port 8080

   # Observe mode — a seat beside a live workload
   podbench attach pod/api-5f6c9b7d8-qz4tn -n demo
   ```

   Both take `--identity` (which key is authorised in the container, default
   `~/.ssh/id_ed25519`), `--config-dir` and `--host-alias`.
4. **Remote-SSH: Connect to Host…**, pick the alias, and wait out the first
   connect while the server downloads.

### Or let `podbench vscode` do all four

One verb lands the seat, sizes the pod, makes the target debuggable and opens
the window:

```
podbench vscode pod/api-5f6c9b7d8-qz4tn -n demo
```

That is the whole command for the common case. It is a separate verb rather
than a flag on `attach` because one of those steps *changes the workload* — it
can raise the target's memory limit — and `attach`'s contract is that it does
not: choosing this verb is asking for an editor and for everything an editor
costs.

**It writes nothing into the folder it opens, and installs nothing into your
application.** Debugging is a second command you run when you want one; see
[Debugging is a step you run](#debugging-is-a-step-you-run) below.

**It proves the alias first** — one `ssh <alias> true`, before anything is
written or downloaded — and if that does not reach the seat, it prints ssh's own
words and stops rather than opening a window that will fail. This is the one
thing VS Code cannot be asked: `code --remote` returns as soon as a window has
the argv, so the connection happens in the GUI afterwards, and a
`--install-extension` that never connected still exits 0. The successful probe
also leaves a `ControlMaster` behind, so the window's own connect is the fast
one.

**It writes** one file, and it is in the seat rather than in your project: the
machine settings — the folder-walk excludes, and on a hotfixed pod
`python.defaultInterpreterPath` for the interpreter your application actually
runs. Then it installs only the extensions this target's debugger needs **in the
remote window**, and opens the seat's home or, on a pod carrying the hotfix
layout, the claim. Those are the two steps most easily got wrong by hand, and
both fail quietly: the wrong folder can end the seat, and a locally installed
extension runs the debug adapter on your laptop. See [the CLI
reference](../reference/cli.md) for the order and the refusals.

Nothing at all lands in the folder. On a hotfixed pod that folder is your own
committed checkout on a shared volume, so anything written there is a permanent
line in somebody's `git status` — and a `launch.json` written at window-open is
stale as soon as you restart, because every configuration podbench can author is
keyed on a pid and a restart changes it. On the p47 replica the same process was
pid 12, then 2446, then 13.

To learn which extensions this target needs, podbench does ask the seat — one
`podbench debug-config --print-config`, which prints and therefore writes and
probes nothing. That answer is what says whether this is a Python seat or a C++
one, which is not something the laptop can see.

**It sizes the pod.** vscode-server measured 1215 MiB live with one extension,
and the headroom that decides is read on every attach anyway — so where this pod
has less, the target's memory limit is raised to a flat 6Gi before the seat
lands, and the number and the reading are both printed. Flat, so a second
`podbench vscode` against the same pod is a no-op rather than another raise. `--resize MEMORY` chooses the number yourself; `--no-resize` declines
the raise and keeps the warning. Read [Attach to a pod](attach-to-a-pod.md) on
what an in-place resize costs — chiefly that it lives on the pod and not on its
controller, so the next rollout takes it away.

**It offers the debug step rather than taking it** — see [Debugging is a step
you run](#debugging-is-a-step-you-run). On a dev pod the offer is shorter and
the run says why: Iterate mode launches the application *from* the seat, so
debugpy is already where the launch configuration needs it and the workload
container has been idled to `sleep` — injecting into that would succeed against
`sleep` and report a debugger nobody can reach.

**It uses the seat that is already there**, whichever of the three modes made
it. A pod you have already `attach`ed is reconnected to; a **dev pod** is
reconnected to through its `podbench` sidecar rather than by landing an ephemeral
seat beside it, which is what used to happen and cost a permanent container name
for a strictly worse view — in a dev pod the application runs as a child of the
sidecar, so a seat in the idled workload container sees nothing. The reconnect
says which mode the seat is, because that decides what the debugger is looking
at. `--new` still lands an Observe-mode seat, which is worth the name only where
the sidecar is non-root and the cluster admits `SYS_PTRACE`.

**It names the other two modes once**, on the run that landed a seat where there
was none, and asks nothing:

```
  other modes are their own verbs: `podbench hotfix init` for a venv on
  a claim that survives restarts, `podbench dev` for a clone the
  application relaunches from. Both change the workload, so neither is
  offered here
```

Said rather than asked, because with no seat in the pod there is nothing
ambiguous to resolve: `attach` is the only one of the three this verb could
carry out, and the other two answers would both have been *go and run a
different command*. A reconnect does not print it — the mode was settled
whenever the seat was landed, and the `KIND` column reports it.

It needs `code` on your PATH — VS Code's Command Palette has *Shell Command:
Install 'code' command in PATH* — and the local **Remote - SSH** extension,
without which `--remote` cannot resolve anything. It drives `code` only;
`cursor`, `codium` and `windsurf` take the same flags but have not been tried,
and a flatpak VS Code cannot put `code` on the host PATH at all.

`podbench attach` is still there and unchanged, for a seat with no editor in it.

### What it prints, and where the detail went

Two blocks: a checklist of what it did, and a short list of what you might do
next.

```
editor
  [ok] ssh reaches the seat, so Remote-SSH will too
  [ok] wrote ~/.vscode-server/data/Machine/settings.json in the seat: the
       folder-walk excludes
  [ok] installing ms-python.python, ms-python.debugpy in the seat; the first
       bootstraps vscode-server, so this is a download (1215 MiB measured, on
       the workload's ephemeral-storage budget in Observe mode)
  [ok] ms-python.python, ms-python.debugpy unpacked in the seat
  [warn] a window already connected to this seat needs Command Palette ->
         Developer: Reload Window, or the debug adapter stays unregistered.
  [ok] asked VS Code to open /root over Remote-SSH

next
  ssh config written to ~/.podbench/config.d/demo-api-5f6c9b7d8-qz4tn-1.conf
  add this to ~/.ssh/config once:  Include ~/.podbench/config.d/*.conf
  or let podbench check and add it:  podbench doctor --fix
  reconnect later with:  ssh podbench-demo-api-5f6c9b7d8-qz4tn-1
  to debug, in the seat:  podbench debug-config --provision
  if the window says 'could not establish connection', the local VS Code has
  no Remote-SSH extension (ms-vscode-remote.remote-ssh); ssh itself reached
  the seat a moment ago with the same config.
```

`[ok]` is a step that happened, `[warn]` one that wants something from you, and
`[FAIL]` one that did not happen at all. **Every step is one line**, and the
mechanism behind each of them is on this page rather than in the terminal —
this block used to say all of it inline, and the reliably-skipped part of a
report is the part written as prose.

**The assessment does not narrate here.** The one `debug-config
--print-config` this verb runs is an internal probe of it: it says which
extensions the seat needs and what the target's interpreter is, and those are
the only two things it is asked. Its account of *debuggers* — which mechanism
refused, which port a server would listen on, the injection command to run by
hand — stays in the seat, because this run set none of that up. Measured on a
live Diamond pod on 2026-08-24 it was 15 lines of a 90-line report, and one of
them read `also emitting for pid 7` after a run that emitted nothing and wrote
nothing anywhere.

If it could not be read at all, that is one `[warn]` line, quoting
`debug-config`'s own last word and naming the command to run in the seat to see
the rest. Run it there and you get all of it, unabridged — it prints, so it
still writes and probes nothing.

`next` is printed whether or not the editor step succeeded. A run that ends at
"ssh does not reach the seat" still landed a seat, and `podbench dbg` and
`podbench pids` reach it over `kubectl exec` regardless — so the alias, the
`Include` and the stanza's path are exactly what that reader needs.

### "unpacked in the seat" is a claim about the seat, not about `code`

`code --remote ssh-remote+<alias> --install-extension` exits 0 for "installed",
for "already installed" **and** for "never reached the remote" — and it answers
from the *laptop's* install list, so an extension you hold locally is reported
already installed and the seat is never contacted, with or without `--force`.
Measured at Diamond on 2026-08-21, against a seat holding no matching path
anywhere on its filesystem. It fails worst for the people most likely to be
here: anyone who debugs Python already has the Python extension locally.

So podbench asks the seat — `ls -1 ~/.vscode-server/extensions`, matched by id
prefix, since the directory carries a version and a platform triple — and only
then says `unpacked in the seat`. *Unpacked*, not *installed*, because the
listing proves presence and not that this run put it there.

Whatever is still missing is installed a second time through the **seat's own**
`code-server`, which is the code path the "Install in SSH: `<alias>`" button
takes; that one goes via the extension service the window is connected to, so
the adapter is live with no reload. It can only run after the window has opened,
because until then there is no server to install through.

If a `[warn]` says an extension did not land, install it from the Extensions
view of the remote window — and check the button reads **Install in SSH:
`<alias>`**, never the plain one. A local install runs the debug adapter on your
laptop, where none of the `/proc/<pid>/root` paths in `launch.json` exist, and
the failure reads as a bad configuration (`program path is missing or invalid`)
rather than as a wrong machine.

(debugging-is-a-step-you-run)=
### Debugging is a step you run

`podbench vscode` opens an editor. When you want a debugger too, run the step
the report offered you, **in the seat** — the integrated terminal of the window
that just opened is already in the right directory:

```
podbench debug-config --provision
```

It writes `.vscode/launch.json` into the directory it is run from, which is the
folder VS Code opened, and F5 works when it finishes. With no pid it picks the
best candidate — the deepest non-shell process — and `podbench pids` is how you
choose another:

```
podbench pids
podbench debug-config 2446 --provision
```

Two commands rather than one, knowingly. Editing needs a seat, a mounted claim
and a window; debugging needs ptrace, a 15 MB install, a one-shot injection and
a port — and most runs never debug. Paying the second bill at window-open
charged everybody for it, on a **guessed pid** that the next restart invalidated.

`--provision` is what a stock Python workload needs. The injection bootstrap
runs in the target's own interpreter, so debugpy has to be importable *there*;
where it is not, `debug-config` refuses and says so, naming this flag. It names
it for debugpy and for no other flavour — there is no `--provision` for a
missing delve. It also names it for the target that *can* import debugpy, whose
configuration would otherwise connect to a port nothing is listening on.

It is a mutation. It writes ~15 MB into the workload's writable layer, on an
ephemeral-storage budget the seat *shares with the workload and cannot reserve*;
it needs egress from the pod, since uv downloads from an index; starting the
server ptraces the app, which stops answering probes for the few seconds that
takes (~3 s measured — compare it against the deadlines the report prints); and
a restart of the target container ends the debugging.

Leave the flag off and `debug-config` authors whatever fits the target as it
stands, and prints the injection command rather than running it.

The two halves do not expire together. The **server** never survives a restart:
it is a live process inside the one that died. The **install** survives one only
where `--provision-dest` names a volume mounted into the target — an `emptyDir`
is pod-scoped and outlives a container. At the default `/opt/podbench-debugpy`
it does not: that is the container's own writable layer, which a restart
rebuilds from the image. Either way you are running `podbench vscode` again,
since without the server there is nothing to connect to.

Baking `debugpy.listen()` into the app is the durable answer, and the only one
that survives a restart. Provisioning is for the pod that is already
misbehaving.

On a **hotfixed** pod the default destination is not `/opt/podbench-debugpy`,
and cannot be. The seat there runs at the target's own uid with no capabilities,
`/opt` in the target is a root-owned `0755` directory, and the install is
refused by ordinary file permissions — which costs the whole cascade behind it:
no importable debugpy, no configuration, no `launch.json`, and no debug adapter
installed into the seat either. Only the laptop can see that, so `podbench
vscode` spells the right destination into the step it offers you:

```
to debug, in the seat:  podbench debug-config --provision --provision-dest /podbench/app/.podbench-debugpy
```

Paste it as printed. That path is writable by the seat, is the same directory in
both mount namespaces, and is a volume, so this is also the case where the
install outlives a restart. There is deliberately no fallback in either
direction: a destination that changed after a refusal would leave you guessing
which one is live. A seat in a hotfixed pod that does not carry the claim mount
keeps `/opt/podbench-debugpy` — the claim is the same directory in both mount
namespaces only where both containers mount it, and podbench decides this on the
mount rather than on the assumption.

A later `podbench vscode` against the same pod passes that destination to its
assessment run too, as an extra place to look for the target's debugpy — so once
you have run the step, the window that follows knows this is a Python seat and
installs the Python extensions for it.

### Re-running it on a window that is already connected? Reload it

`--install-extension` unpacks into the seat's `~/.vscode-server`. A window that
is *already* connected started its extension host before that, and does not pick
it up: the extension is installed, the debug adapter is not registered, and its
`launch.json` entry cannot run. Nothing on the remote side says so — the
debugger is simply not there.

The first run is unaffected, because the install finishes before the window
opens. A later run needs **Command Palette → Developer: Reload Window** only if
it actually put a *new* extension in the seat — but podbench cannot tell that
from "already installed" (`code` exits 0 for both), and cannot tell an open
window from a fresh one either, so it prints the reminder whenever an install
succeeded. On the runs where nothing changed, reloading costs a few seconds and
nothing else.

Run it from a terminal on the machine your VS Code runs on. Inside a Remote-SSH
window, a devcontainer or a Codespace, `code` on the PATH is the *remote* CLI,
which talks to the window you are already in: it would install the extensions
into that machine instead of into the seat. podbench refuses that `code` by name
rather than driving it.

:::{warning}
`podbench vscode` has not been driven against a real VS Code GUI client. The
flags it uses were verified by hand on 2026-08-16; the sequence podbench runs
them in has unit tests and no live proof.
:::

Do **not** reach an Iterate-mode dev pod with `podbench attach`. It works, but
it lands a *second*, ephemeral container inside the dev pod and ignores the
sidecar that is already there — a second copy of the image, a second
vscode-server on the pod's disk budget, and a container name burnt for the
pod's lifetime. `podbench dev` gives its own sidecar the seat.

If Remote-SSH does not offer the alias, it is reading a different config file.
Set `remote.SSH.configFile` to the file that has the `Include`, or point it
straight at `~/.podbench/config.d/<namespace>-<pod>-<n>.conf`, where `<n>` is the
seat's number (a `dev` sidecar, named exactly `podbench`, gets no suffix).

## From home, over a VPN that only forwards ssh

podbench needs no port-forward, no pod IP and no Service — the seat is reached
through `kubectl exec` — so **a reachable API server is the whole requirement**.
One ssh tunnel supplies it, and `k8s/vpn-api-tunnel.sh` builds both halves:

```console
$ ./k8s/vpn-api-tunnel.sh you@ws001.example.ac.uk:beamline-claude-you.kubeconfig
==> read you@ws001.example.ac.uk:beamline-claude-you.kubeconfig over ssh (not kept here)
    --ssh-host defaults to you@ws001.example.ac.uk, the host it came from
==> you@ws001.example.ac.uk:beamline-claude-you.kubeconfig -> k8s/beamline-claude-you-tunnel.kubeconfig
    context   claude-you  (namespace beamline)
    API       k8s-api.example.ac.uk:6443  ->  127.0.0.1:6443
    TLS       verified as k8s-api.example.ac.uk, through the source's own CA
```

It copies the kubeconfig, points the copy at a local port, and forwards that
port to the API server. The token, the CA and the namespace are carried over
untouched; `tls-server-name` is what keeps the certificate valid once the
address is `127.0.0.1`, so the CA the file already carries stays usable and
there is no reason to reach for `insecure-skip-tls-verify`.

**Name the source scp-style and it never lands here.** The kubeconfig is only
ever *input* to the copy the script writes, and it holds a live bearer token —
so `[user@]host:path` reads it over ssh into a temporary file that is removed on
exit, rather than leaving a credential on your laptop to go stale. A local path
works exactly as before; the rule for telling them apart is scp's own, a colon
before the first slash.

That host is also the default `--ssh-host`, since a machine holding a cluster's
kubeconfig is usually a machine that can reach the API server it names. Pass
`--ssh-host` explicitly to exit somewhere else.

Two things about it are worth knowing before you rely on it.

**Run podbench on the machine your VS Code runs on.** `podbench vscode` refuses
a `code` that resolves under `/remote-cli/` or `/.vscode-server/`, which is what
you get from the integrated terminal of a Remote-SSH window or a devcontainer.
`--install-extension` there installs into the machine you are already on, and
the seat ends up with `.vscode` files, no extensions and breakpoints that never
bind. The tunnel is what makes running it locally possible.

**The generated `ProxyCommand` does not carry `--kubeconfig`.** It runs
`kubectl exec`, and it resolves its kubeconfig from the environment of whatever
spawns it. The VS Code that podbench launches inherits your `export KUBECONFIG`,
so the first session works; a VS Code started later from a desktop icon does not,
and its `ProxyCommand` reads `~/.kube/config` instead. For a setup that survives
that, merge the tunnelled config in and select it by name:

```console
$ KUBECONFIG=~/.kube/config:k8s/beamline-claude-you-tunnel.kubeconfig \
    kubectl config view --flatten > ~/.kube/config.new
$ mv ~/.kube/config.new ~/.kube/config
$ uvx podbench vscode <pod> -n beamline --context claude-you
```

The `--context` is embedded in the stanza, so it then resolves with no
environment at all.

The tunnel exits from `--ssh-host`, so that machine's address is what the API
server sees. Where API access is allow-listed by source IP, it is that address
that has to be allowed and not your VPN one.

Close it with `--stop`, and pass `--config-only` if you run the forward yourself
from autossh or a systemd unit.

## The generated stanza, and why each line is there

```
# Generated by podbench. Regenerated on every attach; do not edit.
# target: demo/web-7d9f8c5b4-x2k9p[podbench-1]
Host podbench-demo-web-7d9f8c5b4-x2k9p-1
    HostName web-7d9f8c5b4-x2k9p
    User root
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    ProxyCommand kubectl -n demo exec -i web-7d9f8c5b4-x2k9p -c podbench-1 -- /usr/sbin/sshd -i -e -f /etc/podbench/sshd_config -o LogLevel=ERROR
    ServerAliveInterval 15
    ServerAliveCountMax 3
    ControlMaster auto
    ControlPath /tmp/podbench-cm/%C-2cbae7bf1f9161c8
    ControlPersist 10m
    HostKeyAlias podbench-3f2c1a90-7b6d-4e21-9a55-0c1e2f3a4b5c-podbench-1
    UserKnownHostsFile ~/.podbench/known_hosts
    StrictHostKeyChecking yes
```

Note what the `ProxyCommand` names: **`kubectl`, not podbench**. The launcher's
whole job is to land the seat and write this file, so the seat outlives the
process that created it. Land one with `uvx podbench attach`, which installs
nothing, and Remote-SSH keeps connecting for as long as the pod lives — with the
launcher no longer on the machine at all.

Do not hand-edit it — it is regenerated on every attach, and three of those
lines are load-bearing in ways that fail *silently*:

| Line | If you change it |
|---|---|
| `sshd -i -e` | `-e` is not about log tidiness. **Closing or replacing fd 2 in a `kubectl exec`'d process tears down the whole CRI exec stream**, truncating stdio with `rc=0`. Without `-e`, ssh dies at key exchange with `ssh_dispatch_run_fatal: … Broken pipe` — a network-looking error with a non-network cause. `2>&1` breaks it the same way |
| `-o LogLevel=ERROR` | keeps sshd's stderr byte-free without closing it, which is what satisfies both constraints at once. Anything chattier lands on the ssh client's stderr, which Remote-SSH parses |
| no `-t`, ever | from a script kubectl silently degrades to non-tty and appears to work; with a **real** TTY forced onto the ProxyCommand the ssh client hangs indefinitely |
| `ControlPath /tmp/podbench-cm/%C-<digest>` | `sun_path` is 108 bytes. A control socket next to a kubeconfig or in a workspace directory hits `ControlPath too long`. The multiplexed connection is also a ~6× speedup: 0.345 s cold, 0.058 s over the master. The digest is the seat's, and it is what keeps the multiplexing honest: `%C` hashes the *resolved* `HostName`, which every seat in a pod shares, so on its own it would let a second alias ride the first seat's connection — **skipping the host-key check**, since a multiplexed session never repeats it |
| `ServerAliveInterval`/`CountMax` | a *stalled* transport — what an apiserver or konnectivity hiccup looks like — hangs ssh **forever** without them, and fails in 19 s with them. A hard kill or pod deletion is detected instantly either way |
| `ForwardAgent yes` | present only under `--forward-agent`, and absent otherwise because OpenSSH's own client default is `no`. It lends your agent to everyone who can `kubectl exec` into the namespace for the life of the session — see {ref}`Git in the seat <git-in-the-seat>` before adding it by hand |
| `HostKeyAlias` + `UserKnownHostsFile` | podbench manages its own `known_hosts`, keyed on the pod UID **and the seat**, rather than teaching you `StrictHostKeyChecking no`. Every seat mints its own host key, so one alias over two of them would arrive as a host-key mismatch; a re-created pod is a *new host*, not a MITM warning |

Transport budget, for reference: ~10–11 MB RSS per live session, 26 MB/s
pod→client, 13 MB/s client→pod, 0 failures in 30 connect/disconnect cycles.

The stanza above is an Observe-mode one. A dev pod's differs in exactly two
places, both derived from what the sidecar actually is rather than assumed:

```
    User podbench
    ProxyCommand kubectl -n demo exec -i api-…-podbench -c podbench -- /usr/sbin/sshd -i -e -f /workspace/.podbench/sshd_config -o LogLevel=ERROR
```

`User` is the login name the sidecar reports for the uid it runs as — `root`
for a plain dev pod, and whatever the `podbench-identity` passwd record names
(`podbench`) where the origin declares that volume. The sshd config path
follows the same rule the agent uses inside the container: a non-root seat
keeps its files under `$HOME`, which for the sidecar is `/workspace`; a root
seat keeps them in `/etc/podbench` and `/root` whatever `$HOME` says.

`podbench dev --delete` removes the stanza and its `known_hosts` entry along
with the pod. An `attach` seat's stanza is left in place instead, because that
seat is reconnectable for as long as its pod lives.

## First connect

On first connect Remote-SSH downloads a server build matching your client's
**exact commit** and extracts it into the container. The version check is a hard
handshake rejection — `{"type":"error","reason":"Client refused: version
mismatch"}` — with no negotiation and no minor-version tolerance, which is why
podbench does **not** bake a server into the image: a baked server would be
correct for about four weeks and would break Insiders and stale clients
immediately.

Practical consequences:

* Connecting with two different VS Code versions puts **two** servers in the
  container. That is 1.3 GB before extensions.
* The container needs egress on first connect. Four host groups, not two:
  * `update.code.visualstudio.com` → `vscode.download.prss.microsoft.com` (the
    tarball);
  * `marketplace.visualstudio.com` (the extension gallery);
  * `*.vscode-unpkg.net`, `main.vscode-cdn.net` (extension assets);
  * `crl.microsoft.com` / `www.microsoft.com` (VSIX signature verification).

  An offline bundle must also ship the full `extensionDependencies` closure —
  extensions such as `ms-python.debugpy` still reach the marketplace even when
  installed from a local `.vsix`. Air-gapped operation is unspiked.
* After a pod restart or an OOM the ephemeral container's rootfs is gone and the
  server re-downloads. That is the documented reconnect path (~6 s), not a
  malfunction.

## Extensions, and staying slim

Install as few as you can live with — each one is disk in a budget you do not
control in Observe mode.

| Extension | For | Disk |
|---|---|---|
| `ms-vscode.cpptools` | C/C++ attach configs, gdb via MI | 330 / 261 MiB |
| `vadimcn.vscode-lldb` | Rust attach configs | ~90 MiB |
| `ms-python.python` + `ms-python.debugpy` | Python, and debugpy attach | Pylance alone is a 117 MiB install |

Two reclaims that are known to work, and one caveat:

```
# after the server extracts
rm -rf ~/.vscode-server/bin/*/extensions/{copilot,copilot-chat,mermaid-markdown-features}
# once extensions are installed
rm -rf ~/.vscode-server/data/data/CachedExtensionVSIXs
```

The first takes the server from 646 MiB to **428 MiB (−34 %)**. It was verified
only by "the server still starts and serves `/version`" — a real GUI client may
want what was deleted, so treat it as a reclaim you can try, not a default.

Also: do **not** add `--enable-remote-auto-shutdown` to Remote-SSH's server
arguments. It kills the server after exactly five minutes idle.

## Once you are connected

* Open `/workspace` in Iterate mode — that is the checkout, the venv and the
  sidecar's own `$HOME`.

  An ssh session's `$HOME` is **not** `/workspace`, and this is worth knowing
  before the server unpacks 700 MB somewhere you did not expect: sshd puts a
  session in the home the *passwd record* names, so it is `/home/podbench`
  where the origin declares the `podbench-identity` and `podbench-home`
  volumes, and `/root` on a plain dev pod. A `kubectl exec` shell is the other
  answer — it inherits the container's environment and lands in `/workspace`.
  Only the sidecar's tooling (uv's caches, toolchains and venvs) is pinned to
  the workspace volume; `~/.vscode-server` follows the passwd home. Declaring a
  `podbench-home` volume is what keeps that off the container's writable layer.
* In Observe mode open the seat's **home** — `/root`, or `/home/podbench` where
  the pod declares a `podbench-home` volume — and reach the workload's
  filesystem through `/proc/<pid>/root` from there. `podbench pids` tells you
  which pid.
* In Hotfix mode open the **claim**, at whatever path the application mounts it
  — `/podbench/app` by convention. `podbench vscode` does this for you and says
  so, because it is the only tree in the pod where an edit reaches the running
  process: the home is empty, and the image's copy under `/proc/1/root` is read
  through a mount the supervisor never resolves, so nothing written there ever
  runs. Where the pod carries the layout but *this seat* did not get the mount —
  an application mount with a `subPath` cannot be copied into an ephemeral
  container — the home is opened instead and the reason is printed with it.

  Do **not** open `/`. Opening a *file* under `/proc` is fine; opening a
  *folder* at `/` points the file watcher and the search indexer at `/proc`,
  where every `/proc/<pid>/root` is a symlink into another container's rootfs
  and the walk has no bottom. A seat cannot reserve memory of its own, and an
  OOM-killed ephemeral container **cannot be restarted** — the seat is gone and
  its name is burnt for the pod's lifetime. The seat ships the settings that
  make this survivable (below), which is a second line of defence and not a
  reason to try it.
* `.vscode/launch.json` lives **in the remote window**; every path in it is a
  path in the debug container. Templates for gdb, CodeLLDB and debugpy are in
  [Debug with gdb](debug-with-gdb.md) and
  [Iterate on Python](iterate-on-python.md).
* Terminals are ordinary ssh sessions with the container's `PATH`, so every
  in-pod verb is there as `podbench <verb>` — `podbench pids`, `podbench dbg`,
  `podbench capreport`, `podbench debug-config`, `podbench dev-bootstrap`,
  `podbench run`, `podbench stop`.

(git-in-the-seat)=
## Git in the seat: agent forwarding

A seat holds no credentials, so `git fetch` in one fails — and the first thing it
fails on is **not** authentication. Measured in a live seat on p47, 2026-08-24:

```console
$ cd /podbench/app && git fetch origin
Host key verification failed.
```

`podbench attach --forward-agent` fixes both halves at once, because either on
its own leaves git exactly as broken:

* it puts `ForwardAgent yes` in the generated stanza, so ssh in the seat can use
  the keys your local agent holds. Nothing else was needed for this: OpenSSH's
  client default is `ForwardAgent no` and sshd's is `AllowAgentForwarding yes`,
  so the seat was always willing and the stanza simply never asked. VS Code
  picks it up because Remote-SSH connects through the stanza;
* it copies the **forge's** entries out of your own `~/.ssh/known_hosts` into the
  seat's. Which forge is not guessed: podbench runs `git remote -v` in the seat,
  over **every directory the seat mounts** and its home, and seeds only the ssh
  hosts those remotes name. Mounts rather than one chosen folder, because a
  checkout can be on a hotfix claim, on a PVC the application declared, or in
  the home, and `git -C` on a directory that is not a repository prints nothing.
  On p47 `podbench-home` is an `emptyDir`, so a host accepted interactively dies
  with the pod; this is why it is done programmatically rather than left to you.

The flag is on `attach`, `vscode`, `ssh-config` and `dev`.

Three things it will not do, all deliberate:

* **it never invents trust.** No entry of yours for that host means no entry in
  the seat, and it says so. podbench does not teach `StrictHostKeyChecking no`
  and does not bake a forge's key into the image, for the same reason it does
  neither for the seat's own host key: one is a habit you will apply elsewhere,
  and the other goes stale silently;
* **it copies entries, not your file.** Each copied line is rewritten to name
  the one host it matched, so a `*.example.com` pattern of yours does not become
  a wildcard the seat holds, and the comment goes the way it already does for
  the seat's own entry;
* **`@revoked` and `@cert-authority` lines are skipped.** A marker dropped in the
  copy inverts its meaning — a revoked key rewritten as a plain entry would be
  installed as trusted — so a CA-based setup reports as *no entry found* rather
  than as trust invented for it.

An **https** clone of a public repository needs none of this. If your remote is
`https://github.com/…` the flag has nothing to do, and podbench says so instead
of pretending it seeded something.

### Adding the flag to a seat you are already connected to

**A connection that is already open swallows it.** Every stanza podbench writes
carries `ControlMaster auto` and `ControlPersist`, so a second `ssh` — and
Remote-SSH, which multiplexes the same way — rides the master that is already
there and inherits the settings *it* was opened with. Not the stanza on disk.
Measured on p47, 2026-08-24, with the flag on and the stanza correct:

```console
$ ssh <alias> 'echo $SSH_AUTH_SOCK; cd /podbench/app && git fetch --dry-run origin'
SSH_AUTH_SOCK=unset
git@github.com: Permission denied (publickey).
$ ssh -O exit <alias>
$ ssh <alias> 'echo $SSH_AUTH_SOCK; cd /podbench/app && git fetch --dry-run origin'
SSH_AUTH_SOCK=/tmp/ssh-WN68Z62b2K/agent.2572
From github.com:DiamondLightSource/fastcs-example …
```

An earlier `podbench vscode` had left the master open without forwarding. The
symptom is `Permission denied (publickey)`, which reads as a key problem and is
not one.

podbench checks for this rather than leaving you to find it: under
`--forward-agent` it asks `ssh -O check` about the `ControlPath` it wrote, and
where a master is running it asks that master what `SSH_AUTH_SOCK` a session on
it gets. A master opened by an earlier `--forward-agent` run already forwards
and is left alone; one with no agent earns a warning and the command that fixes
it:

```
close it first:  ssh -O exit podbench-<namespace>-<pod>-<seat>
```

It is not closed for you. The connection riding that socket is routinely a VS
Code window, and tearing one down from a verb whose job is to write a config
file would replace one surprise with another. Close it, then reconnect — the
next connection reads the stanza.

### What it costs, exactly

`authorized_keys` gates ssh. It does **not** gate `kubectl exec`, and podbench's
own report advertises that path. So while the session is up, anyone with
`pods/exec` in the namespace can reach the forwarded socket and authenticate as
you — and an agent forwards *keys*, not a destination, so that means **any host
that trusts the key**: jump boxes, other beamline machines, other organisations.
Bounded in time to the session, unbounded in reach within it.

**Read the rolebinding before you decide.** A facility's RBAC group routinely
contains service accounts and CI identities alongside humans, so "who can exec
here" is often a larger set than "my colleagues".

It is still the least-bad credential to put in a seat, which is why the flag
exists at all: nothing is written to disk, and `SSH_AUTH_SOCK` is set only in the
*ssh session's* environment, so a colleague's `kubectl exec` shell does not have
it and cannot stumble into it. The private key never enters the pod, so the
exposure ends when the session does. A cached `gh` token or a `.git-credentials`
file on a shared path is the opposite on every count — persistent, copyable,
silently reused, and still working next week.

Worth keeping in frame: the established route into these pods is `kubectl exec`
anyway. podbench's ssh layer is *adding* a boundary here, not removing one.

### Forward only the git key

An agent forwards **an agent**, not individual keys, so the granularity comes
from pointing at a different one. Keep a git-only agent:

```console
$ ssh-agent -a /run/user/1000/git-agent.sock
$ SSH_AUTH_SOCK=/run/user/1000/git-agent.sock ssh-add ~/.ssh/id_git
```

and hand it to the editor podbench launches:

```console
$ podbench vscode --agent-socket /run/user/1000/git-agent.sock
```

`--agent-socket` implies `--forward-agent`, and podbench refuses a path that is
not a listening socket rather than starting a run that would fail in the seat
minutes later. It sets `SSH_AUTH_SOCK` for every child of that run — `code`, the
preflight, and the ssh calls that write the machine settings — so only the keys
in that agent ever reach the pod, and the "unbounded in reach" objection above
collapses to one repository host. There is no OpenSSH version floor.

**One caveat, and it is a real one.** `code --remote` hands the argv to a VS Code
that may already be running, and that instance spawns ssh with the environment
*it* was started with. If a window is already open, close it first or export
`SSH_AUTH_SOCK` in the shell you start VS Code from; `podbench attach
--forward-agent` plus your own `ssh` has no such problem.

Two more mitigations, both entirely yours to apply:

* **`ssh-add -c`** makes the agent prompt locally on every use, so a key cannot
  be used silently by anybody, including you;
* **destination-constrained keys** (`ssh-add -h`, OpenSSH 8.9 and later) bind a
  key to named destinations, which closes the "any host that trusts the key"
  half directly rather than by keeping agents apart.

## What the seat configures for you

`podbench agent` writes VS Code's **machine-level** settings into the seat as
part of the same idempotent start-up that writes the host key and the authorized
keys, at `~/.vscode-server/data/Machine/settings.json` — where `~` is the home
the *passwd record* names, so on a `podbench-home` volume they persist across
re-attaches. Machine scope is the only scope that applies to every folder you
open without you having configured anything, which matters because the folder
that kills a seat is the first one.

| Setting | Why |
|---|---|
| `files.watcherExclude`, `search.exclude`, `C_Cpp.files.exclude` for `**/proc/**`, `**/sys/**`, `**/dev/**` | the walk with no bottom, above. `/dev/fd` is a symlink to `/proc/self/fd`, so excluding `/proc` alone leaves a way back in, and cpptools' tag parser walks the workspace on its own account |
| the same three for `**/.vscode-server/**` | the seat's own home is a folder you are told to open, and `~/.vscode-server` is 700 MiB before a single extension |
| `search.followSymlinks: false` | ripgrep is given `--follow` by default. `/proc/<pid>/root` is the doorway into every other container in the pod, and `/proc/self/root` makes the search re-enter itself |
| `python.analysis.exclude` for `/proc/**`, `/sys/**`, `/dev/**` | Pylance walks separately from search, and spells its excludes as a list of absolute globs |

`files.exclude` is deliberately **not** set: that would hide `/proc` from the
explorer, and reading the workload's files through `/proc/<pid>/root` is what
Observe mode is for.

`podbench vscode` merges into that same file on every run — over ssh, as the
login user, so `~` is the home vscode-server itself unpacks into — and adds one
key the agent cannot know:

| Setting | Why |
|---|---|
| `python.defaultInterpreterPath` | the interpreter the *target* runs, which is what answers "no Python interpreter found" on a pod where attach then works. Written only where the folder being opened is the tree that holds it — on a hotfixed pod, the claim, which resolves to the same file in the seat and in the application. Anywhere else the seat's file at that path is a *different* file, and naming it would be a confident wrong answer |

The key is `machine-overridable`, so a value you set in your own workspace still
wins over podbench's.

Nothing goes into the opened folder's `.vscode/settings.json` any more, and that
is a deliberate trade: your folder is often a committed checkout on a shared
volume, and an exclude list is not worth a permanent line in your `git status`.
The cost is that **Kill/Uninstall VS Code Server on Host** now deletes
podbench's excludes with everything else under `~/.vscode-server`. Re-running
`podbench vscode` puts them back before it opens the folder; a window you keep
open through a Kill Server and let VS Code reconnect on its own does not get
them, so re-run the verb rather than reconnecting from the window.

Settings you have written yourself are never overwritten. The file is read as
JSONC — comments and a trailing comma, which is what VS Code writes and what a
real project commits — and edited in place, so the agent adds only the keys that
are missing and everything else, comments included, stays where you put it. A
deliberate `"**/proc/**": false` survives. A file that is not JSONC either is
left exactly as it is, with the reason reported by `podbench agent --self-check`
and in the container's start-up output.

The one thing that removes them is Remote-SSH's **Kill/Uninstall VS Code Server
on Host**, which deletes `~/.vscode-server` wholesale. Re-run `podbench vscode`,
which rewrites the file before it opens anything; re-attaching
(`podbench attach --new`) or re-creating the dev pod works too, because the
agent writes them at start-up.

## When it goes wrong

| Symptom | Likely cause |
|---|---|
| Remote-SSH cannot find the host | the `Include` line is missing, is below a `Host *` block, or `remote.SSH.configFile` points elsewhere. `podbench doctor` tells the first two apart, and `--fix` settles them |
| "Could not establish connection", `Broken pipe` at key exchange | the ProxyCommand was edited; `-e` is mandatory and stderr must not be redirected |
| connection hangs with no output | keepalives removed, or a genuinely stalled apiserver path |
| `…/sshd_config: No such file or directory`, then `kex_exchange_identification: Connection closed by remote host` | the ProxyCommand names sshd's config where the seat's agent did not write it. The seat's *uid* decides that path — root keeps it in `/etc/podbench`, anyone else under `$HOME` — and a reconnect used to infer the uid from the rung it read back off the container. A cluster that strips `capabilities.add` leaves a root seat looking like the degraded rung, and the two answers came apart. Fixed; on an older launcher, `podbench attach --new` lands a seat whose rung is remembered rather than read back |
| server download stalls | the container has no egress to the four host groups above |
| session dies and the workload restarts | the pod hit its memory limit. This is the Observe-mode footgun; an OOM inside an ephemeral container is unrecoverable |
| session dies and the workload restarts *while you were stopped at a breakpoint* | the liveness budget expired — same symptom, different cause. `kubectl describe pod` says `failed liveness probe`, and there is no `OOMKilled` |
| the app stops answering through the Service while you are stopped, and is fine again after you continue | the readiness budget expired. Nothing is broken and nothing restarted; the pod left the Service's routing and re-joined |
| everything is gone after a reconnect | the container restarted, or the pod did. Fresh rootfs, fresh host key. Re-attach (Observe) or make the dev pod again and re-bootstrap (Iterate) |
| `Permission denied (publickey,keyboard-interactive)` with `agent refused operation` above it in the Remote-SSH log | your ssh agent holds that key, so ssh asked the agent to sign and it refused — nothing in the pod is involved. `SSH_AUTH_SOCK= ssh <alias>` in a terminal proves it, and `IdentityAgent none` in a `Host podbench-*` block in your own `~/.ssh/config`, below the `Include` line, makes ssh use the file (never for a FIDO/`sk-*` key or a smartcard). `podbench doctor` names it in advance |
| `Permission denied (publickey)` on a dev pod | the key is authorised from the sidecar's environment, which is fixed when the pod is created — so a dev pod made with a different `--identity` needs `podbench dev --delete` and a fresh one, not a re-run |

Behaviour through konnectivity or an API gateway is unknown — every measurement
here comes from a flat k3s exec path — as is Remote-SSH's own reconnect
behaviour when the pod is deleted mid-session.
