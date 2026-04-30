#!/usr/bin/env bash
set -e
kubectl wait pod -n scenario-test -l app=app-server \
  --for=condition=Ready --timeout=90s
