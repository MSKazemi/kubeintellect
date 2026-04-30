#!/usr/bin/env bash
set -e
kubectl wait pod -n scenario-test -l app=metrics-app \
  --for=condition=Ready --timeout=90s
