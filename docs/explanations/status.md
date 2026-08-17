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
implementation, section 5 the residual risk as it stood at the gate. Several of
those constraints look arbitrary and are not; the failure modes they avoid are
silent.

## Known-unproven, stated plainly

**No real VS Code GUI client has connected yet.** The transport was verified at
the protocol level — HTTP 200 plus a WebSocket `101` through `ssh -L` — and the
server was driven headlessly. Every RSS figure in these docs is therefore a
**lower bound**: no extension host and no language server has ever been
measured, and the memory budget is exactly where that matters. See
[VS Code Remote-SSH](../how-to/vscode-remote-ssh.md).

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

The security-side gaps — the untested seccomp branch of the capability probe,
and the assumption that every container on a node shares one AppArmor profile —
are listed under *Unproven areas* in the [Security model](security.md).
