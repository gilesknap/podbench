# Your first session

By the end of this you will have a debug container running inside a pod, an ssh
session into it, and VS Code editing files that live in your cluster. Nothing
you do here touches a real workload — you will create a throwaway namespace and
delete it at the end.

Budget about fifteen minutes, most of it waiting for image pulls.

You need `uv` and `kubectl` on your machine, the one-time
[ssh `Include` line](installation.md), and a cluster you are allowed to create
pods in. A local [kind](https://kind.sigs.k8s.io) cluster is ideal. The launcher
itself is not installed — `uvx` fetches and runs it.

:::{important}
**Before the first PyPI release** there is nothing for `uvx podbench` to
resolve. Until then, run every `uvx podbench` on this page as:

```
$ uvx --from git+https://github.com/gilesknap/podbench podbench <verb>
```
:::

:::{note}
The measurements quoted throughout these docs were taken on a 6-node k3s
cluster, not on kind. The behaviour is the same; the timings will not be.
:::

## 1. Something to debug

```
$ kubectl create namespace podbench-demo
$ kubectl -n podbench-demo apply -f - <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 1
  selector:
    matchLabels: {app: web}
  template:
    metadata:
      labels: {app: web}
    spec:
      containers:
        - name: web
          image: python:3.12-slim
          command: ["python", "-m", "http.server", "8080"]
          workingDir: /tmp
          ports:
            - containerPort: 8080
          resources:
            limits:
              memory: 3Gi
              ephemeral-storage: 4Gi
---
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  selector: {app: web}
  ports: [{port: 80, targetPort: 8080}]
YAML
$ kubectl -n podbench-demo rollout status deploy/web
```

The limits are deliberate and generous. A VS Code session is a **1.1–1.3 GB**
working set on node disk, and in Observe mode it is spent from *this* pod's
budget — see step 4 below.

Get the pod name:

```
$ kubectl -n podbench-demo get pods
NAME                   READY   STATUS    RESTARTS   AGE
web-6c9d7f4b8b-hq2vn   1/1     Running   0          25s
```

## 2. Attach

```
$ uvx podbench attach web -n podbench-demo
'web' matched pod web-6c9d7f4b8b-hq2vn
```

`web` rather than the whole name: podbench matches what you type against the
pods in the namespace and says what it resolved to. The full name and `pod/NAME`
still work, and a substring matching several pods gets you a list to choose from
(see {ref}`Naming the pod <naming-the-pod>`).

Nothing is installed to run that: `uvx` fetches the launcher, runs it against
your kubeconfig and leaves nothing behind. It does five things:

1. reads the target pod's spec, so it knows the workload container's UID and
   whether the pod insists on `runAsNonRoot`;
2. walks the capability ladder — root + `CAP_SYS_PTRACE` first, the target's own
   UID with no capabilities if admission refuses that — posting each attempt to
   the `ephemeralcontainers` subresource;
3. waits for the container to be genuinely *running*, not merely accepted;
4. runs `capreport` **inside** the container it just landed, on that node;
5. writes an ssh stanza to `~/.podbench/config.d/` and tells you the host alias.

## 3. Wait — what did it just do to my pod?

It appended an ephemeral container to the pod spec, permanently. Ephemeral
containers cannot be removed, restarted or edited; the name `podbench-1` is now
burnt for the rest of that pod's life. Running `attach` again **reconnects** to
it rather than adding a second one.

That is why the demo pod is disposable. On a real pod, read
*Read this before you attach to a live pod* on the [front page](../index.md)
first.

## 4. Read the report before you connect

The point of the capability report is that it is *measured*, not inferred from
the spec podbench asked for:

```
seat        podbench-demo/web-6c9d7f4b8b-hq2vn[podbench-1]  (new)
target      web
rung        full - root + CAP_SYS_PTRACE (live attach)
ladder
  full      landed   admitted by the API server and the kubelet
supports
  [x] live attach (gdb -p <pid>)
  [x] read-only inspect (/proc/<pid>/root, maps, environ)
  [ ] iterate (edit, relaunch, verify through the Service)
  [x] seat (editor, shell, git)
measured
  verdict     live attach available
  blocker     none
  node        kind-worker
  yama        1
  uids        seat 0, target 0
```

Three lines are worth learning to read:

* **`rung`** — what the *cluster* admitted. `degraded` means `SYS_PTRACE` was
  refused by Pod Security Admission and podbench fell back to the target's own
  UID. That is a normal outcome, not a failure, and the command still exits `0`.
* **`blocker`** — what actually stops ptrace, if anything. Four unrelated
  subsystems (missing capability, Yama, seccomp, AppArmor) refuse with the same
  `EPERM`; this line names which.
* **`yama` and `node`** — both are per-node. Attach working on one pod and being
  denied on the next, in the same cluster, is expected: kernel flavours differ.
  podbench never caches a cluster-wide answer.

## 5. Connect with ssh

The last lines of the attach output tell you the alias:

```
ssh config written to ~/.podbench/config.d/podbench-demo-web-6c9d7f4b8b-hq2vn.conf
add this to ~/.ssh/config once:  Include ~/.podbench/config.d/*.conf
then:  ssh podbench-podbench-demo-web-6c9d7f4b8b-hq2vn
```

If you have not added the `Include` line yet, do it now (see
[Installation](installation.md)). Then:

```
$ ssh podbench-podbench-demo-web-6c9d7f4b8b-hq2vn
root@web-6c9d7f4b8b-hq2vn:~# pids
PID  UID  TARGET  CONTAINER      COMM    CMDLINE
1    0    yes     87d20e23a1b4   python  python -m http.server 8080
42   0    no      7206c89bf0e1   sleep   sleep infinity
```

There is no listening socket in that pod, no port-forward and no pod IP
involved. ssh's `ProxyCommand` is a `kubectl exec` running `sshd -i -e` on the
far side; your kubeconfig is the outer authentication and your ssh key is the
inner one. (`-e` is mandatory and is not about logging — see
[VS Code Remote-SSH](../how-to/vscode-remote-ssh.md).)

Look around the workload's filesystem — this works even against a distroless
target with no shell of its own, because you are reading it from *outside*
through the shared PID namespace:

```
# ls /proc/1/root/tmp
# cat /proc/1/environ | tr '\0' '\n'
```

## 6. Connect VS Code

In VS Code, run **Remote-SSH: Connect to Host…** and choose the same alias.
On first connect the server downloads and extracts itself into the container
(about 2 s to download, 6 s to extract, ~680 MiB on disk).

:::{warning}
No real VS Code GUI client has been driven against podbench yet. The transport
was verified at the protocol level and the server was driven headlessly, so the
memory figures in these docs are **lower bounds** — no extension host or
language server has been measured. Expect the connection to work and the
footprint to be larger than quoted.
:::

Open `/` in the remote window and you are editing inside the cluster. See
[VS Code Remote-SSH](../how-to/vscode-remote-ssh.md) for sizing, extensions and
the settings that matter.

## 7. Look at what you have running

```
$ uvx podbench status pod/web-6c9d7f4b8b-hq2vn -n podbench-demo
$ uvx podbench list -n podbench-demo
```

`status` lists every podbench container in a pod, including dead ones whose
names are burnt. `list` does the same across the namespace — useful for finding
the session you forgot about last week.

## 8. Clean up

An ephemeral container dies with its pod, which is the only way to remove one:

```
$ kubectl delete namespace podbench-demo
```

Also drop the generated stanza if you want a tidy config directory:

```
$ rm ~/.podbench/config.d/podbench-demo-web-6c9d7f4b8b-hq2vn.conf
```

## Where next

* [Attach to a pod](../how-to/attach-to-a-pod.md) — reconnecting, restricted
  namespaces, making memory headroom, and what the failures look like.
* [Debug with gdb](../how-to/debug-with-gdb.md) — a distroless target to a
  breakpoint with source, in under ten minutes.
* [Iterate on Python](../how-to/iterate-on-python.md) — the mode that has its
  own resource limits, and where you should do anything heavier than looking.
