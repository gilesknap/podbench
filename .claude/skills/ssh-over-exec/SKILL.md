---
name: ssh-over-exec
description: The invariants of podbench's ssh-through-kubectl-exec transport, all of which fail silently or misleadingly when broken. Read before touching sshcfg.py, agent.py, or anything that generates a ProxyCommand or an sshd config.
---

# ssh over `kubectl exec`

The transport is a complete OpenSSH connection whose only carrier is `kubectl exec`:
`sshd -i` runs inside the debug container as the client's `ProxyCommand`, so there is no
listening socket, no port-forward and no pod IP. Auth to the cluster is the kubeconfig;
auth to the container is a key.

**Every rule here has a silent or misleading failure mode.** That is why the client
config, the server config and the ProxyCommand are generated from one place and never
hand-written, and why `tests/test_sshcfg.py` is full of exact-string assertions. The
regression to fear is a well-meaning tidy-up, not a crash.

## `-e` is mandatory, and not for the reason it looks

`sshd -i -e`. The flag is **not** about log tidiness — it is about keeping **fd 2 open**.

Closing or redirecting stderr in a `kubectl exec`'d process tears down the whole CRI exec
stream and silently truncates stdio with `rc=0`:

```
( echo one; sleep 4; echo two ) | kubectl exec -i pod -c c -- sh -c 'exec cat'
one
two                     # both arrive

( echo one; sleep 4; echo two ) | kubectl exec -i pod -c c -- sh -c 'exec 2>/dev/null; exec cat'
one                     # "two" SILENTLY LOST, rc=0
```

Against sshd it surfaces as a network error — `ssh_dispatch_run_fatal: … Broken pipe`,
dying at `expecting SSH2_MSG_KEX_ECDH_REPLY` — which sends you debugging the wrong layer.

| ProxyCommand | result |
|---|---|
| `sshd -i -e` | works |
| `sshd -i -E /tmp/log` | works (`-E` implies `log_stderr=1`) |
| `sshd -i` | **fails, reproducibly** |
| `sh -c '/usr/sbin/sshd -i'` (no `exec`, shell holds fd 2) | works — **masks the bug** |
| `sh -c 'exec /usr/sbin/sshd -i'` | fails |
| `sh -c 'exec /usr/sbin/sshd -i -e 2>&1'` | fails |

Two spikes disagreed about this (S2 ran without `-e` for a whole session), so the shipped
form is `-e -o LogLevel=ERROR`: zero stderr bytes, fd 2 still open. `tests/e2e/test_s1_transport.py::test_transport_dies_without_dash_e`
edits the config podbench *actually generates* and asserts the transport breaks — if it
ever fails, settle the contradiction, do not delete the flag.

## Never `-t`

`kubectl exec -t` silently degrades to non-tty from a script, so it looks fine — and
**hangs the ssh client forever** when a real TTY is present. There is no case where the
transport wants a TTY.

## Never redirect or merge stderr

Any `2>&1`, `>/dev/null`, or wrapper that closes fd 2 breaks the transport per the above.
`proxy_command` returns argv (no shell) so there is nowhere to introduce one.

## `ControlPath` must be short

`sizeof(sockaddr_un.sun_path)` is 108 bytes. Put the socket next to a kubeconfig or in a
workspace and ssh refuses with `ControlPath too long`. Hence `/tmp/podbench-cm/%C` and
`control_path_ok`.

**Create the parent directory.** ssh will not, and the failure reads like a transport
fault: `unix_listener: cannot bind to path …: No such file or directory`.
`write_ssh_config` calls `ensure_control_dir` because writing the config is when the path
is known.

## `ServerAliveInterval` is mandatory

A *stalled* transport — what an apiserver or konnectivity hiccup looks like — hangs ssh
**forever** with no keepalive. With `ServerAliveInterval 15` / `ServerAliveCountMax 3` it
fails in seconds. A hard kill or pod deletion is detected instantly either way.

## sshd does not leak its own environment

Nothing the image or the launcher put in the container's environment reaches an ssh
session by itself: sshd passes none of its own environment to the commands it runs, and a
non-interactive `ssh host '<cmd>'` sources no profile either. One mechanism, three
symptoms that read as unrelated bugs:

| Lost | How it shows up |
|---|---|
| `PODBENCH_TARGET_CID` | the in-pod verbs fall back to *guessing* which processes belong to the target |
| `PATH` | `--provision` died with `sh: 1: python: not found` — the seat's interpreter is on no default `PATH`. The injection recipe now names it in full (`flavour.SEAT_PYTHON`), because a line printed to be pasted must not depend on a directive sshd may refuse |
| `DEBUGINFOD_URLS` | `set debuginfod enabled on` is inert **over the transport podbench itself generates**, while working under `kubectl exec`, which does inherit the image's environment |

