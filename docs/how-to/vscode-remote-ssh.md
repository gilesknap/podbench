# VS Code Remote-SSH

podbench's transport is a complete, ordinary OpenSSH connection whose only
carrier is `kubectl exec`. Remote-SSH does not know or care: it sees a host in
your ssh config and connects. This page is the client setup, and the numbers you
need to size a pod **before** you attach.

:::{note}
Commands here are written `podbench <verb>` — the only spelling there is. If you
have not installed the launcher, run each as `uvx podbench <verb>`, or, before
the first PyPI release, as
`uvx --from git+https://github.com/gilesknap/podbench podbench <verb>`. See
[Installation](../tutorials/installation.md).
:::

:::{warning}
No real VS Code GUI client has been driven against podbench yet. The transport
was verified at the protocol level — a real vscode-server completed an HTTP
`200` plus a WebSocket `101 Switching Protocols` handshake through `ssh -L`,
with no port-forward and no pod IP — and the server was driven headlessly. Every
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
* **Observe mode** (`podbench attach`) — every byte competes with the live
  workload's limits. Exceeding memory OOM-kills something in the pod cgroup;
  exceeding ephemeral storage evicts the whole pod. Use `attach --resize 6Gi` to
  raise the target's memory limit in place first, and read the caveats on
  [Attach to a pod](attach-to-a-pod.md) before you rely on it.

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

### Or let `attach` do steps 3 and 4

`podbench attach --open` lands the seat and then drives the client for you:

```
podbench attach pod/api-5f6c9b7d8-qz4tn -n demo --open
```

It writes `.vscode/settings.json`, `.vscode/launch.json` and
`.vscode/extensions.json` into the folder it is about to open, installs only the
extensions this target's debugger needs **in the remote window**, and opens the
seat's home. Those are the two steps most easily got wrong by hand, and both
fail quietly: the wrong folder can end the seat, and a locally installed
extension runs the debug adapter on your laptop. See
[the CLI reference](../reference/cli.md) for the order and the refusals.

It needs `code` on your PATH — VS Code's Command Palette has *Shell Command:
Install 'code' command in PATH* — and the local **Remote - SSH** extension,
without which `--remote` cannot resolve anything. `--open` is on `attach` only,
and drives `code` only; `cursor`, `codium` and `windsurf` take the same flags
but have not been tried, and a flatpak VS Code cannot put `code` on the host
PATH at all.

### A stock Python workload gets no `launch.json` without `--provision`

`--open` does not compute the debug configuration itself: it asks the seat, and
`debug-config` is the only thing that can see the target. On a Python app whose
image has no debugpy that ask *refuses*, because the injection bootstrap runs in
the target's own interpreter and therefore needs debugpy importable **there**.
You get the excludes, the folder and the alias, and no `launch.json` at all.

`--provision` is the way through:

```
podbench attach pod/api-5f6c9b7d8-qz4tn -n demo --open --provision
```

The seat then installs debugpy into the target with `uv` before authoring the
configuration. It is opt-in and stays that way: it writes ~15 MB into the
workload's writable layer, on an ephemeral-storage budget the seat *shares with
the workload and cannot reserve*; it needs egress from the pod, since uv
downloads from an index; and no container restart survives it. Without `--open`
it is refused rather than ignored — there is no configuration run for it to
change.

Baking `debugpy.listen()` into the app image is the durable answer, and the only
one that survives a restart. `--provision` is for the pod that is already
misbehaving.

One thing it does **not** do: start the server. The configuration is emitted as
soon as the prerequisites are met, and nothing is listening until the injection
runs — ptracing the workload is not something authoring a `launch.json` may do
on its own. `--open` relays the seat's own output, so the command to run is
printed with the rest, along with every mechanism that said no.

Run it from a terminal on the machine your VS Code runs on. Inside a Remote-SSH
window, a devcontainer or a Codespace, `code` on the PATH is the *remote* CLI,
which talks to the window you are already in: it would install the extensions
into that machine instead of into the seat. podbench refuses that `code` by name
rather than driving it.

:::{warning}
`--open` has not been driven against a real VS Code GUI client. The flags it
uses were verified by hand on 2026-08-16; the sequence podbench runs them in has
unit tests and no live proof.
:::

