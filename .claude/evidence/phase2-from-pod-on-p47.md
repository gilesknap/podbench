# Phase 2 evidence — `--from-pod` against the live p47-beamline targets

Taken 2026-08-22 on `p47-beamline` (pollux), both services on `main` and
carrying their **original** entrypoints — i.e. the pods as a first-time user
meets them, not ones already hotfixed. Read-only: `get pod` and nothing else.

The assertion this exists to record is #176's end-to-end one — that
`initialDelaySeconds: 120` and `periodSeconds: 30` reach the emitted values
**without anyone typing them**, because a chart renders a supplied
`livenessProbe` wholesale and an omitted timing silently becomes the Kubernetes
default.

## `bl47p-mo-ioc-01-0` — the compiled IOC, which has a probe

```
$ podbench hotfix --print-values --app bl47p-mo-ioc-01 \
      --from-pod bl47p-mo-ioc-01-0 -n p47-beamline
```

stderr:

```
podbench: bl47p-mo-ioc-01-0's 'bl47p-mo-ioc-01' already mounts autosave-volume, bl47p-mo-ioc-01-data, config-volume, dev-shm, opis-volume and runtime-volume. The `volumeMounts:` and `volumes:` keys below *replace* the ones your chart renders, they do not add to them - so merge these entries into what the service's values.yaml already has rather than pasting over it. Anything chart-generated will come back on its own; anything the service declares for itself will not.
```

stdout:

```yaml
# 1. values for the podbench release - creates the claim bl47p-mo-ioc-01-podbench-project
hotfixProject:
  enabled: true
  claims:
    - name: bl47p-mo-ioc-01
      size: 2Gi

# 2. values for bl47p-mo-ioc-01's own chart. All five are ordinary passthroughs;
#    the names below are the common convention, not a requirement.
volumes:
  - name: podbench-app
    persistentVolumeClaim:
      claimName: bl47p-mo-ioc-01-podbench-project
  # podbench-home is the seat's, and is deliberately *declared and
  # not mounted* by the application container. An ephemeral container
  # may only mount volumes its pod already declares, and pod volumes
  # cannot be added later - so it is here at deploy time or not at all.
  - name: podbench-home
    emptyDir:
      # vscode-server unpacks to ~700 MiB and a real session reaches
      # 1.1-1.3 GB. Unbounded, that comes out of the node's ephemeral
      # storage and an overrun evicts the pod - application included.
      sizeLimit: 2Gi
volumeMounts:
  # Beside the application's own project, never over it. Nothing the
  # image ships is hidden, so the seed is a plain copy and the seat
  # keeps its own venv.
  - name: podbench-app
    mountPath: /podbench/app
command:
  - bash
  - -c
args:
  - |
    while :; do
      (
        if [ -x /podbench/app/.venv/bin/python ]; then
          export PATH="/podbench/app/.venv/bin:$PATH"
          echo "podbench: running the hotfixed project"
        fi
        exec bash -c /epics/ioc/start.sh
      ) &
      child=$!
      echo $child > /tmp/podbench-child.pid
      wait $child; rc=$?
      kill -TERM -"$child" 2>/dev/null || true
      [ -e /tmp/podbench-hold ] || exit $rc
    done
livenessProbe:
  # The application's own check, short-circuited while the pod is
  # held. Without the wrapper the kubelet restarts a held pod at
  # failureThreshold x periodSeconds and takes the seat with it.
  exec:
    command:
      - bash
      - -c
      - 'if [ -e /tmp/podbench-hold ]; then exit 0; fi; exec /bin/bash /epics/ioc/liveness.sh'
  # Carried over from the target's own probe. A chart renders a
  # supplied livenessProbe wholesale, so anything omitted here
  # silently becomes the Kubernetes default.
  initialDelaySeconds: 120
  periodSeconds: 30
  timeoutSeconds: 1
  successThreshold: 1
  failureThreshold: 3
podSecurityContext:
  # Not optional. The claim and the seat's home are created root:root,
  # and neither the application nor the seat runs as root. Without
  # fsGroup /podbench/app is present and unwritable, which is worse
  # than absent - everything starts, then fails.
  fsGroup: 37887

# Single replica only: the claim is ReadWriteOnce and one checkout
# cannot serve two writers.
```

## `bl47p-ea-fastcs-01-0` — the canonical target, which has no probe

```
$ podbench hotfix --print-values --app bl47p-ea-fastcs-01 \
      --from-pod bl47p-ea-fastcs-01-0 -n p47-beamline
```

stderr:

```
podbench: bl47p-ea-fastcs-01-0's 'bl47p-ea-fastcs-01' already mounts autosave-volume, beamline-data, bl47p-ea-fastcs-01-data, config-volume, opis-volume and runtime-volume. The `volumeMounts:` and `volumes:` keys below *replace* the ones your chart renders, they do not add to them - so merge these entries into what the service's values.yaml already has rather than pasting over it. Anything chart-generated will come back on its own; anything the service declares for itself will not.
```