`SetEnv` in the sshd config is the only route that survives — and podbench generates that
config, so it owns the fix. `agent.SESSION_ENV_PREFIX` carries every `PODBENCH_*`
variable; `agent.SESSION_ENV_NAMES` carries the image's `PATH`, `DEBUGINFOD_URLS` and
`DEBUGINFOD_TIMEOUT` by exact name. An allow-list rather than the whole environment: the
sshd config is world-readable and the seat's environment is where the keys live. A name
listed but unset is simply absent, which is what lets a variable be carried before
anything sets it.

`DEBUGINFOD_URLS` is the one the agent may decide *not* to carry: `agent.check_debuginfod`
connects to that server once at start-up and drops the variable when nothing answers,
because gdb's client has nothing to query without it and would otherwise wait
`DEBUGINFOD_TIMEOUT` per shared library **after** the attach, with the workload stopped.
The reason goes into the start-up log; a `kubectl exec` session still inherits the image's
value, where `podbench dbg --no-debuginfod` is the same decision per run.

> **One `SetEnv` directive carrying every pair, never one per variable.** sshd resolves
> each keyword **first-match-wins**, so a second `SetEnv` line is silently ignored. This
> shipped once: only `PODBENCH_NODE_NAME` arrived and `PODBENCH_TARGET_CID` did not.

sshd reads `SetEnv` as whitespace-separated `NAME=value` pairs, so a value containing
whitespace ends the pair early and a name containing a space or an `=` silently becomes a
different directive. `sshcfg.unsafe_set_env` screens both — but **does not swallow them**:
`ensure_sshd_config` writes the config with everything that did survive and *then* raises,
so `ensure_all` records the reason in the container's start-up log. A `PATH` with a space
in a directory name is not hypothetical, and dropping it without a word is the bug the
widened set exists to fix.

## `PATH` in an ssh session is there only because podbench put it there

`ssh host '<cmd>'` runs a non-interactive shell that sources nothing and inherits none of
the image's `ENV PATH`. What it gets is the `SetEnv` line above, or sshd's compiled-in
default if that line is missing. Two things therefore stay in the image:

- **`/usr/local/bin/podbench`** — the one wrapper in `image/bin/` — names
  `/app/.venv/bin/podbench` by absolute path. `/usr/local/bin` is on sshd's compiled-in
  `PATH` whatever else happens, so the verb still resolves in a seat whose `SetEnv` line
  was never written or was refused. If you add a file there, do the same.
- **`/etc/profile.d/podbench.sh`** — `SetEnv` does not settle an *interactive login*
  shell. Debian's `/etc/profile` assigns `PATH` outright rather than appending, so it
  overwrites what the session was handed; the fragment is sourced afterwards and puts the
  venv back.

## sshd needs an NSS identity

sshd resolves the login name through NSS **before** it looks at a key, so a uid no NSS
source has a record for fails as `Permission denied (publickey)` — pointing at the key,
which is not the problem. Which *source* supplies that record is the subject of the other
skill and is deliberately not `/etc/passwd` for a live-pod seat. `ssh-keygen` cannot even mint a host key (`No user exists for uid
1000`), and `-C` does not help: it calls `getpwuid()` regardless.

See the `ephemeral-containers` skill for the two mechanisms that supply an identity and
why they differ by container kind.

## The seat's uid picks the layout, and the rung is only a guess at it

`SshdLayout.for_uid` puts root's config in `/etc/podbench/sshd_config` and everyone
else's under `$HOME/.podbench/`. The agent picks with `os.geteuid()`; the launcher has to
*predict* the same answer, because the ProxyCommand names the file by absolute path.
Predicting from the **rung** is what broke at DLS on 2026-08-16: `rung_of_spec` reads the
rung back off the container, a mutating admission webhook there had stripped
`capabilities.add`, and a root seat therefore read back as the degraded rung —
`runAsUser: 0`, nothing added. `session.uid or _UNPINNED_UID` then read that pinned 0 as
"nothing pinned", and the stanza pointed at a non-root home the agent had never used.

Two things made it hard to see. The *landing* attach was fine — the ladder remembers the
rung it asked for — so only a reconnect broke, which reads as "it worked yesterday". And
the ProxyCommand's error arrives on the ssh client's stderr, which VS Code reports as a
resolver error with no path in the summary; you have to open the Remote-SSH log.

Same trap for the login name: `podbench` is registered only for a uid NSS cannot already
resolve, so a root seat is `root` and nothing else. Both now come from what the seat
itself is — the uid pinned on it, and its own answer to `--print-login-user`.

## Host keys

Fresh keys per attach would mean either `StrictHostKeyChecking no` — teaching users to
disable host verification — or a warning on every new pod. podbench instead manages
`known_hosts` programmatically, keyed on `HostKeyAlias podbench-<pod uid>`, and supports a
stable key from a Secret. Do not "simplify" this to `no`.

## Measured budgets

Cold connect ~0.345 s; ~0.06 s over `ControlMaster` (≈6×). ~26 MB/s pod→client, ~13 MB/s
client→pod. ~10 MB RSS per live session. 600 s idle survives with no keepalives; a
stalled transport needs them.
