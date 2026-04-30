#!/usr/bin/env bash
set -e
kubectl wait pod -n scenario-test -l app=web-frontend \
  --for=condition=Ready --timeout=90s
kubectl wait pod -n scenario-test -l app=api-server \
  --for=condition=Ready --timeout=90s
