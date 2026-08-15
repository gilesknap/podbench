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

The launcher injects `PODBENCH_TARGET_CID` into the container spec and it reaches the
container — but **not** an ssh session, and a non-interactive `ssh host 'pids'` sources no
profile either. Without forwarding, the in-pod helpers fall back to *guessing* which
processes belong to the target.

`SetEnv` in the sshd config is the only route that survives. And:

> **One `SetEnv` directive carrying every pair, never one per variable.** sshd resolves
> each keyword **first-match-wins**, so a second `SetEnv` line is silently ignored. This
> shipped once: only `PODBENCH_NODE_NAME` arrived and `PODBENCH_TARGET_CID` did not.

Values containing whitespace are dropped — a space ends the pair early.

## Nothing is on `PATH` in an ssh session

`ssh host '<cmd>'` runs a non-interactive shell that sources nothing and does not inherit
the image's `ENV PATH`. Every helper in `image/bin/` names `/app/.venv/bin/podbench` by
absolute path for exactly this reason. If you add a helper, do the same.

## sshd needs an NSS identity

sshd resolves the login name through NSS **before** it looks at a key, so a uid with no
`/etc/passwd` entry fails as `Permission denied (publickey)` — pointing at the key, which
is not the problem. `ssh-keygen` cannot even mint a host key (`No user exists for uid
1000`), and `-C` does not help: it calls `getpwuid()` regardless.

See the `ephemeral-containers` skill for the two mechanisms that supply an identity and
why they differ by container kind.

## Host keys

Fresh keys per attach would mean either `StrictHostKeyChecking no` — teaching users to
disable host verification — or a warning on every new pod. podbench instead manages
`known_hosts` programmatically, keyed on `HostKeyAlias podbench-<pod uid>`, and supports a
stable key from a Secret. Do not "simplify" this to `no`.

## Measured budgets

Cold connect ~0.345 s; ~0.06 s over `ControlMaster` (≈6×). ~26 MB/s pod→client, ~13 MB/s
client→pod. ~10 MB RSS per live session. 600 s idle survives with no keepalives; a
stalled transport needs them.
