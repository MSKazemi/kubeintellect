{{/*
Resolve the target namespace. Falls back to the release namespace.
*/}}
{{- define "langfuse.namespace" -}}
{{- .Values.namespace | default .Release.Namespace }}
{{- end }}
