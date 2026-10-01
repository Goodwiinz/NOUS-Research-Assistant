{{/*
Expand the name of the chart.
*/}}
{{- define "knowledge-graph-analytics.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "knowledge-graph-analytics.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "knowledge-graph-analytics.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "knowledge-graph-analytics.labels" -}}
helm.sh/chart: {{ include "knowledge-graph-analytics.chart" . }}
{{ include "knowledge-graph-analytics.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Values.commonLabels }}
{{ toYaml .Values.commonLabels }}
{{- end }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "knowledge-graph-analytics.selectorLabels" -}}
app.kubernetes.io/name: {{ include "knowledge-graph-analytics.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "knowledge-graph-analytics.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "knowledge-graph-analytics.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Create the image name
*/}}
{{- define "knowledge-graph-analytics.image" -}}
{{- $registry := .Values.global.imageRegistry -}}
{{- if .Values.image.repository -}}
{{- $registry = .Values.image.repository -}}
{{- end -}}
{{- if .Values.global.imageRegistry -}}
{{- $registry = .Values.global.imageRegistry -}}
{{- end -}}
{{- $tag := .Chart.AppVersion -}}
{{- if .Values.image.tag -}}
{{- $tag = .Values.image.tag -}}
{{- end -}}
{{- printf "%s/%s:%s" $registry (default .Chart.Name .Values.nameOverride) $tag -}}
{{- end }}

{{/*
Return the single backend runtime image used by the API, migration init,
Celery worker, Celery beat, and synthetic traffic. Prefer an immutable digest;
retain tag fallback so existing environments keep rendering until their
release workflow supplies a digest.
*/}}
{{- define "knowledge-graph-analytics.backendImage" -}}
{{- $repository := required "backend.image.repository is required" .Values.backend.image.repository -}}
{{- $digest := .Values.backend.image.digest | default "" | trim -}}
{{- if $digest -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" $digest) -}}
{{- fail "backend.image.digest must be empty or an immutable sha256 digest" -}}
{{- end -}}
{{- printf "%s@%s" $repository $digest -}}
{{- else -}}
{{- $tag := .Values.backend.image.tag | default .Chart.AppVersion | required "backend.image.tag or Chart.appVersion is required" -}}
{{- printf "%s:%s" $repository $tag -}}
{{- end -}}
{{- end }}

{{/*
Render the tested source SHA annotation when a release supplied one.
*/}}
{{- define "knowledge-graph-analytics.backendSourceAnnotation" -}}
{{- $sourceSha := .Values.backend.image.sourceSha | default "" | trim -}}
{{- if $sourceSha -}}
{{- if not (regexMatch "^[a-f0-9]{40}$" $sourceSha) -}}
{{- fail "backend.image.sourceSha must be empty or a full 40-character Git SHA" -}}
{{- end -}}
nous-platform.dev/source-sha: {{ $sourceSha | quote }}
{{- end -}}
{{- end }}

{{/*
Create the database URL
*/}}
{{- define "knowledge-graph-analytics.databaseUrl" -}}
{{- $postgres := .Values.postgresql -}}
{{- if $postgres.enabled -}}
{{- printf "postgresql://%s:%s@%s:%d/%s" $postgres.auth.username $postgres.auth.password $postgres.fullname $postgres.primary.service.port $postgres.auth.database -}}
{{- else -}}
{{- printf "%s" .Values.backend.env.DATABASE_URL -}}
{{- end -}}
{{- end }}

{{/*
Create the Redis URL
*/}}
{{- define "knowledge-graph-analytics.redisUrl" -}}
{{- $redis := .Values.redis -}}
{{- if $redis.enabled -}}
{{- $password := "" -}}
{{- if $redis.auth.enabled -}}
{{- $password = printf ":%s@" $redis.auth.password -}}
{{- end -}}
{{- printf "redis://%s%s:%d/0" $password $redis.fullname $redis.master.service.port -}}
{{- else -}}
{{- printf "%s" .Values.backend.env.REDIS_URL -}}
{{- end -}}
{{- end }}

{{/*
Create the Neo4j URI
*/}}
{{- define "knowledge-graph-analytics.neo4jUri" -}}
{{- $neo4j := .Values.neo4j -}}
{{- if $neo4j.enabled -}}
{{- printf "bolt://%s:7687" $neo4j.fullname -}}
{{- else -}}
{{- printf "%s" .Values.backend.env.NEO4J_URI -}}
{{- end -}}
{{- end }}

{{/*
Return the storage class name
*/}}
{{- define "knowledge-graph-analytics.storageClass" -}}
{{- if .Values.global.storageClass -}}
{{- .Values.global.storageClass -}}
{{- else -}}
{{- "gp2" -}}
{{- end -}}
{{- end }}

{{/*
Return the DNS zone
*/}}
{{- define "knowledge-graph-analytics.dnsZone" -}}
{{- if .Values.global.dnsZone -}}
{{- .Values.global.dnsZone -}}
{{- else -}}
{{- "yourdomain.com" -}}
{{- end -}}
{{- end }}

{{/*
Create the backend host
*/}}
{{- define "knowledge-graph-analytics.backendHost" -}}
{{- printf "api.%s" (include "knowledge-graph-analytics.dnsZone" .) -}}
{{- end }}

{{/*
Return the environment name
*/}}
{{- define "knowledge-graph-analytics.environment" -}}
{{- if .Values.global.environment -}}
{{- .Values.global.environment -}}
{{- else -}}
{{- "production" -}}
{{- end -}}
{{- end }}

{{/*
Merge the shared backend.env with a component's env overrides into a single
env list whose names are UNIQUE (the component entry wins over a same-named
backend entry).

Why this exists: the kubelet tolerates duplicate env names at runtime (last
value wins), but ArgoCD diffs via a Kubernetes strategic-merge-patch that
uses `name` as the merge key for the env list. Two entries with the same
`name` in one container make the patch construction fail
("failed to construct strategic merge patch: The order in patch list ...")
and ALL sync for the app stops. So every rendered container env must list
each name exactly once.

Ordering contract:
  - backend.env positions are preserved; an overridden name keeps its slot
    but takes the component's value/valueFrom.
  - component-only names are appended after, in component order.
  - when the component env is unset/empty this is a pure no-op and renders
    identically to `toYaml .Values.backend.env` (pre-override behavior).

Usage:
  env:
    {{- include "knowledge-graph-analytics.mergedEnv"
          (dict "base" .Values.backend.env "override" .Values.celeryWorker.env)
          | nindent 12 }}
*/}}
{{- define "knowledge-graph-analytics.mergedEnv" -}}
{{- $base := .base | default (list) -}}
{{- $override := .override | default (list) -}}
{{- $overrideByName := dict -}}
{{- range $override -}}
{{- $_ := set $overrideByName .name . -}}
{{- end -}}
{{- $seen := dict -}}
{{- $merged := list -}}
{{- range $base -}}
{{- if hasKey $overrideByName .name -}}
{{- $merged = append $merged (index $overrideByName .name) -}}
{{- else -}}
{{- $merged = append $merged . -}}
{{- end -}}
{{- $_ := set $seen .name true -}}
{{- end -}}
{{- range $override -}}
{{- if not (hasKey $seen .name) -}}
{{- $merged = append $merged . -}}
{{- end -}}
{{- end -}}
{{- if $merged -}}
{{- toYaml $merged -}}
{{- end -}}
{{- end }}

{{/*
Return the resource limits for a given component
*/}}
{{- define "knowledge-graph-analytics.resources" -}}
{{- $component := index .Values .component -}}
{{- if $component.resources -}}
resources:
  {{- toYaml $component.resources | nindent 2 }}
{{- end -}}
{{- end }}

{{/*
Return the security context for a given component
*/}}
{{- define "knowledge-graph-analytics.securityContext" -}}
{{- $component := index .Values .component -}}
{{- if $component.securityContext -}}
securityContext:
  {{- toYaml $component.securityContext | nindent 2 }}
{{- end -}}
{{- end }}

{{/*
Return the affinity rules for a given component
*/}}
{{- define "knowledge-graph-analytics.affinity" -}}
{{- $component := index .Values .component -}}
{{- if $component.affinity -}}
affinity:
  {{- toYaml $component.affinity | nindent 2 }}
{{- else if .Values.affinity -}}
affinity:
  {{- toYaml .Values.affinity | nindent 2 }}
{{- end -}}
{{- end }}

{{/*
Return the tolerations for a given component
*/}}
{{- define "knowledge-graph-analytics.tolerations" -}}
{{- $component := index .Values .component -}}
{{- if $component.tolerations -}}
tolerations:
  {{- toYaml $component.tolerations | nindent 2 }}
{{- else if .Values.tolerations -}}
tolerations:
  {{- toYaml .Values.tolerations | nindent 2 }}
{{- end -}}
{{- end }}

{{/*
Return the node selector for a given component
*/}}
{{- define "knowledge-graph-analytics.nodeSelector" -}}
{{- $component := index .Values .component -}}
{{- if $component.nodeSelector -}}
nodeSelector:
  {{- toYaml $component.nodeSelector | nindent 2 }}
{{- else if .Values.nodeSelector -}}
nodeSelector:
  {{- toYaml .Values.nodeSelector | nindent 2 }}
{{- end -}}
{{- end }}

{{/*
Return the pod annotations for a given component
*/}}
{{- define "knowledge-graph-analytics.podAnnotations" -}}
{{- $component := index .Values .component -}}
{{- if $component.podAnnotations -}}
{{- toYaml $component.podAnnotations -}}
{{- else if .Values.podAnnotations -}}
{{- toYaml .Values.podAnnotations -}}
{{- end -}}
{{- end }}

{{/*
Create a default pod security policy
*/}}
{{- define "knowledge-graph-analytics.podSecurityPolicy" -}}
{{- if .Values.podSecurityPolicy.enabled }}
apiVersion: policy/v1beta1
kind: PodSecurityPolicy
metadata:
  name: {{ include "knowledge-graph-analytics.fullname" . }}
  labels:
    {{- include "knowledge-graph-analytics.labels" . | nindent 4 }}
spec:
  privileged: false
  allowPrivilegeEscalation: false
  requiredDropCapabilities:
    - ALL
  volumes:
    - 'configMap'
    - 'emptyDir'
    - 'projected'
    - 'secret'
    - 'downwardAPI'
    - 'persistentVolumeClaim'
  runAsUser:
    rule: 'MustRunAsNonRoot'
  seLinux:
    rule: 'RunAsAny'
  fsGroup:
    rule: 'RunAsAny'
{{- end }}
{{- end }}

{{/* Hash the exact immutable Job pod definition, including template defaults. */}}
{{- define "knowledge-graph-analytics.migrationPod" -}}
metadata:
  {{- if .Values.backend.image.sourceSha }}
  annotations:
    {{- include "knowledge-graph-analytics.backendSourceAnnotation" . | nindent 4 }}
  {{- end }}
  labels:
    {{- include "knowledge-graph-analytics.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: migrations
spec:
  restartPolicy: Never
  {{- with .Values.global.imagePullSecrets }}
  imagePullSecrets:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  serviceAccountName: {{ include "knowledge-graph-analytics.serviceAccountName" . }}
  securityContext:
    {{- toYaml .Values.securityContext | nindent 4 }}
  containers:
    - name: run-migrations
      image: {{ include "knowledge-graph-analytics.backendImage" . | quote }}
      imagePullPolicy: {{ .Values.backend.image.pullPolicy }}
      workingDir: /app
      command: ["alembic", "upgrade", "heads"]
      envFrom:
        {{- toYaml .Values.backend.envFrom | nindent 8 }}
      env:
        {{- include "knowledge-graph-analytics.mergedEnv" (dict "base" .Values.backend.env) | nindent 8 }}
      resources:
        requests:
          cpu: 100m
          memory: 256Mi
        limits:
          cpu: 500m
          memory: 512Mi
{{- end }}
