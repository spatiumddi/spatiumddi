{{/*
Chart-wide helpers. Mostly trivial wrappers around the standard Helm
patterns — extracted so templates don't repeat the boilerplate.
*/}}

{{- define "spatiumddi.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "spatiumddi.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "spatiumddi.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every resource.
*/}}
{{- define "spatiumddi.labels" -}}
helm.sh/chart: {{ include "spatiumddi.chart" . }}
{{ include "spatiumddi.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "spatiumddi.selectorLabels" -}}
app.kubernetes.io/name: {{ include "spatiumddi.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Per-component labels — include via:
  {{- include "spatiumddi.componentLabels" (merge (dict "component" "api") .) | nindent 4 }}
so the helper sees all root context plus the component name.
*/}}
{{- define "spatiumddi.componentLabels" -}}
helm.sh/chart: {{ include "spatiumddi.chart" . }}
app.kubernetes.io/name: {{ include "spatiumddi.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "spatiumddi.componentSelectorLabels" -}}
app.kubernetes.io/name: {{ include "spatiumddi.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Control-plane image — pass `imageName` (e.g. "spatiumddi-api") via merge:
  {{ include "spatiumddi.image" (merge (dict "imageName" "spatiumddi-api") .) }}
*/}}
{{- define "spatiumddi.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s/%s/%s:%s" .Values.image.registry .Values.image.repository .imageName $tag -}}
{{- end -}}

{{/*
Name of the chart-owned secret carrying SECRET_KEY.
*/}}
{{- define "spatiumddi.appSecretName" -}}
{{- if .Values.auth.existingSecret -}}
{{- .Values.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-app" (include "spatiumddi.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Postgres connection parameters. Hostname + port + user + database come from
either the in-chart Postgres StatefulSet (templates/postgres.yaml) or the
externalDatabase block; the password is always referenced via a Secret
keyRef — never inlined.
*/}}
{{- define "spatiumddi.postgresHost" -}}
{{- if .Values.postgresql.enabled -}}
{{- printf "%s-postgresql" (include "spatiumddi.fullname" .) -}}
{{- else -}}
{{- required "externalDatabase.host is required when postgresql.enabled=false" .Values.externalDatabase.host -}}
{{- end -}}
{{- end -}}

{{- define "spatiumddi.postgresPort" -}}
{{- if .Values.postgresql.enabled -}}5432{{- else -}}{{ .Values.externalDatabase.port }}{{- end -}}
{{- end -}}

{{- define "spatiumddi.postgresUser" -}}
{{- if .Values.postgresql.enabled -}}{{ .Values.postgresql.auth.username }}{{- else -}}{{ .Values.externalDatabase.username }}{{- end -}}
{{- end -}}

{{- define "spatiumddi.postgresDatabase" -}}
{{- if .Values.postgresql.enabled -}}{{ .Values.postgresql.auth.database }}{{- else -}}{{ .Values.externalDatabase.database }}{{- end -}}
{{- end -}}

{{/*
Name of the secret carrying the Postgres user password. For the in-
chart Postgres this is the chart-owned ``<fullname>-postgresql`` Secret
(key ``password``) — generated on first install via lookup() and
preserved across upgrades. ``postgresql.auth.existingSecret`` overrides
to a BYO secret. For external DB it's whatever the user set in
externalDatabase.existingSecret.
*/}}
{{- define "spatiumddi.postgresSecretName" -}}
{{- if .Values.postgresql.enabled -}}
{{- if .Values.postgresql.auth.existingSecret -}}
{{- .Values.postgresql.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-postgresql" (include "spatiumddi.fullname" .) -}}
{{- end -}}
{{- else if .Values.externalDatabase.existingSecret -}}
{{- .Values.externalDatabase.existingSecret -}}
{{- else -}}
{{- printf "%s-external-db" (include "spatiumddi.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "spatiumddi.postgresSecretPasswordKey" -}}
{{- if .Values.postgresql.enabled -}}password{{- else -}}{{ .Values.externalDatabase.existingSecretPasswordKey | default "password" }}{{- end -}}
{{- end -}}

{{/*
Redis connection. Hostname + port from either the bundled subchart or
externalRedis.
*/}}
{{- define "spatiumddi.redisHost" -}}
{{- if .Values.redis.enabled -}}
{{- printf "%s-redis-master" (include "spatiumddi.fullname" .) -}}
{{- else -}}
{{- required "externalRedis.host is required when redis.enabled=false" .Values.externalRedis.host -}}
{{- end -}}
{{- end -}}

{{- define "spatiumddi.redisPort" -}}
{{- if .Values.redis.enabled -}}6379{{- else -}}{{ .Values.externalRedis.port }}{{- end -}}
{{- end -}}

{{- define "spatiumddi.redisAuthEnabled" -}}
{{- if .Values.redis.enabled -}}
{{- if .Values.redis.auth.enabled -}}true{{- end -}}
{{- else -}}
{{- if or .Values.externalRedis.password .Values.externalRedis.existingSecret -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{- define "spatiumddi.redisSecretName" -}}
{{- if .Values.redis.enabled -}}
{{- if .Values.redis.auth.existingSecret -}}
{{- .Values.redis.auth.existingSecret -}}
{{- else -}}
{{- printf "%s-redis" (include "spatiumddi.fullname" .) -}}
{{- end -}}
{{- else if .Values.externalRedis.existingSecret -}}
{{- .Values.externalRedis.existingSecret -}}
{{- else -}}
{{- printf "%s-external-redis" (include "spatiumddi.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "spatiumddi.redisSecretPasswordKey" -}}
{{- if .Values.redis.enabled -}}redis-password{{- else -}}{{ .Values.externalRedis.existingSecretPasswordKey | default "password" }}{{- end -}}
{{- end -}}

{{/*
Common env block for api / worker / beat. Only $(POSTGRES_PASSWORD) and
$(REDIS_PASSWORD) reference secrets; everything else is inline.
*/}}
{{- define "spatiumddi.commonEnv" -}}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "spatiumddi.postgresSecretName" . }}
      key: {{ include "spatiumddi.postgresSecretPasswordKey" . }}
- name: SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "spatiumddi.appSecretName" . }}
      key: secret-key
- name: DATABASE_URL
  value: "postgresql+asyncpg://{{ include "spatiumddi.postgresUser" . }}:$(POSTGRES_PASSWORD)@{{ include "spatiumddi.postgresHost" . }}:{{ include "spatiumddi.postgresPort" . }}/{{ include "spatiumddi.postgresDatabase" . }}"
{{- if eq (include "spatiumddi.redisAuthEnabled" .) "true" }}
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "spatiumddi.redisSecretName" . }}
      key: {{ include "spatiumddi.redisSecretPasswordKey" . }}
- name: REDIS_URL
  value: "redis://:$(REDIS_PASSWORD)@{{ include "spatiumddi.redisHost" . }}:{{ include "spatiumddi.redisPort" . }}/0"
- name: CELERY_BROKER_URL
  value: "redis://:$(REDIS_PASSWORD)@{{ include "spatiumddi.redisHost" . }}:{{ include "spatiumddi.redisPort" . }}/1"
- name: CELERY_RESULT_BACKEND
  value: "redis://:$(REDIS_PASSWORD)@{{ include "spatiumddi.redisHost" . }}:{{ include "spatiumddi.redisPort" . }}/2"
{{- else }}
- name: REDIS_URL
  value: "redis://{{ include "spatiumddi.redisHost" . }}:{{ include "spatiumddi.redisPort" . }}/0"
- name: CELERY_BROKER_URL
  value: "redis://{{ include "spatiumddi.redisHost" . }}:{{ include "spatiumddi.redisPort" . }}/1"
- name: CELERY_RESULT_BACKEND
  value: "redis://{{ include "spatiumddi.redisHost" . }}:{{ include "spatiumddi.redisPort" . }}/2"
{{- end -}}
{{- end -}}

{{/*
Init container that blocks until the bundled / external Postgres is
accepting connections. Used by the migrate Job. ``pg_isready`` ships in
the api image (postgresql-client-16, see backend/Dockerfile).
*/}}
{{- define "spatiumddi.waitForPostgresInit" -}}
- name: wait-for-postgres
  image: {{ include "spatiumddi.image" (merge (dict "imageName" "spatiumddi-api") .) }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - sh
    - -c
    - |
      until pg_isready -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -t 3 >/dev/null 2>&1; do
        echo "waiting for postgres at $PGHOST:$PGPORT..."
        sleep 3
      done
      echo "postgres is accepting connections"
  env:
    - name: PGHOST
      value: {{ include "spatiumddi.postgresHost" . | quote }}
    - name: PGPORT
      value: {{ include "spatiumddi.postgresPort" . | quote }}
    - name: PGUSER
      value: {{ include "spatiumddi.postgresUser" . | quote }}
    - name: PGDATABASE
      value: {{ include "spatiumddi.postgresDatabase" . | quote }}
{{- end -}}

{{/*
Init container that blocks until alembic migrations have been applied
(detected by presence of a row in the ``alembic_version`` table). Used
by api / worker / beat so they don't roll out before the schema is in
place. Uses ``psql`` from the api image.

The DATABASE_URL on commonEnv uses the asyncpg driver scheme; psql
needs the plain ``postgresql://`` scheme, so we build connection
arguments from the discrete pieces instead of the URL.
*/}}
{{- define "spatiumddi.waitForMigrateInit" -}}
- name: wait-for-migrate
  image: {{ include "spatiumddi.image" (merge (dict "imageName" "spatiumddi-api") .) }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command:
    - sh
    - -c
    - |
      export PGPASSWORD="$POSTGRES_PASSWORD"
      until psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
            -tAc "SELECT version_num FROM alembic_version LIMIT 1" 2>/dev/null \
          | grep -qE '[a-f0-9]'; do
        echo "waiting for alembic migrations to land..."
        sleep 3
      done
      echo "alembic schema present"
  env:
    - name: POSTGRES_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ include "spatiumddi.postgresSecretName" . }}
          key: {{ include "spatiumddi.postgresSecretPasswordKey" . }}
    - name: PGHOST
      value: {{ include "spatiumddi.postgresHost" . | quote }}
    - name: PGPORT
      value: {{ include "spatiumddi.postgresPort" . | quote }}
    - name: PGUSER
      value: {{ include "spatiumddi.postgresUser" . | quote }}
    - name: PGDATABASE
      value: {{ include "spatiumddi.postgresDatabase" . | quote }}
{{- end -}}
