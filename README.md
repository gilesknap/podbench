# podbench prototype

This branch intentionally keeps two modes and one capability tier:

- podbench attach POD lands a capless ephemeral container using the target
  container's UID, GID and seccomp profile when Kubernetes reports them.
- podbench hotfix manages a source checkout on one persistent claim per
  single-replica workload.

The image contains a shell, Git, uv, gdb, strace and basic process tools.
Enter a seat with the kubectl exec command printed by attach.

## Hotfix lifecycle

1. Add podbench-hotfix-claim as a chart dependency.
2. Generate and deploy the workload values:

       podbench hotfix values --app RELEASE --from-pod POD -n NAMESPACE

3. Initialize its claim:

       podbench hotfix init POD --repo URL -n NAMESPACE

4. Edit /podbench/app in the seat and relaunch:

       podbench hotfix restart POD -n NAMESPACE
       podbench hotfix status -n NAMESPACE

5. Remove the generated workload values, redeploy, then retire the PVC:

       podbench hotfix retire PVC --delete-claim -n NAMESPACE

This is a rapid-iteration prototype. Root and unknown-identity targets are
attempted with the same degraded spec and may not provide useful ptrace access.
