#!/usr/bin/env bash
set -e
kubectl rollout status deployment/kubeintellect -n kubeintellect --timeout=90s
