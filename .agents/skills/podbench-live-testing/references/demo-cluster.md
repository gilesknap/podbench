# Maintained demo cluster

These values came from the 2026-09-12 live test and may drift. Verify them before mutation.

- Workspace: `/workspaces/podbench`
- Namespace: `podbench-demo`
- Scoped kubeconfig: `/workspaces/podbench/k8s/podbench-demo-claude-giles.kubeconfig`
- Administrative kubeconfig: `/workspaces/config`
- Demo Deployment/release: `demo-service`
- Application container: `app`
- Original command: `python /src/demo_service.py`
- Source ConfigMap: `demo-service-src`, mounted at `/src`
- Health endpoint: HTTP `/healthz` on port 8080
- Hotfix claim used by the test: `demo-service-podbench-project`
- Seat image for current tests: `ghcr.io/epics-containers/podbench:prototype-attach-hotfix`

The administrative kubeconfig had no default namespace. Export it when PVC permissions are required and still pass `-n podbench-demo` to every Podbench and `kubectl` operation.

The known-good lifecycle changed the response from `PODBENCH-ORIGINAL-v1` to `PODBENCH-HOTFIX-v2`, changed the supervised child PID without incrementing the Kubernetes app-container restart count, then restored the original response and deleted the claim.
