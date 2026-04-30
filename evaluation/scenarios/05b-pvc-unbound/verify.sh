#!/usr/bin/env bash
set -e
kubectl wait pod -n scenario-test -l app=pvc-app \
  --for=condition=Ready --timeout=90s
