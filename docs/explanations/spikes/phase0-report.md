# Podbench — Phase 0 gate report

**Date:** 2026-08-15
**Cluster under test:** k3s v1.34.6+k3s1, kubectl v1.36.3, 6 nodes, mixed amd64 (nuc2, ws03) /
arm64 (node01 raspi, node02–04 rockchip RK3588).
**Sources:** `spikes/s1.md` … `spikes/s5.md` (all read in full), plus `spikes/capreport.sh`.

---

## 1. Verdict table

| Spike | Subject | Verdict | One-line reason |
|---|---|---|---|
| **S1** | ssh transport: `sshd -i` over `kubectl exec` as ProxyCommand | **PASS** | Every transport property held — remote exec, PTY, scp/sftp, 8-way concurrency, `-L`/`-R`/`-D`, agent forwarding, ControlMaster — and a real vscode-server 1.133.0 completed an HTTP 200 + WebSocket `101 Switching Protocols` handshake through `ssh -L`, with no port-forward and no pod IP. |
| **S2** | vscode-server inside an ephemeral container | **PASS** | Server downloads, extracts, starts, installs extensions and survives client disconnect on **both** amd64 and arm64; cold path 5.76 s / ~10 s. Passes on function, but three brief assumptions (version lock, ~1 GB budget, OOM recoverability) are falsified. |
| **S3** | gdb attach with sysroot against a distroless target | **PASS** | Against `gcr.io/distroless/cc-debian12` (no `/bin/sh`, proven), an ephemeral container with `CAP_SYS_PTRACE` attached to a non-descendant PID under Yama `ptrace_scope=1`, resolved libraries via `/proc/<pid>/root`, and hit a breakpoint with function names, args, locals and source lines. |
| **S4** | Python takeover: dev pod, uv editable install, relaunch on the pod IP | **PASS** | Full Iterate loop works: authored dev-pod spec → uv + CPython 3.12 + `uv pip install -e .` in a real sidecar → relaunch on the shared-netns pod IP:8080 → edited string served **through the Service** in **1.18 s** per edit→relaunch→verify cycle. Vanilla `--copy-to` alone cannot do it (see §3). |
| **S5** | No-cap fallback, Yama diagnosis, capability ladder | **PASS** | The degraded rung is admitted under PSA `restricted:latest` and is genuinely useful (6/6 `/proc` reads at target UID, full source-level debugging via gdb-**launch** with `CapEff: 0000000000000000`), and `capreport` names the blocker correctly in all five configurations tested. |

**5 PASS / 0 PARTIAL / 0 FAIL.**

---

## 2. Gate decision

**Phase 1 may begin.** The brief's condition — *"Do not start Phase 1 until all five pass or the
brief is amended with what was learned"* — is satisfied on the first clause: all five spikes
returned PASS on their stated success criteria.

Nothing is a PARTIAL or FAIL, so no amendment is *required* to unblock the gate. **However**, five
of the brief's load-bearing assumptions were empirically falsified, and three of them silently
break the product's headline demos if implemented as written. These are not "nice to know" — they
change what Phase 1–6 must build:

**Blocking amendments — must be folded into the brief before the phase that consumes them:**

| # | Amendment | Blocks | Found by |
|---|---|---|---|
| A1 | The "download debuginfod sources" story for Observe mode does not exist on Debian/Ubuntu. A source-provisioning design is needed. | Phase 2/3 (Observe mode) | S3 |
| A2 | `kubectl debug --copy-to` strips **all labels**, so the Iterate demo ("edit code, curl the Service, see your change") cannot work. The launcher must author the pod spec. | Phase 4 (Iterate mode) | S4 |
| A3 | The VS Code server version lock is a hard handshake rejection, not a soft fallback. A baked server is correct for ≤ ~4 weeks. | Phase 1/2 (image + bootstrap) | S2 |
| A4 | `capabilities.add: [SYS_PTRACE]` on a non-root container is a **silent no-op** (`CapEff = 0`). Any ladder rung naming both a non-zero `runAsUser` and `SYS_PTRACE` is invalid by construction. | Phase 1 (launcher) + Phase 5 (fallback) | S5 |
| A5 | The `~1 GB` Observe-mode disk budget is already exceeded by the stock server alone (680.8 MiB) before any extension. Restate as ~1.5 GB, or trim. | Phase 1 (image sizing) | S2 |

**One unresolved contradiction between spikes** must be settled before Phase 1 pins the
ProxyCommand — see §5, R1. It is not a gate blocker because a configuration exists that satisfies
both spikes' constraints simultaneously (`sshd -i -e -o LogLevel=ERROR`), but the brief must state
*why* rather than leaving it to a future "cleanup".

---

## 3. Amendments to the design brief

### 3.1 — `-e` on `sshd -i` is about keeping fd 2 **open**, not about log tidiness (S1)

The brief frames `-e` as "log to stderr so stderr noise does not pollute the SSH stream". That is
the wrong mechanism, and the wrong mechanism leads to the wrong "cleanup". S1 isolated the real
cause with a sshd-free experiment: **closing or replacing fd 2 in a `kubectl exec`'d process tears
down the whole CRI exec stream**, silently truncating stdin/stdout with `rc=0`.

```
# control — stderr left alone
( echo one; sleep 4; echo two ) | kubectl exec -i pod -c c -- sh -c 'exec cat'
one
two                     # both arrive

# stderr → /dev/null, which is exactly what sshd's inetd path does without -e/-E
( echo one; sleep 4; echo two ) | kubectl exec -i pod -c c -- sh -c 'exec 2>/dev/null; exec cat'
one                     # "two" SILENTLY LOST, rc=0
```

The failure, when it hits sshd, is a misleading network error:

```
command terminated with exit code 255
ssh_dispatch_run_fatal: Connection to UNKNOWN port 65535: Broken pipe
```

with `ssh -vv` dying at `debug1: expecting SSH2_MSG_KEX_ECDH_REPLY`. The full matrix:

| ProxyCommand | result |
|---|---|
| `sshd -i -e` | works |
| `sshd -i -E /tmp/sshd.log` | works (`-E` implies `log_stderr=1`) |
| `sshd -i` (syslog) | **FAILS, reproducibly** |
| `sh -c '/usr/sbin/sshd -i'` (shell does **not** exec, holds fd 2) | works — **masks the bug** |
| `sh -c 'exec /usr/sbin/sshd -i'` | FAILS |
| `sh -c 'exec /usr/sbin/sshd -i -e 2>&1'` (stderr merged to stdout) | FAILS |
| `sh -c 'echo hello; exec /usr/sbin/sshd -i -e'` (pre-banner noise) | works |

**Consequence:** any wrapper that redirects or closes sshd's stderr breaks the transport with a
network-looking error, and a non-`exec` wrapper hides the requirement in testing until someone adds
`exec` for tidiness. Pin the invocation and add a regression test that asserts the transport
**dies** without `-e`.

### 3.2 — debuginfod gives symbols but **not sources** on Debian (S3)