stdout:

```yaml
# 1. values for the podbench release - creates the claim bl47p-ea-fastcs-01-podbench-project
hotfixProject:
  enabled: true
  claims:
    - name: bl47p-ea-fastcs-01
      size: 2Gi

# 2. values for bl47p-ea-fastcs-01's own chart. All five are ordinary passthroughs;
#    the names below are the common convention, not a requirement.
volumes:
  - name: podbench-app
    persistentVolumeClaim:
      claimName: bl47p-ea-fastcs-01-podbench-project
  # podbench-home is the seat's, and is deliberately *declared and
  # not mounted* by the application container. An ephemeral container
  # may only mount volumes its pod already declares, and pod volumes
  # cannot be added later - so it is here at deploy time or not at all.
  - name: podbench-home
    emptyDir:
      # vscode-server unpacks to ~700 MiB and a real session reaches
      # 1.1-1.3 GB. Unbounded, that comes out of the node's ephemeral
      # storage and an overrun evicts the pod - application included.
      sizeLimit: 2Gi
volumeMounts:
  # Beside the application's own project, never over it. Nothing the
  # image ships is hidden, so the seed is a plain copy and the seat
  # keeps its own venv.
  - name: podbench-app
    mountPath: /podbench/app
command:
  - bash
  - -c
args:
  - |
    while :; do
      (
        if [ -x /podbench/app/.venv/bin/python ]; then
          export PATH="/podbench/app/.venv/bin:$PATH"
          echo "podbench: running the hotfixed project"
        fi
        exec bash -c 'stdio-socket --ptty "fastcs-example run /epics/ioc/config/controller.yaml"'
      ) &
      child=$!
      echo $child > /tmp/podbench-child.pid
      wait $child; rc=$?
      kill -TERM -"$child" 2>/dev/null || true
      [ -e /tmp/podbench-hold ] || exit $rc
    done
podSecurityContext:
  # Not optional. The claim and the seat's home are created root:root,
  # and neither the application nor the seat runs as root. Without
  # fsGroup /podbench/app is present and unwritable, which is worse
  # than absent - everything starts, then fails.
  fsGroup: 37887

# Single replica only: the claim is ReadWriteOnce and one checkout
# cannot serve two writers.
```

## Diff against the hand-written values

Against `p47-services` branch `podbench-hotfix-test`, which carries the layout
somebody wrote by hand for the first live run. Compared semantically (parsed,
not textually), key by key.

| Key | `bl47p-ea-fastcs-01` | `bl47p-mo-ioc-01` |
|---|---|---|
| `volumeMounts` | identical | differs — `dev-shm` |
| `command` | identical | identical |
| `args` | differs — `bash -c` wrapper | differs — `bash -c` wrapper |
| `livenessProbe` | identical (absent in both) | differs — `successThreshold: 1` |
| `podSecurityContext` | identical | identical |
| `volumes` | differs — claim kind | differs — claim kind, `dev-shm` |

Every difference, accounted for:

**`volumes`: a real `persistentVolumeClaim` where the hand-written file used a
generic ephemeral volume.** Expected and intended. The hand-written values used
an ephemeral volume because the test account has no `create` on
persistentvolumeclaims and the per-pod claim did not exist yet; Phase 5's
per-pod claim work is what makes the emitted form deployable.

**`args`: `exec bash -c 'X'` where the hand-written file had `exec X`.**
Cosmetic, and the emitted form is the more correct one. Both pods declare
`command: ["bash", "-c"]` with the real command in `args`, and reproducing both
faithfully is what keeps an entrypoint containing shell syntax intact. `exec`
replaces the process either way, so the running process is the same one.

**`livenessProbe`: `successThreshold: 1`, which the hand-written file omitted.**
Correct, and strictly safer. It is read off the *live* pod, so it carries the
fields Kubernetes defaulted rather than only the ones a human noticed — which is
exactly the lesson #176 taught.

**`dev-shm` and `/dev/shm` absent from the emitted values.** A real defect,
found by taking this diff, and fixed in this phase. `values_snippet` emits a
`volumes:` key that *replaces* the chart's rather than adding to it, so pasting
the output verbatim onto `bl47p-mo-ioc-01` would have left that IOC without
`/dev/shm`. Podbench cannot merge them itself — read from a live pod, a
chart-generated volume and one the service declared for itself are
indistinguishable — so `--from-pod` now names them and says which way the key
resolves (`EXISTING_MOUNTS_WARNING`, visible in the stderr above).

## What was *not* measured here

The deliberate failure paths — wrong context, forbidden `get pods`, absent pod —
were exercised as unit tests rather than against p47, because each needs a
broken credential and the tunnel is a shared resource. They are re-proved on the
cluster in Phase 5.
