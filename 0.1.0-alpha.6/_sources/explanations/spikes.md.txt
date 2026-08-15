# Phase 0 spikes

Podbench's design brief ordered its phases by *risk*, not by component, and put five
throwaway experiments in front of everything else:

> Do not start Phase 1 until all five pass or the brief is amended with what was learned.

These are the findings notes from those experiments, kept verbatim. They are the reason
several things in podbench are built the way they are rather than the obvious way, so
they are recorded as evidence rather than summarised away — when a later change looks
like an easy simplification, the relevant note usually explains what it would break.

All five were run on 2026-08-15 against a real k3s v1.34 cluster (six nodes, mixed
amd64/arm64, Ubuntu hosts with Yama `ptrace_scope=1`) rather than the kind cluster the
brief assumed. Start with the gate report, which collates them.

```{toctree}
:maxdepth: 1

spikes/phase0-report
spikes/s1
spikes/s2
spikes/s3
spikes/s4
spikes/s5
```

## Verdicts

| Spike | Subject | Verdict |
|---|---|---|
| [S1](spikes/s1) | ssh transport: `sshd -i` over `kubectl exec` as ProxyCommand | PASS |
| [S2](spikes/s2) | vscode-server inside an ephemeral container | PASS |
| [S3](spikes/s3) | gdb attach with sysroot against a distroless target | PASS |
| [S4](spikes/s4) | Python takeover: dev pod, uv editable install, relaunch on the pod IP | PASS |
| [S5](spikes/s5) | No-cap fallback, Yama diagnosis, capability ladder | PASS |

Five passes, but five of the brief's load-bearing assumptions were falsified in the
process; the [gate report](spikes/phase0-report) lists the amendments and which phase
each one blocks.
