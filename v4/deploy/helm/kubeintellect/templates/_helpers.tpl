{{/*
Expand the name of the chart.
*/}}
{{- define "kubeintellect.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "kubeintellect.fullname" -}}
{{- default .Chart.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "kubeintellect.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "kubeintellect.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "kubeintellect.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kubeintellect.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Fail-loud validation of setting combinations the server would reject (or silently mishandle) at
startup. Failing at `helm template` / `helm install` beats a pod that crash-loops.
Included from configmap.yaml, which every install renders. Messages mirror the server's own.
*/}}
{{- define "kubeintellect.validate" -}}
{{- $provider := .Values.config.llmProvider | default "openai" -}}
{{- /* config.extraEnv is rendered last in the ConfigMap, so its CORTEX_V4_ENABLED wins. */ -}}
{{- $cortex := .Values.config.cortexV4Enabled | default false | toString -}}
{{- if hasKey (.Values.config.extraEnv | default dict) "CORTEX_V4_ENABLED" -}}
{{- $cortex = index .Values.config.extraEnv "CORTEX_V4_ENABLED" | toString -}}
{{- end -}}
{{- if and (eq $provider "anthropic") (ne (lower $cortex) "true") -}}
{{- fail "config.llmProvider=anthropic requires config.cortexV4Enabled=true. The default V2 graph has no Anthropic backend: running it would send every coordinator and subagent prompt -- including the cluster data in it -- to OpenAI using OPENAI_API_KEY, and ANTHROPIC_API_KEY would be ignored. KubeIntellect refuses to start rather than send cluster data to a vendor you did not select. Fix: set config.cortexV4Enabled=true to use Anthropic, or set config.llmProvider to the provider you actually want (openai / azure / qwen / local)." -}}
{{- end -}}
{{- if and (eq $provider "local") (not .Values.secrets.openaiBaseUrl) -}}
{{- fail "config.llmProvider=local requires secrets.openaiBaseUrl (the in-cluster URL of your OpenAI-compatible model server, e.g. http://ollama.ollama.svc.cluster.local:11434/v1). Left empty the server falls back to http://localhost:11434/v1, which inside a pod is the pod itself, so the startup check could never reach a model. See docs/local-llm.md." -}}
{{- end -}}
{{- if and .Values.config.promqlDetectionEnabled (not .Values.config.prometheusUrl) -}}
{{- fail "config.promqlDetectionEnabled=true requires config.prometheusUrl. Without it every `promql:` detect query is blind and nothing can fire." -}}
{{- end -}}
{{- if hasKey .Values.config "postmortemMinGrounding" -}}
{{- $g := .Values.config.postmortemMinGrounding | float64 -}}
{{- if or (lt $g 0.0) (gt $g 1.0) -}}
{{- fail (printf "config.postmortemMinGrounding must be between 0.0 and 1.0, got %v." .Values.config.postmortemMinGrounding) -}}
{{- end -}}
{{- end -}}
{{- /* No SQLite / non-Postgres mode exists in this chart: Postgres is always present (in-cluster
       or postgres.external), so config.selfGovernEnabled needs no storage-mode guard here. */ -}}
{{- end -}}

{{/*
Startup-probe failureThreshold for llmProvider=local. Budget = probe timeout x distinct models +
margin; threshold = ceil(budget / period). Refuses an explicit threshold below the budget.
*/}}
{{- define "kubeintellect.localStartupThreshold" -}}
{{- $sp := .Values.startupProbe | default dict -}}
{{- $period := int ($sp.periodSeconds | default 10) -}}
{{- if lt $period 1 -}}{{- fail "startupProbe.periodSeconds must be >= 1." -}}{{- end -}}
{{- $models := int ($sp.models | default 2) -}}
{{- $margin := int ($sp.marginSeconds | default 120) -}}
{{- $timeout := float64 (.Values.config.localLlmProbeTimeoutSeconds | default 180) -}}
{{- $budget := addf (mulf $timeout (float64 $models)) (float64 $margin) -}}
{{- $needed := int (ceil (divf $budget (float64 $period))) -}}
{{- $explicit := int ($sp.failureThreshold | default 0) -}}
{{- if and (gt $explicit 0) (lt $explicit $needed) -}}
{{- fail (printf "startupProbe.failureThreshold=%d x periodSeconds=%d gives %ds, below the %ds the local-LLM startup check may need (localLlmProbeTimeoutSeconds %v x %d models + %ds margin); the kubelet would kill the pod mid-check. Set failureThreshold to at least %d, or 0 to compute it." $explicit $period (mul $explicit $period) (int $budget) $timeout $models $margin $needed) -}}
{{- end -}}
{{- if gt $explicit 0 }}{{ $explicit }}{{ else }}{{ $needed }}{{ end -}}
{{- end -}}
