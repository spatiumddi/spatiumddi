{{/* Common labels */}}
{{- define "spatium-dns.labels" -}}
app.kubernetes.io/name: spatium-dns
app.kubernetes.io/instance: {{ .instance }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
spatiumddi.org/dns-flavor: {{ .server.flavor }}
spatiumddi.org/dns-role: {{ .server.role }}
{{- end -}}
