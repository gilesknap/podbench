---
name: podbench-live-testing
description: Validate Podbench attach and hotfix behavior against a Kubernetes cluster, including safe workload wiring, observable restart checks, and complete cleanup. Use for live Podbench cluster tests or diagnosis; do not use for unit-only changes.
---

# Podbench Live Testing

Test the smallest path that proves the requested behavior. Treat cluster state and repository state as separate: record both before mutation, verify the externally visible result, and restore temporary cluster resources when the test ends.

Before running live commands:

- Read repository instructions and inspect `git status`, the current CLI help, and the target workload. Do not assume remembered flags still match the checkout.
- Establish the kubeconfig and namespace explicitly. Pass the namespace to Podbench as well as `kubectl`; a kubeconfig may not define a default namespace.
- Confirm the caller has the exact permissions required. For ephemeral containers, use `kubectl auth can-i update pods --subresource=ephemeralcontainers`.
- Preserve the user's commit and deployment scope. Live-test authorization permits the requested cluster operations; it does not imply permission to commit or retain test resources.

Choose the relevant procedure:

- For attaching a seat, process visibility, image reuse, or SSH foundations, read [references/attach.md](references/attach.md).
- For claim wiring, checkout initialization, edit/restart, reinstall, status, retirement, or cleanup, read [references/hotfix.md](references/hotfix.md).
- When working with the maintained demo environment, read [references/demo-cluster.md](references/demo-cluster.md) and verify its remembered values before use.

Report observable evidence: selected pod and target, seat creation or reuse, target process visibility, old and new application PID, response before and after an edit, Kubernetes container restart count, health state, and final cleanup. Distinguish expected degraded-seat limitations from product failures.