The brief leans on debuginfod for Observe mode's disk budget. Symbols do arrive — S3 got a fully
symbolised, source-line-annotated backtrace across coreutils *and* glibc for a stripped binary, for
**4.7 MB** of `~/.cache/debuginfod_client` (glibc alone 4.0 MB), and it works *through* the sysroot
including for distroless libs. But **every source fetch failed**:

```
Download failed: Invalid argument.  Continuing without source file ./src/sleep.c.
142	src/sleep.c: Inappropriate ioctl for device.
```

Two independent, both-fatal causes, each proven:

1. Debian's `-dbgsym` packages carry `DW_AT_comp_dir : .` (reproducible-builds normalisation),
   confirmed by `readelf --debug-dump=info` on the downloaded debuginfo. The debuginfod protocol
   requires an **absolute** `/FILENAME`, so gdb's `./src/sleep.c` is rejected client-side with
   `EINVAL`.
2. The server has no sources anyway:
   `https://debuginfod.debian.net/buildid/<bid>/debuginfo` → **200**, but
   `.../source/src/sleep.c` → **404**. Same on the federated `debuginfod.elfutils.org`, and for
   glibc under every plausible path form (`/usr/src/glibc/…`, `/build/glibc-…`, `/sysdeps/…`) →
   `Server query failed: No such file or directory`.

**Amendment:** keep debuginfod wired up (symbols are cheap and valuable) but Podbench needs a real
source-provisioning story: source in the target image, a source sidecar volume, or client-side
source mapping to the developer's checkout. Fedora/RHEL debuginfod is known to serve sources;
Debian/Ubuntu targets will not.

Also, and not in the brief: **`ca-certificates` is mandatory in the image**. `debian:bookworm-slim`
ships none and `libdebuginfod` then fails the TLS handshake **silently** — no `Downloading…` lines,
every library shows `(*) missing debugging information`. Users will read that as "debuginfod
doesn't work in podbench".

### 3.3 — `set sysroot` is not the whole fix; four more things are mandatory (S3)

The brief treats sysroot as *the* fix. It is one of five required elements.

* **Sysroot does not cover source lookup.** With a correct sysroot and the source physically
  present in the target rootfs, gdb still reports `19  victim.c: No such file or directory.`
  Function names, args and locals are all correct; only the source text is missing.
  Fix: `directory /proc/<pid>/root` — generic, needs no `DW_AT_comp_dir` knowledge, and yields a
  clean `info source` fullname (`/proc/1/root/app/src/victim.c`).
  **Do not** use the generic `set substitute-path / /proc/<pid>/root/` — it functions, but gdb
  re-applies the substitution on display and emits
  `/proc/1/root/proc/1/root/proc/1/root/proc/1/root/app/src/victim.c`, which is exactly the string a
  DAP client hands to the editor.
* **Sysroot breaks `libthread_db`.** Once set, gdb refuses to auto-load the target's
  `libthread_db.so.1`: `auto-loading has been declined by your 'auto-load safe-path' … thread
  debugging will not be available.` No `info threads`, no per-thread backtraces. Fix, narrowly:
  `add-auto-load-safe-path /proc/<pid>/root` (not `set auto-load safe-path /`).
* **Ordering is load-bearing, and getting it wrong fails plausibly.**

  | order | libs | main executable / user frames |
  |---|---|---|
  | `set sysroot` → `file` → `attach` | correct | correct |
  | `attach` → `set sysroot` | fixed up on the fly | **broken** — `Error reading attached process's symbol file.`, frames `#3 0x… in ?? ()`, `#5 0x405d400000000000 in ?? ()` |
  | `attach` → `set sysroot` → `file <root><exe>` → `sharedlibrary` | correct | recovers |

  A plausible-looking libc backtrace with garbage above it is the most dangerous failure found in
  S3. An explicit `file /proc/<pid>/root$(readlink /proc/<pid>/exe)` **before** `attach` is required.

### 3.4 — The "no sysroot" failure mode is not what the brief describes (S3)

gdb 13's default sysroot is `target:`, not `/`. With only `CAP_SYS_PTRACE` the no-sysroot case
fails **loudly**, not silently:

```
warning: "target:/app/victim": could not open as an executable file: Operation not permitted.
Could not open `target:/lib/x86_64-linux-gnu/libc.so.6' as an executable file: Operation not permitted
#0  0x000076694f49b503 in ?? ()
```

Root cause proven: gdb's `target:` access uses `linux_mntns_access_fs()` → `setns(CLONE_NEWNS)`,
which needs **`CAP_SYS_ADMIN`**, not `CAP_SYS_PTRACE`.

The *wrong-symbols* mode the brief describes requires `set sysroot /` **and** a version-skewed
debug image. Against Debian-12-distroless the bug is **invisible** (both glibcs share build-id
`93ac61ec…` and the backtrace is correct). Against `ubuntu:24.04` (glibc 2.39) it is loud:

```
### WRONG: set sysroot /
0x000077f6377dcb7a in wcsxfrm_l () from /lib/x86_64-linux-gnu/libc.so.6
#2  0x00005c459789e35f in outer_loop () at victim.c:29        <- wrong line, too
#5  0x00005c45978a0d80 in __frame_dummy_init_array_entry ()

