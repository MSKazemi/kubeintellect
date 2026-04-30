#!/usr/bin/env bash
set -e
kubectl wait deployment nginx -n scenario-test \
  --for=condition=Available --timeout=90s
