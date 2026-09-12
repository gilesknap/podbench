# Attach validation

Inspect `podbench attach --help` first. In the trimmed CLI, the workload argument is a pod name and the target-container option is `--target`; do not substitute a deployment name or `--container` without checking that the interface has changed.

Use an explicit image and pull policy when validating freshly built behavior. Confirm whether the command creates or reuses a seat, then run the printed `kubectl exec` command non-interactively for repeatable checks. Validate:

- the seat image/version is the one requested;
- the seat targets the intended application container;
- the target process appears in the shared PID namespace;
- expected shared mounts are visible; and
- the application remains Ready with an unchanged restart count.

Test reuse by repeating the same request. When image-selection behavior is in scope, request a different image or `--new` and verify that a new seat is created rather than silently reusing an incompatible seat.

Ephemeral containers cannot be removed from a pod. Use a disposable workload or plan a workload rollout for cleanup. A capless degraded seat may see the target process while being unable to ptrace it or traverse protected paths such as `/proc/1/root`; record that as a runtime-security limitation unless the requested contract promises stronger access.

An attach test proves the seat and printed entry command. It does not prove SSH or VS Code Remote-SSH. For those, also verify the SSH server, authentication/forwarding route, stable connection target, and a real remote shell before calling the experience seamless.
