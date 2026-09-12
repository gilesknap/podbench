{{- define "podbench-hotfix-claim.name" -}}
{{- default (printf "%s-podbench-project" .Release.Name) .Values.claimName | trunc 63 | trimSuffix "-" -}}
{{- end -}}
