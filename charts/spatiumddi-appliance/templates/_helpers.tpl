{{/*
  Shared helpers for the spatiumddi-appliance chart (issue #183 Phase 2).
*/}}

{{/* Chart-level name + fullname helpers. The Helm convention is to
     prefix all rendered resources with the release name + chart name
     so two installs can coexist; on a single-node appliance both are
     fixed by the supervisor, but we keep the helper for symmetry
     with the existing charts/spatiumddi/ chart. */}}
{{- define "spatiumddi-appliance.name" -}}
{{- default "spatiumddi-appliance" .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "spatiumddi-appliance.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "spatiumddi-appliance.labels" -}}
app.kubernetes.io/name: {{ include "spatiumddi-appliance.name" . }}
{{ include "spatiumddi-appliance.podLabels" . }}
{{- end -}}

{{/* Pod-template labels: everything in .labels EXCEPT app.kubernetes.io/name,
     which every pod template sets to its own workload name (dns-bind9,
     dhcp-kea, ...) so the selector matches. Including .labels there too
     emitted the key twice in one map — YAML that yaml.v2 resolves last-wins
     (which is why the live pods were always labelled correctly) but that a
     strict decoder rejects outright: kubeconform, and kubectl's
     --validate=strict. Found by the #966 render gate on the first run. */}}
{{- define "spatiumddi-appliance.podLabels" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: spatiumddi
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}

{{/* Image-resolution helper. Air-gap forces pullPolicy: Never; the
     bytes live in containerd's content store already (preloaded at
     firstboot from /usr/lib/spatiumddi/images/*.tar.zst). */}}
{{- define "spatiumddi-appliance.imageRef" -}}
{{- $repo := required "image.repository required" .repository -}}
{{- $tag := default $.global.imageTag .tag -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
