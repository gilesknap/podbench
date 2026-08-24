{{/*
The claim's name.

`<release>-podbench-project` by default, which is the same name podbench's
central `hotfixProject.claims[]` route produces for an application of that name
and the same one `podbench hotfix values` emits into the target's `volumes:`
entry. The pod and the claim have to agree on it or the workload
mounts a claim that does not exist, so the default is the answer and the
override is for a claim that already exists under another name.

As a subchart, `.Release.Name` is the *parent's* release - the service - which
is exactly the scope the claim's lifecycle should match.
*/}}
{{- define "podbench-hotfix-claim.name" -}}
{{- default (printf "%s-podbench-project" .Release.Name) .Values.claimName }}
{{- end }}
