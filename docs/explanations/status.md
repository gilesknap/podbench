# What is proven, and what is not

podbench is a packaging exercise over individually-proven Kubernetes and Linux
features, and the parts that carry the most weight were *measured* rather than
reasoned. This page says which parts those are, and — more usefully — which
parts they are not.

## The evidence

Five spikes ran against a real 6-node k3s cluster and all five passed. They are
kept verbatim in [Spikes](spikes.md), along with the
[Phase 0 gate report](spikes/phase0-report.md) that consolidates them. The
report is the empirical basis for most of the non-obvious behaviour in the tool:
it falsified five of the [design brief](design-brief.md)'s load-bearing
assumptions, and **where the brief and the report disagree, the report wins**.

Section 3 is the evidence, section 4 the constraint checklist that shaped the
implementation, section 5 the residual risk — as it stood at the gate, and with
a dated paragraph on each row that a later session has settled or added to.
Several of those constraints look arbitrary and are not; the failure modes they
avoid are silent.

The spikes ran on a cluster this project owns. A field session on 2026-08-17 did
not: an EPICS IOC at Diamond, in a namespace where the user is not an admin,
under someone else's Kyverno policies and against a RHEL-family target image. It
closed the largest item below and opened two others, and most of what it found
was invisible from this side of the cluster boundary.

## Known-unproven, stated plainly

**A real VS Code GUI client has now connected — and the numbers still have not
been taken.** On 2026-08-17 a Remote-SSH client reached a seat, started an
extension host, unpacked `ms-vscode.cpptools`, and drove gdb through the C++
adapter into a live IOC. The *mechanism* is no longer an assumption; the
**budget** still is. Every RSS figure in these docs remains a **lower bound** —
no per-extension measurement was taken during that session, and the seat had
been given `--resize 6Gi` before anything started. See
[VS Code Remote-SSH](../how-to/vscode-remote-ssh.md).

**The seat's gdb is only as new as the image's binutils.** gdb reads ELF through
BFD, so its ability to read a binary is really binutils'. Against a RHEL-family
target, bookworm's binutils 2.40 rejected `/usr/bin/bash` outright —
`.gnu.version_r invalid entry` — and the file would not open at all. That is
*not* "no debug information": a stripped binary debugs fine at the address
level, and the two are indistinguishable from the editor. A target built by a
newer toolchain than the image ships is an image bump rather than something the
launcher can work around; CodeLLDB is the escape hatch, since it carries its own
reader — except where this seat keeps a file at the target's exe path, which
withdraws the lldb entry as well: lldb has issue #90 too and, unlike gdb, cannot
be staged out of it (measured with a standalone lldb; CodeLLDB's own bundled
lldb was not observed). `debug-config` now asks gdb before emitting a `cppdbg`
entry — **but
that refusal has never fired in a cluster**, because the one binary that
triggers it is the one target selection now avoids.

**Admission engines beyond Pod Security Admission are handled, barely
exercised.** Kyverno refused a seat at Diamond over a field podbench had never
set — a `validate.pattern` rule fails on an *absent* field — and the ladder
treated that as fatal instead of dropping a rung. Both are fixed, and the ladder
now degrades through any webhook denial while still raising a webhook that
failed to *answer*. **Gatekeeper is untested.** An engine that **mutates**
rather than refuses now is: one that strips `capabilities.add` leaves a root
seat that reads back as the degraded rung and attaches perfectly well, which is
the opposite way round from the worry recorded here, and it was reporting rather
than debugging that it broke (issue #89). `status` no longer describes a seat
from the rung it landed on; it reports what `capreport` measured in it, or
`not probed`.

**Source provisioning for Observe mode is an open design problem.** Debian's
debuginfod serves symbols but **not** sources, and `set sysroot` does not cover
source lookup at all. Fedora/RHEL debuginfod is known to serve sources; Debian
and Ubuntu targets need one of the other routes. See
[Debug with gdb](../how-to/debug-with-gdb.md) for where sources actually come
from today.

**In-place pod resize is partly proven, and it diverges a pod from its
controller.** `attach --resize` was measured on three pods, two of them
Deployment-managed — but on one Kubernetes version, and the raised limit lives
on the *pod*, not on its controller, so a rollout regenerates the pod from an
unchanged template and silently reverts it. A `LimitRange` bounding
`maxLimitRequestRatio` is now handled — the request moves with the limit, by an
amount read from the namespace — after it made `--resize` unusable across a
whole namespace at Diamond on 2026-08-16. A **`ResourceQuota` is still
untested**, as is a second Kubernetes version.

**Hotfix mode has never been run against a cluster.** The workflow exists —
`podbench hotfix init|apply|status|consolidate`, plus `hotfix --print-values`
for the chart snippet — and is unit-tested, but every one of those tests drives
a temp directory and a fake `kubectl`. `attach --mount` puts the claim into the
seat at the application's own mountPath, so the workflow is reachable end to
end; reachable is not the same as demonstrated. See
[What `hotfix` does](hotfix-flow.md).

**Which half you are running is now measured, not guessed.** The launcher and
the image are one release in two places, and they can differ: `uvx` resolves the
launcher on every invocation, while a seat comes from whatever copy of the image
tag the node already has. Every symptom of that mismatch looks like a bug in the
newer half — a fix present in the launcher and absent from the seat reads
exactly like a fix that does not work. `attach` and `status` now run
`podbench --version` in the seat and print it on a `version` row, and `attach`
warns when it differs from its own; only where the seat will not answer does the
report fall back to naming the tag as one that moves. When iterating on such a
tag, `attach --pull always --new` is what puts a current seat in the pod.

What is *not* closed is the other end of it: a release image can be built from a
tree that setuptools_scm marks `.dev`/`+g<sha>`, and one was — an image tagged
`0.4.0b1` whose seat reported `0.4.0b2.dev0+g01d9ac8f8.d20260818`, a post-tag
build of a dirty context. CI now refuses to publish a release tag from such a
build, which stops it recurring but does not retag what is already in the
registry.

The security-side gaps — the untested seccomp branch of the capability probe,
and an LSM label mismatch never having been observed — are listed under
*Unproven areas* in the [Security model](security.md).
