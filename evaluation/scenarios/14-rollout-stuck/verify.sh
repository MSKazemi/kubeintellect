#!/usr/bin/env bash
set -e
kubectl rollout status deployment/web-api -n scenario-test --timeout=90s
