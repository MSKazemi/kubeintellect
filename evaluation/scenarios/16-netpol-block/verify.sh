#!/usr/bin/env bash
set -e
kubectl wait pod -n scenario-test -l app=backend-api \
  --for=condition=Ready --timeout=60s
kubectl wait pod -n scenario-test -l app=client-app \
  --for=condition=Ready --timeout=60s
