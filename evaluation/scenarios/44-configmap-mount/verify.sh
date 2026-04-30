#!/usr/bin/env bash
set -e
kubectl wait pod -l app=busybox-config -n scenario-test \
  --for=condition=Ready --timeout=90s
kubectl exec -n scenario-test deploy/busybox-config -- env | grep "DB_HOST=postgres.svc"
