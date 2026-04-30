#!/usr/bin/env bash
set -e
kubectl wait pod -n scenario-test -l app=probe-app \
  --for=condition=Ready --timeout=90s
