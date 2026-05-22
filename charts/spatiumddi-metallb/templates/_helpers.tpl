{{/*
  Shared helpers for the spatiumddi-metallb chart. Mirrors the
  spatiumddi-appliance helpers so the moved metallb-pool / metallb-bgp
  templates render identical labels.
*/}}

{{- define "spatiumddi-metallb.name" -}}
{{- default "spatiumddi-metallb" .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "spatiumddi-metallb.labels" -}}
app.kubernetes.io/name: {{ include "spatiumddi-metallb.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: spatiumddi
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}
