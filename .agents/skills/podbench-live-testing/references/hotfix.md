# Hotfix validation

Capture the target Deployment template and the application's baseline response before wiring hotfix mode. Identify its exact container, command, arguments, mounts, security context, probes, readiness, and restart count so rollback is deterministic.

Generate values from the running pod using both the release name and source pod. The current shape is:

```text
podbench hotfix values --app RELEASE --from-pod POD --container CONTAINER -n NAMESPACE
```

Prefer a fallback entrypoint for a live test: run the editable file from `/podbench/app` when present and the image's original entrypoint otherwise. This lets the newly wired workload become healthy before `hotfix init` populates the claim.

When the image supplies its entrypoint and the Pod declares only `args`, pass
`--entrypoint` explicitly. For a conditional fallback, make the override one
executable shell command such as `bash -c 'if ...; then ...; else ...; fi'`.
Initialization runs `uv sync` only for repositories containing
`pyproject.toml`; non-Python repositories are valid hotfix checkouts too.

Exercise the lifecycle with evidence at each boundary:

1. Wire the `podbench-app` volume at `/podbench/app` and wait for the rollout to become Ready.
2. Confirm `hotfix status` sees an empty claim.
3. Run `hotfix init` with an actual repository and confirm its metadata, checkout, environment setup, and seat.
4. Make a visible source edit through the shared claim. Record `/tmp/podbench-child.pid`, the baseline response, and the Kubernetes restart count.
5. Run `hotfix restart`. Confirm the child PID changes, the response reflects the edit, and the application container restart count does not change.
6. When dependency refresh is in scope, repeat with `--reinstall` and recheck health and response.
7. If retirement safety is in scope, confirm retirement refuses while a pod still mounts the claim.

HTTP, TCP, and gRPC probes cannot observe the hotfix hold file and remain active
during restart. Generated values extend their failure threshold over the
two-minute restart window; confirm the deployed values retained that extension.
The restart sends TERM, then escalates to KILL after a bounded grace period so a
slow child cannot make the supervisor exit. Exec probes use the generated
hold-aware wrapper. In both cases, require the child PID to change without an
application-container restart.

On an RWX/NFS claim, validate initialization with a real repository. A failed
initialization must be retryable, and Git operations must accept the fsGroup-owned
mount root without weakening the claim permissions.

For cleanup, restore the original Deployment template and wait for the old wired pod to finish terminating before retiring the claim. A rollout can report success while the terminating old pod still mounts the PVC, and an unfiltered pod selector may return that old pod. Verify the replacement pod's owner, response, readiness, and restart count, then retire/delete the unmounted claim and confirm no hotfix resources remain.
