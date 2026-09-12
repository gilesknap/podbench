---
name: gkcluster-t11
description: Manage Giles's gkcluster through its GitOps repository and validate the T11 beamline deployment in t11-beamline. Use for cluster inspection, Argo CD changes, T11 scheduling, storage, RBAC, rollout, and live failure diagnosis; do not use for generic Podbench attach or hotfix testing.
---

# gkcluster and T11

Treat gkcluster as a live personal cluster, not a disposable test fixture. Establish
the current state before acting and distinguish repository, Argo CD, and workload
state in every handoff.

Use `/workspaces/config` explicitly and verify that it still selects context
`default` and the expected API server before mutation. Read-only inspection does
not authorize workload restarts, storage changes, GitHub publication, or merges.
Get explicit authorization for those actions and for any operation that could
discard PVC data.

Prefer GitOps changes in `gilesknap/tpi-k3s-ansible`. Create its worktrees under
`/workspaces/podbench/.agents/worktrees/`, preserve unrelated changes, run
`just pre-commit`, and report local validation separately from PR, merge, Argo
sync, and live acceptance. Personal-cluster exceptions belong there rather than
in production T11 repositories unless the user asks otherwise.

Before a cluster change, capture:

- the kubeconfig context and API server;
- node architecture, schedulability, and taints;
- the affected Argo Application revision, sync, health, operation, and conditions;
- the exact live resources and permissions involved.

After a merged change, hard-refresh the owning Argo Application, wait for the
expected commit revision and completed operation, then validate the externally
visible behavior. A Healthy application can remain OutOfSync because of CRD or
hook drift, so report sync and health independently and inspect non-Synced
resources rather than collapsing them into one result.

For T11 deployment, scheduling, storage, permissions, or diagnosis, read
[references/t11.md](references/t11.md) before acting.
