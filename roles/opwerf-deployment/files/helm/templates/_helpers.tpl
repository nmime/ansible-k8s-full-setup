{{- define "opwerf.name" -}}
{{- default .Chart.Name .Values.app.name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "opwerf.fullname" -}}
{{- $name := default .Chart.Name .Values.app.name }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "opwerf.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: opwerf
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/environment: {{ .Values.global.environment | default "production" }}
{{- end }}

{{- define "opwerf.selectorLabels" -}}
app.kubernetes.io/name: {{ include "opwerf.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "opwerf.redisUrl" -}}
redis://:$(REDIS_PASSWORD)@{{ .Values.dragonfly.host }}:{{ .Values.dragonfly.port }}
{{- end }}

{{- define "opwerf.credentialProxyUrl" -}}
http://{{ include "opwerf.name" . }}-credential-proxy.{{ .Release.Namespace }}.svc.cluster.local:4000
{{- end }}

{{- define "opwerf.elasticsearchUrl" -}}
{{- if .Values.elasticsearch.enabled }}
{{ .Values.elasticsearch.scheme }}://{{ .Values.elasticsearch.host }}:{{ .Values.elasticsearch.port }}
{{- end }}
{{- end }}
