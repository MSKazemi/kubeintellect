#!/usr/bin/env bash
set -e
kubectl wait pod -n scenario-test -l app=memory-hog \
  --for=condition=Ready --timeout=90s
