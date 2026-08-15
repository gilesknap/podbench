{{/*
Expand the name of the chart.
*/}}
{{- define "podbench.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "podbench.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "podbench.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "podbench.labels" -}}
helm.sh/chart: {{ include "podbench.chart" . }}
{{ include "podbench.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "podbench.selectorLabels" -}}
app.kubernetes.io/name: {{ include "podbench.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
The name of one application's seat-identity ConfigMap. Derived from the
application's name rather than the release's: the volume that references it
lives in the *application's* pod spec, so the name is something a second chart
has to be told, and `podbench patch --print-values` derives it the same way.
Call with one entry of .Values.seatIdentity.apps as the context.
*/}}
{{- define "podbench.seatIdentityName" -}}
{{- default (printf "%s-podbench-identity" .name) .configMapName }}
{{- end }}

{{/*
The scratch workspace PVC name, so the launcher and the chart agree on it
without the user having to repeat themselves.
*/}}
{{- define "podbench.scratchPvcName" -}}
{{- default (printf "%s-scratch" (include "podbench.fullname" .)) .Values.scratchPvc.name }}
{{- end }}
