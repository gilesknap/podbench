# podbench prototype

This branch intentionally keeps two modes and one capability tier:

- podbench attach POD lands a capless ephemeral container using the target
  container's UID, GID and seccomp profile when Kubernetes reports them.
- podbench hotfix manages a source checkout on one persistent claim per
  single-replica workload.

The image contains a shell, Git, uv, gdb, strace and basic process tools.
Attach prints an SSH command carried by `kubectl exec` (no pod network), the
`~/.ssh/config` Include line, and a raw exec fallback. Images pull by default.
Inside a seat, `podbench debug` shows the process tree and attaches GDB to the
selected process using its container filesystem.
Development builds use `ghcr.io/gilesknap/podbench:prototype-attach-hotfix`;
override that with `--image` or `PODBENCH_IMAGE`.
Run `podbench doctor` to check local and cluster prerequisites; `--fix` only
creates the SSH config directory and installs that Include safely.

## Hotfix lifecycle

1. Add podbench-hotfix-claim as a chart dependency.
2. Generate and deploy the workload values:

       podbench hotfix values --app RELEASE --from-pod POD -n NAMESPACE

3. Initialize its claim:

       podbench hotfix init POD --repo URL -n NAMESPACE

   Python projects are synced with uv. Other repositories are cloned without a
   dependency-install step.

   Generated values keep liveness probes but extend non-exec probe failure
   thresholds for the two-minute restart window.

4. Edit /podbench/app in the seat and relaunch:

       podbench hotfix restart POD -n NAMESPACE
       podbench hotfix status -n NAMESPACE

5. Remove the generated workload values, redeploy, then retire the PVC:

       podbench hotfix retire PVC --delete-claim -n NAMESPACE

This is a rapid-iteration prototype. Root and unknown-identity targets are
attempted with the same degraded spec and may not provide useful ptrace access.