Do **not** reach an Iterate-mode dev pod with `podbench attach`. It works, but
it lands a *second*, ephemeral container inside the dev pod and ignores the
sidecar that is already there — a second copy of the image, a second
vscode-server on the pod's disk budget, and a container name burnt for the
pod's lifetime. `podbench dev` gives its own sidecar the seat.

If Remote-SSH does not offer the alias, it is reading a different config file.
Set `remote.SSH.configFile` to the file that has the `Include`, or point it
straight at `~/.podbench/config.d/<namespace>-<pod>.conf`.

## The generated stanza, and why each line is there

```
# Generated by podbench. Regenerated on every attach; do not edit.
# target: demo/web-7d9f8c5b4-x2k9p[podbench-1]
Host podbench-demo-web-7d9f8c5b4-x2k9p
    HostName web-7d9f8c5b4-x2k9p
    User root
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    ProxyCommand kubectl -n demo exec -i web-7d9f8c5b4-x2k9p -c podbench-1 -- /usr/sbin/sshd -i -e -f /etc/podbench/sshd_config -o LogLevel=ERROR
    ServerAliveInterval 15
    ServerAliveCountMax 3
    ControlMaster auto
    ControlPath /tmp/podbench-cm/%C
    ControlPersist 10m
    HostKeyAlias podbench-3f2c1a90-7b6d-4e21-9a55-0c1e2f3a4b5c
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
| `ControlPath /tmp/podbench-cm/%C` | `sun_path` is 108 bytes. A control socket next to a kubeconfig or in a workspace directory hits `ControlPath too long`. The multiplexed connection is also a ~6× speedup: 0.345 s cold, 0.058 s over the master |
| `ServerAliveInterval`/`CountMax` | a *stalled* transport — what an apiserver or konnectivity hiccup looks like — hangs ssh **forever** without them, and fails in 19 s with them. A hard kill or pod deletion is detected instantly either way |
| `HostKeyAlias` + `UserKnownHostsFile` | podbench manages its own `known_hosts`, keyed on the pod UID, rather than teaching you `StrictHostKeyChecking no`. A re-created pod is a *new host*, not a MITM warning |

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
rm -rf ~/.vscode-server/data/CachedExtensionVSIXs
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
  `podbench capreport`, `podbench dev-bootstrap`, `podbench run`, `podbench
  stop`.

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

`attach --open` writes all of those a second time, into the
`.vscode/settings.json` of the folder it opens. It opens a *single* folder, so
that file is VS Code's **workspace** settings, where window- and resource-scoped
keys are both honoured — including `C_Cpp.files.exclude`, which is the only one
that stops cpptools' tag parser, and cpptools is what `--open` installs for a
C/C++ target. The folder copy matters because it is the one podbench fully
controls: `~/.vscode-server` belongs to the client, and **Kill/Uninstall VS Code
Server on Host** takes the machine file with it. Inside a home the entry that
earns its place first is `**/.vscode-server/**` — 700 MiB before a single
extension, in the folder podbench is about to open.

The `/proc` and `/sys` entries in that copy are belt-and-braces rather than the
working guard. `search.exclude` patterns are matched **workspace-relative**, so
from a folder at `$HOME` its `**/proc/**` cannot match anything;
`files.watcherExclude` and `python.analysis.exclude` are matched against
absolute paths and do work from either scope. Machine scope is what covers the
folder you open *next* — including the `/` that starts the walk with no bottom —
which is why both copies exist.

Settings you have written yourself are never overwritten. The agent adds only
the keys that are missing, so a deliberate `"**/proc/**": false` survives, and a
file it cannot parse — VS Code allows comments in `settings.json`, `json` does
not — is left exactly as it is, with the reason reported by
`podbench agent --self-check` and in the container's start-up output.

The one thing that removes them is Remote-SSH's **Kill/Uninstall VS Code Server
on Host**, which deletes `~/.vscode-server` wholesale. Nothing rewrites the file
until the agent next starts, so re-attach (`podbench attach --new`) or re-create
the dev pod before opening a folder again.

## When it goes wrong

| Symptom | Likely cause |
|---|---|
| Remote-SSH cannot find the host | the `Include` line is missing, is below a `Host *` block, or `remote.SSH.configFile` points elsewhere. `podbench doctor` tells the first two apart, and `--fix` settles them |
| "Could not establish connection", `Broken pipe` at key exchange | the ProxyCommand was edited; `-e` is mandatory and stderr must not be redirected |
| connection hangs with no output | keepalives removed, or a genuinely stalled apiserver path |
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