### RIGHT: set sysroot /proc/1/root
0x000077f6377dcb7a in clock_nanosleep () from /proc/1/root/lib/x86_64-linux-gnu/libc.so.6
#3  0x00005c459789e35c in outer_loop () at victim.c:35
```

`clock_nanosleep` reported as `wcsxfrm_l`, duplicated/interleaved frames, and even the user-code
line number wrong. **Ship this transcript verbatim as the canonical docs example**, and note that a
matched-distro debug image hides the bug entirely.

**Anti-pattern to document, not adopt:** `CAP_SYS_ADMIN` makes gdb's default `target:` sysroot work
with **zero configuration**. It also breaks `libthread_db` (`Expected absolute pathname for
libpthread in the inferior, but got target:/lib/…`) and is container-escape-adjacent, rejected by
any restricted PSA. Document it so nobody rediscovers it as a shortcut.

### 3.5 — `--copy-to` strips **all labels and annotations**, so the clone gets no Service traffic (S4)

The brief claims `--copy-to` strips probes. It does — and much more. Measured, twice:

| field | after `--copy-to` |
|---|---|
| `metadata.labels` | **removed (nil)** |
| `metadata.annotations` | **removed** |
| `metadata.ownerReferences` | **removed** (orphan; no RS adoption, no GC) |
| readiness / liveness / startup probes | **removed on every container**, even in `--image` mode |
| `nodeName` | cleared; pod reschedules normally |
| ports / resources / volumeMounts / volumes | preserved |
| `shareProcessNamespace` | set `true` **only** when `--image` adds a container |

**The headline Iterate demo cannot work with vanilla `--copy-to` at all** — the clone is invisible
to the Service, the endpointslice keeps only the original pod, and the user sees the *old* response
forever with no diagnostic. `kubectl debug --copy-to` also prints **nothing** on success, and has
**no `--dry-run`** (`error: unknown flag: --dry-run`), so the output cannot even be previewed.

Two further `--copy-to` limits that justify the authored-spec approach:
`--copy-to --image --container podbench` does create a real sidecar and does set
`shareProcessNamespace: true`, but that container comes out with `resources: {}` and **no workspace
volume**, and there is no flag to set either.

Label policy is the crux: keeping the Service-selector labels while **dropping `pod-template-hash`**
put the dev pod in the endpointslice without making it a ReplicaSet member (verified: RS stayed
`DESIRED 1 / CURRENT 1`, neither pod reaped). Keeping the hash would have made two pods match a
`replicas: 1` RS and one would be reaped.

### 3.6 — Taking Service traffic is not binary; there is a ~50 % broken window (S4)

A probe-less dev pod is `Ready` the instant its containers start, so it joins the endpointslice
while nothing is listening on `:8080`:

```
000 ERR 000 ERR 200 000 ERR 200 200 000 ERR 000 ERR
```

Fix, verified in both directions: a `tcpSocket: {port: 8080}` readinessProbe **on the podbench
sidecar**. The dev pod then joins the Service exactly when the relaunched process binds and **drops
out again within ~6 s** when the process is killed — Service membership tracks the inner loop
automatically. This belongs in the authored spec, not as an option.

### 3.7 — The VS Code version lock is a hard handshake rejection (S2)

Probed at the raw remote-protocol level (WS upgrade, 13-byte framed control messages):

```
### A: WRONG commit (deadbeef…)
after connectionType:  {"type":"error","reason":"Client refused: version mismatch"}   <<CLOSED>>
### B: CORRECT commit (a5b5009…)
after connectionType:  {"type":"error","reason":"Unauthorized client refused"}        <<CLOSED>>
```

(B fails only on the *next* check, the signed-data challenge the probe fakes — the commit check
passed.) Confirmed in `out/server-main.js`:

```js
let S=v.commit, T=this._productService.commit;
if (S && T && S !== T) return m("Client refused: version mismatch");
```

No negotiation, no minor-version tolerance, no server-side override. **A baked server is correct
for at most ~4 weeks** (VS Code's monthly cadence) and breaks Insiders/stale clients immediately.
Since the download costs only 2.17 s and the whole cold path 5.76 s, while
`apt-get install openssh-server` costs **14–24 s**, the correct trade is: **bake the base, download
the server on first connect.**

### 3.8 — The ~1 GB Observe-mode budget is already blown (S2)

| Metric | amd64 (nuc2) | arm64 (node02, RK3588) |
|---|---|---|
| Server tarball | 224,027,728 B (213.6 MiB) | 215,331,901 B (205.4 MiB) |
| Download time | 2.17 s | 2.26 s |
| Extract time | 5.62 s | 5.56 s |
| **Extracted server on disk** | **713,893,077 B (680.8 MiB)** | **669,263,321 B (638.3 MiB)** |
| Idle server RSS | ~97 MiB | ~92 MiB |
| `ms-vscode.cpptools` on disk | **330 M** (x64) | **261 M** (arm64) |
| cpptools install | 8.35 s | 8.07 s |
| Cold bootstrap over ssh | **5.76 s** | ~10 s |

`~/.vscode-server` reached **995 MiB on arm64 with exactly one extension**, and **2.2 GB** with two
server versions and six extensions. `data/data/CachedExtensionVSIXs` is another **190 M** after six
extensions (safe to delete post-install). Realistic Observe container: **1.1–1.3 GB of node disk**.

Disk, not memory, is the constraint (idle RSS is only ~97 MiB). Trimming works: deleting
`extensions/copilot` (160 M), `extensions/copilot-chat` and `extensions/mermaid-markdown-features`
(59 M) takes the server from 646 M to **428 M (−34 %)** and it still starts and serves
`/version`.

**Amendment: restate the budget as ~1.5 GB, or make trimming mandatory.**

### 3.9 — An OOM inside the debug container is unrecoverable (S2)

The ephemeral container has **no cgroup limits of its own** (`memory.max = max`,
`cpu.max = max 100000`; an ephemeral container spec cannot carry `resources`) but is still confined
by the **pod** cgroup. Allocating 3.6 GiB in a pod whose app container is limited to 3Gi
OOM-killed **the ephemeral container itself**:

```
$ kubectl get pod target-amd64 -o jsonpath='{.status.ephemeralContainerStatuses[0].state}'
{"terminated":{"exitCode":137,"reason":"OOMKilled",...}}
$ kubectl get pod target-amd64 -o jsonpath='{.status.containerStatuses[0].restartCount}'
0                                     # app container untouched
```

Ephemeral containers cannot restart, and a replacement comes up with a **completely fresh rootfs**
(`ls: cannot access '/root/.vscode-server'`) — the 690 MiB server, all extensions and the sshd host
keys are gone.

**New lever the brief does not mention:** `kubectl patch pod --subresource resize` works on this
cluster and is **non-disruptive to a running ephemeral container**. 3Gi→6Gi→7Gi, `ResizeCompleted`
event, zero restarts, ephemeral container stayed `{"running":{…}}` throughout, and could then
allocate 4.2 GiB. **Podbench can make room for itself before attaching.**

### 3.10 — `capabilities.add:[SYS_PTRACE]` on a non-root container is a silent no-op (S5)

This is the mystery-EPERM the brief names as the worst field failure — and it is **self-inflicted
by the launcher's own manifest**, not caused by the cluster. With `runAsUser: 1000`:

```
Uid:	1000	1000	1000	1000
CapPrm:	0000000000000000
CapEff:	0000000000000000
CapBnd:	00000000a80c25fb      <-- SYS_PTRACE (bit 19) in BOUNDING only
CapAmb:	0000000000000000
```

The kernel grants capabilities to non-root UIDs only via the ambient set, which the CRI does not
populate. The pod is admitted, the container runs, everything looks right, and ptrace fails with a
bare `EPERM`. **`CAP_SYS_PTRACE` requires `runAsUser: 0`, full stop.** The brief treats "grant
SYS_PTRACE" and "run as UID 1000" as independent ladder knobs; they are mutually exclusive.

(`kubectl` does warn on stderr in passing: *"Non-root user is configured for the entire target Pod,
and some capabilities granted by debug profile may not work…"* — easy to miss.)

### 3.11 — Root without the cap is strictly **worse** than non-root at the target's UID (S5)

| path | uid 1000, CapEff 0 | uid 0, CapEff 0 | uid 0 + SYS_PTRACE |
|---|---|---|---|
| `readlink /proc/T/root` | **OK** | FAIL | OK |
| `ls /proc/T/root/etc` (**sysroot**) | **OK** | Permission denied | OK |
| `/proc/T/maps`, `/proc/T/smaps` | **OK** | Permission denied | OK |
| `/proc/T/environ` | **OK** | Permission denied | OK |
| `/proc/T/cmdline`, `/fd`, `/status`, `/wchan` | OK | OK | OK |
| `readlink /proc/T/exe`, `/cwd` | **OK** | FAIL | OK |
| `/proc/T/stack` | denied | denied | denied *(needs CAP_SYS_ADMIN)* |
| `/proc/T/syscall` | **Operation not permitted** | Operation not permitted | OK |
| `open /proc/T/mem` | **Permission denied** | Permission denied | SUCCESS |

`dbg-c` (uid 0, no cap) reads 3/6 probe paths; `dbg-b` (uid 1000, zero caps) reads 6/6. Proof the
sysroot really crossed the container boundary at zero caps — an Ubuntu 24.04 debug container
reading the Debian target's rootfs:

```
PODBENCH_SECRET_MARKER=s5-environ-canary
6549010cd000-6549010ce000 r--p 00000000 00:4f5 2378534   /usr/local/bin/python3.12
PRETTY_NAME="Debian GNU/Linux 13 (trixie)"
```

**Amendment:** the degraded fallback must match the **target's UID**, never default to root.
And `/proc/<pid>/mem` and `/proc/<pid>/syscall` are **not** in the read-exempt set — they use
`PTRACE_MODE_ATTACH`. Any "read-only memory inspection" feature planned on `/proc/<pid>/mem` will
not work in the degraded rung.

### 3.12 — "Start it yourself and attach" does **not** satisfy Yama (S5)

| relationship | ptrace attach at uid 1000, CapEff 0, yama=1 |
|---|---|
| target is a **fork of the tracer process itself** | **rc=0, OK** |
| target started by the **same shell** (sibling of gdb) | **rc=-1 EPERM** |
| sibling that called `prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)` | **rc=0, full backtrace** |
| `gdb ./binary` (`PTRACE_TRACEME` via fork/exec) | **works** |

The natural workflow `myprog & ; gdb -p $!` makes gdb a **sibling**, and it is denied. But
`gdb ./binary` gives full source-level debugging — breakpoints, `run`, `continue`, backtrace,
argument and global inspection — at uid 1000 with `CapEff: 0000000000000000`, under PSA
`restricted` with `RuntimeDefault` seccomp.

**Amendment: document and design the inner loop as gdb-LAUNCH, not gdb-attach.** Attach is the
privileged special case. Where attach to an already-running process is genuinely required, offer
`prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)` as a documented one-line change to the target program —
the only fix needing no capability and no node change.

### 3.13 — Yama is not uniform, and the difference is kernel flavour, not arch (S5)

| node | arch | kernel | `ptrace_scope` |
|---|---|---|---|
| node01 | aarch64 | `6.8.0-1051-raspi` | `1` |
| node02 | aarch64 | `6.1.0-1025-rockchip` | **file does not exist** |
| node03 | aarch64 | `6.1.0-1025-rockchip` | **file does not exist** |
| node04 | aarch64 | `6.1.0-1025-rockchip` | **file does not exist** |
| nuc2 | x86_64 | `6.17.0-20-generic` | `1` |
| ws03 | x86_64 | `7.0.0-28-generic` | `1` |

Confirmed empirically, not inferred: the **byte-identical** no-cap same-UID ephemeral container
reports `DENIED BY YAMA` on nuc2 and `LIVE ATTACH AVAILABLE` (`PTRACE_ATTACH(1) rc=0`) on node02.
Two arm64 nodes disagree with each other, so an arch-based heuristic would be wrong.
**Podbench must probe per-node and can never cache a cluster-wide answer.**
`/proc/sys` is mounted `ro` in every container, so `ptrace_scope` is genuinely a node-level knob.

### 3.14 — `kubectl debug` flag semantics differ from the brief (S1, S3, S5)

* **`--custom` takes a FILE PATH, not inline JSON** (S1):
  `error: must pass a container spec json file for custom profile: open {"securityContext":…}: no such file or directory`.
  The CLI must write a temp file.
* **`--custom` MERGES with a profile applied AFTER your JSON** (S5). `--custom
  '{"securityContext":{"runAsUser":1000}}'` produced
  `{"capabilities":{"add":["SYS_PTRACE"]},"runAsUser":1000}` — i.e. the invalid rung from §3.10,
  built for you:

  | `--profile` | resulting securityContext with `--custom '{"runAsUser":1000}'` |
  |---|---|
  | *(default, legacy)* | `{"capabilities":{"add":["SYS_PTRACE"]},"runAsUser":1000}` |
  | `general` | `{"capabilities":{"add":["SYS_PTRACE"]},"runAsUser":1000}` |
  | `baseline` | `{"runAsUser":1000}` |
  | `restricted` | `{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"runAsNonRoot":true,"runAsUser":1000,"seccompProfile":{"type":"RuntimeDefault"}}` |

  **Podbench must POST to the `ephemeralcontainers` subresource directly**, not build its ladder on
  `kubectl debug --custom`. (Silver lining: `--profile=restricted` emits exactly the correct rung-2
  shape, so it is a valid manual fallback to document for users.)
* **`--profile=general` already adds `SYS_PTRACE`** (S3) — the `--custom` file for that purpose is
  redundant. Verified: a container created with `--profile=general` and **no** `--custom` still came
  out with `{"capabilities":{"add":["SYS_PTRACE"]}}`.

### 3.15 — Target-PID discovery must key off the container runtime ID (S3)

| rule | verdict |
|---|---|
| "target is PID 1" | works only in the simple case; **wrong** under `shareProcessNamespace: true`, where PID 1 is `/pause` |
| `/proc/<pid>/cgroup != "0::/"` | excludes my own processes but **includes every other ephemeral debug container's** — 3 spurious hits observed on a pod with 4 debug containers |
| `/proc/<pid>/ns/mnt == /proc/1/ns/mnt` | exact on a normal pod; **fails completely** under `shareProcessNamespace: true` |
| **`/proc/<pid>/cgroup` contains the target container's runtime ID** | **correct in all cases** |

```
1    0::/../cri-containerd-d5daaa53…scope   /pause
597  0::/../cri-containerd-87d20e23…scope   /app/victim      <- target containerID
603  0::/../cri-containerd-7206c89b…scope   /bin/sleep 100000
609  0::/                                   sleep infinity   <- my own
```

Note the in-container cgroup path is **relative** (`0::/../cri-containerd-<id>.scope`) because the
ephemeral container gets its own cgroup namespace — **substring** match on the ID, never path
equality. This means Podbench must inject the target container ID as an env var at `kubectl debug`
time (`--env=PODBENCH_TARGET_CID=${CID#*://}`), which the brief does not anticipate.

### 3.16 — Port conflicts have a silent, dangerous mode (S4)

Three distinct behaviours, only one of them loud:

* **Plain conflict:** `OSError: [Errno 98] Address already in use` — fine.
* **`SO_REUSEPORT` silent split:** if the existing listener set `SO_REUSEPORT` (uvicorn
  multi-worker, gunicorn `reuse_port`, many Go/Rust servers), a second `SO_REUSEPORT` bind
  **succeeds with no error** and the kernel splits traffic. Measured through the Service:
  5 × new code / 3 × old code / 2 × new code. Nothing in any log says so.
* **`TIME_WAIT` lockout:** after a `SO_REUSEPORT` listener has served connections, a plain
  `SO_REUSEADDR` rebind fails for the full ~60 s TIME_WAIT window **while `ss -lntp` shows no
  listener at all**. Recovery verified at ~70 s.

And a false PASS: a naive "poll the port" relaunch wrapper printed `LISTENING after 1 polls`,
exit 0, while the relaunch had actually died with `EADDRINUSE` and the Service kept serving stale
code. **Pre-flight the port; verify your own child owns the socket.**

### 3.17 — The mount-namespace rule is confirmed but harsher than stated (S4)

From inside the target container, a `.pth` pointing at a debug-container-only path:

* **Path-style `.pth` — silently ignored, no warning at all.** `site.py` only appends directories
  that exist. `on sys.path? False`, then `ModuleNotFoundError: No module named 'podbench_demo'`
  surfaces much later as an unrelated-looking import error.
* **Exec-style `.pth`** (what modern PEP-660 editable installs emit) — prints a traceback
  (`Error processing line 1 of …/podbench_exec.pth … Remainder of file ignored`) but is
  **non-fatal**; the interpreter still exits 0.

Also, the `/proc/<pid>/root` bridge is **one-directional by capability**: sidecar → app works, app →
sidecar finds nothing (`CapEff` delta is exactly bit 19). This is a *good* security property — a
compromised app container cannot reach the debug toolchain — but it also means "just symlink from
the target into the workspace" is not available as a workaround. The design rule stands.

### 3.18 — Two rejection channels, not one (S5)

**Synchronous** (API server, PSA). Stable substring across every level and phrasing:

```
must not include "SYS_PTRACE" in securityContext.capabilities.add
```

The surrounding phrase differs: `unrestricted capabilities` under `restricted:latest`,
`non-default capabilities` under `baseline:latest`.

**Asynchronous** (kubelet). On a target pod with `runAsNonRoot: true`, a root debug container is
accepted by the API server (`kubectl debug` exits **0**) and then fails:

```
dbg-root  {"waiting":{"message":"container's runAsUser breaks non-root policy
(pod: \"victim_podbench-s5(b89a16a0-…)\", container: dbg-root)",
"reason":"CreateContainerConfigError"}}
```

The launcher must poll `.status.ephemeralContainerStatuses[?(@.name==…)].state.waiting` for this.
A pre-flight read of the target pod's `securityContext.runAsNonRoot` lets it skip rung 1 instead.

### 3.19 — Assorted brief corrections

* **Root is not needed for the transport** (S1). `sshd -i` runs fine as uid 1000 (verified via
  `setpriv`: remote command `uid=1000(dev)`, `scp` rc=0, PTY `/dev/pts/0`) — and then needs **no
  `/run/sshd` at all**. As root, `/run/sshd` is mandatory: without it every connection dies with
  `Missing privilege separation directory: /run/sshd`. So config and host keys should be
  user-relative (`$HOME/.podbench/`) and PSA-restricted deployments stay viable; gate only
  ptrace/`/proc` features on root.
* **`kubectl exec -t` is a latent footgun** (S1). From a script kubectl silently degrades to
  non-tty (`Unable to use a TTY - input is not a terminal…`) and it appears to work; with a **real**
  TTY forced onto the ProxyCommand (`script -q -c "kubectl exec -it …"`) the ssh client **hung
  indefinitely**. Podbench should refuse `-t`.
* **Pre-banner stdout noise is tolerated to exactly 1023 lines** (S1); 1024 →
  `banner exchange: Connection to UNKNOWN port 65535: invalid format`. Any stdout write *after*
  sshd starts is fatal. Relevant for MOTD-printing entrypoints.
* **`ControlPath` hits the 108-byte `sun_path` limit** (S1): `ControlPath too long ('…' >= 108
  bytes)`. Control sockets must live somewhere short (`/tmp/podbench-cm/%C`), never next to a
  kubeconfig or in a workspace directory.
* **`ServerAliveInterval` is mandatory** (S1). A *stalled* transport (SIGSTOP on the ProxyCommand —
  what an apiserver/konnectivity hiccup looks like) hangs ssh **forever** with no keepalive; with
  `ServerAliveInterval=5 ServerAliveCountMax=3` it fails in **19 s**
  (`Timeout, server podbench-target not responding.`). A *hard* kill or pod deletion is detected
  instantly and cleanly (`command terminated with exit code 137` → `client_loop: send disconnect:
  Broken pipe`, rc=255).
* **`kill -0 $pid` is not a valid liveness check** (S2). Under `--target`, PID 1 in the shared
  namespace is the target app, which does not reap; orphans become permanent zombies. S2's v1
  bootstrap reported "already running, port 38327" for a server that had exited — a client would
  have connected to a dead port. **Liveness must be an HTTP probe of `/version` matched against the
  expected commit.**
* **`bin/code-server` exits 0 when the interpreter is missing** (S2). On Alpine it printed
  `/srv/node: not found` and returned **exit 0**. Never trust its exit code.
* **The Alpine/musl claim holds, for a more basic reason than the brief gives** (S2): the failure is
  a missing ELF interpreter (`/lib64/ld-linux-x86-64.so.2`), not symbol versions. Independent symbol
  analysis pins the requirement at exactly `GLIBC_2.28` + `GLIBCXX_3.4.21`, i.e. Debian ≥ 10,
  Ubuntu ≥ 18.04, RHEL/UBI ≥ 8. `gcompat` untested.
* **Remote-SSH's `--enable-remote-auto-shutdown` kills the server after exactly 5 min idle** (S2):
  started 10:44:32, `ServerLifetime: all consumers inactive, shutting down` at 10:49:32. Omit it.
* **Air-gap needs FOUR host groups, not two** (S2), the fourth observed by strace:
  tarball origin (`update.code.visualstudio.com` → `vscode.download.prss.microsoft.com`), gallery
  (`marketplace.visualstudio.com`), asset CDN (`*.vscode-unpkg.net`, `main.vscode-cdn.net`), and
  **`crl.microsoft.com` / `www.microsoft.com` for VSIX signature verification**.
* **"arm64 is slow" does not apply here** (S2). The RK3588 extracted 646 MiB in **5.56 s** and
  downloaded 205 MiB in **2.26 s** — statistically identical to the x86 NUC. That claim was about
  image pulls.
* **`RuntimeDefault` seccomp does not block `ptrace(2)`** but **does** block
  `personality(ADDR_NO_RANDOMIZE)` (S5): gdb warns `Error disabling address space randomization:
  Operation not permitted` and leaves ASLR on, so addresses are non-reproducible run to run.
* **A gdb built with Python hard-fails if its Python stdlib is missing** (S5) — it does not degrade
  (`Python path configuration: … Python not initialized`). Ship `/usr/lib/pythonX.Y` (~19 MB) with
  `PYTHONHOME`/`PYTHONPATH`, or select a gdb without Python support.
* **Ephemeral containers CAN mount the pod's existing volumes** (S5) — the only practical way to get
  a toolchain into a non-root debug container that cannot `apt-get`.

---

## 4. Implementation constraints — checklist for Phase 1–6

### 4.1 ssh transport (Phase 1)

- [ ] **Exact ProxyCommand shape.** `-i` and `-e` are both mandatory; `-o LogLevel=ERROR` gives zero
      stderr bytes *and* keeps fd 2 alive:
      ```
      ProxyCommand kubectl -n <ns> exec -i <pod> -c podbench -- \
        /usr/sbin/sshd -i -e -f /etc/podbench/sshd_config -o LogLevel=ERROR
      ```
- [ ] **Never** `-t`. **Never** redirect, merge (`2>&1`) or close sshd's stderr. **Never** wrap in a
      shell that `exec`s without `-e`.
- [ ] Ship a regression test that asserts the transport **dies** without `-e`, so the fd-2 reason is
      documented in code (a non-`exec` wrapper masks it).
- [ ] **Exact minimal sshd_config** (5 lines, `sshd -t` → `CONFIG_OK`; no `PidFile` — `-i` never
      writes one):
      ```
      HostKey /etc/ssh/ssh_host_ed25519_key
      PermitRootLogin prohibit-password
      AuthorizedKeysFile /root/.ssh/authorized_keys
      UsePAM no
      Subsystem sftp /usr/lib/openssh/sftp-server
      ```
      Non-root variant (uid 1000, no `/run/sshd` needed) — user-relative paths + `StrictModes no`.
- [ ] **Generated client config defaults:**
      ```
      ServerAliveInterval 15
      ServerAliveCountMax 3
      ControlMaster auto
      ControlPath /tmp/podbench-cm/%C       # mode 0700, created at config-gen time; MUST be short
      ControlPersist 10m
      IdentitiesOnly yes
      ```
- [ ] Create `/run/sshd` **at container start** (it is often a tmpfs) when running as root; run
      `ssh-keygen -A` or mount host keys.
- [ ] **Startup self-check** for the fd-2 tripwire before declaring the pod ready: run
      `kubectl exec -i … -- sh -c 'exec cat'` with a delayed second line and confirm **both** lines
      return.
- [ ] RBAC required: `create pods/exec` + `get pods` in the namespace. Nothing else — no Services,
      no NetworkPolicy holes, no pod IP reachability.
- [ ] Budget: cold connect **0.345 s** median (0.058–0.062 s over ControlMaster, ~6×);
      **26 MB/s** pod→client, **13 MB/s** client→pod; ~10–11 MB RSS per live session;
      **0/30** churn failures.

### 4.2 vscode-server bootstrap (Phase 2)

- [ ] **Download on first connect; do not bake the server.** Take `$COMMIT` from the client
      (Remote-SSH supplies it; a native client reads the local `product.json`).
- [ ] Ship S2's **v2 bootstrap script verbatim**. Three load-bearing properties:
      HTTP `/version` liveness matched against `$COMMIT` (**never `kill -0`**), **stage-then-move**
      extraction, and `setsid nohup … </dev/null &`.
- [ ] Parse the port from `Extension host agent listening on <port>` (`--port=0`). Never trust
      `code-server`'s exit code.
- [ ] **Omit `--enable-remote-auto-shutdown`.**
- [ ] Base image must be **glibc ≥ 2.28**; `debian:bookworm-slim` (2.36) is the tested default.
      Document Alpine as unsupported.
- [ ] **Trim post-extract:** `rm -rf $SRV/extensions/{copilot,copilot-chat,mermaid-markdown-features}`
      (−218 MiB, −34 %, verified still serving `/version`) and
      `rm -rf $DATA/data/CachedExtensionVSIXs` (−190 MiB after 6 extensions).
- [ ] **Size the pod before attaching:** `kubectl patch pod --subresource resize` to raise the target
      container's memory limit, restore on detach. Fall back to a loud pre-flight warning when the
      subresource is unavailable. Say, on success too, that the raised limit is on the pod and not
      on whatever owns it, so a rollout reverts it (R13).
- [ ] Treat the ephemeral container as **strictly disposable** — nothing may live only in its
      writable layer. Name containers `podbench-<n>` with an incrementing suffix: a dead ephemeral
      container name is **burnt for the pod's lifetime**, and `kubectl debug … -- true` leaves a
      permanently unusable `Completed` container. Always pass a long-running command; gate readiness
      on `wait --for=jsonpath='…ephemeralContainerStatuses[?(@.name=="X")].state.running.startedAt'`.
- [ ] **Size budgets to plan against (measured):** server 680.8 MiB (amd64) / 638.3 MiB (arm64);
      cpptools 330 M / 261 M; one-extension `~/.vscode-server` 995 MiB; two servers + six extensions
      2.2 GB; idle RSS ~97 MiB / ~92 MiB; realistic Observe container **1.1–1.3 GB node disk**.

### 4.3 gdb (Phase 3)

- [ ] **Exact incantation** (S3's `dbg` helper, tested end to end). Order matters:
      ```
      set pagination off
      set sysroot /proc/<PID>/root                 # BEFORE attach
      directory /proc/<PID>/root                   # sources; NOT substitute-path
      add-auto-load-safe-path /proc/<PID>/root     # or you lose libthread_db
      set debuginfod enabled on                    # symbols only; needs ca-certificates
      file /proc/<PID>/root$(readlink /proc/<PID>/exe)   # strip a trailing " (deleted)"
      attach <PID>
      ```
- [ ] **PID discovery:** substring-match the target's `containerID` (scheme stripped) against
      `/proc/<pid>/cgroup`. Inject it at debug time as `PODBENCH_TARGET_CID`. Fallback
      (`cgroup != "0::/"`) is wrong when a second podbench session is attached — warn when used.
- [ ] Stay on `CAP_SYS_PTRACE` + explicit sysroot. **Never** `CAP_SYS_ADMIN`.
- [ ] Image contents: `gdb` (13.x Debian 12; pulls `libpython3.11`, budget for it),
      **`ca-certificates` (mandatory)**, `binutils` for `readelf`, optionally `debuginfod-find`.
      Match the debug image's distro/release to the common target base, but never rely on it.
- [ ] Docs must ship the §3.4 `wcsxfrm_l` transcript verbatim, state that sysroot fixes libraries
      not sources, and warn that `set sysroot` **after** `attach` produces a plausible-looking wrong
      backtrace.

### 4.4 Iterate mode / dev pod (Phase 4)

- [ ] **Author the pod spec in the launcher.** Do not shell out to `kubectl debug --copy-to`
      (no `--dry-run`, no `resources`, no workspace volume, silent on success).
- [ ] **Label policy:** copy the origin pod's labels, then delete `pod-template-hash`,
      `controller-revision-hash`, `controller-uid`, `batch.kubernetes.io/controller-uid`,
      `batch.kubernetes.io/job-name`, `job-name`, `statefulset.kubernetes.io/pod-name`. Add
      `podbench.dev/devpod: "true"` + an annotation recording the origin pod. Gate the whole thing
      behind an explicit `--take-traffic`, **default off** — silently joining a production Service
      is a foot-cannon.
- [ ] **Always** put `readinessProbe: {tcpSocket: {port: <target port>}, periodSeconds: 2,
      failureThreshold: 1}` on the **podbench** container.
- [ ] Idle the target container (`command: ["sleep","infinity"]`, drop `args`), strip
      `readinessProbe`/`livenessProbe`/`startupProbe`/`lifecycle` from it, set `restartPolicy: Never`
      on the dev pod, clear `nodeName`, drop `uid`/`resourceVersion`/`ownerReferences`/`managedFields`
      /`generateName`/`status`.
- [ ] Sidecar gets: its own `resources`, an `emptyDir` workspace (4 Gi was fine), `HOME=/workspace`,
      `securityContext.capabilities.add: [SYS_PTRACE]`, and `spec.shareProcessNamespace: true`.
- [ ] **Cutover mode:** `kubectl patch svc --type=json -p '[{"op":"replace","path":"/spec/selector",…}]'`.
      A **merge** patch silently unions the maps (observed: `{app: …, podbench.dev/devpod: "true"}`,
      which dropped the original pod out of the endpointslice). Record the original selector for an
      exact restore.
- [ ] **Pre-flight the port before every relaunch** — `ss -lntp` (sees all containers via shared
      netns + PID ns + `CAP_SYS_PTRACE`), identify the owning container via `/proc/<pid>/root`,
      **and count TIME_WAIT entries** so the user is told to wait rather than staring at
      `EADDRINUSE`.
- [ ] The relaunch wrapper must track **its own child PID**, confirm it is alive, and confirm that
      PID owns the listening socket (match the socket inode against `/proc/<pid>/fd`). A socket poll
      alone gives a false PASS.
- [ ] **Never `pkill -f`** in generated helpers — under `shareProcessNamespace: true` it matches the
      invoking shell (`exit code 143`) and every container's processes. Kill by recorded PID.
- [ ] Bake the toolchain: `curl`, `ca-certificates`, `git`, `xz-utils`, `procps`, `iproute2`, `uv`,
      pre-seeded CPython. `apt-get` was **10.6 s of the 19 s** loop (55 %).
- [ ] Budget: authored spec → Ready **4.3 s**; uv + `uv python install 3.12` **2.3 s**;
      `uv venv` + `uv pip install -e .` **1.0 s**; first relaunch **0.85 s**;
      **edit → relaunch → verified through the Service: 1.18 s**.

### 4.5 Capability ladder and `capreport` (Phase 5)

- [ ] **Two rungs only, valid by construction:**
      - **rung 1 (full):** `runAsUser: 0` **AND** `capabilities.add: [SYS_PTRACE]`.
        **Reject the combination of `SYS_PTRACE` with a non-zero `runAsUser` in the launcher**, with
        an explicit message — never ship a container that silently has `CapEff: 0`.
      - **rung 2 (degraded):** `runAsUser: <target's UID>`, `runAsGroup: <target's GID>`,
        `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false`,
        `seccompProfile: RuntimeDefault`, `runAsNonRoot: true`. Verified **admitted** under
        `restricted:latest`, with the full degraded loop working inside it.
- [ ] Discover the target UID first (`/proc/<pid>/status`, or the target container's
      `securityContext`) — `cmdline`/`status`/`fd` are readable even from the wrong UID.
      **Do not default the fallback to root.**
- [ ] Add ephemeral containers via `POST /api/v1/namespaces/{ns}/pods/{pod}/ephemeralcontainers`,
      not `kubectl debug`.
- [ ] Handle **both** rejection channels: synchronous PSA `Forbidden` (match the stable substring
      `must not include "SYS_PTRACE" in securityContext.capabilities.add`) → fall straight to rung 2;
      and asynchronous kubelet `CreateContainerConfigError` /
      `container's runAsUser breaks non-root policy` → pre-empt by reading the target pod's
      `securityContext.runAsNonRoot`.
- [ ] **Ship `capreport` and run it on every session start**, printing the verdict before the user's
      prompt. Exit codes drive mode selection: **0** = live attach, **10** = read-only debugging,
      **20** = nothing. Its logic: read `CapEff` bit 19, `CapBnd`, `CapAmb`, `Seccomp`, `NoNewPrivs`,
      AppArmor profile (self **and** target), `yama/ptrace_scope`, target uid/comm/TracerPid; then
      run a **scratch `PTRACE_ATTACH` on its own forked child** (always permitted by Yama, so a
      failure there is structural → seccomp/AppArmor/yama=3) and a **live attach on the target**;
      then a 6-path `/proc` read matrix. Probe backends in order: bundled `ptprobe` binary →
      `python3` ctypes → compile with `cc`/`gcc`; report "skipped" if none.
- [ ] **Probe per node, never cache cluster-wide.** Surface node name + Yama state in the session
      banner so "attach works on one run, denied on the next" is explicable.
- [ ] Package gdb **with** its Python stdlib (`/usr/lib/pythonX.Y`, ~19 MB, `PYTHONHOME`/`PYTHONPATH`
      set) or pick a non-Python gdb.
- [ ] Warn once that ASLR cannot be disabled under `RuntimeDefault` seccomp.
- [ ] Document the inner loop as **gdb-launch**; document `prctl(PR_SET_PTRACER,
      PR_SET_PTRACER_ANY)` as the one-line target-side opt-in for genuine attach.

### 4.6 Arch and node caveats

- [ ] Server tarballs exist for both arches at the same commit; extension installs auto-resolve the
      platform build (`-linux-x64` / `-linux-arm64`). Detect via `uname -m`.
- [ ] arm64 (RK3588) is **not** slower for this workload; only image pulls differ.
- [ ] **Yama differs per node, by kernel flavour** (§3.13) — rockchip 6.1 nodes have no Yama at all.
- [ ] S3 was tested **amd64 only**; the distroless/dbgsym/build-id path needs re-checking on arm64.
- [ ] Cold-start timings are unobtainable on this cluster: `ws03` carries a `workstation` taint and
      `node01` a control-plane taint, so **`nuc2` is the only schedulable amd64 node**. All S2/S4
      numbers are warm-image-cache.

---

## 5. Open questions and residual risk

**R1 — S1 and S2 contradict each other on `sshd -e`. Unresolved.**
S1 proved `-e` is mandatory (`sshd -i` alone fails reproducibly at KEX with `Broken pipe`) and
isolated the mechanism to fd-2 teardown of the CRI exec stream. S2 ran
`ProxyCommand kubectl exec -i … -- /usr/sbin/sshd -i` — **no `-e`** — successfully for an entire
spike, and its note explicitly recommends *"do not pass `-e` to sshd"* on stderr-pollution grounds.
Both cannot be unconditionally true, so some environmental variable (syslog socket presence, image
state, kubectl/CRI buffering, timing) determines whether the fd-2 teardown fires. **Where it bites:**
if Phase 1 picks S2's form, the transport works in dev and fails intermittently in the field with a
network-looking error. **Mitigation now:** use `-e -o LogLevel=ERROR`, which S1 verified produces
**zero stderr bytes** (answering S2's objection) while keeping fd 2 open (answering S1's). **Action:**
Phase 1 must reproduce both configurations back to back on the same pod and record which variable
flips it.

**R2 — No real VS Code GUI client has ever connected. The single biggest open number.**
S2 explicitly flags this: no extension host and no language server was ever spawned, so every RSS
figure (~97 MiB idle) is a **lower bound**. Pylance alone is a 117 MiB install and its runtime RSS
is unmeasured; cpptools likewise. **Where it bites:** the memory budget, the pod-resize headroom
calculation, and therefore whether Podbench OOM-kills itself (§3.9, unrecoverable) inside tightly
limited production pods. Measure before Phase 2 ships.

**R3 — Trimming the built-in extensions is unvalidated against a real client.**
The −218 MiB trim was verified only by "server starts and serves `/version`". A GUI client may
require what was deleted. Re-validate before making the trim default.

**R4 — Source provisioning for Observe mode is an unsolved design problem** (§3.2). debuginfod gives
symbols but no sources on Debian/Ubuntu, and `set sysroot` does not cover sources at all. Three
candidate designs exist (source in the target image, source sidecar volume, client-side mapping to
the developer's checkout); none has been spiked. Phase 3 cannot ship "step through your code"
without picking one.

**R5 — gdb's DAP/MI mode is untested.** S3 tested CLI gdb only. The VS Code C++ extension consumes
`info source`'s **fullname**, which is exactly the field the nested-`substitute-path` bug corrupts
(§3.3). If DAP normalises paths differently, the `directory /proc/<pid>/root` recommendation may
need revisiting. Untested and directly on the Phase 3 critical path.

**R6 — Multithreaded and non-root targets are unproven for gdb.** S3 exercised a single-threaded
target only; `libthread_db` loaded and `info threads` listed the one LWP after
`add-auto-load-safe-path`, but a genuinely multithreaded target was never tried. Targets with
`runAsUser != 0` or in a user namespace were also not tested.

**R7 — The seccomp branch of `capreport` is untested code.** S5 could not install a `localhost/`
seccomp profile on a node (spike rules), so the "seccomp filter is rejecting ptrace" verdict path
has never executed. `RuntimeDefault` was tested and **allows** ptrace. Treat that branch as
unverified.

**R8 — AppArmor uniformity is an assumption.** Every container observed ran under
`cri-containerd.apparmor.d (enforce)`, and ptrace worked only because that profile permits ptrace
between peers **in the same profile**. A target with a *custom* AppArmor profile breaks this.
`capreport` reports both profiles, but the failure has never been observed, so the diagnostic text
for it is unvalidated.

**R9 — Host key identity is unsolved and not in the brief** (S1). `ssh-keygen -A` mints fresh keys
per attach, so `known_hosts` either warns on every new pod or must be bypassed. The spike used
`StrictHostKeyChecking no`. Shipping that teaches users to disable host verification. Needs either a
host key delivered from a Secret, or `HostKeyAlias` keyed on pod UID with programmatic
`known_hosts` management.

**R10 — Ephemeral-container state loss on pod restart.** The apt-installed sshd, host keys,
`authorized_keys`, the 690 MiB server and all extensions live in the ephemeral container's writable
layer, and the ephemeral container spec is immutable and unrestartable. A pod restart or an OOM
means a fresh rootfs and **new host keys** (compounding R9). The mitigation (re-bootstrap, ~6 s) is
cheap but must be the documented reconnect path, not a surprise.

**R11 — Untested transport topologies.** All numbers come from a flat k3s exec path. Behaviour
through konnectivity or an API gateway — particularly the 600 s idle survival and the 19 s stall
detection — is unknown. Likewise Remote-SSH's own reconnect behaviour when the pod is deleted or
evicted mid-session was never exercised.

**R12 — Air-gap is harder than two allowlists** (§3.19). Four host groups, and extensions with
`extensionDependencies` (e.g. `ms-python.debugpy`) still reach the marketplace even when installed
from a local `.vsix`, so an offline bundle must ship the full dependency closure. Unspiked.

**R13 — In-place pod resize works on a controller-managed pod, but it silently diverges that pod
from its controller.** The spike itself was thin evidence: 3Gi→6Gi→7Gi, `ResizeCompleted`, no
restarts, non-disruptive to a running ephemeral container — but one standalone pod
(`podbench-s2/target-amd64`), patched by hand, with no controller in the picture. It has since been
measured on **two further pods, both Deployment-managed, both resized by `podbench attach --resize`
rather than by raw kubectl** — which settles the controller question and raises a different one
nobody had written down. The Kubernetes version is *not* a second data point: everything below ran
on the same k3s v1.34.6+k3s1 cluster as the spikes, so "one Kubernetes version" still stands.

*Measured 2026-08-15 on k3s v1.34.6+k3s1: `podbench-demo/demo-service`, a one-replica
`python:3.12-slim` Deployment pinned to an amd64 node with `limits.memory: 256Mi`, whose seat had
just been OOM-killed. Raised by `podbench attach --new --resize 4Gi`, then read back:*

| check | result |
|---|---|
| pod spec `limits.memory` | `256Mi` → `4Gi` |
| `memory.max` in the **app** container's own cgroup | `4294967296` — the kernel applied it, not just the API server |
| `app` container restarts | 0, `started=true` — genuinely in place |
| pod conditions | `Ready=True`, `ContainersReady=True` |
| the app through its Service | uninterrupted throughout |

**A Deployment does not fight an in-place resize.** The pod is owned by
`ReplicaSet/demo-service-5cbbc6654f` and the ReplicaSet left the raised limit alone, which is what
the controller's contract predicts: a ReplicaSet reconciles pod *existence*, not pod *spec*. So the
common case — resizing a pod under a Deployment — is fine, and "a controller that would fight the
change" was the wrong thing to have been afraid of.

**The right thing to be afraid of is drift.** The resize writes to the pod; the Deployment's
template still says `256Mi`, and nothing reconciles the two. The divergence therefore holds
indefinitely and then vanishes without warning: any rollout, scale, image bump or eviction
regenerates the pod from the unchanged template at the original limit. Someone who resizes to make
a seat viable and later triggers a rollout for an unrelated reason gets a pod that OOMs again with
no visible connection to what they did. It is the same class of drift that `podbench patch status`
exists to surface — a pod quietly unlike the thing that declares it — arriving in a place nothing
was watching.

That reversion is measured, not predicted. The same manifest in a scratch namespace, resized to
`1Gi` (pod `1Gi` / template `256Mi`, `memory.max` `1073741824`, 0 restarts, `started=true`), then
`kubectl rollout restart deployment/demo-service`: the replacement pod came back at **`256Mi`**,
carrying no seat, with nothing in the rollout that mentioned either.

The divergence outlives a *container* restart, which is the same fact from the other side: re-read
8 h later, the resized `podbench-demo` pod still carried `4Gi` against a `256Mi` template and
`memory.max` was still `4294967296` **after** the `app` container had restarted once (exit 137).
The raised limit lives on the pod object, so only regenerating the pod reverts it.

**Still unproven, and still in the warning:** a namespace with a `LimitRange` or a `ResourceQuota`.
Neither namespace measured had either, so nothing here says how the resize behaves when the raised
limit has to be re-admitted against a quota. A second Kubernetes version is also still untested.
Podbench is still being asked to depend on this to avoid the unrecoverable OOM of §3.9.

**`--resize` is memory-only.** It takes a memory value and patches `limits.memory`; a CPU limit is
not raised with it. That did not bite in this measurement — `cpu.stat` showed `nr_throttled 0` with
a vscode-server plus extensions running — but a throttled seat under a tight `limits.cpu` has no
mitigation in the flag.
