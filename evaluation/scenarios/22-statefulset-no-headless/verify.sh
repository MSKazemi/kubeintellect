#!/usr/bin/env bash
set -e
kubectl wait pod -n scenario-test -l app=db-cluster \
  --for=condition=Ready --timeout=90s
